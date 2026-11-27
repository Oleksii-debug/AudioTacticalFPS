"""Real canonical producer -> reviewed root preservation boundary.

The two producer fixtures are exact public source blobs from canonical184dd83d.
They run against actual SQLite/catalog/original files, with no external engine,
provider, private document or model. Opaque state is never a resumability claim.
"""
from contextlib import closing
from hashlib import sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

import carr_library.application as application
from carr_library.application import LibraryApp
from carr_library.ingest.retained_assets import RetainedAssets
from carr_library.ingest.restore import RestoreError
import carr_library.library_backup as backup
from carr_library.operations import ResourceBroker, ResourceLimits
import carr_library.preserved_state as preserved
from carr_library.storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from carr_library.storage.schema_identity import expected_schema_fingerprint

FIXTURES = Path(__file__).with_name('fixtures')
PRODUCER_BLOBS = {
    'canonical_library_backup.py': '01b4945c5f51ec90012b1ec895cca929dbdaff13',
    'canonical_state_backup.py': '0224cf450d7d3790876bee9b03ad0573f70ba173',
    'canonical_v5_library_backup.py': '8adc760d1ea99f87ecb7c577e2a9b264e1a3d812',
    'canonical_v5_state_backup.py': 'a6b01f762dc362dfd23544c6f5f374fddd210787',
}


def load_producer(name, filename, monkeypatch):
    path = FIXTURES / filename
    raw = path.read_bytes().replace(b'\r\n', b'\n')
    assert sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest() == PRODUCER_BLOBS[filename]
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, name, module)
    specification.loader.exec_module(module)
    return module


@pytest.fixture(params=[4, 5], ids=['canonical184dd-schema4', 'canonical1eff-schema5'])
def canonical_producer(monkeypatch, request):
    prefix = 'canonical_' if request.param == 4 else 'canonical_v5_'
    load_producer('carr_library.state_backup', prefix + 'state_backup.py', monkeypatch)
    module = load_producer('carr_library._canonical_backup_fixture',
                           prefix + 'library_backup.py', monkeypatch)
    return SimpleNamespace(create_backup=module.create_backup, schema_version=request.param)


def file_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':')) + '\n').encode('utf-8'))


def resign(folder, manifest):
    write_json(folder / 'manifest.json', manifest)
    return sha256((folder / 'manifest.json').read_bytes()).hexdigest()


def make_legacy(tmp_path, monkeypatch, producer, *, version=4, state_kind='populated'):
    # Build genuine accepted V4 data through the application. This changes only
    # the fixture's target schema; production migration is not modified.
    with monkeypatch.context() as legacy:
        legacy.setattr(application, 'CURRENT_SCHEMA_VERSION', producer.schema_version)
        app = LibraryApp(tmp_path / 'legacy-library')
        workspace = app.create_workspace('Public synthetic legacy library')
        topic = app.create_topic(workspace, 'Saved original evidence')
        app.add_text(workspace, 'Original', 'Точна цитата. Public railway archive.',
                     authors=('Synthetic author',))
        hit, = app.search(workspace, 'Точна')
        app.save_hit(workspace, topic, hit.ref, hit.revision, 'Separate user-authored note.')
        evidence = app.evidence(workspace, topic)[0]

    if version == 3:
        # A historical V3 backup has no pending-import component. Use a closed
        # catalog snapshot and registered originals, not a forged manifest.
        root = tmp_path / 'legacy-without-import-journal'
        root.mkdir()
        with closing(Storage(app.database, schema_version=producer.schema_version)) as storage:
            storage.backup(root / 'library.sqlite')
        shutil.copytree(app.assets_dir, root / 'originals')
        app = SimpleNamespace(root=root, database=root / 'library.sqlite',
                              assets=RetainedAssets(root / 'originals'))
    state = app.root / 'state'
    state.mkdir()
    values = {}
    if state_kind == 'populated':
        values = {
            'speech/session.reader.json': {
                'schema': 'carr.reader-session.v1',
                'document': {'source_text': 'Original\r\n  Unapproved edit history.', 'edits': []},
                'voice_id': 'old-local-voice', 'cursor': 0},
            'studio/projects/public/project.json': {
                'schema': 1, 'id': 'public', 'original_script': 'Preserve exactly.\r\n',
                'roles': [], 'scenes': [], 'revision': 1},
            'assistant/' + 'a' * 32 + '.json': {
                'schema': 'carr-assistant-job/1', 'status': 'queued',
                'plan': {'historical_path': '/do-not-open-or-execute'}},
            'translation/jobs/' + 'b' * 32 + '.json': {
                'schema': 'carr-assistant-job/1', 'kind': 'translation/1',
                'authorization': {'approved': True}, 'source_text': 'Unverified derivative source'},
            'providers/public.profile.json': {
                'credential_ref': 'synthetic-reference-not-a-secret', 'enabled': True,
                'endpoint': 'https://example.invalid/never-contact'},
            'audio/engines.json': {'schema': 1, 'ffmpeg': '/do-not-launch'},
        }
        for name, value in values.items():
            write_json(state / name, value)
        (state / 'speech' / '.ignored.lock').write_bytes(b'')
        (state / 'speech' / 'unsaved.tmp').write_bytes(b'Not a committed project.')
    elif state_kind == 'empty-nested':
        (state / 'speech' / 'empty-folder').mkdir(parents=True)
    elif state_kind != 'empty':
        raise AssertionError(state_kind)
    folder = tmp_path / 'canonical-backup'
    checksum = producer.create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == version
    assert manifest['schema_version'] == producer.schema_version
    assert manifest['schema_fingerprint'] == expected_schema_fingerprint(producer.schema_version)
    assert 'sidecars' not in manifest and 'provider_restore_mode' not in manifest
    return app, workspace, topic, evidence, folder, checksum, manifest, values


@pytest.mark.parametrize('version', [3, 4])
def test_actual_canonical_writer_restores_catalog_and_inert_unchanged_state(
        tmp_path, monkeypatch, canonical_producer, version):
    app, workspace, topic, evidence, folder, checksum, manifest, values = make_legacy(
        tmp_path, monkeypatch, canonical_producer, version=version)
    before = file_bytes(folder)
    source_originals = file_bytes(app.root / 'originals')

    def forbidden(*args, **kwargs):
        pytest.fail('A backup restore must not activate a network service, engine or job.')
    with monkeypatch.context() as passive:
        passive.setattr(subprocess, 'Popen', forbidden)
        passive.setattr(socket, 'create_connection', forbidden)
        passive.setattr(socket.socket, 'connect', forbidden)
        target = backup.restore_backup(folder, tmp_path / 'restored', checksum)

    assert file_bytes(folder) == before
    assert file_bytes(target / 'originals') == source_originals
    assert not (target / 'state').exists(), 'Legacy execution paths must never become live.'
    receipt = json.loads((target / preserved.DIRECTORY / preserved.REVIEW).read_bytes())
    assert receipt['status'] == preserved.STATUS and receipt['migration_completed'] is False
    assert receipt['execution_authorized'] is receipt['provider_authorized'] is receipt['quotation_authority'] is False
    assert receipt['origin'] == {
        'kind': 'canonical-library-backup', 'manifest_version': version,
        'manifest_sha256': checksum, 'generated_audio_included': False}
    assert {item['filename'] for item in receipt['files']} == set(values)
    for item in receipt['files']:
        raw = (target / preserved.DIRECTORY / 'files' / item['filename']).read_bytes()
        assert raw == before['state/' + item['filename']]
        assert sha256(raw).hexdigest() == item['sha256']
    assert 'not been migrated' in preserved.review_notice(target)
    with sqlite3.connect(target / 'library.sqlite') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == canonical_producer.schema_version
    restored = LibraryApp(target)
    assert restored.evidence(workspace, topic)[0] == evidence
    assert restored.search(workspace, 'Точна')[0].quote == 'Точна'
    with restored.read(workspace) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
    assert file_bytes(folder) == before


@pytest.mark.parametrize('version', [3, 4])
@pytest.mark.parametrize('state_kind', ['empty', 'empty-nested'])
def test_actual_producer_empty_state_grammar_is_preserved(
        tmp_path, monkeypatch, canonical_producer, version, state_kind):
    _, _, _, _, folder, checksum, manifest, _ = make_legacy(
        tmp_path, monkeypatch, canonical_producer, version=version, state_kind=state_kind)
    assert manifest['state'] == []
    assert list((folder / 'state').iterdir()) == []
    target = backup.restore_backup(folder, tmp_path / 'restored', checksum)
    receipt = json.loads((target / preserved.DIRECTORY / preserved.REVIEW).read_bytes())
    assert receipt['files'] == [] and receipt['status'] == preserved.STATUS
    assert (target / preserved.DIRECTORY / 'files').is_dir()
    assert not (target / 'state').exists()


@pytest.mark.parametrize('with_policy', [False, True])
def test_preserved_state_survives_typed_v7_roundtrip_and_policy_remains_typed(
        tmp_path, monkeypatch, canonical_producer, with_policy):
    _, workspace, topic, evidence, folder, checksum, _, _ = make_legacy(
        tmp_path, monkeypatch, canonical_producer)
    restored = LibraryApp(backup.restore_backup(folder, tmp_path / 'first-restore', checksum))
    original_state = file_bytes(restored.root / preserved.DIRECTORY)
    if with_policy:
        ResourceBroker.for_app(restored.root).configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    second = tmp_path / 'typed-v7'
    digest = backup.create_backup(restored, second)
    value = json.loads((second / 'manifest.json').read_bytes())
    assert value['version'] == 7
    assert set(value) == {'version', 'kind', 'schema_version', 'schema_fingerprint', 'database',
                          'assets', 'sidecars', 'provider_restore_mode', 'pending',
                          'transcription', 'preserved_state'}
    assert value['provider_restore_mode'] == 'LOCAL_ONLY'
    assert bool(value['transcription']['files']) is with_policy
    assert value['preserved_state']['status'] == preserved.STATUS
    assert file_bytes(second / preserved.DIRECTORY) == original_state
    assert {p.relative_to(second).as_posix() for p in second.rglob('*') if p.is_file()} == (
        set(backup.declared_backup_files(value)) | {'manifest.json'})
    target = backup.restore_backup(second, tmp_path / 'second-restore', digest)
    assert file_bytes(target / preserved.DIRECTORY) == original_state
    assert not (target / 'state/assistant').exists()
    assert not (target / 'state/translation').exists()
    assert not (target / 'state/providers').exists()
    assert (target / 'state/operations/resources.json').exists() is with_policy
    assert LibraryApp(target).evidence(workspace, topic)[0] == evidence


def test_live_canonical_state_is_preserved_inertly_without_copying_typed_runtime_neighbors(tmp_path):
    app = LibraryApp(tmp_path / 'live')
    write_json(app.root / 'state/ui/progress.json', {'old_voice': '/never-open'})
    write_json(app.root / 'state/translation/jobs' / ('a' * 32 + '.json'),
               {'kind': 'translation/1', 'authorization': {'approved': True}})
    write_json(app.root / 'state/providers/account.profile.json',
               {'credential_ref': 'synthetic-reference', 'enabled': True})
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    originals = file_bytes(app.root / 'state')
    folder = tmp_path / 'backup'
    checksum = backup.create_backup(app, folder)
    value = json.loads((folder / 'manifest.json').read_bytes())
    assert value['version'] == 7
    assert [row['filename'] for row in value['transcription']['files']] == ['resource-policy.json']
    names = {row['filename'] for row in value['preserved_state']['files']}
    assert not any('operations/' in name or 'transcription/' in name for name in names)
    assert 'files/ui/progress.json' in names and any('translation/jobs/' in name for name in names)
    assert file_bytes(app.root / 'state') == originals
    target = backup.restore_backup(folder, tmp_path / 'restored', checksum)
    assert (target / 'state/operations/resources.json').exists()
    assert not (target / 'state/providers').exists()
    assert not (target / 'state/translation').exists()
    assert 'requires review' in preserved.review_notice(target)


@pytest.mark.parametrize('damage', ['same-size', 'missing', 'extra-file', 'extra-directory'])
def test_canonical_state_damage_prevents_publication(
        tmp_path, monkeypatch, canonical_producer, damage):
    _, _, _, _, folder, checksum, _, _ = make_legacy(tmp_path, monkeypatch, canonical_producer)
    path = folder / 'state/audio/engines.json'
    if damage == 'same-size':
        raw = bytearray(path.read_bytes())
        raw[-2] ^= 1
        path.write_bytes(raw)
    elif damage == 'missing':
        path.unlink()
    elif damage == 'extra-file':
        (folder / 'state/unknown.txt').write_bytes(b'Unlisted')
    else:
        (folder / 'state/unknown-empty').mkdir()
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        backup.restore_backup(folder, target, checksum)
    assert not target.exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a//b', 'a/../b', 'x\\y',
    'file:stream', 'CON.json', 'nested/LPT1.txt', 'COM¹.txt', 'CONIN$.txt', 'LPT2 .txt',
    'trailing.', 'trailing ', 'a?b', 'a/\x01b'])
def test_preservation_rejects_nonportable_paths_without_allocating_payloads(name):
    with pytest.raises(ValueError):
        preserved.validate_canonical_state([{'filename': name, 'size_bytes': 0,
                                              'sha256': sha256(b'').hexdigest()}])


@pytest.mark.parametrize('change', ['mixed-shapes', 'provider-permission', 'schema-unknown', 'boolean-option'])
def test_canonical_shape_does_not_relax_typed_manifest_acceptance(
        tmp_path, monkeypatch, canonical_producer, change):
    _, _, _, _, folder, _, manifest, _ = make_legacy(tmp_path, monkeypatch, canonical_producer)
    if change == 'mixed-shapes':
        manifest['sidecars'] = []
    elif change == 'provider-permission':
        manifest['provider_restore_mode'] = 'REMOTE_ALLOWED'
    elif change == 'schema-unknown':
        manifest['schema_version'] = 6
        manifest['schema_fingerprint'] = '0' * 64
    else:
        manifest['generated_audio_included'] = 1
    checksum = resign(folder, manifest)
    with pytest.raises(RestoreError):
        backup.restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


def test_preserved_authority_flags_cannot_be_reenabled_by_resigning_both_manifests(
        tmp_path, monkeypatch, canonical_producer):
    _, _, _, _, source, checksum, _, _ = make_legacy(tmp_path, monkeypatch, canonical_producer)
    app = LibraryApp(backup.restore_backup(source, tmp_path / 'first', checksum))
    folder = tmp_path / 'second'
    backup.create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    receipt_path = folder / preserved.DIRECTORY / preserved.REVIEW
    receipt = json.loads(receipt_path.read_bytes())
    receipt['provider_authorized'] = True
    write_json(receipt_path, receipt)
    raw = receipt_path.read_bytes()
    row = next(row for row in manifest['preserved_state']['files'] if row['filename'] == preserved.REVIEW)
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = resign(folder, manifest)
    with pytest.raises(ValueError, match='inert|preservation'):
        backup.restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


def test_preservation_bytes_and_files_share_the_existing_global_library_ceiling(tmp_path):
    app = LibraryApp(tmp_path / 'live')
    write_json(app.root / 'state/speech/draft.json', {'original': 'Public source.', 'edit': 'User text.'})
    ResourceBroker.for_app(app.root).configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    folder = tmp_path / 'reference'
    backup.create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    inventory = backup.declared_backup_files(manifest)
    assert any(name.startswith(preserved.DIRECTORY + '/') for name in inventory)
    for name, arguments in [('files', {'max_files': len(inventory)}),
                            ('bytes', {'max_bytes': sum(row['size_bytes'] for row in inventory.values()) - 1})]:
        target = tmp_path / name
        with pytest.raises(ValueError, match='budget|limit'):
            backup.create_backup(app, target, **arguments)
        assert not target.exists()


def test_oversized_canonical_inventory_is_rejected_before_any_writable_storage(
        tmp_path, monkeypatch, canonical_producer):
    _, _, _, _, folder, _, manifest, _ = make_legacy(tmp_path, monkeypatch, canonical_producer)
    manifest['state'][0]['size_bytes'] = backup.MAX_BACKUP_BYTES
    checksum = resign(folder, manifest)
    monkeypatch.setattr(backup, 'Storage', lambda *a, **k: pytest.fail('No writable storage before aggregate validation.'))
    with pytest.raises(ValueError, match='limit|budget'):
        backup.restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


def test_mixed_current_translation_and_canonical_layout_refuses_implicit_ledger_merge(tmp_path):
    app = LibraryApp(tmp_path / 'live')
    write_json(app.root / 'state/speech/old.json', {'old': 'Reading state'})
    current = app.root / 'state/translation' / ('d' * 32 + '.json')
    write_json(current, {'schema': 'carr-translation-job/1'})
    before = file_bytes(app.root / 'state')
    with pytest.raises(RestoreError, match='independent cost ledgers'):
        backup.create_backup(app, tmp_path / 'rejected')
    assert file_bytes(app.root / 'state') == before
    assert not (tmp_path / 'rejected').exists()


def test_direntry_link_count_difference_does_not_change_path_identity(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    write_json(root / 'state/speech/session.json', {'original': 'Public synthetic text'})
    real_scandir = os.scandir

    class Entry:
        def __init__(self, item):
            self.item, self.name, self.path = item, item.name, item.path
        def is_dir(self, **options):
            return self.item.is_dir(**options)
        def stat(self, **options):
            actual = self.item.stat(**options)
            fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            return SimpleNamespace(**{key: getattr(actual, key) for key in fields}, st_nlink=0,
                                   st_file_attributes=getattr(actual, 'st_file_attributes', 0))

    class Scan:
        def __init__(self, path):
            self.opened = real_scandir(path)
        def __enter__(self):
            return (Entry(item) for item in self.opened)
        def __exit__(self, *args):
            self.opened.close()

    monkeypatch.setattr(os, 'scandir', Scan)
    target = tmp_path / 'preserved'
    component = preserved.snapshot(root, target, preserve_live=True)
    assert (target / 'files/speech/session.json').read_bytes() == (root / 'state/speech/session.json').read_bytes()
    assert component['status'] == preserved.STATUS


def test_same_metadata_source_change_fails_content_reattestation(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'live')
    source = app.root / 'state/speech/a.json'
    write_json(source, {'value': 'old'})
    original = source.read_bytes()
    replacement = original.replace(b'old', b'new')
    stamp = source.stat()
    normal_identity = preserved._identity
    monkeypatch.setattr(preserved, '_identity', lambda info:
                        tuple(value for index, value in enumerate(normal_identity(info)) if index != 6))
    transfer = preserved._transfer
    changed = []

    def mutate(path, destination, **options):
        result = transfer(path, destination, **options)
        if Path(path) == source and destination is not None and not changed:
            source.write_bytes(replacement)
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            changed.append(True)
        return result

    monkeypatch.setattr(preserved, '_transfer', mutate)
    with pytest.raises(ValueError, match='checksum|changed'):
        backup.create_backup(app, tmp_path / 'rejected')
    assert changed and source.read_bytes() == replacement
    assert not (tmp_path / 'rejected').exists()


def test_cancel_before_publication_and_existing_target_never_destroy_data(
        tmp_path, monkeypatch, canonical_producer):
    _, _, _, _, folder, checksum, _, _ = make_legacy(tmp_path, monkeypatch, canonical_producer)
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'keep.txt').write_bytes(b'User-owned destination')
    with pytest.raises(FileExistsError):
        backup.restore_backup(folder, target, checksum)
    assert (target / 'keep.txt').read_bytes() == b'User-owned destination'
    with pytest.raises(InterruptedError):
        backup.restore_backup(folder, tmp_path / 'cancelled', checksum, cancelled=lambda: True)
    assert not (tmp_path / 'cancelled').exists()


@pytest.mark.parametrize('kind', ['file-symlink', 'directory-symlink', 'hardlink', 'ancestor-symlink'])
def test_preservation_never_follows_aliases_outside_the_trusted_archive(
        tmp_path, monkeypatch, canonical_producer, kind):
    _, _, _, _, folder, checksum, _, _ = make_legacy(tmp_path, monkeypatch, canonical_producer)
    path = folder / 'state/audio/engines.json'
    outside = tmp_path / 'outside'
    outside.mkdir()
    raw = path.read_bytes()
    (outside / 'engines.json').write_bytes(raw)
    if kind == 'file-symlink':
        path.unlink()
        path.symlink_to(outside / 'engines.json')
    elif kind == 'directory-symlink':
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
    elif kind == 'hardlink':
        os.link(path, outside / 'linked.json')
    else:
        alias = tmp_path / 'alias'
        alias.symlink_to(folder.parent, target_is_directory=True)
        folder = alias / folder.name
    with pytest.raises(ValueError, match='link|regular'):
        backup.restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('pair', [('A/x.json', 'a/y.json'), ('é/x.json', 'e\u0301/y.json'),
                                 ('folder', 'folder/file.json')])
def test_preservation_rejects_nfc_case_and_file_directory_aliases(pair):
    entries = [{'filename': name, 'size_bytes': 0, 'sha256': sha256(b'').hexdigest()} for name in pair]
    with pytest.raises(ValueError, match='Ambiguous|directory|Duplicate'):
        preserved.validate_canonical_state(entries)


def test_schema_number_and_signed_fingerprint_do_not_replace_actual_sqlite_identity(
        tmp_path, monkeypatch, canonical_producer):
    _, _, _, _, folder, _, manifest, _ = make_legacy(tmp_path, monkeypatch, canonical_producer)
    other = 5 if canonical_producer.schema_version == 4 else 4
    manifest['schema_version'] = other
    manifest['schema_fingerprint'] = expected_schema_fingerprint(other)
    checksum = resign(folder, manifest)
    monkeypatch.setattr(backup, 'Storage', lambda *a, **k: pytest.fail('Wrong SQLite layout reached writable Storage.'))
    with pytest.raises(RestoreError, match='schema'):
        backup.restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
