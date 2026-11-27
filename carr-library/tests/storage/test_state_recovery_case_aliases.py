"""Windows spellings cannot turn executable typed state into opaque backup data."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProviderProfile
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_recovery import prepare_restored_state, validate_state_documents
from carr_library.transcription import settings


IDENTITY = 'a' * 32
WORKSPACE = 'case-safe-workspace'
SCOPE = sha256(WORKSPACE.encode()).hexdigest()


def document(kind, root):
    if kind == 'publication':
        return {
            'schema': 'resonance-publication/1', 'id': IDENTITY, 'status': 'prepared',
            'studio_root': str(root / 'studio'), 'output': str(root.parent / 'old-output.epub'),
            'artifact': {'size': 321, 'sha256': '1' * 64},
            'source': {'job_id': 'b' * 32, 'job_sha256': '2' * 64,
                'project_id': 'c' * 32, 'project_sha256': '3' * 64,
                'source_script_sha256': '4' * 64,
                'artifacts': [{'name': name, 'size': 1, 'sha256': '5' * 64}
                              for name in ('project.json', 'timeline.json', 'master.wav')]},
            'created': '2026-11-27T00:00:00Z', 'notice': 'Historical publication.'}
    if kind == 'audio':
        return {'schema': 1, 'ffmpeg': str(root.parent / 'absent-ffmpeg.exe'),
                'ffprobe': str(root.parent / 'absent-ffprobe.exe'), 'piper': [],
                'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    if kind == 'transcription-engine':
        return {'version': 1, 'python': str(root.parent / 'absent-python.exe'),
                'worker': str(root.parent / 'absent-worker.py'),
                'models_dir': str(root.parent / 'absent-models'),
                'python_sha256': '6' * 64, 'worker_sha256': '7' * 64,
                'network': 'offline', 'inference_qualified': False}
    assert kind == 'transcription-job'
    return {'version': 1, 'id': IDENTITY, 'workspace': WORKSPACE,
            'title': 'Historical ASR job', 'created': 1.0, 'updated': 1.0,
            'status': 'ready', 'sources': [{'filename': 'source.wav',
                'original_name': str(root.parent / 'original.wav'), 'size_bytes': 4,
                'sha256': sha256(b'data').hexdigest()}],
            'settings': settings(), 'done': 0, 'failed': 0, 'receipts': [],
            'artifacts': [], 'run_context': None, 'last_error': ''}


CASES = (
    ('publication', f'studio/publications/{IDENTITY}.json', 'restore_requires_output'),
    ('audio', 'audio/engines.json', 'restore_requires_revalidation'),
    ('transcription-engine', 'transcription/engine.json', 'restore_requires_revalidation'),
    ('transcription-job', f'transcription/{SCOPE}/jobs/{IDENTITY}/job.json', 'restore_requires_review'),
)


def alias(name, spelling):
    parts = name.split('/')
    if spelling in ('directories', 'both'):
        parts[:-1] = [part.upper() for part in parts[:-1]]
    if spelling in ('extension', 'both'):
        parts[-1] = parts[-1][:-5] + '.JSON'
    return '/'.join(parts)


def stored(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)
    raw = path.read_bytes()
    return path, {'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


@pytest.mark.parametrize('kind,name,marker', CASES, ids=[item[0] for item in CASES])
@pytest.mark.parametrize('spelling', ('canonical', 'directories', 'extension', 'both'))
def test_typed_restore_marks_every_windows_alias_without_renaming(tmp_path, kind, name, marker, spelling):
    root = tmp_path / 'state'
    selected = alias(name, spelling)
    before = document(kind, root)
    path, entry = stored(root, selected, before)
    original = path.read_bytes()
    validate_state_documents(root, [entry])
    assert path.read_bytes() == original
    prepare_restored_state(root, [entry])
    assert read_json(path) == {**before, marker: True}
    assert [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()] == [selected]


@pytest.mark.parametrize('kind,name,marker', CASES, ids=[item[0] for item in CASES])
def test_full_backup_retains_original_spelling_and_revokes_authority(tmp_path, kind, name, marker):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Case-safe restore')
    name = alias(name, 'both')
    before = document(kind, app.root / 'state')
    original, _ = stored(app.root / 'state', name, before)
    original_bytes = original.read_bytes()
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert name in {entry['filename'] for entry in manifest['state']}
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    assert read_json(restored / 'state' / name) == {**before, marker: True}
    assert original.read_bytes() == original_bytes
    assert (folder / 'state' / name).read_bytes() == original_bytes


@pytest.mark.parametrize('kind,name,marker', CASES, ids=[item[0] for item in CASES])
def test_invalid_typed_marker_in_alias_blocks_backup_before_publication(tmp_path, kind, name, marker):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Reject aliased authority')
    value = {**document(kind, app.root / 'state'), marker: 1}
    path, _ = stored(app.root / 'state', alias(name, 'both'), value)
    original = path.read_bytes()
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists()
    assert path.read_bytes() == original


@pytest.mark.parametrize('kind,name,marker', CASES, ids=[item[0] for item in CASES])
def test_known_alias_cannot_bypass_json_budget(tmp_path, monkeypatch, kind, name, marker):
    import carr_library.state_recovery as recovery
    def forbidden(*args, **kwargs):
        raise AssertionError('Oversized typed state must be rejected before opening it.')
    monkeypatch.setattr(recovery, 'read_json', forbidden)
    with pytest.raises(ValueError, match='budget'):
        validate_state_documents(tmp_path, [{'filename': alias(name, 'both'),
            'size_bytes': 32 * 1024 * 1024 + 1, 'sha256': '0' * 64}])


@pytest.mark.parametrize('kind,name,marker', CASES, ids=[item[0] for item in CASES])
def test_malformed_json_in_known_alias_is_not_opaque_state(tmp_path, kind, name, marker):
    name = alias(name, 'both')
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    raw = b'not valid JSON'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        validate_state_documents(tmp_path, [{'filename': name, 'size_bytes': len(raw),
                                            'sha256': sha256(raw).hexdigest()}])
    assert path.read_bytes() == raw


def test_case_sensitive_profile_identity_is_preserved_while_credentials_are_revoked(tmp_path):
    profile = ProviderProfile('MixedCase_Profile', 'Existing connection', 'openai-compatible',
        'https://example.test/v1', 'model', enabled=True,
        credential_ref='carr/provider/MixedCase_Profile/' + 'b' * 32)
    before = {'schema': 'carr-provider-profile/1', **profile.to_dict()}
    path, entry = stored(tmp_path, 'Providers/MixedCase_Profile.PROFILE.JSON', before)
    prepare_restored_state(tmp_path, [entry])
    assert read_json(path) == {**before, 'credential_ref': None, 'enabled': False}


def test_opaque_uppercase_user_filenames_and_original_bytes_are_preserved(tmp_path):
    path, entry = stored(tmp_path, 'User Notes/Точний Текст.JSON', {'original': 'Source text'})
    before = path.read_bytes()
    prepare_restored_state(tmp_path, [entry])
    assert path.read_bytes() == before
    assert [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file()] == [entry['filename']]


def test_portable_alias_conflicts_still_fail_before_restore_publication(tmp_path):
    from carr_library.state_backup import restore_state
    source = tmp_path / 'backup-state'
    _, first = stored(source, 'Studio/Publications/' + IDENTITY + '.json', document('publication', source))
    second = {**first, 'filename': 'studio/publications/' + IDENTITY + '.JSON'}
    target = tmp_path / 'restored-state'
    with pytest.raises(ValueError, match='Duplicate'):
        restore_state(source, target, [first, second])
    assert not target.exists()
