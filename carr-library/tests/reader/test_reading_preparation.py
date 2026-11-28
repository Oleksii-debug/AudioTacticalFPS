"""Reading-only preparation integrity, explicit contexts and real worker ownership."""
from dataclasses import asdict, replace
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest

from carr_library.reader import preparation as subject
from carr_library.reader.narration import (
    NarrationStore, PronunciationRule, apply_preparation, comparison_text, decode_project,
    encode_project, new_project, redo, revise, save_as, undo,
)
from carr_library.reader.preparation import PreparationContext, decode_preparation

_spec = importlib.util.spec_from_file_location('_preparation_fixture', Path(__file__).with_name('preparation_fixture.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
manifest, preview, normal_response = _fixture.manifest, _fixture.preview, _fixture.normal_response


@pytest.mark.parametrize('context', [
    dict(language='zz', kind='number'),
    dict(language='en', kind='guess'),
    dict(language='sk', kind='number', number_mode='ordinal'),
    dict(language='uk', kind='symbol'),
    dict(language='en', kind='date', date_order='auto'),
    dict(language='en', kind='date', number_mode='ordinal'),
    dict(language='en', kind='unit'),
    dict(language='en', kind='number', grammatical_case='dative'),
])
def test_contexts_refuse_unsupported_or_implicit_interpretation(context):
    with pytest.raises(ValueError):
        PreparationContext(**context)


def test_explicit_preview_replays_undo_redo_and_fork_without_touching_original():
    project = new_project('Source 42\r\nУкраїна 😀.')
    proposal = preview(project, 7, 9, 'forty-two')
    changed = apply_preparation(project, proposal)
    assert project.current_text == project.original_text == changed.original_text
    assert changed.current_text == 'Source forty-two\r\nУкраїна 😀.'
    assert undo(changed).current_text == project.current_text
    assert redo(undo(changed)) == changed
    assert decode_project(encode_project(changed)) == changed
    fork = save_as(changed, title='Second reading copy')
    assert fork.id != changed.id and fork.revisions == changed.revisions
    assert decode_project(encode_project(fork)) == fork
    assert 'manifest SHA256' in comparison_text(changed)


@pytest.mark.parametrize('field,value', [
    ('input_sha256', '0' * 64), ('source_sha256', '0' * 64), ('output_sha256', '0' * 64),
    ('start', 0), ('end', 1), ('before', '99'), ('after', 'wrong'),
])
def test_saved_preparation_tampering_is_refused_before_it_can_become_history(field, value):
    project = new_project('A 42 B')
    changed = apply_preparation(project, preview(project, 2, 4, 'forty-two'))
    raw = json.loads(encode_project(changed))
    raw['revisions'][1]['preparation'][field] = value
    with pytest.raises(ValueError, match='invalid'):
        decode_project(json.dumps(raw).encode())


def test_preview_cannot_apply_to_stale_reading_or_changed_original():
    original = new_project('42')
    proposal = preview(original, 0, 2, 'forty-two')
    with pytest.raises(ValueError, match='provenance'):
        apply_preparation(revise(original, '43'), proposal)
    with pytest.raises(ValueError, match='provenance'):
        apply_preparation(new_project('Other original'), proposal)


@pytest.mark.parametrize('scoped', [False, True])
def test_unprepared_legacy_serialization_has_no_new_revision_fields(scoped):
    project = new_project('Original')
    if scoped:
        project = replace(project, rules=(PronunciationRule('a', 'b', language='uk', language_scope=True),))
    raw = json.loads(encode_project(project))
    assert raw['version'] == (3 if scoped else 2)
    assert all(set(row) == {'number', 'text', 'label', 'created_at'} for row in raw['revisions'])
    assert 'legacy_origin' not in raw
    assert decode_project(encode_project(project)) == project
    incomplete_legacy = dict(raw, version=4, legacy_origin=None)
    with pytest.raises(ValueError):
        decode_project(json.dumps(incomplete_legacy).encode())
    raw['revisions'][0]['preparation'] = None
    with pytest.raises(ValueError):
        decode_project(json.dumps(raw).encode())


def test_v5_requires_typed_complete_provenance_and_rejects_executable_context():
    project = new_project('42')
    raw = asdict(preview(project, 0, 2, 'forty-two').record)
    assert decode_preparation(raw).before == '42'
    raw['settings'] += (('execute', 'ignored-book-command'),)
    with pytest.raises(ValueError, match='context'):
        decode_preparation(raw)


@pytest.mark.parametrize('flag', ['license_accepted', 'trusted_weights'])
@pytest.mark.parametrize('engine', ['reading-preparation', 'hunspell'])
def test_optional_asset_review_is_required_before_any_worker_execution(tmp_path, monkeypatch, flag, engine):
    path = manifest(tmp_path, engine, **{flag: False})
    calls = []
    monkeypatch.setattr(subject, '_run', lambda *_a, **_k: calls.append(True))
    project = new_project('42' if engine == 'reading-preparation' else 'helo')
    with pytest.raises(ValueError, match='Review'):
        if engine == 'reading-preparation':
            subject.prepare_selection(project, 0, 2, PreparationContext('en', 'number'), path)
        else:
            subject.spellcheck_selection(project, 0, 4, path, language='en')
    assert calls == []


def test_modified_runtime_file_is_rejected_before_worker_and_host_fallback(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    (path.parent / 'packages/num2words/__init__.py').write_text('changed reviewed asset')
    calls = []
    monkeypatch.setattr(subject, '_run', lambda *_a, **_k: calls.append(True))
    with pytest.raises(ValueError, match='Checksum'):
        subject.prepare_selection(new_project('42'), 0, 2, PreparationContext('en', 'number'), path)
    assert calls == []


def test_explicit_selected_request_is_isolated_and_never_builds_shell_source(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    observed = []
    def run(command, **options):
        request = json.loads(options['data'])
        observed.append((command, request, options))
        assert command[1:4] == ['-I', '-B', '-S']
        assert Path(command[-1]).name == 'preparation_worker.py'
        assert request['selected'] == '42'
        assert request['packages'] == str(path.parent / 'packages')
        assert request['context']['language'] == 'en'
        return normal_response('forty-two', path)
    monkeypatch.setattr(subject, '_run', run)
    project = new_project('Keep 42 exact.')
    result = subject.prepare_selection(project, 5, 7, PreparationContext('en', 'number'), path)
    assert result.text == 'Keep forty-two exact.' and project.current_text == 'Keep 42 exact.'
    assert len(observed) == 1 and result.record.manifest_sha256 == sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('reason', ['invalid-selection', 'work-limit', 'unavailable'])
def test_worker_refusal_preserves_both_original_and_reading_revision(tmp_path, monkeypatch, reason):
    path = manifest(tmp_path)
    project = new_project('03/04/26')
    before = encode_project(project)
    monkeypatch.setattr(subject, '_run', lambda *_a, **_k: json.dumps({'error': reason}).encode())
    with pytest.raises(ValueError):
        subject.prepare_selection(project, 0, len(project.current_text), PreparationContext('en', 'date'), path)
    assert encode_project(project) == before


def test_cancelled_selection_does_not_open_a_missing_runtime(tmp_path):
    with pytest.raises(InterruptedError, match='cancelled'):
        subject.prepare_selection(new_project('42'), 0, 2, PreparationContext('en', 'number'),
                                  tmp_path / 'absent.json', cancelled=lambda: True)


def test_real_owned_worker_timeout_leaves_source_and_runtime_bytes_unchanged(tmp_path, monkeypatch):
    from carr_library.audio.engines import _run
    path = manifest(tmp_path)
    before = {p: p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    def sleeping(_command, **options):
        return _run([sys.executable, '-I', '-B', '-S', '-c', 'import time; time.sleep(10)'], **options)
    monkeypatch.setattr(subject, '_run', sleeping)
    project = new_project('42')
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        subject.prepare_selection(project, 0, 2, PreparationContext('en', 'number'), path, timeout=1.0)
    assert time.monotonic() - started < 5
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert project.current_text == project.original_text == '42'


def test_hunspell_offsets_scoped_overrides_and_one_occurrence_application(tmp_path, monkeypatch):
    path = manifest(tmp_path, 'hunspell')
    project = replace(new_project('Привіт 😀 helo\r\nhelo code_7 𐌰'), rules=(
        PronunciationRule('Привіт', 'Привіт', language='en', language_scope=True),
        PronunciationRule('helo', 'different voice', language='en', voice_id='other', language_scope=True),
    ))
    calls = []
    def run(command, **options):
        if Path(command[-1]).name == 'pronunciation_worker.py':
            from carr_library.audio.engines import _run
            return _run(command, **options)
        request = json.loads(options['data'])
        calls.append(request)
        assert request['words'] == ['helo']
        return json.dumps({'engine_version': '1.7.6', 'isolated': True, 'no_site': True, 'words': [
            {'word': 'helo', 'correct': False, 'suggestions': ['hello']}]}).encode()
    monkeypatch.setattr(subject, '_run', run)
    report = subject.spellcheck_selection(project, 0, len(project.current_text), path, language='en')
    assert report.checked_words == 2 and report.protected_words == 1 and report.skipped_tokens == 2
    assert [(item.start, item.end) for item in report.issues] == [(9, 13), (15, 19)]
    proposal = subject.spelling_proposal(project, report, 1, 'hello')
    changed = apply_preparation(project, proposal)
    assert changed.current_text == 'Привіт 😀 helo\r\nhello code_7 𐌰'
    assert changed.original_text == project.original_text
    assert dict(changed.revisions[-1].preparation.settings)['dictionary_license'] == 'CC0-1.0'
    rule = subject.accepted_word_rule(report, 0)
    assert rule.written == rule.spoken == 'helo' and rule.language == 'en' and rule.language_scope
    assert len(calls) == 1


def test_pronunciation_override_has_priority_over_automatic_number_proposal(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    project = replace(new_project('42'), rules=(PronunciationRule('42', 'explicit answer'),))
    calls = []
    monkeypatch.setattr(subject, '_run', lambda *_a, **_k: calls.append(True))
    with pytest.raises(ValueError, match='override'):
        subject.prepare_selection(project, 0, 2, PreparationContext('en', 'number'), path)
    assert calls == []


def test_dictionary_language_mismatch_is_refused_before_native_load(tmp_path, monkeypatch):
    path = manifest(tmp_path, 'hunspell')
    calls = []
    monkeypatch.setattr(subject, '_run', lambda *_a, **_k: calls.append(True))
    with pytest.raises(ValueError, match='exact language'):
        subject.spellcheck_selection(new_project('слово'), 0, 5, path, language='uk')
    assert calls == []


@pytest.mark.parametrize('response', [
    {'engine_version': 'wrong', 'words': []},
    {'engine_version': '1.7.6', 'words': [{'word': 'different', 'correct': False, 'suggestions': ['hello']}]},
    {'engine_version': '1.7.6', 'words': [{'word': 'helo', 'correct': True, 'suggestions': ['hello']}]},
])
def test_invalid_native_inventory_cannot_manufacture_a_reading_edit(tmp_path, monkeypatch, response):
    path = manifest(tmp_path, 'hunspell')
    monkeypatch.setattr(subject, '_run', lambda *_a, **_k: json.dumps(dict(response, isolated=True, no_site=True)).encode())
    project = new_project('helo')
    with pytest.raises(ValueError):
        subject.spellcheck_selection(project, 0, 4, path, language='en')
    assert len(project.revisions) == 1


def test_unlisted_runtime_file_is_not_silently_imported(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    (path.parent / 'packages/unreviewed.py').write_text('not reviewed')
    calls = []
    monkeypatch.setattr(subject, '_run', lambda *_a, **_k: calls.append(True))
    with pytest.raises(ValueError, match='missing from'):
        subject.prepare_selection(new_project('42'), 0, 2, PreparationContext('en', 'number'), path)
    assert calls == []


def test_optional_worker_does_not_fall_back_to_host_site_packages(tmp_path):
    path = manifest(tmp_path)  # deliberately lacks installed third-party metadata
    project = new_project('42')
    with pytest.raises(ValueError, match='unavailable'):
        subject.prepare_selection(project, 0, 2, PreparationContext('en', 'number'), path)
    assert project.current_text == project.original_text == '42'


def test_worker_origin_outside_selected_optional_pack_is_refused(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    def outside(*_args, **_kwargs):
        value = json.loads(normal_response('forty-two', path))
        value['module_origins']['babel.units']['path'] = str(tmp_path / 'host-site-packages/units.py')
        return json.dumps(value).encode()
    monkeypatch.setattr(subject, '_run', outside)
    with pytest.raises(ValueError, match='outside'):
        subject.prepare_selection(new_project('42'), 0, 2, PreparationContext('en', 'number'), path)
