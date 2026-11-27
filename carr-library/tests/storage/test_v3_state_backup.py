"""Independent V3 full-backup checks with real SQLite and private sidecars."""
from hashlib import sha256
import json
import os
import wave
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.profiles import ProfileStore, ProviderProfile
from carr_library.runtime_settings import load_settings, save_settings
from carr_library.speech import ReadingDocument, Voice
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.studio.render import ProductionService
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.state_backup import snapshot_state, validate_state


class InventoryOnly:
    def voices(self):
        return (Voice('test:inventory', 'Inventory fixture', 'test', 'en'),)


def generated_cache(state):
    key = sha256(b'synthetic completed PCM fixture').hexdigest()
    path = state / 'studio/cache' / (key + '-0123456789.wav')
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 800)
    atomic_json(path.with_name(key + '.json'), {
        'schema': 1, 'key': key, 'file': path.name, 'sha256': sha256(path.read_bytes()).hexdigest(),
        'duration': .1, 'sample_rate': 8000, 'channels': 1})
    return path


def resign(folder, manifest):
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.fixture
def populated(tmp_path):
    app = LibraryApp(tmp_path / 'дані')
    first = app.create_workspace('Перша бібліотека')
    second = app.create_workspace('Second private library')
    app.add_text(first, 'Оригінал', 'Точний оригінальний текст — «цитата».')
    app.add_text(second, 'Separate original', 'A separate private source.')
    topic = app.create_topic(first, 'Research')
    hit, = app.search(first, 'Точний')
    app.save_hit(first, topic, hit.ref, hit.revision, 'Separate private note')
    state = app.root / 'state'
    # Use the selected primary producers rather than arbitrary JSON that no
    # installed reader could validate or reopen.
    save_settings(state, load_settings(state))
    reader = ReaderDraftStore(app.root)
    document = ReadingDocument('Точний текст')
    document.replace(0, len(document.text), 'Точний текст.')
    reader.save(ReaderResume(document, 'test:inventory', cursor=3))
    profiles = ProfileStore(state / 'providers')
    profiles.save(ProviderProfile('account', 'Synthetic account reference', 'openai-compatible',
        'https://example.invalid/v1', 'fixture-model',
        credential_ref='carr/provider/account/' + 'a' * 32))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    studio = ProductionService(state / 'studio', voices=InventoryOnly())
    project = studio.create('Narrator: Preserve a queued production.', voice_ids=('test:inventory',))
    studio.request_pause(studio.submit(project, outputs))
    assistant = AssistantService(app)
    plan = Plan(Scope(workspace_id=first, output_root=outputs), (Step('library.list'),))
    assistant_job = assistant.submit(plan)
    assert assistant.run(assistant_job).status == 'complete'
    assistant.pause(assistant_job)
    values = {p.relative_to(state).as_posix(): read_json(p)
              for p in state.rglob('*.json')}
    generated_cache(state)
    (state / 'studio/.render.lock').write_bytes(b'')
    (state / 'studio/.pending-edit').write_bytes(b'not committed')
    (state / 'speech/.pending-recovery.tmp').write_bytes(b'not committed')
    return app, first, second, topic, values


@pytest.mark.parametrize('version', [3, 4])
def test_backup_restores_exact_state_sources_and_evidence_without_cache(populated, tmp_path, version):
    app, first, second, topic, values = populated
    before = app.evidence(first, topic)[0]
    original_files = {p.name: p.read_bytes() for p in app.assets_dir.iterdir()}
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['version'] == 4 and manifest['generated_audio_included'] is False
    assert {entry['filename'] for entry in manifest['sidecars']} == {'state/' + name for name in values}
    for entry in manifest['sidecars']:
        raw = (folder / entry['filename']).read_bytes()
        assert len(raw) == entry['size_bytes'] and sha256(raw).hexdigest() == entry['sha256']
    if version == 3:
        manifest['version'] = 3
        manifest['state'] = [{**entry, 'filename': entry['filename'].removeprefix('state/')}
                             for entry in manifest.pop('sidecars')]
        digest = resign(folder, manifest)
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    recovered = LibraryApp(destination)
    assert {w.id for w in recovered.workspaces()} == {first, second}
    assert recovered.evidence(first, topic)[0] == before
    assert {p.name: p.read_bytes() for p in recovered.assets_dir.iterdir()} == original_files
    for name, value in values.items():
        assert read_json(destination / 'state' / name) == value
    assert not (destination / 'state/studio/cache').exists()
    assert not list((destination / 'state').rglob('*.lock'))
    assert not list((destination / 'state').rglob('*.tmp'))
    assert all(p.stat().st_nlink == 1 for p in (destination / 'state').rglob('*') if p.is_file())


def test_optional_generated_cache_roundtrips_exactly(populated, tmp_path):
    app, *_ = populated
    folder = tmp_path / 'with-cache'
    digest = create_backup(app, folder, include_generated=True)
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['generated_audio_included'] is True
    destination = restore_backup(folder, tmp_path / 'restored-with-cache', digest)
    original = app.root / 'state/studio/cache'
    assert {p.name: p.read_bytes() for p in (destination / 'state/studio/cache').iterdir()} == {
        p.name: p.read_bytes() for p in original.iterdir()}


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'unlisted', 'missing-state-directory', 'wrong-manifest'])
def test_corrupt_state_never_publishes_usable_restore(populated, tmp_path, damage):
    app, first, second, topic, _ = populated
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    state_file = folder / 'state/audio/engines.json'
    if damage == 'corrupt':
        raw = bytearray(state_file.read_bytes())
        raw[-2] ^= 1
        state_file.write_bytes(raw)
    elif damage == 'missing':
        state_file.unlink()
    elif damage == 'unlisted':
        (folder / 'state/extra.json').write_text('{}')
    elif damage == 'missing-state-directory':
        import shutil
        shutil.rmtree(folder / 'state')
    else:
        manifest = json.loads((folder / 'manifest.json').read_text())
        manifest['sidecars'][0]['size_bytes'] += 1
        raw = json.dumps(manifest).encode()
        (folder / 'manifest.json').write_bytes(raw)
        digest = sha256(raw).hexdigest()
    destination = tmp_path / 'restore'
    with pytest.raises((ValueError, OSError)):
        restore_backup(folder, destination, digest)
    assert not destination.exists()
    assert app.evidence(first, topic)[0].note == 'Separate private note'


@pytest.mark.parametrize('filename', ['../escape.json', '/escape.json', 'a/../escape.json', 'a\\escape.json',
                                     'C:/escape.json', 'a//b.json', 'CON.json', 'a/aux', 'a/file.', 'a/file ',
                                     'a/\x00bad', './file.json'])
def test_state_manifest_rejects_unsafe_portable_paths(filename):
    with pytest.raises(ValueError):
        validate_state([{'filename': filename, 'size_bytes': 0, 'sha256': sha256(b'').hexdigest()}])


def test_state_manifest_rejects_case_duplicates_and_byte_overflow():
    good = {'filename': 'Folder/voice.json', 'size_bytes': 0, 'sha256': sha256(b'').hexdigest()}
    with pytest.raises(ValueError, match='Duplicate'):
        validate_state([good, {**good, 'filename': 'folder/VOICE.JSON'}])
    with pytest.raises(ValueError, match='budget'):
        validate_state([{**good, 'size_bytes': 33 * 1024**3}])


@pytest.mark.parametrize('kind', ['file-symlink', 'directory-symlink', 'hardlink'])
def test_backup_rejects_linked_state(populated, tmp_path, kind):
    app, *_ = populated
    outside = tmp_path / 'private-outside'
    outside.mkdir()
    secret = outside / 'private.json'
    secret.write_text('{"secret":"outside scope"}')
    link = app.root / 'state/linked'
    if kind == 'file-symlink':
        link.symlink_to(secret)
    elif kind == 'directory-symlink':
        link.symlink_to(outside, target_is_directory=True)
    else:
        os.link(secret, link)
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists()


def test_dangling_state_root_is_not_silently_omitted(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    (app.root / 'state').symlink_to(tmp_path / 'not-existing', target_is_directory=True)
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists()


def test_active_state_database_requires_consistent_adapter(populated, tmp_path):
    app, *_ = populated
    (app.root / 'state/active.sqlite').write_bytes(b'not a snapshot')
    with pytest.raises(ValueError, match='(?:snapshot|backup) adapter'):
        create_backup(app, tmp_path / 'backup')


@pytest.mark.parametrize('mutation', ['change', 'delete', 'add'])
def test_snapshot_detects_changes_to_already_copied_tree(tmp_path, mutation):
    root = tmp_path / 'source'
    root.mkdir()
    first = root / 'a.json'
    first.write_bytes(b'old')
    (root / 'b.json').write_bytes(b'b' * (2 * 1024 * 1024))
    target = tmp_path / 'stage'
    target.mkdir()
    modified = False
    def concurrent_writer():
        nonlocal modified
        if not modified and (target / 'a.json').exists() and (target / 'b.json').exists():
            modified = True
            if mutation == 'change':
                first.write_bytes(b'new')
            elif mutation == 'delete':
                first.unlink()
            else:
                (root / 'c.json').write_text('added while backup is running')
        return False
    with pytest.raises((ValueError, OSError), match='changed|snapshot|backup'):
        snapshot_state(root, target, cancelled=concurrent_writer)
    assert modified


@pytest.mark.parametrize('state_kind', ['absent', 'empty', 'zero-byte'])
def test_cancelled_backup_never_publishes_success_for_empty_state(tmp_path, state_kind):
    app = LibraryApp(tmp_path / 'data')
    if state_kind != 'absent':
        (app.root / 'state').mkdir()
    if state_kind == 'zero-byte':
        (app.root / 'state/empty.txt').write_bytes(b'')
    destination = tmp_path / 'backup'
    with pytest.raises(InterruptedError):
        create_backup(app, destination, cancelled=lambda: True)
    assert not destination.exists()


def test_state_publication_crash_retains_incomplete_restore_marker(populated, tmp_path, monkeypatch):
    app, *_ = populated
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    destination = tmp_path / 'restored'
    original = os.link
    def interrupted(source, target, *args, **kwargs):
        if Path(target).is_relative_to(destination / 'state'):
            raise OSError('simulated state disk full')
        return original(source, target, *args, **kwargs)
    monkeypatch.setattr(os, 'link', interrupted)
    with pytest.raises(OSError, match='disk full'):
        restore_backup(backup, destination, digest)
    assert (destination / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(destination)


def test_legacy_backup_still_restores_without_state(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Legacy')
    app.add_text(workspace, 'Source', 'Legacy source stays readable.')
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['version'] == 4 and manifest['sidecars'] == []
    manifest['version'] = 2
    del manifest['sidecars'], manifest['generated_audio_included']
    digest = resign(backup, manifest)
    assert json.loads((backup / 'manifest.json').read_text())['version'] == 2
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    assert restored.search(workspace, 'Legacy')[0].quote == 'Legacy'
    assert not (restored.root / 'state').exists()
