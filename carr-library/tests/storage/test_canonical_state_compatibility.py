"""Real canonical producer -> reviewed root preservation boundary.

Producer fixtures are exact public source blobs from canonical184dd and 1eff.
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
    'canonical_v5_application.py': '747c692692f191161b1ca6b5c212abdf0e17345a',
    'canonical_v5_worker.py': 'e219ff740c54b6280fc0dc6a09e53da1671a4dc7',
    'canonical_v5_pending_backup.py': 'c039d5d7000ac5a4adafdc631820933c420dba84',
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


def make_canonical_committed_candidate(tmp_path, monkeypatch, boundary):
    """Actual canonical supervisor/child; interrupt only after a durable event."""
    for filename in ('canonical_v5_application.py', 'canonical_v5_worker.py',
                     'canonical_v5_pending_backup.py'):
        raw = (FIXTURES / filename).read_bytes().replace(b'\r\n', b'\n')
        assert sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest() == PRODUCER_BLOBS[filename]
    root = tmp_path / 'canonical-source-library'
    source = tmp_path / 'canonical-original.txt'
    source.write_bytes('Railway source.\r\nТочний оригінал.\fSecond segment.'.encode('utf-8'))
    observed = tmp_path / 'producer-observation.json'
    committed = tmp_path / 'child-committed'
    # The fixture sources keep their exact bytes. Only the trusted test child
    # bootstrap selects those sources and adds an after-publication barrier.
    roots = [str(Path(application.__file__).resolve().parents[1]),
             str(Path(application.__file__).resolve().parents[2])]
    loader = f"""
import importlib.util, json, os, sys
from pathlib import Path
sys.path[:0] = {roots!r}
import carr_library, carr_library.ingest
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
worker = load('carr_library.ingest.worker', {str(FIXTURES / 'canonical_v5_worker.py')!r})
carr_library.ingest.worker = worker
application = load('carr_library.application', {str(FIXTURES / 'canonical_v5_application.py')!r})
carr_library.application = application
"""
    child = loader
    if boundary == 'after-publication':
        child += f"""
publish = application._CatalogPublisher.publish_revision
def stop_after_commit(self, *args, **kwargs):
    result = publish(self, *args, **kwargs)
    Path({str(committed)!r}).write_text('durable publication observed', encoding='utf-8')
    os._exit(7)
application._CatalogPublisher.publish_revision = stop_after_commit
"""
    child += "\nraise SystemExit(worker.main())\n"
    script = loader + f"""
from carr_library.extract.page_ocr import Config
app = application.LibraryApp({str(root)!r})
workspace = app.create_workspace('Actual canonical committed candidate')
popen = worker.subprocess.Popen
def launch(command, **options):
    command = list(command)
    if '-c' in command:
        index = command.index('-c') + 1
        if (index < len(command)
                and "runpy.run_module('carr_library.ingest.worker'" in command[index]):
            command[index] = {child!r}
    return popen(command, **options)
worker.subprocess.Popen = launch
invoke = worker._invoke
def abandon_parent(*args, **kwargs):
    result = invoke(*args, **kwargs)
    Path({str(observed)!r}).write_text(json.dumps(dict(workspace=workspace, result=result)),
                                         encoding='utf-8')
    # Exit after the actual worker has stopped, before the supervisor can
    # reconcile or write a final request status. No subprocess is left alive.
    os._exit(9)
worker._invoke = abandon_parent
worker.run_request(app.root, workspace, {str(source)!r},
    dict(title='Actual canonical original', authors=['Synthetic author'], language='uk',
         edition='First', volume=None, collections=[], work_id='canonical-work',
         edition_id='canonical-edition'), Config(), timeout=12.0)
raise AssertionError('The test supervisor should have stopped before finalization.')
"""
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', script],
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 9, result.stdout + result.stderr
    observation = json.loads(observed.read_bytes())
    expected = 'failed' if boundary == 'after-publication' else 'complete'
    assert observation['result']['state'] == expected, observation
    assert committed.exists() is (boundary == 'after-publication')
    request_path, = (root / 'import-requests').glob('*.json')
    record = json.loads(request_path.read_bytes())
    assert record['state'] == 'running' and 'result' not in record
    assert record['publication_candidate']['attempt'] == record['attempt']
    with sqlite3.connect(root / 'imports.sqlite') as db:
        assert db.execute('SELECT state FROM job').fetchone()[0] == (
            'pending' if boundary == 'after-publication' else 'complete')
    app = LibraryApp(root)
    with app.read(observation['workspace']) as db:
        receipt = tuple(db.execute('SELECT revision,request_id,attempt,sha256 '
                                   'FROM import_publication').fetchone())
        assert receipt == (record['publication_candidate']['revision'], record['request_id'],
                           record['attempt'], record['publication_candidate']['sha256'])
        from carr_library.ingest import worker as reviewed_worker
        # Same actual publication, different historical digest recipe. Pending
        # restore discards this process hint; it does not reinterpret the hash.
        state = reviewed_worker._publication_state(db, record, receipt[0])
        assert state['sha256'] != receipt[3]
        assert db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 1
    folder = tmp_path / 'canonical-candidate-backup'
    with monkeypatch.context() as actual_producer:
        load_producer('carr_library.state_backup', 'canonical_v5_state_backup.py', actual_producer)
        load_producer('carr_library.pending_backup', 'canonical_v5_pending_backup.py', actual_producer)
        producer = load_producer('carr_library._canonical_candidate_backup',
                                 'canonical_v5_library_backup.py', actual_producer)
        checksum = producer.create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 4 and manifest['schema_version'] == 5
    assert manifest['pending']['data_dir'] == str(root)
    return SimpleNamespace(root=root, source=source, folder=folder, checksum=checksum,
                           manifest=manifest, record=record, receipt=receipt,
                           workspace=observation['workspace'])


@pytest.mark.parametrize('boundary', ['after-publication', 'after-worker-return'])
def test_actual_canonical_lost_parent_candidate_restores_and_explicitly_resumes(
        tmp_path, monkeypatch, boundary):
    from carr_library.ingest import worker

    data = make_canonical_committed_candidate(tmp_path, monkeypatch, boundary)
    original = data.source.read_bytes()
    before = file_bytes(data.folder)
    data.source.unlink()
    shutil.rmtree(data.root)

    def forbidden(*args, **kwargs):
        pytest.fail('Restore or an already cancelled attempt must not launch a worker.')
    with monkeypatch.context() as passive:
        passive.setattr(subprocess, 'Popen', forbidden)
        target = backup.restore_backup(data.folder, tmp_path / 'restored', data.checksum)
        restored = LibraryApp(target)
        record = worker.load_request(target, data.record['request_id'])
        assert record['state'] == 'running' and 'result' not in record
        assert 'attempt' not in record and 'publication_candidate' not in record
        assert record['data_dir'] == str(target)
        assert Path(record['source']).parent == target / 'originals'
        assert Path(record['source']).read_bytes() == original
        cancelled = restored.resume_import(data.workspace, record['request_id'],
                                          cancelled=lambda: True)
        assert cancelled['state'] == 'cancelled'
        assert not cancelled.get('publication_recovered')
    with restored.read(data.workspace) as db:
        assert tuple(db.execute('SELECT revision,request_id,attempt,sha256 '
                                'FROM import_publication').fetchone()) == data.receipt
    result = restored.resume_import(data.workspace, record['request_id'], timeout=15)
    assert result['state'] == 'complete' and result['id'] == data.receipt[0], result
    assert result['isolated'] is True
    after = worker.load_request(target, record['request_id'])
    assert after['attempt'] != data.record['attempt']
    assert after['publication_candidate'] is None
    with restored.read(data.workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 1
        assert tuple(db.execute('SELECT revision,request_id,attempt,sha256 '
                                'FROM import_publication').fetchone()) == data.receipt
    assert restored.search(data.workspace, 'Точний')[0].quote == 'Точний'
    assert restored.assets.resolve(record['asset_sha256']).read_bytes() == original
    assert file_bytes(data.folder) == before


@pytest.mark.parametrize('tampering', ['foreign-source', 'foreign-attempt', 'foreign-job'])
def test_canonical_committed_candidate_keeps_existing_pending_identity_checks(
        tmp_path, monkeypatch, tampering):
    data = make_canonical_committed_candidate(tmp_path, monkeypatch, 'after-publication')
    name = 'import-requests/' + data.record['request_id'] + '.json'
    record = json.loads((data.folder / 'pending' / name).read_bytes())
    candidate = record['publication_candidate']
    if tampering == 'foreign-source':
        candidate['manifest']['sha256'] = '0' * 64
    elif tampering == 'foreign-attempt':
        candidate['attempt'] = 'f' * 32
    else:
        candidate['revision'] = 'e' * 64
    write_json(data.folder / 'pending' / name, record)
    raw = (data.folder / 'pending' / name).read_bytes()
    entry = next(item for item in data.manifest['pending']['files'] if item['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = resign(data.folder, data.manifest)
    with pytest.raises(ValueError, match='candidate|foreign'):
        backup.restore_backup(data.folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


def test_atomic_json_uses_actual_path_and_handle_metadata_through_roundtrip(tmp_path):
    from carr_library.durable_state import atomic_bytes

    root = tmp_path / 'live'
    source = root / 'state/speech/session.json'
    source.parent.mkdir(parents=True)
    raw = b'{"source_text":"Public synthetic narration"}\n'
    atomic_bytes(source, raw)
    path_info = source.lstat()
    with source.open('rb') as stream:
        handle_info = os.fstat(stream.fileno())
        assert preserved._file_binding(path_info) == preserved._file_binding(handle_info), (
            preserved._identity(path_info), preserved._identity(handle_info))
    component = preserved.snapshot(root, tmp_path / 'component', preserve_live=True)
    assert (tmp_path / 'component/files/speech/session.json').read_bytes() == raw
    restored = preserved.restore(tmp_path / 'component', tmp_path / 'copy', component)
    assert restored == component
    assert (tmp_path / 'copy/files/speech/session.json').read_bytes() == raw
    assert source.read_bytes() == raw


@pytest.mark.parametrize('operation', ['transfer', 'review'])
@pytest.mark.parametrize('condition', ['api-clock-difference', 'handle-changed', 'identity-changed'])
def test_stat_apis_bind_identity_but_compare_change_stamps_within_each_api(
        tmp_path, monkeypatch, operation, condition):
    root = tmp_path / 'live'
    source = root / 'state/speech/session.json'
    write_json(source, {'source_text': 'Public synthetic reading copy.'})
    component = tmp_path / 'component'
    preserved.snapshot(root, component, preserve_live=True)
    stat = preserved.os.fstat
    calls = []

    def handle_stat(descriptor):
        value = stat(descriptor)
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size',
                  'st_mtime_ns', 'st_ctime_ns', 'st_file_attributes')
        result = SimpleNamespace(**{key: getattr(value, key, 0) for key in fields})
        # Model a stable API-specific clock representation. This must neither
        # hide a real within-handle change nor allow a different inode to pass.
        result.st_mtime_ns += 1000
        result.st_ctime_ns += 2000
        calls.append(descriptor)
        if condition == 'handle-changed' and len(calls) > 1:
            result.st_mtime_ns += 1
        elif condition == 'identity-changed':
            result.st_ino += 1
        return result

    monkeypatch.setattr(preserved.os, 'fstat', handle_stat)
    if operation == 'transfer':
        def invoke():
            return preserved._transfer(source, tmp_path / 'copied.json')
    else:
        def invoke():
            return preserved._review_bytes(component)
    if condition == 'api-clock-difference':
        invoke()
        assert len(calls) == 2
        if operation == 'transfer':
            assert (tmp_path / 'copied.json').read_bytes() == source.read_bytes()
    else:
        with pytest.raises(ValueError, match='changed'):
            invoke()


@pytest.mark.parametrize('invalid', [1, False, 'true'])
def test_resigned_live_origin_requires_exact_boolean_generated_inclusion(
        tmp_path, invalid):
    app = LibraryApp(tmp_path / 'live')
    write_json(app.root / 'state/speech/session.json', {'text': 'Public authored state.'})
    folder = tmp_path / 'typed-backup'
    backup.create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    path = folder / preserved.DIRECTORY / preserved.REVIEW
    receipt = json.loads(path.read_bytes())
    assert receipt['origin']['kind'] == 'recognized-canonical-live-layout'
    assert receipt['origin']['generated_audio_included'] is True
    receipt['origin']['generated_audio_included'] = invalid
    write_json(path, receipt)
    raw = path.read_bytes()
    entry = next(item for item in manifest['preserved_state']['files']
                 if item['filename'] == preserved.REVIEW)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = resign(folder, manifest)
    with pytest.raises(ValueError, match='live-state'):
        backup.restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
