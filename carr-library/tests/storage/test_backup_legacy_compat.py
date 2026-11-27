"""Authentic frozen-writer fixtures, exact retention and transactional restore."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_compat.archive import recovery_notices
from carr_library.library_backup import create_backup, restore_backup


FIXTURES = Path(__file__).parents[1] / 'fixtures/legacy-backups-5580d4d'
INDEX = json.loads((FIXTURES / 'fixtures.json').read_text(encoding='utf-8'))


def archive(tmp_path, version):
    entry = INDEX['archives'][version]
    source = FIXTURES / (version + '.zip')
    assert sha256(source.read_bytes()).hexdigest() == entry['archive_sha256']
    directory = tmp_path / 'legacy'
    directory.mkdir()
    with ZipFile(source) as incoming:
        # The committed synthetic ZIP itself is hash-bound before extraction.
        incoming.extractall(directory)
    assert sha256((directory / 'manifest.json').read_bytes()).hexdigest() == entry['manifest_sha256']
    return directory, entry


def file_bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('version', ['v3', 'v4', 'v3-empty'])
def test_authentic_legacy_restores_core_and_preserves_every_original_component_inert(tmp_path, version):
    source, data = archive(tmp_path, version)
    before = file_bytes(source)
    destination = restore_backup(source, tmp_path / 'restored', data['manifest_sha256'])
    assert file_bytes(source) == before
    retained = destination / 'legacy-backups' / data['manifest_sha256']
    assert file_bytes(retained) == before
    assert (retained / 'originals').is_dir()
    assert all(path.stat().st_nlink == 1 for path in retained.rglob('*') if path.is_file())
    assert not any((destination / name).exists() for name in
                   ('audio-projects', 'provider-settings', 'agent-jobs', 'import-batches'))
    notice, = recovery_notices(destination)
    assert notice['archive_status'] == 'inert'
    if version == 'v4':
        statuses = {item['component']: item['status'] for item in notice['components']}
        assert statuses['pending-imports'] == statuses['provider-settings'] == statuses['agent-jobs'] == 'inert'
        assert statuses['audio-projects/' + data['project_id']] == 'inert'
        assert statuses['canonical-pending-imports'] == 'restored'
        assert any(item['reference'] == data['required_audio'] and item['included'] is False
                   for item in notice['required_inputs'])
        with sqlite3.connect(retained / 'recovery/provider-settings/providers.sqlite') as db:
            assert db.execute('SELECT mode FROM policy').fetchone() == ('HYBRID',)
            assert db.execute('SELECT credential_ref FROM profiles').fetchone()[0] is not None
        # Those exact historical declarations remain under the inert archive;
        # no active provider settings or OS credential lookups were created.
        assert not (destination / 'provider-settings').exists()
    else:
        assert not (destination / 'import-requests').exists()
    restored = LibraryApp(destination)
    assert restored.workspaces()[0].id == data['workspace']
    if version != 'v3-empty':
        assert restored.search(data['workspace'], 'Exact')[0].quote == 'Exact'
        assert not (destination / 'narration-projects').exists()


@pytest.mark.parametrize('version', ['v3', 'v4', 'v3-empty'])
def test_restored_legacy_archive_survives_v5_rebackup_without_losing_empty_originals(tmp_path, version):
    source, data = archive(tmp_path, version)
    destination = restore_backup(source, tmp_path / 'first', data['manifest_sha256'])
    app = LibraryApp(destination)
    complete = tmp_path / 'v5'
    digest = create_backup(app, complete)
    value = json.loads((complete / 'manifest.json').read_text())
    assert value['version'] == 5
    assert value['extensions']['legacy_archives'] == [dict(manifest_sha256=data['manifest_sha256'],
        format='legacy-recovery-v4' if version == 'v4' else 'legacy-sidecars-v3', status='inert')]
    final = restore_backup(complete, tmp_path / 'second', digest)
    assert file_bytes(final / 'legacy-backups' / data['manifest_sha256']) == file_bytes(source)
    assert (final / 'legacy-backups' / data['manifest_sha256'] / 'originals').is_dir()
    assert recovery_notices(final) == recovery_notices(destination)


def test_workspace_scope_omits_independent_inert_legacy_archives(tmp_path):
    source, data = archive(tmp_path, 'v4')
    app = LibraryApp(restore_backup(source, tmp_path / 'first', data['manifest_sha256']))
    backup = tmp_path / 'scoped'
    digest = create_backup(app, backup, authorized_workspaces={data['workspace']})
    value = json.loads((backup / 'manifest.json').read_text())
    assert value['extensions']['legacy_archives'] == []
    assert not (backup / 'extensions/legacy-backups').exists()
    destination = restore_backup(backup, tmp_path / 'scoped-restored', digest)
    assert not (destination / 'legacy-backups').exists()


def reseal(source):
    value = json.loads((source / 'manifest.json').read_text())
    for item in value.get('recovery', {}).get('files', []):
        path = source / 'recovery' / item['path']
        raw = path.read_bytes()
        item.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = json.dumps(value).encode()
    (source / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('damage', ['mixed-dialect', 'numeric-reinterpretation', 'unlisted', 'provider-schema', 'wrong-digest'])
def test_legacy_schema_or_authenticated_envelope_tampering_fails_before_destination(tmp_path, damage):
    source, data = archive(tmp_path, 'v4')
    digest = data['manifest_sha256']
    manifest = source / 'manifest.json'
    if damage in ('mixed-dialect', 'numeric-reinterpretation'):
        value = json.loads(manifest.read_text())
        if damage == 'mixed-dialect':
            value.update(state=[], generated_audio_included=False)
        else:
            value['version'] = 5
        manifest.write_text(json.dumps(value), encoding='utf-8')
        digest = reseal(source)
    elif damage == 'unlisted':
        (source / 'extra.txt').write_text('not authorized by manifest')
    elif damage == 'provider-schema':
        with sqlite3.connect(source / 'recovery/provider-settings/providers.sqlite') as db:
            db.execute('CREATE TABLE sqliteXextra(secret TEXT)')
        digest = reseal(source)
    else:
        digest = '0' * 64
    destination = tmp_path / 'rejected'
    with pytest.raises((ValueError, sqlite3.Error)):
        restore_backup(source, destination, digest)
    assert not destination.exists()


def test_legacy_external_audio_path_is_declared_without_any_probe(tmp_path, monkeypatch):
    source, data = archive(tmp_path, 'v4')
    original_stat = Path.stat
    def guarded(path, *args, **kwargs):
        assert str(path) != data['required_audio'], 'legacy restore accessed an unavailable external cue'
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', guarded)
    destination = restore_backup(source, tmp_path / 'restored', data['manifest_sha256'])
    notice, = recovery_notices(destination)
    assert any(item['kind'] == 'audio-file' for item in notice['required_inputs'])


def test_compatible_retained_legacy_text_queue_resumes_through_the_canonical_service(tmp_path):
    from carr_library.ingest.worker import load_request
    source, data = archive(tmp_path, 'v4')
    destination = restore_backup(source, tmp_path / 'restored', data['manifest_sha256'])
    request = load_request(destination, data['request_id'])
    assert request['data_dir'] == str(destination)
    assert Path(request['source']).is_relative_to(destination / 'originals')
    original = json.loads((source / 'recovery/import-requests' / (data['request_id'] + '.json')).read_text())
    assert request['config'] == original['config']
    assert request['job'] == original['job']
    assert request['metadata'] == original['metadata']
    app = LibraryApp(destination)
    result = app.resume_import(data['workspace'], data['request_id'])
    assert result['state'] == 'complete'
    assert result['id'] == original['job'] and result['edition'] == original['metadata']['edition_id']
    assert app.search(data['workspace'], 'Pending preserved source.')[0].quote == 'Pending preserved source.'


def test_unmapped_managed_ocr_profile_stays_inert_with_original_queue_bytes_preserved(tmp_path):
    source, data = archive(tmp_path, 'v4')
    path = source / 'recovery/import-requests' / (data['request_id'] + '.json')
    record = json.loads(path.read_text())
    record['config']['model_profile'] = 'best'
    path.write_text(json.dumps(record), encoding='utf-8')
    digest = reseal(source)
    destination = restore_backup(source, tmp_path / 'restored', digest)
    assert not (destination / 'import-requests').exists()
    assert (destination / 'legacy-backups' / digest / path.relative_to(source)).read_bytes() == path.read_bytes()
    notice, = recovery_notices(destination)
    assert not any(item['component'] == 'canonical-pending-imports' for item in notice['components'])
    assert next(item for item in notice['components'] if item['component'] == 'pending-imports')['status'] == 'inert'


def add_inert_batch(source, data, selected_source):
    """Add an authored batch to an authentic fixture, then bind its exact bytes."""
    from carr_library.backup_compat.batches import STATES
    manifest = json.loads((source / 'manifest.json').read_text())
    request = json.loads((source / 'recovery/import-requests' / (data['request_id'] + '.json')).read_text())
    identity = 'a' * 32
    row = dict(row_id='1' * 32, work_id='2' * 32, edition_id='3' * 32,
               request_id='4' * 32, source=selected_source, file='original.txt',
               title='Retained declaration', state='queued', message='',
               manifest=dict(sha256='e' * 64, mime='text/plain', size=9),
               duplicate_of=None, result=None)
    counts = dict.fromkeys(STATES, 0)
    counts['queued'] = 1
    batch = dict(version=1, batch_id=identity, data_dir=manifest['recovery']['source_root'],
                 workspace=data['workspace'], created='2026-01-01T00:00:00Z',
                 updated='2026-01-01T00:00:00Z', state='queued', collections=[],
                 recursive=False, config=request['config'], rows=[row], counts=counts, total=1)
    relative = 'import-batches/' + identity + '.json'
    path = source / 'recovery' / relative
    path.parent.mkdir()
    raw = json.dumps(batch).encode('utf-8')
    path.write_bytes(raw)
    manifest['recovery']['files'].append(dict(path=relative, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    manifest['recovery']['files'].sort(key=lambda item: item['path'])
    (source / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return reseal(source), path


def test_other_platform_batch_is_preserved_inert_and_declares_unretained_input_without_probing(tmp_path, monkeypatch):
    source, data = archive(tmp_path, 'v4')
    external = '/never-open-legacy-source/original.txt' if os.name == 'nt' else r'Z:\never-open-legacy-source\original.txt'
    digest, batch_path = add_inert_batch(source, data, external)
    before = file_bytes(source)
    original_stat = Path.stat
    def guarded(path, *args, **kwargs):
        assert str(path) != external, 'inert batch declaration was used as a filesystem path'
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', guarded)
    destination = restore_backup(source, tmp_path / 'restored', digest)
    assert file_bytes(source) == before
    assert file_bytes(destination / 'legacy-backups' / digest) == before
    assert not (destination / 'import-batches').exists()
    assert not (destination / 'import-requests').exists()
    notice, = recovery_notices(destination)
    assert not any(item['component'] == 'canonical-pending-imports' for item in notice['components'])
    assert next(item for item in notice['components'] if item['component'] == 'pending-imports')['status'] == 'inert'
    required, = [item for item in notice['required_inputs'] if item['reference'] == external]
    assert required == dict(component='legacy-backups/' + digest + '/' + batch_path.relative_to(source).as_posix(),
                            kind='source-file', reference=external, included=False)
    backup = tmp_path / 'complete'
    next_digest = create_backup(LibraryApp(destination), backup)
    restored = restore_backup(backup, tmp_path / 'again', next_digest)
    assert recovery_notices(restored) == (notice,)


@pytest.mark.parametrize('source_name', ['relative/original.txt', r'Z:\safe\..\outside.txt'])
def test_inert_batch_still_rejects_relative_or_traversing_source_declarations(tmp_path, source_name):
    source, data = archive(tmp_path, 'v4')
    digest, _ = add_inert_batch(source, data, source_name)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(source, destination, digest)
    assert not destination.exists()


@pytest.mark.parametrize('declared_root', ['/former-machine/library', r'Z:\former-machine\library'])
def test_legacy_recovery_original_root_is_portable_inert_data(tmp_path, monkeypatch, declared_root):
    from carr_library.backup_compat import recovery
    source, _ = archive(tmp_path, 'v4')
    manifest = json.loads((source / 'manifest.json').read_text())['recovery']
    manifest['source_root'] = declared_root

    def forbidden_host_path(*args, **kwargs):
        raise AssertionError('An original-machine declaration became a host filesystem path')

    monkeypatch.setattr(recovery, 'Path', forbidden_host_path)
    assert recovery.validate_manifest(manifest) is manifest


@pytest.mark.parametrize('declared_root', [None, True, 1, '', 'relative/library',
    r'Z:relative', 'file:///former-machine/library', '/former\x00machine', '/' + 'a' * 32768],
    ids=['none', 'boolean', 'integer', 'empty', 'relative-posix', 'relative-windows',
         'file-uri', 'nul', 'overlong'])
def test_legacy_recovery_rejects_invalid_original_root_declarations(tmp_path, declared_root):
    from carr_library.backup_compat import recovery
    source, _ = archive(tmp_path, 'v4')
    manifest = json.loads((source / 'manifest.json').read_text())['recovery']
    manifest['source_root'] = declared_root
    with pytest.raises(ValueError, match='Invalid recovery manifest'):
        recovery.validate_manifest(manifest)
