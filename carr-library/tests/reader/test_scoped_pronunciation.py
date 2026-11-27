"""Scoped dictionary behavior and real isolated regex process boundaries."""
from dataclasses import replace
import json
import subprocess
import sys
from pathlib import Path
import threading
import time

import pytest

from carr_library.reader import narration as subject
from carr_library.reader.narration import (
    NarrationStore, PronunciationRule, new_project, pronunciation_preview,
    encode_project, decode_project, encode_pronunciation_dictionary,
    decode_pronunciation_dictionary, load_pronunciation_dictionary, revise, undo,
)


def test_scope_is_explicit_and_specific_voice_overrides_only_the_same_source_match():
    rules = (
        PronunciationRule('Read', 'generic', language='legacy label'),
        PronunciationRule('Read', 'мова', language='uk', language_scope=True),
        PronunciationRule('Read', 'voice', voice_id='fixture:voice'),
        PronunciationRule('Read', 'голос', language='uk', language_scope=True, voice_id='fixture:voice'),
        PronunciationRule('Read more', 'longest'),
    )
    assert pronunciation_preview('Read Read more', rules) == ('generic longest', 2)
    assert pronunciation_preview('Read', rules, language='UK') == ('мова', 1)
    assert pronunciation_preview('Read', rules, voice_id='fixture:voice') == ('voice', 1)
    assert pronunciation_preview('Read', rules, language='uk', voice_id='fixture:voice') == ('голос', 1)
    assert pronunciation_preview('Read', rules, language='uk-UA', voice_id='other') == ('generic', 1)


def test_missing_context_skips_scoped_pattern_without_launching_a_worker(monkeypatch):
    from carr_library.audio import engines
    monkeypatch.setattr(engines, '_run', lambda *a, **k: pytest.fail('An unselected scoped pattern ran.'))
    rules = (PronunciationRule('(a+)+$', 'x', regex=True, language='uk', language_scope=True),
             PronunciationRule('a', 'b', voice_id='fixture:unselected'))
    assert pronunciation_preview('aaaa', rules) == ('aaaa', 0)


def test_real_regex_and_literals_merge_in_one_unicode_non_cascading_pass():
    rules = (
        PronunciationRule('Mr.', 'Містер'),
        PronunciationRule('Містер', 'must not cascade'),
        PronunciationRule(r'\b(Ivan)\b', r'<\1>', language='uk', language_scope=True,
                          voice_id='fixture:voice', regex=True, ignore_case=True),
        PronunciationRule('[x]', r'\1', ignore_case=True),
    )
    original = 'Mr. Ivan IVAN [X] 😀'
    assert pronunciation_preview(original, rules, language='uk', voice_id='fixture:voice',
                                 timeout=3) == (r'Містер <Ivan> <IVAN> \1 😀', 4)
    assert original == 'Mr. Ivan IVAN [X] 😀'


def test_real_zero_width_matches_insert_without_losing_source_characters():
    rules = (PronunciationRule('^', 'START ', regex=True), PronunciationRule('$', ' END', regex=True))
    assert pronunciation_preview('abc', rules, timeout=3) == ('START abc END', 2)
    # A longer match at the same source position wins before specificity/order.
    assert pronunciation_preview('abc', rules + (PronunciationRule('a', 'A'),), timeout=3) == ('Abc END', 2)


@pytest.mark.parametrize('pattern,replacement', [('(', 'x'), ('(a)', r'\2')],
                         ids=['invalid-pattern', 'unknown-capture'])
def test_invalid_pattern_or_replacement_cannot_partially_edit_a_project(pattern, replacement):
    project = replace(new_project('a original'), rules=(PronunciationRule('original', 'changed'),
                      PronunciationRule(pattern, replacement, regex=True)))
    before = encode_project(project)
    with pytest.raises(ValueError):
        pronunciation_preview(project.current_text, project.rules, timeout=3)
    assert encode_project(project) == before


def _observe_children(monkeypatch):
    from carr_library.audio import engines
    started, children, commands = threading.Event(), [], []
    real = engines.start_owned_process
    def launch(command, **kwargs):
        process = real(command, **kwargs)
        children.append(process)
        commands.append((command, kwargs))
        if any(str(value).endswith('pronunciation_worker.py') for value in command):
            started.set()
        return process
    monkeypatch.setattr(engines, 'start_owned_process', launch)
    return started, children, commands


def test_actual_catastrophic_regex_hits_deadline_and_leaves_no_live_child(monkeypatch):
    _, children, commands = _observe_children(monkeypatch)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match='time budget'):
        pronunciation_preview('a' * 20000 + '!', (PronunciationRule('(a+)+$', 'x', regex=True),), timeout=.2)
    assert time.monotonic() - started < 10
    assert children and all(process.poll() is not None for process in children)
    command, options = commands[0]
    assert '-I' in command and '-B' in command and '-S' in command
    assert isinstance(command, list) and options['stdin_file'] is not None
    assert 'a' * 100 not in ' '.join(str(part) for part in command)


def test_actual_started_regex_worker_is_cancelled_and_reaped(monkeypatch):
    started, children, _ = _observe_children(monkeypatch)
    with pytest.raises(InterruptedError):
        pronunciation_preview('a' * 20000 + '!', (PronunciationRule('(a+)+$', 'x', regex=True),),
                              cancelled=started.is_set, timeout=3)
    assert started.is_set() and all(process.poll() is not None for process in children)


def test_real_pattern_match_count_and_expansion_are_bounded():
    with pytest.raises(ValueError, match='budget'):
        pronunciation_preview('a' * 11000, (PronunciationRule('(?=a)', 'x', regex=True),), timeout=3)
    with pytest.raises(ValueError, match='budget'):
        pronunciation_preview('a' * 300, (PronunciationRule('a', 'λ' * 4000, regex=True),), timeout=3)


def test_cancellation_also_interrupts_the_ordinary_literal_matcher():
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 3
    with pytest.raises(InterruptedError):
        pronunciation_preview('a' * 10000, (PronunciationRule('aaaaab', 'x'),), cancelled=cancelled)


def test_legacy_project_bytes_keep_version_two_and_descriptive_language_semantics():
    project = replace(new_project('Nitra'), rules=(PronunciationRule('Nitra', 'Нітра', 'uk'),))
    raw = encode_project(project)
    value = json.loads(raw)
    assert value['version'] == 2
    assert value['rules'] == [dict(written='Nitra', spoken='Нітра', language='uk')]
    assert encode_project(decode_project(raw)) == raw
    assert pronunciation_preview('Nitra', decode_project(raw).rules) == ('Нітра', 1)
    value['version'] = 1
    del value['original_path']
    upgraded = decode_project(json.dumps(value).encode())
    assert upgraded.rules == project.rules and json.loads(encode_project(upgraded))['version'] == 2


def test_scoped_project_uses_explicit_version_three_and_preserves_undo_source():
    project = replace(new_project('One 23'), rules=(PronunciationRule(r'\d+', 'number', regex=True,
                      language='en', language_scope=True, voice_id='fixture:voice'),))
    raw = encode_project(project)
    assert json.loads(raw)['version'] == 3
    restored = decode_project(raw)
    assert restored == project
    text, _ = pronunciation_preview(restored.current_text, restored.rules, language='en',
                                    voice_id='fixture:voice', timeout=3)
    changed = revise(restored, text, label='Explicit pronunciation preview')
    assert changed.original_text == 'One 23' and changed.current_text == 'One number'
    earlier = undo(changed)
    assert earlier.current_text == project.current_text and earlier.rules == project.rules
    assert earlier.original_text == project.original_text and earlier.cursor == 0


def test_legacy_versions_never_silently_activate_new_rule_fields():
    project = replace(new_project('a'), rules=(PronunciationRule('a', 'b', regex=True),))
    value = json.loads(encode_project(project))
    value['version'] = 2
    with pytest.raises(ValueError, match='Legacy'):
        decode_project(json.dumps(value).encode())


def test_canonical_dictionary_is_explicitly_imported_with_language_and_voice_scope():
    value = dict(schema='carr.pronunciations.v1', rules=[
        dict(pattern=r'\bword\b', replacement='слово', regex=True, ignore_case=True,
             language='uk', voice_id='fixture:voice')])
    rules = decode_pronunciation_dictionary(json.dumps(value).encode())
    assert rules == (PronunciationRule(r'\bword\b', 'слово', language='uk', voice_id='fixture:voice',
                                      regex=True, ignore_case=True, language_scope=True),)
    assert decode_pronunciation_dictionary(encode_pronunciation_dictionary(rules)) == rules
    assert pronunciation_preview('word', rules) == ('word', 0)


def test_interchange_preserves_legacy_label_without_promoting_it_to_scope(tmp_path):
    rules = (PronunciationRule('word', 'spoken', language='only a label'),)
    path = tmp_path / 'dictionary.json'
    path.write_bytes(encode_pronunciation_dictionary(rules))
    assert load_pronunciation_dictionary(path) == rules
    assert not rules[0].language_scope
    assert pronunciation_preview('word', rules, language='other') == ('spoken', 1)


@pytest.mark.parametrize('value', [
    [{'written': 'a', 'spoken': 'b', 'regex': 1}],
    [{'written': 'a', 'spoken': 'b', 'language_scope': True}],
    [{'written': 'a', 'spoken': 'b', 'voice_id': 'bad\nvoice'}],
    [{'written': 'a', 'spoken': 'b', 'execute': 'shell'}],
    [{'written': 'a', 'spoken': 'b'}, {'written': 'a', 'spoken': 'c', 'language': 'label'}],
    {'schema': 'unknown-dictionary', 'rules': []},
], ids=['boolean-flags', 'missing-language', 'control-voice', 'unknown-field', 'duplicate-scope', 'unknown-schema'])
def test_dictionary_invalid_scope_flags_and_unsafe_fields_fail_closed(value):
    with pytest.raises(ValueError):
        decode_pronunciation_dictionary(json.dumps(value).encode())


def test_scoped_dictionary_survives_current_complete_backup_and_restore(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Pronunciation recovery')
    project = replace(new_project('Word'), rules=(
        PronunciationRule('word', 'слово', language='uk', language_scope=True,
                          voice_id='fixture:voice', ignore_case=True),))
    store = NarrationStore(app.root)
    digest = store.save(project)
    manifest = create_backup(app, tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', manifest)
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', manifest)
    saved, restored_digest = NarrationStore(restored).load(project.id)
    assert saved == project and restored_digest == digest
    assert pronunciation_preview(saved.current_text, saved.rules, language='uk',
                                 voice_id='fixture:voice', timeout=3) == ('слово', 1)


def test_malformed_worker_response_is_not_adopted(monkeypatch):
    from carr_library.audio import engines
    monkeypatch.setattr(engines, '_run', lambda *a, **k: b'{"matches":[[0,999,"wrong",0]]}')
    with pytest.raises(ValueError, match='invalid match'):
        pronunciation_preview('a', (PronunciationRule('a', 'b', regex=True),))


@pytest.mark.parametrize('pattern,text,replacement', [
    ('(.*)', 'a' * 10000, r'\\1' * 100),
    ('(.*)', 'a' * 10000, r'\101\011'),
    ('(a)' * 11, 'a' * 11, r'\11'),
    ('(a)' * 11, 'a' * 11, r'\111'),
    ('(?P<letter>a)b', 'ab', r'\g<letter>' * 100),
    ('(a)?b', 'b', r'\1' * 100),
    ('(.)', 'К', r'\g<0>'),
], ids=['literal-backslashes', 'octal-literals', 'two-digit-group',
        'three-octal-digits', 'named-small-capture', 'unmatched-capture', 'whole-match'])
def test_preallocation_bound_preserves_small_captures_and_literal_template_escapes(pattern, text, replacement):
    import re
    from carr_library.reader.pronunciation_worker import _expansion_fits
    match = re.match(pattern, text)
    assert match is not None
    assert _expansion_fits(match, replacement, 900000)
    assert len(match.expand(replacement)) <= 900000


def test_capture_expansion_guard_uses_spans_before_copying_source_groups():
    from types import SimpleNamespace
    from carr_library.reader.pronunciation_worker import _expansion_fits
    class SpansOnly:
        regs = ((0, 8 * 1024 * 1024), (0, 8 * 1024 * 1024))
        re = SimpleNamespace(groupindex={'whole': 1})
        def group(self, *args):
            pytest.fail('Sizing copied a captured source string.')
        def expand(self, *args):
            pytest.fail('Sizing allocated a replacement before admitting it.')
    assert not _expansion_fits(SpansOnly(), r'\1' * 2000, 900000)
    assert not _expansion_fits(SpansOnly(), r'\g<whole>' * 400, 900000)


def test_real_large_capture_expansion_is_rejected_without_any_posix_memory_limit():
    """Exercise the actual fixed worker with resource import disabled on every OS."""
    from carr_library.reader import pronunciation_worker
    message = dict(version=1, text='a' * (8 * 1024 * 1024), rules=[
        dict(index=0, pattern='(.*)', replacement=r'\1' * 2000, regex=True, ignore_case=False)])
    script = "import runpy,sys; sys.modules['resource']=None; runpy.run_path(sys.argv[1],run_name='__main__')"
    result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', script,
                             str(Path(pronunciation_worker.__file__).resolve())],
                            input=json.dumps(message).encode('utf-8'), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')[:500]
    assert json.loads(result.stdout) == {'error': 'work-limit'}
