"""Completed source translations export literal Studio projects, without speech.

The existing HTTPS fixture is synthetic. VoiceRegistry is the actual registry;
its test provider declares capabilities and fingerprints real local test files,
but deliberately cannot synthesize or claim acoustic language quality.
"""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from carr_library.assistant.state import read_json
from carr_library.durable_state import encode
from carr_library.library_backup import restore_backup
from carr_library.application import LibraryApp
from carr_library.speech import Voice, VoiceRegistry
from carr_library.speech.reading import ReadingDocument
from carr_library.studio.project import Project, ProjectStore, Role, bilingual_project

_existing = runpy.run_path(str(Path(__file__).with_name('test_translation.py')))
connection = _existing['connection']
translation_window = _existing['translation_window']


@pytest.fixture
def completed(tmp_path, connection):
    case = _existing['bound_ready'](tmp_path, connection, first=1, last=1,
        raw='[Music: suspense]\nNarrator: Literal words, Україна e\u0301 🙂.\n[Pause: 8]\n')
    identity, approval = _existing['submit_bound'](case)
    assert case.service.resume(identity)['status'] == 'complete'
    case.identity, case.approval = identity, approval
    return case


@pytest.fixture
def voices(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    host = tmp_path / 'declared-host.bin'
    host.write_bytes(b'Synthetic identity fixture, never executed.')
    state = SimpleNamespace(records=[
        Voice('test:original', 'Original declared voice', 'Synthetic declarations', 'en'),
        Voice('test:translation', 'Translation declared voice', 'Synthetic declarations', 'uk'),
        Voice('test:regional', 'Regional voice is not an automatic fallback', 'Synthetic declarations', 'en-US'),
    ], assets={}, registries=[], closed=[], renders=[])
    for voice in state.records:
        path = tmp_path / (voice.id.replace(':', '-') + '.bin')
        path.write_bytes(('Exact test asset for ' + voice.id).encode())
        state.assets[voice.id] = path

    class Provider:
        name, executable = 'Synthetic declarations, not inference', host
        def voices(self, *, cancelled=lambda: False):
            if cancelled():
                raise InterruptedError('Voice review cancelled.')
            return tuple(state.records)
        def fingerprint(self, identity):
            return sha256(state.assets[identity].read_bytes()).hexdigest()
        def render(self, *args, **kwargs):
            state.renders.append(True)
            pytest.fail('Translation export started automatic speech.')
        def close(self):
            state.closed.append(self)

    def registry(_root, *, cancelled=lambda: False):
        value = VoiceRegistry([Provider()], cancelled=cancelled)
        state.registries.append(value)
        return value
    monkeypatch.setattr(runtime_settings, 'voice_registry', registry)
    return state


def selected(case):
    choices = case.service.narration_choices(case.identity)
    assert [record['voice']['id'] for record in choices['original']] == ['test:original']
    assert [record['voice']['id'] for record in choices['translation']] == ['test:translation']
    return choices['original'][0], choices['translation'][0]


def unchanged_evidence(case):
    return (case.service.jobs.path(case.identity).read_bytes(),
            case.service.gateway.ledger._path(case.approval.id).read_bytes())


def test_completed_source_export_is_a_literal_reopenable_studio_project(
        completed, voices, tmp_path, connection, monkeypatch):
    from carr_library.ui.studio_dialog import StudioController
    case = completed
    chosen = selected(case)
    original = case.app.assets.resolve(case.imported['asset'])
    original_bytes = original.read_bytes()
    before, calls = unchanged_evidence(case), len(connection['calls'])
    monkeypatch.setattr(case.service.gateway, 'request', lambda *a, **kw: pytest.fail('New provider call'))
    monkeypatch.setattr(case.vault, 'get', lambda *a, **kw: pytest.fail('New credential access'))
    output = tmp_path / 'bilingual'
    result = case.service.export(case.identity, output, narration_voices=chosen)
    assert result['status'] == 'complete'
    project = Project.from_dict(read_json(output / 'bilingual.studio.json'))
    reading = ReadingDocument.load(output / 'translation.reading.json')
    report = read_json(output / 'translation-provenance.json')
    assert reading.source_text == case.source.text and reading.text != case.source.text
    assert reading.source_anchor == case.document.source_anchor
    assert reading.source_span(0, len(reading.text)).exact is False
    assert [(role.voice_id, role.language) for role in project.roles] == [
        ('test:original', 'en'), ('test:translation', 'uk')]
    assert project.original_script == (output / 'bilingual.txt').read_text(encoding='utf-8')
    saved = case.service.get(case.identity)
    assert len(project.scenes) == len(saved['chunks'])
    for scene, chunk, receipt in zip(project.scenes, saved['chunks'], saved['receipts']):
        assert scene.music is None and scene.cues == ()
        assert ''.join(line.text for line in scene.lines if line.role == 'original') == chunk
        assert ''.join(line.text for line in scene.lines if line.role == 'translation') == receipt['text']
        assert all(case.identity in line.source_ref and 'no quotation or live source authority' in line.source_ref
                   for line in scene.lines)
    assert report['quotation_authority'] is False
    assert report['narration']['project_fingerprint'] == project.fingerprint
    for name, checksum in report['files'].items():
        assert sha256((output / name).read_bytes()).hexdigest() == checksum
    assert len((output / 'bilingual.studio.json').read_bytes()) <= 16 * 1024**2
    assert original.read_bytes() == original_bytes and unchanged_evidence(case) == before
    assert len(connection['calls']) == calls and not voices.renders
    assert len(voices.closed) == len(voices.registries)

    # Use the actual Studio JSON/draft/revision path, without a new job/store.
    controller = StudioController(case.app)
    controller.service.register_project_inputs(project, imported=True)
    controller.service.projects.save(project)
    controller.project = project
    reopened = controller.save(project.original_script, project.title, tuple(role.voice_id for role in project.roles))
    assert reopened.to_dict() == project.to_dict()
    edited = controller.save(project.original_script, 'Explicit display-title edit', ())
    assert edited.revision == project.revision + 1 and edited.scenes == project.scenes
    assert edited.roles == project.roles and edited.original_script == project.original_script
    archive = tmp_path / 'backup'
    checksum = case.app.backup(archive)
    root = restore_backup(archive, tmp_path / 'restored', checksum)
    restored = ProjectStore(root / 'state/studio/projects').load(project.id)
    assert restored == edited
    assert LibraryApp(root).transcription(case.workspace).list() == ()


@pytest.mark.parametrize('change', ['removed', 'language', 'same-name-asset'])
def test_changed_selected_voice_is_refused_before_output(completed, voices, tmp_path, change):
    case, chosen = completed, selected(completed)
    before = unchanged_evidence(case)
    if change == 'removed':
        voices.records = voices.records[:1]
    elif change == 'language':
        voices.records[1] = replace(voices.records[1], language='sv')
    else:
        voices.assets['test:translation'].write_bytes(b'Changed bytes, unchanged advertised name.')
    output = tmp_path / 'changed-voice'
    with pytest.raises(ValueError, match='voice|language'):
        case.service.export(case.identity, output, narration_voices=chosen)
    assert not output.exists() and unchanged_evidence(case) == before and not voices.renders


@pytest.mark.parametrize('change', ['voice', 'settings', 'source', 'cancel'])
def test_late_change_never_publishes_a_bilingual_completion_manifest(
        completed, voices, tmp_path, monkeypatch, change):
    import carr_library.assistant.translation as translation
    case, chosen = completed, selected(completed)
    before, stopped = unchanged_evidence(case), []
    original = case.app.assets.resolve(case.imported['asset'])
    write = translation.atomic_json
    def changed(path, value, *args, **kwargs):
        result = write(path, value, *args, **kwargs)
        if Path(path).name == 'bilingual.studio.json':
            if change == 'voice':
                voices.assets['test:translation'].write_bytes(b'Changed after project staging.')
            elif change == 'settings':
                from carr_library.runtime_settings import load_settings, save_settings
                root = case.app.root / 'state'
                value = load_settings(root)
                value['dordiai'] = {'enabled': False}
                save_settings(root, value)
            elif change == 'source':
                original.write_bytes(original.read_bytes() + b'changed')
            else:
                stopped.append(True)
        return result
    monkeypatch.setattr(translation, 'atomic_json', changed)
    output = tmp_path / 'interrupted'
    with pytest.raises((ValueError, InterruptedError)):
        case.service.export(case.identity, output, narration_voices=chosen, cancelled=lambda: bool(stopped))
    assert (output / 'bilingual.studio.json').exists()  # Incomplete staging is not success.
    assert not (output / 'translation-provenance.json').exists()
    assert unchanged_evidence(case) == before and not voices.renders


def test_archived_source_and_tampered_receipt_do_not_gain_narration_authority(
        completed, voices, tmp_path):
    case, chosen = completed, selected(completed)
    before = unchanged_evidence(case)
    case.app.archive_workspace(case.workspace)
    with pytest.raises(ValueError, match='archived'):
        case.service.export(case.identity, tmp_path / 'archived', narration_voices=chosen)
    assert not (tmp_path / 'archived').exists() and unchanged_evidence(case) == before


@pytest.mark.parametrize('change', ['cancel', 'source'])
def test_final_voice_hash_cannot_hide_cancellation_or_changed_source(
        completed, voices, tmp_path, monkeypatch, change):
    case, chosen = completed, selected(completed)
    output, stopped, observed = tmp_path / 'last-hash', [], []
    before = unchanged_evidence(case)
    fingerprint = VoiceRegistry.fingerprint
    def changed(registry, identity):
        value = fingerprint(registry, identity)  # Complete the actual fixture identity check.
        if identity == 'test:translation' and (output / 'bilingual.studio.json').exists():
            observed.append(True)
            if change == 'cancel':
                stopped.append(True)
            else:
                original = case.app.assets.resolve(case.imported['asset'])
                original.write_bytes(original.read_bytes() + b'changed during final voice inspection')
        return value
    monkeypatch.setattr(VoiceRegistry, 'fingerprint', changed)
    with pytest.raises((ValueError, InterruptedError)):
        case.service.export(case.identity, output, narration_voices=chosen, cancelled=lambda: bool(stopped))
    assert observed == [True] and not (output / 'translation-provenance.json').exists()
    assert unchanged_evidence(case) == before and not voices.renders


def test_self_rehashed_translation_cannot_become_a_studio_project(completed, voices, tmp_path):
    case, chosen = completed, selected(completed)
    job = case.service.jobs.load(case.identity)
    job['receipts'][0]['text'] = 'Unaccepted substituted translation.'
    job['receipts'][0]['translation_sha256'] = sha256(job['receipts'][0]['text'].encode()).hexdigest()
    case.service.jobs.save(job)
    before = unchanged_evidence(case)
    with pytest.raises(ValueError, match='decoded|receipt|changed'):
        case.service.export(case.identity, tmp_path / 'unaccepted', narration_voices=chosen)
    assert not (tmp_path / 'unaccepted').exists() and unchanged_evidence(case) == before


@pytest.mark.parametrize('kind', ['pasted', 'automatic-source', 'unfinished'])
def test_unsupported_narration_scope_never_inspects_voices_or_sends_a_request(
        tmp_path, connection, voices, kind):
    case = _existing['bound_ready'](tmp_path, connection, first=1, last=1, raw='A single original.')
    source = 'auto' if kind == 'automatic-source' else 'en'
    bound = kind != 'pasted'
    approval, cost = _existing['translation_approval'](case.profile, case.source.text, source, 'uk', '1',
                                                     source_bound=bound)
    options = {'source_document': case.document, 'topic': case.topic} if bound else {}
    identity = case.service.submit(case.profile, case.source.text, source, 'uk',
        authorization=approval, per_call_cost=cost, **options)
    if kind != 'unfinished':
        assert case.service.resume(identity)['status'] == 'complete'
    count = len(connection['calls'])
    with pytest.raises(ValueError, match='completed source-bound|explicit source language'):
        case.service.narration_choices(identity)
    assert not voices.registries and not voices.renders and len(connection['calls']) == count


def test_narration_serialization_budget_fails_before_export_without_rebilling(
        completed, voices, tmp_path, monkeypatch, connection):
    import carr_library.studio.project as studio
    case, chosen = completed, selected(completed)
    before, count = unchanged_evidence(case), len(connection['calls'])
    monkeypatch.setattr(studio, 'MAX_PROJECT_IMPORT_BYTES', 128)
    with pytest.raises(ValueError, match='16 MiB'):
        case.service.export(case.identity, tmp_path / 'over-budget', narration_voices=chosen)
    assert not (tmp_path / 'over-budget').exists() and unchanged_evidence(case) == before
    assert len(connection['calls']) == count
    # The completed translation remains usable with its original text workflow.
    assert case.service.export(case.identity, tmp_path / 'text-only')['status'] == 'complete'


def test_literal_builder_keeps_every_character_and_obeys_real_import_budget():
    roles = (Role('original', 'Original', 'declared:en', 'en'),
             Role('translation', 'Translation', 'declared:uk', 'uk'))
    original = '[Music: suspense]\n' + 'Source e\u0301 🙂.\r\n' * 900
    translated = '[Scene: not a direction]\n' + 'Переклад 🙂.\n' * 900
    project = bilingual_project(((original, translated),), title='Literal paired project',
        roles=roles, translation_id='a' * 32, source_sha256=sha256(original.encode()).hexdigest())
    assert ''.join(line.text for line in project.scenes[0].lines if line.role == 'original') == original
    assert ''.join(line.text for line in project.scenes[0].lines if line.role == 'translation') == translated
    assert max(len(line.text) for line in project.scenes[0].lines) <= 2400
    assert Project.from_dict(json.loads(encode(project.to_dict()))) == project
    huge = '🙂' * 1_100_000
    with pytest.raises(ValueError, match='16 MiB'):
        bilingual_project(((huge, huge),), title='Too large', roles=roles,
            translation_id='b' * 32, source_sha256=sha256(huge.encode()).hexdigest())


@pytest.mark.parametrize('selection', [(-1, -1), (0, -1), (0, 0), 'destroy'])
def test_native_voice_chooser_requires_both_choices_and_ignores_destroyed_controls(
        completed, voices, translation_window, monkeypatch, selection):
    from carr_library.ui.translation_dialog import choose_narration_voices
    choices = completed.service.narration_choices(completed.identity)
    window, wx = translation_window, sys.modules['wx']
    control = wx.Dialog
    monkeypatch.setattr(control, 'SetSizerAndFit', lambda self, value: None, raising=False)
    monkeypatch.setattr(control, 'CreateButtonSizer', lambda self, value: None, raising=False)
    wx.NOT_FOUND, wx.OK, wx.CANCEL = -1, 1, 2
    def show(dialog):
        rows = [item for item in window.controls if item.parent is dialog and item.name.endswith(('(en)', '(uk)'))]
        assert len(rows) == 2 and all(row.selection == -1 for row in rows)
        if selection == 'destroy':
            dialog.handlers['destroy'](SimpleNamespace(GetId=lambda: dialog.GetId(), Skip=lambda: None))
            dialog.Destroy()
            for row in rows:
                row.GetSelection = lambda: pytest.fail('Read a destroyed selection control')
        else:
            for row, index in zip(rows, selection):
                row.SetSelection(index)
        return wx.ID_OK
    monkeypatch.setattr(control, 'ShowModal', show)
    if selection == (0, 0):
        assert choose_narration_voices(None, choices) == (choices['original'][0], choices['translation'][0])
    else:
        with pytest.raises((ValueError, InterruptedError)):
            choose_narration_voices(None, choices)
    assert not voices.renders


def test_native_completed_job_exports_through_the_same_narration_service(
        completed, voices, translation_window, monkeypatch, tmp_path, connection):
    case, window, wx = completed, translation_window, sys.modules['wx']
    frame = window.bind(case)
    wx.NOT_FOUND, wx.OK, wx.CANCEL = -1, 1, 2
    control, base_show = wx.Dialog, wx.Dialog.ShowModal
    monkeypatch.setattr(control, 'SetSizerAndFit', lambda self, value: None, raising=False)
    monkeypatch.setattr(control, 'CreateButtonSizer', lambda self, value: None, raising=False)
    def show(dialog):
        if dialog.title.startswith('Bilingual narration'):
            rows = [item for item in window.controls if item.parent is dialog and item.name.endswith(('(en)', '(uk)'))]
            assert len(rows) == 2 and all(row.selection == -1 for row in rows)
            for row in rows:
                row.SetSelection(0)
            return wx.ID_OK
        return base_show(dialog)
    monkeypatch.setattr(control, 'ShowModal', show)
    before, count = unchanged_evidence(case), len(connection['calls'])
    def scenario():
        window.click('Saved jobs')
        window.click('Bilingual audio project')
        assert 'No speech has run' in window.find('Preview, supported').value
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame)
    files = list(tmp_path.glob('Translation-*/bilingual.studio.json'))
    assert len(files) == 1
    assert Project.from_dict(read_json(files[0])).roles[1].voice_id == 'test:translation'
    assert unchanged_evidence(case) == before and len(connection['calls']) == count
    assert not voices.renders and not window.gateways
