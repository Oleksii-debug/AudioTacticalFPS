"""Publication receipts survive SQLite backup without reopening old output paths."""
from hashlib import sha256
import json
from pathlib import Path
import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_recovery import prepare_restored_state, validate_state_documents
from carr_library.studio.publishing import validate_publication_document
from carr_library.studio.publishing_source import LibraryPublicationService


IDENTITY = 'a' * 32


def receipt(root):
    return {
        'schema': 'resonance-publication/1', 'id': IDENTITY, 'status': 'prepared',
        'studio_root': str(root / 'state' / 'studio'), 'output': str(root.parent / 'former-output.epub'),
        'artifact': {'size': 321, 'sha256': '1' * 64},
        'source': {'job_id': 'b' * 32, 'job_sha256': '2' * 64, 'project_id': 'c' * 32,
            'project_sha256': '3' * 64, 'source_script_sha256': '4' * 64,
            'artifacts': [{'name': name, 'size': 1, 'sha256': '5' * 64}
                          for name in ('project.json', 'timeline.json', 'master.wav')]},
        'created': '2026-11-27T00:00:00Z', 'notice': 'Historical measured-line publication receipt.'
    }


def store(root, value):
    path = root / 'state' / 'studio' / 'publications' / (IDENTITY + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)
    return path


def test_full_backup_preserves_publication_identity_and_revokes_implicit_output(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Publication recovery fixture')
    before = receipt(app.root)
    store(app.root, before)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    after = read_json(restored / 'state/studio/publications' / (IDENTITY + '.json'))
    assert after == {**before, 'restore_requires_output': True}
    validate_publication_document(after)
    # Even matching the current state root cannot restore external-file authority.
    after['studio_root'] = str(restored / 'state/studio')
    atomic_json(restored / 'state/studio/publications' / (IDENTITY + '.json'), after, replace=True)
    with pytest.raises(ValueError, match='explicitly select'):
        LibraryPublicationService(restored).verify(IDENTITY)


def test_same_directory_restore_marks_receipt_without_rebinding_source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Publication recovery fixture')
    before = receipt(app.root)
    path = store(app.root, before)
    entries = [{'filename': f'studio/publications/{IDENTITY}.json',
                'size_bytes': path.stat().st_size, 'sha256': sha256(path.read_bytes()).hexdigest()}]
    prepare_restored_state(app.root / 'state', entries)
    assert read_json(path) == {**before, 'restore_requires_output': True}


@pytest.mark.parametrize('bad', [
    lambda row: row.update(credential='must not be accepted'),
    lambda row: row.update(restore_requires_output=1),
    lambda row: row['source'].update(secret='must not be accepted'),
    lambda row: row['source']['artifacts'][0].update(name='../outside'),
    lambda row: row['source']['artifacts'][0].update(name='C:relative'),
    lambda row: row['artifact'].update(size=True),
    lambda row: row.update(id='d' * 32),
])
def test_invalid_typed_publication_receipt_blocks_backup_publication(tmp_path, bad):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Publication recovery fixture')
    value = receipt(app.root); bad(value)
    store(app.root, value)
    target = tmp_path / 'rejected-backup'
    with pytest.raises(ValueError):
        create_backup(app, target)
    assert not target.exists()


@pytest.mark.parametrize('raw', [b'not-json', b'[]', b'{"schema":"unknown"}'])
def test_known_publication_path_cannot_fall_back_to_opaque_state(tmp_path, raw):
    root = tmp_path / 'state'
    path = root / 'studio/publications' / (IDENTITY + '.json')
    path.parent.mkdir(parents=True); path.write_bytes(raw)
    entries = [{'filename': path.relative_to(root).as_posix(), 'size_bytes': len(raw),
                'sha256': sha256(raw).hexdigest()}]
    with pytest.raises(ValueError):
        validate_state_documents(root, entries)


def test_known_publication_path_obeys_metadata_budget_before_reading(tmp_path):
    with pytest.raises(ValueError, match='budget'):
        validate_state_documents(tmp_path, [{'filename': f'studio/publications/{IDENTITY}.json',
            'size_bytes': 32 * 1024 * 1024 + 1, 'sha256': '0' * 64}])
