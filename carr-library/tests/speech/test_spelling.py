"""Exact-source spelling replies and actual bounded isolated child regressions."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

import pytest

from carr_library.speech import spelling
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner


def request(text='😀 mispelled word', language='en-US', operation='check'):
    return {'version': 1, 'request_id': 'a' * 32, 'operation': operation,
        'text': text, 'text_sha256': sha256(text.encode('utf-8')).hexdigest(), 'language': language}


def reply(value, rows=None):
    result = {key: value[key] for key in ('version', 'request_id', 'operation', 'text_sha256', 'language')}
    result.update(status='ok', result=rows if rows is not None else [
        {'start_utf16': 3, 'length_utf16': 9, 'action': 1,
         'replacements': ['misspelled'], 'suggestions_limited': False}])
    return result


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def test_native_utf16_positions_map_to_exact_python_fragment_without_source_rewrite():
    source = request()
    preview = spelling._decode_reply(encode(reply(source)), source)
    assert preview.language == 'en-US' and preview.text_sha256 == source['text_sha256']
    issue, = preview.issues
    assert (issue.start, issue.end, issue.word) == (2, 11, 'mispelled')
    assert source['text'] == '😀 mispelled word'


@pytest.mark.parametrize('change', [
    ('request_id', 'b' * 32), ('text_sha256', '0' * 64), ('language', 'en-GB'),
    ('operation', 'languages'), ('version', True), ('version', 2), ('extra', 'value'),
])
def test_foreign_or_ambiguous_reply_cannot_offer_a_correction(change):
    source = request()
    value = reply(source)
    value[change[0]] = change[1]
    with pytest.raises(spelling.SpellingError):
        spelling._decode_reply(encode(value), source)


@pytest.mark.parametrize('key,value', [
    ('start_utf16', 1), ('start_utf16', -1), ('start_utf16', True),
    ('start_utf16', 99), ('length_utf16', 0), ('length_utf16', -1),
    ('length_utf16', True), ('length_utf16', 100000), ('action', 4),
    ('action', True), ('replacements', ['\x00']), ('replacements', ['x' * 257]),
    ('replacements', list(map(str, range(13)))), ('suggestions_limited', 1),
])
def test_malformed_native_spans_and_suggestions_are_rejected(key, value):
    source = request()
    result = reply(source)
    result['result'][0][key] = value
    with pytest.raises(spelling.SpellingError):
        spelling._decode_reply(encode(result), source)


def test_overlapping_issues_and_duplicate_json_fields_are_rejected():
    source = request()
    value = reply(source)
    value['result'].append(deepcopy(value['result'][0]))
    with pytest.raises(spelling.SpellingError):
        spelling._decode_reply(encode(value), source)
    raw = encode(reply(source)).replace(b'"version": 1', b'"version": 1, "version": 1')
    with pytest.raises(spelling.SpellingError):
        spelling._decode_reply(raw, source)


@pytest.mark.parametrize('action,replacements', [(0, ['x']), (2, []), (2, ['x', 'y']), (3, ['x'])])
def test_native_corrective_actions_have_exact_preview_semantics(action, replacements):
    source = request()
    value = reply(source)
    value['result'][0].update(action=action, replacements=replacements)
    with pytest.raises(spelling.SpellingError):
        spelling._decode_reply(encode(value), source)


@pytest.mark.parametrize('action,replacements', [(0, []), (2, ['fixed']), (3, [''])])
def test_native_delete_and_replace_actions_are_only_proposals(action, replacements):
    source = request()
    value = reply(source)
    value['result'][0].update(action=action, replacements=replacements)
    preview = spelling._decode_reply(encode(value), source)
    assert preview.issues[0].replacements == tuple(replacements)
    assert source['text'] == '😀 mispelled word'


@pytest.mark.parametrize('languages', [['en-US', 'en-us'], ['../en'], [True], [''], ['a' * 65]])
def test_language_inventory_cannot_introduce_fallback_or_alias(languages):
    source = request('', None, 'languages')
    with pytest.raises(spelling.SpellingError):
        spelling._decode_reply(encode(reply(source, languages)), source)


def test_languages_remain_explicit_and_empty_inventory_is_truthful():
    source = request('', None, 'languages')
    assert spelling._decode_reply(encode(reply(source, ['uk-UA', 'en-US'])), source) == ('en-US', 'uk-UA')
    assert spelling._decode_reply(encode(reply(source, [])), source) == ()


@pytest.mark.parametrize('text,language', [
    ('', 'en-US'), ('   ', 'en-US'), ('x\x00y', 'en-US'), ('\ud800', 'en-US'),
    ('x' * 50001, 'en-US'), ('text', ''), ('text', 'auto language'), ('text', None),
], ids=['empty', 'space', 'nul', 'surrogate', 'oversize', 'no-language', 'not-installed', 'null-language'])
def test_invalid_input_fails_before_process_creation(monkeypatch, text, language):
    monkeypatch.setattr(spelling, 'run_process', lambda *a, **k: pytest.fail('Unexpected child'))
    with pytest.raises(ValueError):
        spelling.WindowsSpelling().check(text, language)


CHILD = r'''
import hashlib,json,sys,time
request=json.loads(open(sys.argv[1],encoding='utf-8').read())
reply={key:request[key] for key in ('version','request_id','operation','text_sha256','language')}
reply.update(status='ok',result=['en-US'] if request['operation']=='languages' else [])
sys.stdout.write(json.dumps(reply))
'''


def synthetic_worker(tmp_path, monkeypatch, source=CHILD):
    module = tmp_path / 'spelling.py'
    worker = tmp_path / 'spelling_worker.py'
    worker.write_text(source, encoding='utf-8')
    monkeypatch.setattr(spelling, '__file__', str(module))
    return worker


def test_actual_isolated_child_keeps_document_off_argv_and_checks_source_bytes(tmp_path, monkeypatch):
    synthetic_worker(tmp_path, monkeypatch)
    real, calls = spelling.run_process, []

    def observed(args, **options):
        calls.append((tuple(args), options.copy()))
        return real(args, **options)

    monkeypatch.setattr(spelling, 'run_process', observed)
    source = 'Private sample not intended for argv'
    preview = spelling.WindowsSpelling().check(source, 'en-US')
    assert preview.issues == ()
    args, options = calls[0]
    assert all(source not in argument for argument in args)
    assert args[1:5] == ('-I', '-B', '-X', 'utf8')
    assert options['owned_tree'] is True
    assert not Path(options['cwd']).exists()


def test_source_replacement_between_capture_and_execution_is_rejected(tmp_path, monkeypatch):
    worker = synthetic_worker(tmp_path, monkeypatch)
    real = spelling.run_process

    def replace(args, **options):
        worker.write_text(CHILD + '\n# changed\n', encoding='utf-8')
        return real(args, **options)

    monkeypatch.setattr(spelling, 'run_process', replace)
    with pytest.raises(spelling.SpellingError):
        spelling.WindowsSpelling().languages()


def test_actual_child_ignores_unchecked_bytecode_cache(tmp_path, monkeypatch):
    import py_compile
    worker = synthetic_worker(tmp_path, monkeypatch)
    original = worker.read_bytes()
    worker.write_text("raise RuntimeError('Unchecked cached worker was executed')\n", encoding='utf-8')
    py_compile.compile(str(worker), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    worker.write_bytes(original)
    assert spelling.WindowsSpelling().languages() == ('en-US',)


@pytest.mark.parametrize('mode', ['cancel', 'timeout'])
def test_actual_hung_child_is_bounded_and_its_private_text_cleaned(tmp_path, monkeypatch, mode):
    marker = tmp_path / 'started'
    child = ("from pathlib import Path\nimport os,time\np=Path(" + repr(str(marker))
             + ")\nt=p.with_suffix('.tmp')\nt.write_text(str(os.getpid()))\nos.replace(t,p)\ntime.sleep(60)\n")
    synthetic_worker(tmp_path, monkeypatch, child)
    calls, real = [], spelling.run_process

    def observed(args, **options):
        calls.append(options['cwd'])
        return real(args, **options)

    monkeypatch.setattr(spelling, 'run_process', observed)
    # Start the actual timeout only after a separately bounded real-child
    # acknowledgement. Windows venv redirects may have a different parent PID;
    # the marker is written atomically by the worker itself.
    from carr_library.audio import process as audio_process
    actual_start = audio_process._start_process

    def acknowledged_start(args, **options):
        child = actual_start(args, **options)
        deadline = time.monotonic() + 15
        while not marker.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                audio_process._stop_owned(child)
                raise AssertionError('The real spelling child did not acknowledge startup.')
            time.sleep(.01)
        return child

    monkeypatch.setattr(audio_process, '_start_process', acknowledged_start)
    started = time.monotonic()
    with pytest.raises(InterruptedError if mode == 'cancel' else spelling.SpellingError):
        spelling.WindowsSpelling(timeout=.7).languages(
            cancelled=(lambda: marker.exists()) if mode == 'cancel' else lambda: False)
    assert time.monotonic() - started < 24
    assert calls and all(not Path(path).exists() for path in calls)
    assert marker.exists() and int(marker.read_text()) > 0


def test_uncertain_child_cleanup_retains_private_request_until_owned_barrier(tmp_path, monkeypatch):
    synthetic_worker(tmp_path, monkeypatch)
    released, captured = [False], []
    owner = CleanupOwner(lambda: released[0])

    def pending(args, **options):
        captured.append(Path(options['cwd']))
        raise AudioCleanupPending('pending', cleanup_owner=owner)

    monkeypatch.setattr(spelling, 'run_process', pending)
    with pytest.raises(AudioCleanupPending) as caught:
        spelling.WindowsSpelling().check('Private request', 'en-US')
    assert caught.value.cleanup_owner is owner
    assert (captured[0] / 'request.json').exists()
    assert owner.close() is False
    released[0] = True
    assert owner.close() is True and not captured[0].exists()


def test_cancellation_before_start_never_creates_a_worker(monkeypatch):
    monkeypatch.setattr(spelling, 'run_process', lambda *a, **k: pytest.fail('Unexpected child'))
    with pytest.raises(InterruptedError):
        spelling.WindowsSpelling().languages(cancelled=lambda: True)


def test_non_windows_native_worker_reports_unavailable_without_fallback():
    if sys.platform == 'win32':
        # Native qualification is the dedicated actual-dictionary/UI probe.
        assert spelling.WindowsSpelling().languages() is not None
    else:
        with pytest.raises(spelling.SpellingError, match='require Windows'):
            spelling.WindowsSpelling().languages()
