"""Complete producer roundtrips through the one library backup registry."""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_audio, library_backup
from carr_library.assistant import AssistantService, Scope
from carr_library.evidence_drafts import EvidenceDrafts
from carr_library.extract.ocr_languages import load_presets, store_preset
from carr_library.extract.page_ocr import Config
from carr_library.keyboard_preferences import KeymapStore
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.ui.audio_studio import StudioController
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry


def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\x20\x03\xe0\xfc' * 4000)
    return path.read_bytes()


def _manifest(folder):
    return json.loads((folder / 'manifest.json').read_bytes())


def _resign(folder, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _file_bytes(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in folder.rglob('*') if p.is_file()}


def _studio(app, script='[output sample_rate=8000 stems=false]\n[sound "EFFECT.WAV"]'):
    studio = StudioController(private_root=app.root)
    studio.new(script, title='Saved sound project', input_kind='script')
    saved = studio.save_in_library()
    raw = _wav(studio.output_dir / 'assets' / 'EFFECT.WAV')
    studio.edit_script(script + '\n[pause 0.1]')
    recovery = studio.save_recovery()
    return studio, saved, recovery, raw


@pytest.fixture
def complete_state(tmp_path):
    app = LibraryApp(tmp_path / 'Library')
    workspace = app.create_workspace('Research')
    app.add_text(workspace, 'Original', 'Exact source. Україна and Slovensko.',
                 edition_id='edition')
    page = ReadingDesk(app, workspace, 'edition').page(1)
    reading = ReadingState(app.root)
    reading.remember(page.location)
    bookmark = reading.add_bookmark(page.location, 'Exact source anchor')
    topic = app.create_topic(workspace, 'Notes')
    hit, = app.search(workspace, 'Exact source')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Committed note')
    row, = app.evidence(workspace, topic)
    draft = EvidenceDrafts(app.root).checkpoint(row, 'Unsaved research note.')
    registry = CommandRegistry(builtin_commands())
    overrides = {'library.new': 'Ctrl+Alt+N'}
    KeymapStore(app.root / 'keyboard-shortcuts.json').save(overrides, registry)
    narrator = NarrationStore(app.root)
    narration = new_project(page.text, title='Source reading copy', source_location=page.location)
    narrator.save(narration)
    reading_draft = revise(narration, 'A derivative reading draft.')
    narrator.save(reading_draft, draft=True)
    store_preset(app.root, 'Fast English', Config(languages='eng', model_profile='fast'))
    store_preset(app.root, 'Best English', Config(languages='eng', model_profile='best'))
    studio, saved, recovery, audio = _studio(app)
    return SimpleNamespace(app=app, workspace=workspace, page=page, bookmark=bookmark,
        row=row, draft=draft, registry=registry, overrides=overrides, narration=narration,
        reading_draft=reading_draft, studio=studio, saved=saved, recovery=recovery, audio=audio)


def test_actual_research_and_studio_producers_survive_verified_backup_reopen(complete_state, tmp_path):
    s = complete_state
    (s.app.root / 'credentials.json').write_text('private synthetic marker')
    (s.app.root / 'models').mkdir()
    (s.app.root / 'models' / 'external-weights.bin').write_bytes(b'external optional dependency')
    (s.studio.output_dir / 'assets' / 'private.json').write_text('private synthetic marker')
    originals = _file_bytes(s.app.root / 'originals')
    saved, recovery = s.saved.read_bytes(), s.recovery.read_bytes()
    folder = tmp_path / 'complete'
    digest = s.app.backup(folder)
    inventory = verify_backup(folder, digest)
    assert inventory['version'] == 4
    assert not inventory['generated_audio_included']  # No render has been produced.
    names = {entry['filename'] for entry in inventory['sidecars']}
    assert {'keyboard-shortcuts.json', 'evidence-drafts.json', 'reading-state.json',
            'ocr-language-presets.json'} <= names
    assert s.saved.relative_to(s.app.root).as_posix() in names
    assert s.recovery.relative_to(s.app.root).as_posix() in names
    assert not any('credentials' in n or n.startswith('models/') or n.endswith('private.json') for n in names)
    before = _file_bytes(folder)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    app = LibraryApp(target)
    assert app.evidence(s.workspace, s.row.topic_id) == (s.row,)
    assert EvidenceDrafts(target).get(*s.draft.key) == s.draft
    assert KeymapStore(target / 'keyboard-shortcuts.json').load() == s.overrides
    assert ReadingState(target).resume(s.workspace, 'edition') == s.page.location
    assert ReadingState(target).bookmarks(s.workspace, 'edition') == (s.bookmark,)
    assert NarrationStore(target).load(s.narration.id)[0] == s.narration
    assert NarrationStore(target).load(s.narration.id, draft=True)[0] == s.reading_draft
    assert {item['model_profile'] for item in load_presets(target)} == {'fast', 'best'}
    assert _file_bytes(target / 'originals') == originals
    project = target / s.saved.relative_to(s.app.root)
    reopened = StudioController(private_root=target).open(project)
    assert not reopened.dirty and reopened.output_dir.is_dir()
    assert reopened.output_dir.is_relative_to(target)
    assert project.read_bytes() == saved
    reopened.recover()
    assert reopened.script == s.studio.script
    assert reopened.output_dir.is_relative_to(target)
    assert (reopened.output_dir / 'assets' / 'EFFECT.WAV').read_bytes() == s.audio
    assert project.with_name(project.name + '.recovery.json').read_bytes() == recovery
    assert all((target / name).read_bytes() == (folder / name).read_bytes() for name in names)
    assert all((target / name).stat().st_nlink == 1 for name in names)
    assert _file_bytes(folder) == before
    assert s.saved.read_bytes() == saved and s.recovery.read_bytes() == recovery
    assert _file_bytes(s.app.root / 'originals') == originals


@pytest.mark.parametrize('layout', ['root-v4', 'legacy-sidecars-v4'])
def test_exact_historical_v4_layouts_authenticate_without_rewriting(tmp_path, layout):
    app = LibraryApp(tmp_path / 'data')
    app.create_workspace('Legacy format compatibility')
    studio, saved, recovery, audio = _studio(app)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = _manifest(folder)
    if layout == 'legacy-sidecars-v4':
        payload = folder / 'sidecars'
        payload.mkdir()
        for name in {entry['filename'].split('/')[0] for entry in manifest['sidecars']}:
            (folder / name).rename(payload / name)
        manifest.pop('generated_audio_included')
        manifest['sidecars'] = [dict(path=e['filename'], size_bytes=e['size_bytes'], sha256=e['sha256'])
                               for e in manifest['sidecars']]
        digest = _resign(folder, manifest)
    before = _file_bytes(folder)
    assert verify_backup(folder, digest) == manifest
    target = restore_backup(folder, tmp_path / 'restored', digest)
    reopened = StudioController(private_root=target).open(target / saved.relative_to(app.root))
    assert reopened.output_dir.is_relative_to(target) and not reopened.dirty
    reopened.recover()
    assert reopened.script == studio.script
    assert (reopened.output_dir / 'assets' / 'EFFECT.WAV').read_bytes() == audio
    assert _file_bytes(folder) == before


@pytest.mark.parametrize('change', ['mixed-fields', 'mixed-descriptor', 'false-generated', 'audio-v3'])
def test_ambiguous_audio_manifests_fail_before_destination(tmp_path, change):
    app = LibraryApp(tmp_path / 'data')
    app.create_workspace('Checked schemas')
    studio, *_ = _studio(app)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = _manifest(folder)
    if change == 'mixed-fields':
        manifest['state'] = []
    elif change == 'mixed-descriptor':
        manifest['sidecars'][0]['path'] = manifest['sidecars'][0]['filename']
    elif change == 'audio-v3':
        manifest['version'] = 3
        manifest.pop('generated_audio_included')
    else:
        name = 'audio-projects/' + studio.project_id + '/audio/render-journal.json'
        raw = b'{}'
        path = folder / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        manifest['sidecars'].append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
        manifest['generated_audio_included'] = False
    digest = _resign(folder, manifest)
    with pytest.raises(ValueError):
        verify_backup(folder, digest)
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


def test_read_only_verification_streams_audio_without_a_second_audio_copy(complete_state, tmp_path, monkeypatch):
    folder = tmp_path / 'backup'
    digest = create_backup(complete_state.app, folder)
    before = _file_bytes(folder)
    copy_record = backup_audio.copy_record
    verified = []
    def inspect(root, relative, target, identity, cancelled):
        assert target is None
        verified.append(relative)
        return copy_record(root, relative, target, identity, cancelled)
    monkeypatch.setattr(backup_audio, 'copy_record', inspect)
    assert verify_backup(folder, digest)['version'] == 4
    assert any('/audio/assets/' in name for name in verified)
    assert _file_bytes(folder) == before


@pytest.mark.parametrize('limit', ['files', 'bytes', 'cancelled', 'disk-full'])
def test_budget_and_copy_failures_never_publish_an_incomplete_backup(complete_state, tmp_path, monkeypatch, limit):
    s = complete_state
    before = s.saved.read_bytes(), s.recovery.read_bytes()
    options = {}
    if limit == 'files':
        options['max_sidecar_files'] = 1
    elif limit == 'bytes':
        options['max_sidecar_bytes'] = 1
    elif limit == 'cancelled':
        options['cancelled'] = lambda: True
    else:
        monkeypatch.setattr(backup_audio.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
    with pytest.raises((ValueError, OSError, InterruptedError)):
        create_backup(s.app, tmp_path / 'rejected', **options)
    assert not (tmp_path / 'rejected').exists()
    assert (s.saved.read_bytes(), s.recovery.read_bytes()) == before


def test_primary_assistant_keeps_complete_audio_and_its_recovery_receipt(complete_state, tmp_path):
    s = complete_state
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = AssistantService(s.app)
    scope = Scope(s.workspace, (), outputs, allow_full_backup=True,
                  max_bytes=16 * 1024**2, max_files=1000)
    job = service.submit(service.preview('Backup "complete"', scope))
    result = service.run(job)
    assert result.status == 'complete', result.message
    saved = result.results[0]
    folder = outputs / 'complete'
    value = verify_backup(folder, saved['manifest_sha256'])
    assert any(row['filename'] == 'state/assistant/' + job + '.json' for row in value['sidecars'])
    assert any(row['filename'].endswith('/studio/project.json') for row in value['sidecars'])
    before = _file_bytes(folder)
    assert service.resume(job).status == 'complete'
    assert _file_bytes(folder) == before
    target = restore_backup(folder, tmp_path / 'restored', saved['manifest_sha256'])
    restored = AssistantService(LibraryApp(target))
    with pytest.raises(PermissionError, match='another data directory'):
        restored.run(job)
    assert 'scope' not in restored.saved_plan_for_review(job)
    reopened = StudioController(private_root=target).open(target / s.saved.relative_to(s.app.root))
    reopened.recover()
    assert reopened.script == s.studio.script
