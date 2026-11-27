"""Engine-independent integrity tests; actual native execution is a separate probe."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.speech import spelling
from carr_library.audio.process import ProcessResult
from carr_library.speech.reading import ReadingDocument
from carr_library.ui.spelling_dialog import (
    ReadingSnapshot, SpellingReview, apply_correction, preview_correction,
)


def review_selected(*paths, **options):
    options.setdefault('state_root', Path(paths[0]).parent / 'state')
    return spelling.review_hunspell(*paths, **options)


def check_selected(text, profile, **options):
    options.setdefault('state_root', Path(profile.library.path).parent / 'state')
    return spelling.check_spelling(text, profile, **options)


def request_bytes(options):
    return (Path(options['cwd']) / 'request.json').read_bytes()


def outcome(raw, diagnostics=b'', code=0):
    return ProcessResult(raw, diagnostics, 0, code)


@pytest.fixture
def profile(tmp_path):
    (tmp_path / 'state').mkdir()
    paths = [tmp_path / name for name in ('selected-library.bin', 'synthetic.aff', 'synthetic.dic')]
    # No executable code is loaded by profile review or synthetic protocol tests.
    for path, data in zip(paths, (b'not an executable library', b'SET UTF-8\n', b'2\nhello\nworld\n')):
        path.write_bytes(data)
    return review_selected(*paths, language='en-test',
        provenance='Original test fixtures; executable payload deliberately unavailable')


def reply(request, *, issues=None):
    data = json.loads(request)
    return json.dumps({'encoding': 'UTF-8', 'checked': len(data['words']),
        'issues': [] if issues is None else issues, 'request_sha256': sha256(request).hexdigest()}).encode('utf-8')


def test_review_hashes_exact_selected_files_without_loading_runtime(profile, monkeypatch):
    monkeypatch.setattr(spelling, 'run_process', lambda *args, **kwargs: pytest.fail('Profile review executed native code'))
    selected = review_selected(profile.library.path, profile.affix.path, profile.dictionary.path,
        language=profile.language, provenance=profile.provenance)
    assert selected == profile
    assert selected.library.sha256 == sha256(b'not an executable library').hexdigest()
    assert len(selected.identity) == 64
    assert replace(selected, language='different').identity != selected.identity


@pytest.mark.parametrize('field', ['language', 'provenance'])
def test_explicit_profile_requires_language_and_provenance(profile, field):
    values = dict(language=profile.language, provenance=profile.provenance)
    values[field] = ''
    with pytest.raises(ValueError):
        review_selected(profile.library.path, profile.affix.path, profile.dictionary.path, **values)


def test_same_file_and_hardlinked_dictionary_are_refused(profile, tmp_path):
    with pytest.raises(ValueError, match='distinct'):
        review_selected(profile.library.path, profile.affix.path, profile.affix.path,
            language='en', provenance='explicit review')
    link = tmp_path / 'linked.dic'
    os.link(profile.dictionary.path, link)
    with pytest.raises(ValueError, match='links'):
        review_selected(profile.library.path, profile.affix.path, link,
            language='en', provenance='explicit review')


def test_mutation_during_profile_hash_is_refused(profile):
    path = Path(profile.dictionary.path)
    calls = 0
    def mutate():
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b'2\nchanged\nbytes\n')
        return False
    with pytest.raises(ValueError, match='changed|size budget'):
        spelling._review_file(path, 'dictionary', mutate)
    assert calls >= 2 and path.read_bytes() == b'2\nchanged\nbytes\n'


def test_profile_hash_budget_and_cancel_stop_before_any_runtime(profile, monkeypatch):
    monkeypatch.setitem(spelling.FILE_BUDGETS, 'dictionary', 3)
    with pytest.raises(ValueError, match='size budget'):
        spelling._review_file(profile.dictionary.path, 'dictionary', lambda: False)
    with pytest.raises(InterruptedError):
        spelling._review_file(profile.library.path, 'library', lambda: True)


def test_exact_word_ranges_preserve_emoji_crlf_combining_marks_and_apostrophes():
    text = "😀 hello\r\nп’ять cafe\u0301 well-known 123 end!"
    bounds, words = spelling.word_ranges(text)
    assert bounds == (0, len(text))
    assert tuple(word for _, _, word in words) == ('hello', 'п’ять', 'cafe\u0301', 'well-known', 'end')
    assert all(text[first:last] == word for first, last, word in words)
    assert words[1][0] == 9
    selected = (words[1][0], words[2][1])
    assert spelling.word_ranges(text, bounds=selected)[1] == words[1:3]


@pytest.mark.parametrize('bounds', [(True, 1), (0, 999), (-1, 2), (2, 1), [0, 2]])
def test_invalid_reading_scope_is_refused(bounds):
    with pytest.raises(ValueError):
        spelling.word_ranges('words', bounds=bounds)


def test_word_scan_is_bounded_and_cancellable_even_inside_one_long_token(monkeypatch):
    monkeypatch.setattr(spelling, 'MAX_WORDS', 2)
    with pytest.raises(ValueError, match='smaller spelling scope'):
        spelling.word_ranges('one two three')
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 3
    with pytest.raises(InterruptedError):
        spelling.word_ranges('a' * 20_000, cancelled=cancelled)


def test_protocol_deduplicates_requests_but_retains_each_exact_issue_range(profile, monkeypatch):
    calls = []
    def worker(argv, **kwargs):
        calls.append((argv, kwargs))
        data = json.loads(request_bytes(kwargs))
        assert data['words'] == ['hello', 'wrld', 'слово']
        return outcome(reply(request_bytes(kwargs), issues=[{'index': 1, 'reason': 'not_in_dictionary',
                                                    'suggestions': ['world']}]))
    monkeypatch.setattr(spelling, 'run_process', worker)
    result = check_selected('😀 hello\r\nwrld слово wrld', profile)
    assert result.words == 4 and len(result.issues) == 2
    assert [(issue.start, issue.end) for issue in result.issues] == [(9, 13), (20, 24)]
    assert all(issue.suggestions == ('world',) for issue in result.issues)
    argv, options = calls[0]
    assert argv[1:4] == ['-I', '-B', '-S'] and argv[4].endswith('spelling_worker.py')
    assert options['allow_failure'] is True and options['budget'].timeout == 30
    assert options['owned_windows_job'] is (os.name == 'nt')
    assert 'input_bytes' not in options and len(argv[-1]) == 64
    assert options['watch'] == (options['cwd'],)


@pytest.mark.parametrize('malformation', ['identity', 'count', 'duplicate', 'index', 'suggestion', 'extra', 'unencodable'])
def test_unbound_or_malformed_worker_result_cannot_be_used(profile, monkeypatch, malformation):
    def worker(argv, **kwargs):
        value = json.loads(reply(request_bytes(kwargs), issues=[
            {'index': 0, 'reason': 'not_in_dictionary', 'suggestions': ['hello']}]))
        if malformation == 'identity': value['request_sha256'] = '0' * 64
        elif malformation == 'count': value['checked'] = True
        elif malformation == 'duplicate': value['issues'] *= 2
        elif malformation == 'index': value['issues'][0]['index'] = 2
        elif malformation == 'suggestion': value['issues'][0]['suggestions'] = ['x\0y']
        elif malformation == 'extra': value['unreviewed'] = True
        else: value['issues'][0]['reason'] = 'unencodable'
        return outcome(json.dumps(value).encode())
    monkeypatch.setattr(spelling, 'run_process', worker)
    with pytest.raises(ValueError):
        check_selected('helo', profile)


@pytest.mark.parametrize('code,payload,diagnostics', [
    (0, b'{"error":"changed"}', b''), (0, b'{"error":"unavailable"}', b''),
    (1, b'', b''), (0, b'not JSON', b''), (0, b'{}', b'native dictionary warning'),
])
def test_changed_unavailable_crashed_or_warning_runtime_fails_closed(profile, monkeypatch, code, payload, diagnostics):
    monkeypatch.setattr(spelling, 'run_process', lambda *args, **kwargs: outcome(payload, diagnostics, code))
    with pytest.raises(ValueError):
        check_selected('hello', profile)


def test_too_long_words_are_visible_unchecked_issues_not_silently_accepted(profile, monkeypatch):
    monkeypatch.setattr(spelling, 'run_process', lambda *args, **kwargs: outcome(reply(request_bytes(kwargs))))
    text = 'x' * 129 + ' hello'
    result = check_selected(text, profile)
    assert result.words == 2 and len(result.issues) == 1
    assert result.issues[0].reason == 'too_long' and result.issues[0].word == 'x' * 129


def test_repeated_unknown_words_cannot_overflow_review(profile, monkeypatch):
    def worker(*args, **kwargs):
        return outcome(reply(request_bytes(kwargs), issues=[{'index': 0, 'reason': 'not_in_dictionary', 'suggestions': []}]))
    monkeypatch.setattr(spelling, 'run_process', worker)
    with pytest.raises(ValueError, match='500'):
        check_selected('unknown ' * 501, profile)


def test_actual_child_refuses_changed_dictionary_before_loading_dummy_library(profile):
    path = Path(profile.dictionary.path)
    path.write_bytes(b'2\nother\nwords\n')
    with pytest.raises(ValueError, match='changed'):
        check_selected('hello', profile)


def test_actual_child_refuses_non_library_without_importing_application(profile):
    with pytest.raises(ValueError, match='C API'):
        check_selected('hello', profile)


@pytest.mark.parametrize('failure', [InterruptedError, TimeoutError, RuntimeError])
def test_parent_removes_owned_staging_after_reaped_worker_failure(profile, monkeypatch, failure):
    staged = []
    def fail_after_staging(argv, **kwargs):
        root = Path(kwargs['cwd']); staged.append(root)
        binding = json.loads(request_bytes(kwargs))['staging']
        assert binding['path'] == str(root) and binding['inode'] == root.stat().st_ino
        (root / 'dictionary.dic').write_bytes(b'owned partial private dictionary')
        (root / 'hunspell.dll').write_bytes(b'owned partial native library')
        raise failure('The owned child has exited; synthetic failure boundary.')
    monkeypatch.setattr(spelling, 'run_process', fail_after_staging)
    with pytest.raises(failure):
        check_selected('hello', profile)
    assert staged and not staged[0].exists()


def make_review(profile, text='😀 hello\r\nwrld'):
    document = ReadingDocument(text)
    start = text.index('wrld')
    snapshot = ReadingSnapshot.capture(document, (start, start + 4))
    result = spelling.SpellingResult(profile, sha256(text.encode()).hexdigest(), (0, len(text)),
        2, 'UTF-8', (spelling.SpellingIssue(start, start + 4, 'wrld', ('world',)),))
    return document, SpellingReview(snapshot, result)


def test_correction_is_explicit_one_undoable_edit_with_original_source_retained(profile):
    document, review = make_review(profile)
    before = document.to_dict()
    preview = preview_correction(review, 0, 'world', review.snapshot)
    assert document.to_dict() == before
    assert apply_correction(document, preview, review.snapshot)
    assert document.revision == 1 and document.text == '😀 hello\r\nworld'
    assert document.source_text == '😀 hello\r\nwrld'
    assert document.undo() and document.text == document.source_text


@pytest.mark.parametrize('change', ['text', 'selection', 'document', 'range', 'identity'])
def test_stale_or_tampered_correction_preserves_reading_document(profile, change):
    document, review = make_review(profile)
    preview = preview_correction(review, 0, 'world', review.snapshot)
    current = review.snapshot
    if change == 'text':
        document.replace(0, 1, 'new')
    elif change == 'selection':
        current = ReadingSnapshot.capture(document, (0, 0))
    elif change == 'document':
        document = ReadingDocument(document.text)
    elif change == 'range':
        result = replace(review.result, issues=(replace(review.result.issues[0], end=10000),))
        preview = replace(preview, review=replace(review, result=result))
    else:
        preview = replace(preview, review=replace(review, result=replace(review.result, text_sha256='0' * 64)))
    before = document.to_dict()
    with pytest.raises(ValueError):
        apply_correction(document, preview, current)
    assert document.to_dict() == before

def test_library_selection_is_required_before_native_work(profile, monkeypatch):
    monkeypatch.setattr(spelling, 'run_process', lambda *a, **k: pytest.fail('Unselected native worker.'))
    with pytest.raises(ValueError, match='current library'):
        spelling.check_spelling('hello', profile)
    with pytest.raises(ValueError, match='state directory'):
        spelling.check_spelling('hello', profile, state_root=Path(profile.library.path).parent)


@pytest.mark.parametrize('timeout', [True, 0, .001, 301, float('nan'), '30'])
def test_invalid_native_budget_is_refused_before_staging(profile, monkeypatch, timeout):
    monkeypatch.setattr(spelling, 'TemporaryDirectory', lambda *a, **k: pytest.fail('Invalid budget staged files.'))
    with pytest.raises(ValueError, match='finite'):
        check_selected('hello', profile, timeout=timeout)


def test_shared_broker_admits_hashes_native_files_request_and_result_once(profile, monkeypatch):
    from carr_library.operations import ResourceBroker
    root = Path(profile.library.path).parent
    observed = []
    def worker(*args, **options):
        snapshot = ResourceBroker.for_app(root).snapshot()
        assert len(snapshot['leases']) == 1
        record = snapshot['leases'][0]
        assert record['operation_id'].startswith('spelling:')
        request = record['request']
        assert request['cpu_slots'] == 1 and request['memory_bytes'] == 1024**3
        needed = (sum(getattr(profile, role).size for role in spelling.FILE_BUDGETS)
                  + spelling.MAX_REQUEST_BYTES + 2 * spelling.MAX_REPLY_BYTES + 16 * 1024**2)
        assert request['disk_bytes'] == needed
        observed.append(Path(options['cwd']))
        return outcome(reply(request_bytes(options)))
    monkeypatch.setattr(spelling, 'run_process', worker)
    assert not check_selected('hello', profile).issues
    assert len(observed) == 1 and not observed[0].exists()
    assert ResourceBroker.for_app(root).snapshot()['leases'] == []


def test_changed_same_path_library_after_policy_lookup_has_no_new_stage(profile, monkeypatch):
    from carr_library.operations import ResourceBroker
    root = Path(profile.library.path).parent
    previous = root.with_name(root.name + '-previous')
    original_factory = ResourceBroker.for_app
    def replaced(cls, value, **options):
        broker = original_factory(value, **options)
        root.rename(previous); root.mkdir()
        (root / 'state').mkdir()
        return broker
    monkeypatch.setattr(ResourceBroker, 'for_app', classmethod(replaced))
    monkeypatch.setattr(spelling, 'TemporaryDirectory', lambda *a, **k: pytest.fail('Changed root created staging.'))
    with pytest.raises(ValueError, match='library.*changed'):
        check_selected('hello', profile)
    assert tuple(path.name for path in root.iterdir()) == ('state',)
    assert list((root / 'state').iterdir()) == []
    assert previous.is_dir()


def test_source_guard_rechecked_before_a_queued_native_request(profile, monkeypatch):
    from carr_library.operations import ResourceBroker
    original_factory = ResourceBroker.for_app
    allowed = True
    def guard():
        if not allowed:
            raise ValueError('The original retained source is no longer available.')
    def changed(cls, root, **options):
        nonlocal allowed
        broker = original_factory(root, **options); allowed = False
        return broker
    monkeypatch.setattr(ResourceBroker, 'for_app', classmethod(changed))
    monkeypatch.setattr(spelling, 'run_process', lambda *a, **k: pytest.fail('Stale source started native work.'))
    with pytest.raises(ValueError, match='retained source'):
        check_selected('hello', profile, guard=guard)


def test_same_size_dictionary_change_after_native_result_is_not_accepted(profile, monkeypatch):
    def worker(*args, **options):
        result = outcome(reply(request_bytes(options)))
        path = Path(profile.dictionary.path)
        original = path.read_bytes()
        path.write_bytes(original.replace(b'hello', b'other'))
        return result
    monkeypatch.setattr(spelling, 'run_process', worker)
    with pytest.raises(ValueError, match='changed'):
        check_selected('hello', profile)


def test_duplicate_native_result_fields_are_refused(profile, monkeypatch):
    def worker(*args, **options):
        raw = reply(request_bytes(options)).decode()
        return outcome(('{"encoding":"UTF-8",' + raw[1:]).encode())
    monkeypatch.setattr(spelling, 'run_process', worker)
    with pytest.raises(ValueError, match='complete review'):
        check_selected('hello', profile)


def test_profile_growth_reads_only_captured_size_plus_one(tmp_path, monkeypatch):
    import builtins
    path = tmp_path / 'growing.dic'
    original = b'1\nhello\n'
    path.write_bytes(original)
    actual_open, reads = Path.open, []
    class Growing:
        def __init__(self, stream): self.stream, self.grown = stream, False
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            if not self.grown:
                self.grown = True
                with builtins.open(path, 'ab') as target:
                    target.write(b'x' * 4096)
            data = self.stream.read(size); reads.append(len(data))
            return data
    def opened(value, mode='r', *args, **kwargs):
        stream = actual_open(value, mode, *args, **kwargs)
        return Growing(stream) if value == path and mode == 'rb' else stream
    monkeypatch.setattr(Path, 'open', opened)
    with pytest.raises(ValueError):
        spelling._review_file(path, 'dictionary', lambda: False)
    assert sum(reads) == len(original) + 1


def test_real_child_cleanup_retains_request_dictionary_and_actual_shared_lease(profile, monkeypatch):
    import subprocess
    import sys
    import threading
    from carr_library.audio.cleanup import pending_cleanup
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceBroker

    entered, release = threading.Event(), threading.Event()
    retained, failures = {}, []
    root = Path(profile.library.path).parent
    original = {role: Path(getattr(profile, role).path).read_bytes() for role in spelling.FILE_BUDGETS}
    def interrupted(*args, **options):
        stage = Path(options['cwd'])
        (stage / 'dictionary.dic').write_bytes(original['dictionary'])
        (stage / 'hunspell.dll').write_bytes(original['library'])
        environment = os.environ.copy()
        environment.pop('__PYVENV_LAUNCHER__', None); environment.pop('PYTHONEXECUTABLE', None)
        executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True))
        child = subprocess.Popen([executable, '-I', '-B', '-c', 'import time; time.sleep(45)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, env=environment)
        retained.update(child=child, stage=stage)
        def settle():
            if not release.is_set(): return False
            if child.poll() is None: child.terminate()
            child.wait(timeout=5)
            return True
        error = pending_cleanup(settle, message='Synthetic spelling child cleanup.')
        retained['owner'] = error.cleanup_owner
        entered.set()
        raise error
    monkeypatch.setattr(spelling, 'run_process', interrupted)
    def work():
        try:
            check_selected('hello', profile)
        except BaseException as error:
            failures.append(error)
    worker = threading.Thread(target=work, daemon=True, name='test-spelling-owned-child')
    try:
        worker.start()
        assert entered.wait(15), 'The real child did not start.'
        assert retained['child'].poll() is None and worker.is_alive()
        assert (retained['stage'] / 'request.json').is_file()
        assert (retained['stage'] / 'dictionary.dic').read_bytes() == original['dictionary']
        assert not retained['owner'].quiescent
        leases = ResourceBroker.for_app(root).snapshot()['leases']
        assert len(leases) == 1 and leases[0]['operation_id'].startswith('spelling:')
    finally:
        release.set(); worker.join(15)
        child = retained.get('child')
        died_before_fallback = child is not None and child.poll() is not None
        worker_ended_before_fallback = not worker.is_alive()
        if child is not None and child.poll() is None:
            child.kill(); child.wait(timeout=5); worker.join(5)
    assert died_before_fallback and worker_ended_before_fallback
    assert len(failures) == 1 and isinstance(failures[0], AudioCleanupPending)
    assert retained['owner'].quiescent and not retained['stage'].exists()
    assert ResourceBroker.for_app(root).snapshot()['leases'] == []
    assert {role: Path(getattr(profile, role).path).read_bytes() for role in spelling.FILE_BUDGETS} == original


def test_stage_delete_failure_keeps_cleanup_owner_and_lease_until_confirmed(profile, monkeypatch):
    import threading
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceBroker

    original_temporary = spelling.TemporaryDirectory
    entered, release = threading.Event(), threading.Event()
    retained, failures = {}, []
    root = Path(profile.library.path).parent
    class Temporary:
        def __init__(self, **options):
            self.actual = original_temporary(**options); self.name = self.actual.name
            retained['stage'] = Path(self.name)
        def cleanup(self):
            entered.set()
            if not release.is_set():
                raise PermissionError('Synthetic retained staging handle.')
            self.actual.cleanup()
    monkeypatch.setattr(spelling, 'TemporaryDirectory', Temporary)
    monkeypatch.setattr(spelling, 'run_process', lambda *a, **k: outcome(reply(request_bytes(k))))
    def work():
        try:
            check_selected('hello', profile)
        except BaseException as error:
            failures.append(error)
    worker = threading.Thread(target=work, daemon=True, name='test-spelling-stage-owner')
    try:
        worker.start()
        assert entered.wait(15) and worker.is_alive()
        assert (retained['stage'] / 'request.json').is_file()
        assert len(ResourceBroker.for_app(root).snapshot()['leases']) == 1
    finally:
        release.set(); worker.join(15)
    assert not worker.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], AudioCleanupPending)
    assert failures[0].cleanup_owner.quiescent
    assert not retained['stage'].exists() and ResourceBroker.for_app(root).snapshot()['leases'] == []


def test_review_only_hashing_holds_real_cpu_memory_lease_without_a_disk_reservation(tmp_path, monkeypatch):
    from carr_library.operations import ResourceBroker

    (tmp_path / 'state').mkdir()
    paths = [tmp_path / name for name in ('not-executable.bin', 'fixture.aff', 'fixture.dic')]
    contents = (b'not executable code', b'SET UTF-8\n', b'2\nhello\nworld\n')
    for path, content in zip(paths, contents):
        path.write_bytes(content)
    original = spelling._review_file
    observed = []

    def inspect(path, role, cancelled):
        leases = ResourceBroker.for_app(tmp_path).snapshot()['leases']
        assert len(leases) == 1 and leases[0]['operation_id'].startswith('spelling:')
        request = leases[0]['request']
        assert request['cpu_slots'] == 1 and request['memory_bytes'] == 1024**3
        assert request['disk_bytes'] == 0 and request['disk_path'] is None
        assert request['additional_disks'] == []
        observed.append(role)
        return original(path, role, cancelled)

    monkeypatch.setattr(spelling, '_review_file', inspect)
    monkeypatch.setattr(spelling, 'run_process',
        lambda *args, **kwargs: pytest.fail('Hash-only review executed native code.'))
    monkeypatch.setattr(spelling, 'TemporaryDirectory',
        lambda *args, **kwargs: pytest.fail('Hash-only review created a native staging directory.'))
    result = spelling.review_hunspell(*paths, language='synthetic',
        provenance='Original inert test bytes', state_root=tmp_path / 'state')
    assert observed == ['library', 'affix', 'dictionary']
    assert tuple(getattr(result, name).sha256 for name in observed) == tuple(
        sha256(content).hexdigest() for content in contents)
    assert [path.read_bytes() for path in paths] == list(contents)
    assert ResourceBroker.for_app(tmp_path).snapshot()['leases'] == []
