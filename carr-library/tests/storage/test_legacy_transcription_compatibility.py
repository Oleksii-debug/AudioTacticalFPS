"""All retained ASR families coexist; historical bytes never authorize execution."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library import backup_legacy_state, legacy_transcription as recovery
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.transcription import TranscriptionService as ScopedService
from carr_library.transcription_service import TranscriptionService
from carr_library.transcription_receipt import validate_settings
from carr_library.transcription_state import read_definition
from carr_library import transcription_backup
from tests.storage.legacy_transcription_fixture import canonical_data, current_runtime
from tests.storage.test_transcription_backup import saved


def test_all_three_asr_families_roundtrip_independently_without_starting_workers(saved, tmp_path):
    app, workspace, scoped, scoped_id, revision, private, original = saved
    canonical = canonical_data(app.root, complete=True)
    runtime = current_runtime(tmp_path / 'current-runtime')
    current = TranscriptionService(app.root)
    current_id = current.prepare([canonical.source_path], runtime, validate_settings({}))
    definition = read_definition(current._job(current_id))
    scoped_bytes = {path.relative_to(app.root).as_posix(): path.read_bytes()
                    for path in transcription_backup.state_paths(app.root)}
    backup = tmp_path / 'all-families'
    checksum = create_backup(app, backup)
    verify_backup(backup, checksum)
    relocated = restore_backup(backup, tmp_path / 'relocated', checksum)
    assert {name: (relocated / name).read_bytes() for name in scoped_bytes} == scoped_bytes
    assert not (relocated / 'state/transcription/jobs').exists()
    assert not (relocated / 'state/transcription/profiles').exists()
    reopened = LibraryApp(relocated)
    old = ScopedService(reopened, workspace)
    assert old.verify(scoped_id)['done'] == 1 and old.edits(scoped_id)[0]['id'] == revision
    assert read_definition(relocated / 'transcription-jobs' / current_id) == definition
    rows = recovery.jobs(relocated)
    assert len(rows) == 1 and rows[0]['reference']['job'] == canonical.job['id']
    assert recovery.read_transcript(relocated, rows[0]['reference'], canonical.source['id'])['text'] == canonical.text
    assert not (relocated / 'transcription-jobs' / current_id / 'execution.json').exists()
    again = tmp_path / 'again'
    verify_backup(again, create_backup(reopened, again))


@pytest.mark.parametrize('name', ['unknown.json', 'unknown-directory'])
def test_unknown_sibling_is_not_hidden_by_disjoint_family_inventory(saved, tmp_path, name):
    app = saved[0]
    canonical_data(app.root)
    unexpected = app.root / 'state/transcription' / name
    if name.endswith('.json'):
        unexpected.write_text('{}')
    else:
        unexpected.mkdir()
    target = tmp_path / 'refused'
    with pytest.raises((ValueError, RuntimeError)):
        create_backup(app, target)
    assert not target.exists()


@pytest.mark.parametrize('name', ['profiles', 'jobs'])
def test_canonical_root_must_be_directory_even_with_existing_scoped_state(saved, tmp_path, name):
    app = saved[0]
    (app.root / 'state/transcription' / name).write_bytes(b'Not a canonical directory')
    with pytest.raises((ValueError, RuntimeError, OSError)):
        create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


@pytest.mark.parametrize('rejected', [False, True], ids=['matching-manifest', 'changed-manifest'])
def test_new_preparation_binds_the_reviewed_current_manifest_before_publication(tmp_path, rejected):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Explicit recovery')
    saved = canonical_data(app.root)
    recovery.retain_live(app.root)
    reference = recovery.jobs(app.root)[0]['reference']
    manifest = current_runtime(tmp_path / 'runtime')
    expected = sha256(manifest.read_bytes()).hexdigest()
    if rejected:
        value = json.loads(manifest.read_bytes())
        value['model_id'] = 'different-current-identity'
        manifest.write_text(json.dumps(value), encoding='utf-8')
    service = TranscriptionService(app.root)
    if rejected:
        with pytest.raises(ValueError):
            recovery.import_as_new(service, reference, [saved.source['id']], manifest, validate_settings({}),
                                   expected_manifest_sha256=expected)
        assert service.jobs() == ()
    else:
        result = recovery.import_as_new(service, reference, [saved.source['id']], manifest, validate_settings({}),
                                        expected_manifest_sha256=expected)
        assert result['definition']['runtime_manifest_sha256'] == expected
        assert not (service._job(result['id']) / 'execution.json').exists()
    assert not list(app.root.glob('.legacy-transcription-*'))


def test_generic_historical_budget_and_deep_portable_paths_remain_unchanged():
    assert backup_legacy_state.MAX_STATE_BYTES == 32 * 1024**3
    deep = '/'.join(['x'] * 1000 + ['saved.json'])
    row = dict(filename=deep, size_bytes=1, sha256='1' * 64)
    backup_legacy_state.validate_state([row])
    large = dict(row, filename='saved.bin', size_bytes=backup_legacy_state.MAX_STATE_BYTES + 1)
    with pytest.raises(ValueError):
        backup_legacy_state.validate_state([large])
    backup_legacy_state.validate_state([large], max_bytes=backup_legacy_state.MAX_TYPED_ARCHIVE_BYTES)


def test_catalog_exclusions_cannot_hide_arbitrary_unlisted_historical_files(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'hidden.bin').write_bytes(b'not catalog')
    with pytest.raises(ValueError, match='catalog'):
        backup_legacy_state.verify_state(source, None, [], ignored={'hidden.bin'})
