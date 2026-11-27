"""Actual canonical snapshots, retained originals and typed recovery through deltas."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_incremental as incremental
from carr_library.backup_incremental import (BackupReference, create_incremental_backup,
    verify_incremental_backup, restore_incremental_backup)
from carr_library.library_backup import create_backup, verify_backup, restore_backup
from carr_library.reader.reading_state import ReadingLocation, ReadingState
from carr_library.keybindings import encode_preferences
from carr_library.storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from carr_library.storage.schema_identity import expected_schema_fingerprint
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


def bytes_in(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('utf-8')



def actual_location(app, workspace, edition='original'):
    with app.read(workspace) as db:
        row = db.execute('SELECT p.revision,e.asset,p.ordinal,p.physical,p.digest '
            'FROM catalog_edition e JOIN active_page p ON p.workspace=e.workspace '
            'AND p.edition=e.id AND p.asset=e.asset '
            'WHERE e.workspace=? AND e.id=? ORDER BY p.ordinal LIMIT 1', (workspace, edition)).fetchone()
    assert row is not None
    revision, asset, ordinal, physical, digest = row
    return ReadingLocation(workspace, edition, revision,
        'physical_page' if physical is not None else 'logical_segment',
        physical if physical is not None else ordinal, digest, asset)


def state_entry(manifest, name):
    if type(manifest.get('sidecars')) is list:
        return next(row for row in manifest['sidecars'] if row['filename'] == name)
    if name.startswith('state/'):
        return next(row for row in manifest['state'] if row['filename'] == name[len('state/'):])
    return next(row for row in manifest['sidecars']['files']
                if 'sidecars/' + row['path'] == name)


def reseal_layer(layer, descriptor, snapshot):
    raw = encoded(snapshot)
    (layer / 'snapshot.json').write_bytes(raw)
    descriptor['snapshot_sha256'] = sha256(raw).hexdigest()
    raw = encoded(descriptor)
    (layer / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()

@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Incremental recovery')
    app.add_text(workspace, 'Exact retained original',
                 'Незмінний оригінал. Source evidence.\n' * 12000, edition_id='original')
    document = ReadingDocument('Original reading copy. 😀')
    drafts = ReaderDraftStore(app.root)
    drafts.save(ReaderResume(document, 'missing:exact-voice', 1, 67, 0, 0))
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    base = tmp_path / 'full'
    digest = create_backup(app, base)
    return app, workspace, document, drafts, BackupReference(base, digest)


def changed(source, tmp_path, *, name='delta'):
    app, workspace, document, drafts, base = source
    document.set_text('Revised reading copy. 😀')
    location = actual_location(app, workspace)
    reading = ReadingState(app.root)
    reading.remember(location)
    reading.add_bookmark(location, 'Reviewed location')
    drafts.save(ReaderResume(document, 'missing:exact-voice', 1, 67, 8, 2.5))
    receipt = create_incremental_backup(app, tmp_path / name, base=base)
    return receipt


def test_changed_only_storage_restores_after_actual_source_deletion_without_execution(
        source, tmp_path, monkeypatch):
    app, workspace, document, drafts, base = source
    base_bytes = bytes_in(base.path)
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Passive backup started a process.'))
    receipt = changed(source, tmp_path)
    layer = json.loads((receipt.path / 'manifest.json').read_bytes())
    assert all(not name.startswith('originals/') for name in layer['stored'])
    assert drafts.path.relative_to(app.root).as_posix() in layer['stored']
    assert receipt.stored_files < receipt.total_files
    assert receipt.stored_bytes < receipt.logical_bytes
    assert bytes_in(base.path) == base_bytes
    assert all(path.stat().st_nlink == 1 for path in base.path.rglob('*') if path.is_file())
    assert all(path.stat().st_nlink == 1 for path in receipt.path.rglob('*') if path.is_file())
    proof = verify_incremental_backup(receipt.path, receipt.manifest_sha256, bases=(base,))
    assert proof['chain'] == [receipt.manifest_sha256, base.sha256]
    assert proof['snapshot_sha256'] == receipt.snapshot_sha256
    expected_draft = document.to_dict()
    expected_bookmarks = ReadingState(app.root).bookmarks(workspace, 'original')
    expected_resume = ReadingState(app.root).resume(workspace, 'original')
    shutil.rmtree(app.root)
    destination = restore_incremental_backup(receipt.path, tmp_path / 'restored',
                                             receipt.manifest_sha256, bases=(base,))
    restored = LibraryApp(destination)
    assert restored.edition_metadata(workspace, 'original').title == 'Exact retained original'
    assert {path.name: path.read_bytes() for path in restored.assets_dir.iterdir()} == {
        name.split('/', 1)[1]: raw for name, raw in base_bytes.items() if name.startswith('originals/')}
    resume = ReaderDraftStore(destination).load()
    assert resume.document.to_dict() == expected_draft
    assert (resume.voice_id, resume.cursor, resume.audio_seconds) == ('missing:exact-voice', 8, 2.5)
    assert ReadingState(destination).bookmarks(workspace, 'original') == expected_bookmarks
    assert ReadingState(destination).resume(workspace, 'original') == expected_resume
    assert resume.document.undo() and resume.document.text == document.source_text
    assert bytes_in(base.path) == base_bytes


def test_multiple_layers_keep_explicit_parent_identity_and_latest_database(source, tmp_path):
    app, workspace, document, drafts, base = source
    first = changed(source, tmp_path)
    first_ref = BackupReference(first.path, first.manifest_sha256)
    app.add_text(workspace, 'New exact original', 'A later original.', edition_id='later')
    second = create_incremental_backup(app, tmp_path / 'second', base=first_ref, ancestors=(base,))
    before = bytes_in(base.path), bytes_in(first.path)
    with pytest.raises(ValueError, match='required base'):
        restore_incremental_backup(second.path, tmp_path / 'missing-base', second.manifest_sha256,
                                   bases=(base,))
    assert not (tmp_path / 'missing-base').exists()
    root = restore_incremental_backup(second.path, tmp_path / 'restored', second.manifest_sha256,
                                     bases=(base, first_ref))
    restored = LibraryApp(root)
    assert restored.edition_metadata(workspace, 'later').title == 'New exact original'
    assert restored.edition_metadata(workspace, 'original').title == 'Exact retained original'
    assert (bytes_in(base.path), bytes_in(first.path)) == before


@pytest.mark.parametrize('damage', ['stored', 'inherited', 'snapshot', 'manifest', 'extra', 'missing', 'hardlink'])
def test_corrupt_required_bytes_never_publish_a_restored_library(source, tmp_path, damage):
    app, _, _, _, base = source
    receipt = changed(source, tmp_path)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    if damage == 'stored':
        path = receipt.path / 'files' / descriptor['stored'][0]
        path.write_bytes(b'changed')
    elif damage == 'inherited':
        path = next((base.path / 'originals').iterdir())
        path.write_bytes(b'changed')
    elif damage == 'snapshot':
        (receipt.path / 'snapshot.json').write_bytes(b'{}')
    elif damage == 'manifest':
        (receipt.path / 'manifest.json').write_bytes(b'{}')
    elif damage == 'extra':
        (receipt.path / 'unexpected').write_bytes(b'unreviewed')
    elif damage == 'missing':
        (receipt.path / 'files' / descriptor['stored'][0]).unlink()
    else:
        os.link(receipt.path / 'snapshot.json', tmp_path / 'shared-snapshot')
    before = bytes_in(base.path), bytes_in(receipt.path)
    destination = tmp_path / 'must-not-publish'
    with pytest.raises((ValueError, OSError)):
        restore_incremental_backup(receipt.path, destination, receipt.manifest_sha256, bases=(base,))
    assert not destination.exists()
    assert (bytes_in(base.path), bytes_in(receipt.path)) == before


def test_resigned_invalid_typed_reader_data_is_still_rejected(source, tmp_path):
    app, _, _, drafts, base = source
    receipt = changed(source, tmp_path)
    name = drafts.path.relative_to(app.root).as_posix()
    path = receipt.path / 'files' / name
    document = json.loads(path.read_bytes())
    document['execute'] = 'never accepted'
    raw = encoded(document)
    path.write_bytes(raw)
    snapshot = json.loads((receipt.path / 'snapshot.json').read_bytes())
    row = state_entry(snapshot, name)
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    snapshot_raw = encoded(snapshot)
    (receipt.path / 'snapshot.json').write_bytes(snapshot_raw)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    descriptor['snapshot_sha256'] = sha256(snapshot_raw).hexdigest()
    manifest_raw = encoded(descriptor)
    (receipt.path / 'manifest.json').write_bytes(manifest_raw)
    with pytest.raises(ValueError):
        restore_incremental_backup(receipt.path, tmp_path / 'rejected',
            sha256(manifest_raw).hexdigest(), bases=(base,))
    assert not (tmp_path / 'rejected').exists()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'files/../../escape', 'C:/outside'])
def test_resigned_traversal_cannot_read_or_publish_outside_the_layer(source, tmp_path, name):
    _, _, _, _, base = source
    receipt = changed(source, tmp_path)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    descriptor['stored'] = [name]
    raw = encoded(descriptor)
    (receipt.path / 'manifest.json').write_bytes(raw)
    sentinel = tmp_path / 'outside'
    sentinel.write_bytes(b'Keep outside bytes')
    with pytest.raises(ValueError):
        restore_incremental_backup(receipt.path, tmp_path / 'rejected', sha256(raw).hexdigest(), bases=(base,))
    assert sentinel.read_bytes() == b'Keep outside bytes'
    assert not (tmp_path / 'rejected').exists()


def test_chain_bound_prevents_creation_of_an_unrestorable_new_layer(source, tmp_path, monkeypatch):
    app, _, _, _, base = source
    monkeypatch.setattr(incremental, 'MAX_CHAIN', 1)
    first = changed(source, tmp_path)
    selected = BackupReference(first.path, first.manifest_sha256)
    with pytest.raises(ValueError, match='new full backup'):
        create_incremental_backup(app, tmp_path / 'too-deep', base=selected, ancestors=(base,))
    assert not (tmp_path / 'too-deep').exists()
    assert verify_incremental_backup(first.path, first.manifest_sha256, bases=(base,))['chain'] == [
        first.manifest_sha256, base.sha256]


def test_selected_checksum_and_ancestor_identity_are_required(source, tmp_path):
    app, _, _, _, base = source
    with pytest.raises(ValueError, match='digest mismatch'):
        create_incremental_backup(app, tmp_path / 'bad-base',
                                  base=BackupReference(base.path, '0' * 64))
    with pytest.raises(ValueError, match='only once'):
        create_incremental_backup(app, tmp_path / 'duplicate', base=base, ancestors=(base, base))
    assert not (tmp_path / 'bad-base').exists() and not (tmp_path / 'duplicate').exists()


@pytest.mark.parametrize('phase', ['before', 'copy', 'space'])
def test_cancel_or_insufficient_space_keeps_bases_and_publishes_nothing(source, tmp_path, monkeypatch, phase):
    app, _, document, drafts, base = source
    document.set_text('A changed draft.')
    drafts.save(ReaderResume(document, '', 0, 80, 0, 0))
    before = bytes_in(base.path)
    stopped = phase == 'before'
    original_copy = incremental._copy
    def copy(*args, **kwargs):
        nonlocal stopped
        result = original_copy(*args, **kwargs)
        stopped = True
        return result
    if phase == 'copy':
        monkeypatch.setattr(incremental, '_copy', copy)
    elif phase == 'space':
        monkeypatch.setattr(incremental.shutil, 'disk_usage', lambda _path: SimpleNamespace(free=0))
    with pytest.raises((ValueError, InterruptedError, OSError)):
        create_incremental_backup(app, tmp_path / 'cancelled', base=base, cancelled=lambda: stopped)
    assert not (tmp_path / 'cancelled').exists()
    assert bytes_in(base.path) == before
    assert not list(tmp_path.glob('.carr-incremental-*'))


def test_competing_destination_is_never_overwritten(source, tmp_path, monkeypatch):
    app, _, _, _, base = source
    destination = tmp_path / 'destination'
    publish = incremental._publish
    def competing(stage, selected, stored, **options):
        selected.mkdir()
        (selected / 'keep').write_bytes(b'Competitor owns this destination.')
        return publish(stage, selected, stored, **options)
    monkeypatch.setattr(incremental, '_publish', competing)
    with pytest.raises(FileExistsError):
        create_incremental_backup(app, destination, base=base)
    assert bytes_in(destination) == {'keep': b'Competitor owns this destination.'}


def test_cli_create_verify_restore_reuses_the_same_canonical_services(source, tmp_path, capsys):
    from carr_library.backup_incremental_cli import main
    app, workspace, _, _, base = source
    layer = tmp_path / 'cli-layer'
    assert main(['create', '--data-dir', str(app.root), '--output', str(layer),
                 '--base', str(base.path), base.sha256]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['path'] == str(layer) and receipt['base_required'] is True
    common = ['--source', str(layer), '--sha256', receipt['manifest_sha256'],
              '--base', str(base.path), base.sha256]
    assert main(['verify', *common]) == 0
    assert json.loads(capsys.readouterr().out)['chain'] == [receipt['manifest_sha256'], base.sha256]
    destination = tmp_path / 'cli-restored'
    assert main(['restore', *common, '--output', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)['restored_root'] == str(destination)
    assert LibraryApp(destination).edition_metadata(workspace, 'original').title == 'Exact retained original'
    assert main(['restore', *common, '--output', str(destination)]) == 1
    assert 'Incremental backup:' in capsys.readouterr().err



def test_base_corruption_during_current_snapshot_work_is_detected_before_publication(
        source, tmp_path, monkeypatch):
    app, _, document, drafts, base = source
    document.set_text('Changed draft to ensure an actual delta copy.')
    drafts.save(ReaderResume(document, '', 0, 80, 0, 0))
    original_copy = incremental._copy
    damaged = False
    def copy(*args, **kwargs):
        nonlocal damaged
        result = original_copy(*args, **kwargs)
        if not damaged:
            damaged = True
            original = next((base.path / 'originals').iterdir())
            original.write_bytes(b'External modification during backup.')
        return result
    monkeypatch.setattr(incremental, '_copy', copy)
    with pytest.raises(ValueError):
        create_incremental_backup(app, tmp_path / 'rejected', base=base)
    assert damaged and not (tmp_path / 'rejected').exists()
    assert not list(tmp_path.glob('.carr-incremental-*'))


@pytest.mark.parametrize('operation', ['create', 'recheck'])
def test_full_base_budget_is_checked_before_any_payload_verification(source, tmp_path, monkeypatch, operation):
    app, _, _, _, base = source
    # This is an actual complete backup with its independently retained digest.
    # A one-byte budget cannot admit its real database and retained originals.
    before = bytes_in(base.path)
    def forbidden(*args, **kwargs):
        pytest.fail('The over-budget backup reached payload verification.')
    monkeypatch.setattr(incremental.full, 'verify_backup', forbidden)
    with pytest.raises(ValueError, match='budget'):
        if operation == 'create':
            create_incremental_backup(app, tmp_path / 'over-budget', base=base, max_bytes=1)
        else:
            incremental._recheck_chain((base,), lambda: False, 1)
    assert not (tmp_path / 'over-budget').exists()
    assert bytes_in(base.path) == before
    assert not list(tmp_path.glob('.carr-incremental-*'))


def _seal_full(root, manifest):
    raw = encoded(manifest)
    (root / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _as_flat(root, manifest):
    """Convert only the already verified native inventory into the released flat envelope."""
    entries = [{**row, 'filename': 'state/' + row['filename']}
               for row in manifest.get('state', ())]
    sidecars = manifest.get('sidecars')
    if sidecars is not None:
        for row in sidecars['files']:
            source = root / 'sidecars' / row['path']
            target = root / row['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            entries.append({'filename': row['path'], 'size_bytes': row['size_bytes'],
                            'sha256': row['sha256']})
        shutil.rmtree(root / 'sidecars')
    value = {key: manifest[key] for key in
             ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
    value.update(version=4, sidecars=sorted(entries, key=lambda row: row['filename']),
                 generated_audio_included=manifest.get('generated_audio_included', False))
    if 'pending' in manifest:
        value['pending'] = manifest['pending']
    return value, _seal_full(root, value)


@pytest.mark.parametrize('schema', [3, 4, 5, 6], ids=['schema3', 'schema4', 'schema5', 'schema6'])
@pytest.mark.parametrize('version', [1, 2], ids=['manifest1', 'manifest2'])
def test_released_full_databases_remain_exact_readable_bases(tmp_path, schema, version):
    full = tmp_path / 'released-full'
    full.mkdir()
    (full / 'originals').mkdir()
    database = full / 'library.sqlite'
    with closing(Storage(database, schema_version=schema)) as storage:
        storage.check()
    raw = database.read_bytes()
    manifest = {'version': version, 'kind': 'carr-library', 'schema_version': schema,
        'database': {'filename': 'library.sqlite', 'size_bytes': len(raw),
                     'sha256': sha256(raw).hexdigest()}, 'assets': []}
    if version >= 2:
        manifest['schema_fingerprint'] = expected_schema_fingerprint(schema)
    checksum = _seal_full(full, manifest)
    before = bytes_in(full)
    assert verify_backup(full, checksum) == manifest
    restored = restore_backup(full, tmp_path / 'legacy-restored', checksum)
    with closing(Storage(restored / 'library.sqlite')) as storage:
        assert storage.schema_version == schema
    # Opening through the normal current application performs its checked migration.
    app = LibraryApp(restored)
    with closing(Storage(app.database)) as storage:
        assert storage.schema_version == CURRENT_SCHEMA_VERSION
    workspace = app.create_workspace('Migrated actual release')
    app.add_text(workspace, 'Later original', 'New source after migration.', edition_id='later')
    receipt = create_incremental_backup(app, tmp_path / 'delta',
                                       base=BackupReference(full, checksum))
    root = restore_incremental_backup(receipt.path, tmp_path / 'latest',
        receipt.manifest_sha256, bases=(BackupReference(full, checksum),))
    assert LibraryApp(root).edition_metadata(workspace, 'later').title == 'Later original'
    assert bytes_in(full) == before


@pytest.mark.parametrize('shape', ['state-v3', 'sidecars-v3', 'flat-v4'])
def test_released_state_envelopes_remain_valid_incremental_bases(source, tmp_path, shape):
    app, workspace, document, drafts, base = source
    manifest = json.loads((base.path / 'manifest.json').read_bytes())
    assert manifest['version'] == 5
    if shape == 'flat-v4':
        _, checksum = _as_flat(base.path, manifest)
    else:
        # A released v3 envelope predates the separate pending component.
        # The current source fixture keeps its requests; only this deliberate
        # historical snapshot projection omits that newer component.
        if 'pending' in manifest:
            manifest.pop('pending')
            shutil.rmtree(base.path / 'pending')
        if shape == 'state-v3':
            manifest.pop('sidecars')
            shutil.rmtree(base.path / 'sidecars')
        else:
            manifest.pop('state')
            manifest.pop('generated_audio_included')
            shutil.rmtree(base.path / 'state')
        manifest['version'] = 3
        checksum = _seal_full(base.path, manifest)
    assert verify_backup(base.path, checksum) == json.loads((base.path / 'manifest.json').read_bytes())
    selected = BackupReference(base.path, checksum)
    before = bytes_in(base.path)
    document.set_text('Current copy after released backup.')
    drafts.save(ReaderResume(document))
    receipt = create_incremental_backup(app, tmp_path / 'delta', base=selected)
    root = restore_incremental_backup(receipt.path, tmp_path / 'restored',
                                     receipt.manifest_sha256, bases=(selected,))
    assert ReaderDraftStore(root).load().document.text == document.text
    assert LibraryApp(root).edition_metadata(workspace, 'original').title == 'Exact retained original'
    assert bytes_in(base.path) == before


@pytest.mark.parametrize('base_shape', ['native', 'flat'])
def test_delta_retains_tabs_owned_clips_and_pending_original_then_restores_without_authority(
        tmp_path, monkeypatch, base_shape):
    import io
    import wave
    from uuid import uuid4
    from carr_library.audio import Effect
    from carr_library.audio.effect_presets import EffectPresetStore
    from carr_library.catalog.intake import snapshot
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request, record_progress, load_request
    from carr_library.speech.tabs import ReadingTab, ReadingTabs, ReadingTabsStore
    from carr_library.studio.project import Cue, Scene, Project, ProjectStore
    from carr_library.studio import backup_inputs

    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('One verified composition')
    app.add_text(workspace, 'Retained evidence', 'Immutable exact evidence.', edition_id='original')
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    tabs = ReadingTabsStore(app.root)
    first, second = ReadingTab('primary', 'First copy'), ReadingTab.new('Second copy')
    for entry in (first, second):
        tabs.draft(entry).save(ReaderResume(ReadingDocument(entry.label), 'missing:voice'))
    index = ReadingTabs((first, second), second.id)
    tabs.save(index, expected_token=None)

    studio = app.root / 'state' / 'studio'
    (studio / 'inputs').mkdir(parents=True)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b'\x01\x00' * 800)
    audio = buffer.getvalue()
    input_path = studio / 'inputs' / (sha256(audio).hexdigest() + '.audio')
    input_path.write_bytes(audio)
    cue = Cue('owned-clip', str(input_path), duration=.04, role='microphone',
              effects=(Effect('gain', gain_db=-1),))
    project = Project('1' * 32, 'Reviewed clips', (),
        (Scene('scene', 'Scene', (), cues=(cue,), minimum_duration=.1),),
        schema=3, effects=(Effect('gain', gain_db=-2),))
    ProjectStore(studio / 'projects').save(project)
    preset = EffectPresetStore(app.root).save('Explicit chain', (Effect('gain', gain_db=-3),))

    base_dir = tmp_path / 'full'
    base_digest = create_backup(app, base_dir)
    if base_shape == 'flat':
        _, base_digest = _as_flat(base_dir, json.loads((base_dir / 'manifest.json').read_bytes()))
    base = BackupReference(base_dir, base_digest)
    base_before = bytes_in(base_dir)

    changed_document = ReadingDocument('Second copy')
    changed_document.replace(0, len(changed_document.text), 'Reviewed second copy.')
    tabs.draft(second).save(ReaderResume(changed_document, 'missing:voice', cursor=8))
    original = tmp_path / 'pending.txt'
    original_bytes = b'First pending page.\fSecond pending page.\fThird pending page.'
    original.write_bytes(original_bytes)
    metadata = dict(title='Unfinished source', authors=[], language=None, edition=None,
                    volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    calls = 0
    def stop_after_first():
        nonlocal calls
        calls += 1
        return calls >= 2
    partial = app._import_file(workspace, original, **metadata, cancelled=stop_after_first,
                              _request_id=request['request_id'], _expected_manifest=planned)
    assert partial['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=partial)

    # Copy/restore is passive even with saved voice and clip declarations.
    with monkeypatch.context() as passive:
        passive.setattr(subprocess, 'Popen',
                        lambda *_a, **_k: pytest.fail('Passive backup started a process.'))
        receipt = create_incremental_backup(app, tmp_path / 'delta', base=base)
        snapshot_value = json.loads((receipt.path / 'snapshot.json').read_bytes())
        assert snapshot_value['version'] == 5 and snapshot_value['schema_version'] == 6
        assert snapshot_value['pending']['files']
        original.unlink()
        shutil.rmtree(app.root)
        root = restore_incremental_backup(receipt.path, tmp_path / 'restored',
                                         receipt.manifest_sha256, bases=(base,))
    restored = LibraryApp(root)
    recovered_tabs = ReadingTabsStore(root)
    assert recovered_tabs.load()[0] == index
    recovered = recovered_tabs.draft(second).load()
    assert recovered.document.to_dict() == changed_document.to_dict()
    assert recovered.voice_id == 'missing:voice' and recovered.cursor == 8
    current = ProjectStore(root / 'state' / 'studio' / 'projects').load(project.id)
    assert current.revision == project.revision + 1
    relocated = Path(current.scenes[0].cues[0].path)
    assert relocated == root / 'state' / 'studio' / 'inputs' / input_path.name
    assert relocated.read_bytes() == audio
    assert '0 unavailable clip reference(s)' in backup_inputs.recovery_notice(root / 'state' / 'studio', current)
    assert EffectPresetStore(root).get(preset.id).effects == preset.effects
    rebound = load_request(root, request['request_id'])
    assert rebound['data_dir'] == str(root)
    assert all(name not in rebound for name in
               ('attempt', 'attempt_id', 'publication', 'publication_candidate'))
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete' and completed['id'] == partial['id']
    assert completed['edition'] == partial['edition']
    assert any('Second' in hit.quote for hit in restored.search(workspace, 'Second'))
    assert bytes_in(base_dir) == base_before


def test_deleted_optional_state_is_not_resurrected_from_an_ancestor(source, tmp_path):
    app, _, _, _, base = source
    optional = app.root / 'state' / 'recovery' / 'old-note.txt'
    optional.parent.mkdir(parents=True, exist_ok=True)
    optional.write_bytes(b'An explicitly deleted inert draft.')
    first = create_incremental_backup(app, tmp_path / 'first', base=base)
    first_ref = BackupReference(first.path, first.manifest_sha256)
    optional.unlink()
    second = create_incremental_backup(app, tmp_path / 'second', base=first_ref, ancestors=(base,))
    root = restore_incremental_backup(second.path, tmp_path / 'restored', second.manifest_sha256,
                                     bases=(base, first_ref))
    assert not (root / optional.relative_to(app.root)).exists()
    assert not any(row['filename'].endswith('old-note.txt') for row in json.loads(
        (second.path / 'snapshot.json').read_bytes())['state'])


@pytest.mark.parametrize('phase', ['file', 'snapshot'])
def test_cancelled_publication_keeps_a_visibly_incomplete_layer_without_a_manifest(
        source, tmp_path, monkeypatch, phase):
    app, _, document, drafts, base = source
    document.set_text('Changed data requiring publication.')
    drafts.save(ReaderResume(document))
    destination = tmp_path / 'interrupted'
    before = bytes_in(base.path)
    real_link, stopped, reached = incremental.os.link, False, []
    def link(source_path, target_path, *args, **kwargs):
        nonlocal stopped
        result = real_link(source_path, target_path, *args, **kwargs)
        target_path = Path(target_path)
        if target_path.is_relative_to(destination):
            reached.append(target_path)
            if ((phase == 'file' and target_path.is_relative_to(destination / 'files'))
                    or phase == 'snapshot' and target_path.name == 'snapshot.json'):
                stopped = True
        return result
    monkeypatch.setattr(incremental.os, 'link', link)
    with pytest.raises(InterruptedError):
        create_incremental_backup(app, destination, base=base, cancelled=lambda: stopped)
    assert reached and destination.is_dir() and not (destination / 'manifest.json').exists()
    assert bytes_in(base.path) == before
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_incremental_backup(destination, '0' * 64, bases=(base,))
    assert not list(tmp_path.glob('.carr-incremental-*'))


def test_cancel_after_manifest_publication_preserves_committed_receipt(source, tmp_path, monkeypatch):
    app, _, _, _, base = source
    destination = tmp_path / 'committed'
    real_link, stopped = incremental.os.link, False
    def link(source_path, target_path, *args, **kwargs):
        nonlocal stopped
        result = real_link(source_path, target_path, *args, **kwargs)
        if Path(target_path) == destination / 'manifest.json':
            stopped = True
        return result
    monkeypatch.setattr(incremental.os, 'link', link)
    receipt = create_incremental_backup(app, destination, base=base, cancelled=lambda: stopped)
    assert stopped and (destination / 'manifest.json').is_file()
    assert sha256((destination / 'manifest.json').read_bytes()).hexdigest() == receipt.manifest_sha256
    assert verify_incremental_backup(destination, receipt.manifest_sha256, bases=(base,))['chain'] == [
        receipt.manifest_sha256, base.sha256]


@pytest.mark.parametrize('damage', ['case-alias', 'directory-alias', 'unknown-fields', 'bool-size'],
                         ids=['case-alias', 'directory-alias', 'unknown-fields', 'bool-size'])
def test_resealed_snapshot_aliases_and_bad_shapes_fail_before_payload_copy(
        source, tmp_path, monkeypatch, damage):
    _, _, _, drafts, base = source
    receipt = changed(source, tmp_path)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    snapshot = json.loads((receipt.path / 'snapshot.json').read_bytes())
    row = state_entry(snapshot, drafts.path.relative_to(source[0].root).as_posix())
    if damage == 'case-alias':
        snapshot['state'].append({**row, 'filename': row['filename'].upper()})
    elif damage == 'directory-alias':
        snapshot['state'].append({**row, 'filename': 'Speech/other.reader.json'})
    elif damage == 'unknown-fields':
        snapshot['unreviewed_fields'] = True
    else:
        row['size_bytes'] = True
    checksum = reseal_layer(receipt.path, descriptor, snapshot)
    monkeypatch.setattr(incremental, '_copy',
                        lambda *_a, **_k: pytest.fail('Malformed snapshot reached payload copying.'))
    with pytest.raises(ValueError):
        restore_incremental_backup(receipt.path, tmp_path / 'rejected', checksum, bases=(base,))
    assert not (tmp_path / 'rejected').exists()


def test_new_snapshot_exceeding_logical_budget_never_publishes_a_layer(source, tmp_path):
    app, _, _, _, base = source
    manifest = json.loads((base.path / 'manifest.json').read_bytes())
    files, _ = incremental._inventory(manifest, incremental.DEFAULT_MAX_BYTES)
    allowance = sum(row['size_bytes'] for row in files.values()) + 4096
    oversized = app.root / 'state' / 'recovery' / 'new-note.txt'
    oversized.parent.mkdir(parents=True, exist_ok=True)
    oversized.write_bytes(b'x' * 32768)
    before = bytes_in(base.path)
    with pytest.raises(ValueError, match='budget'):
        create_incremental_backup(app, tmp_path / 'rejected', base=base, max_bytes=allowance)
    assert not (tmp_path / 'rejected').exists()
    assert bytes_in(base.path) == before
    assert not list(tmp_path.glob('.carr-incremental-*'))


def test_actual_streamed_original_above_64_mib_survives_full_delta_and_fresh_restore(
        tmp_path, monkeypatch):
    """Reuse the actual source-profile fixture and its real isolated worker."""
    import runpy
    fixture = runpy.run_path(str(Path(__file__).with_name('test_streamed_source_backup.py')))
    app, workspace, source_path, request = fixture['prepared'](tmp_path, large=True)
    maximum = 64 * 1024**2
    assert maximum < source_path.stat().st_size <= request['source_profile']['maximum_bytes']
    completed = app.resume_import(workspace, request['request_id'], timeout=180)
    assert completed['state'] == 'complete', completed
    assert completed['asset'] == request['expected_manifest']['sha256']
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    document = ReadingDocument('First reviewed large-source reading copy.')
    drafts = ReaderDraftStore(app.root)
    drafts.save(ReaderResume(document))
    base = tmp_path / 'full-streamed'
    base_digest = create_backup(app, base)
    manifest = json.loads((base / 'manifest.json').read_bytes())
    assert manifest['version'] == 5 and manifest['schema_version'] == CURRENT_SCHEMA_VERSION
    asset, = manifest['assets']
    assert asset['sha256'] == completed['asset'] and asset['size_bytes'] == source_path.stat().st_size > maximum
    base_manifest = (base / 'manifest.json').read_bytes()
    original = base / 'originals' / asset['filename']

    # A genuine >64 MiB original and a valid current SQLite schema still do
    # not acquire admission from MIME/manifest bytes without a sealed profile.
    unprofiled = tmp_path / 'unprofiled-full'
    (unprofiled / 'originals').mkdir(parents=True)
    shutil.copyfile(original, unprofiled / 'originals' / asset['filename'])
    with closing(Storage(unprofiled / 'library.sqlite', schema_version=CURRENT_SCHEMA_VERSION)) as storage:
        with storage.transaction():
            storage.db.execute('INSERT INTO catalog_asset(digest,mime,size) VALUES(?,?,?)',
                               (asset['sha256'], asset['mime'], asset['size_bytes']))
        storage.check()
    database_bytes = (unprofiled / 'library.sqlite').read_bytes()
    unprofiled_manifest = dict(version=2, kind='carr-library', schema_version=CURRENT_SCHEMA_VERSION,
        schema_fingerprint=expected_schema_fingerprint(CURRENT_SCHEMA_VERSION),
        database={'filename': 'library.sqlite', 'size_bytes': len(database_bytes),
                  'sha256': sha256(database_bytes).hexdigest()}, assets=[dict(asset)])
    unprofiled_digest = _seal_full(unprofiled, unprofiled_manifest)
    with pytest.raises(ValueError, match='no exact sealed streamed source profile'):
        create_incremental_backup(app, tmp_path / 'refused-unprofiled',
                                  base=BackupReference(unprofiled, unprofiled_digest))
    assert not (tmp_path / 'refused-unprofiled').exists()

    document.replace(0, len(document.text), 'Revised reviewed large-source reading copy.')
    drafts.save(ReaderResume(document, 'unavailable:passive-voice', cursor=8))
    # The source is already sealed. Backup/recovery must use its retained bytes
    # and checked source profile, with no new import worker or audio activation.
    monkeypatch.setattr(subprocess, 'Popen',
        lambda *_a, **_k: pytest.fail('Completed streamed backup started a process.'))
    receipt = create_incremental_backup(app, tmp_path / 'streamed-delta',
                                       base=BackupReference(base, base_digest))
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    assert 'originals/' + asset['filename'] not in descriptor['stored']
    proof = verify_incremental_backup(receipt.path, receipt.manifest_sha256,
                                     bases=(BackupReference(base, base_digest),))
    assert proof['chain'] == [receipt.manifest_sha256, base_digest]
    source_path.unlink()
    shutil.rmtree(app.root)
    root = restore_incremental_backup(receipt.path, tmp_path / 'streamed-restored',
        receipt.manifest_sha256, bases=(BackupReference(base, base_digest),))
    restored = LibraryApp(root)
    retained = restored.assets_dir / asset['filename']
    assert retained.stat().st_size == asset['size_bytes']
    assert incremental.full.hash_file(retained) == asset['sha256']
    hit, = restored.search_page(workspace, 'Portableneedle original evidence', limit=1).results
    assert hit.verified and hit.revision == completed['id']
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Portableneedle original evidence'
    resumed = ReaderDraftStore(root).load()
    assert resumed.document.to_dict() == document.to_dict() and resumed.cursor == 8
    assert resumed.voice_id == 'unavailable:passive-voice'
    assert (base / 'manifest.json').read_bytes() == base_manifest
    assert incremental.full.hash_file(original) == asset['sha256']


@pytest.mark.parametrize('mime,size', [
    ('application/pdf', 64 * 1024**2 + 1),
    ('text/plain', 128 * 1024**2 + 1),
    ('text/plain', True),
], ids=['ordinary-format-over-64-mib', 'streamed-text-over-128-mib', 'boolean-size'])
def test_incremental_outer_source_caps_reject_before_any_payload_verification(
        source, tmp_path, monkeypatch, mime, size):
    app, workspace, document, drafts, base = source
    manifest = json.loads((base.path / 'manifest.json').read_bytes())
    original, = manifest['assets']
    manifest['assets'] = [{**original, 'mime': mime, 'size_bytes': size,
        'filename': original['sha256'] + incremental.full.SUFFIXES[mime]}]
    checksum = _seal_full(base.path, manifest)
    monkeypatch.setattr(incremental.full, 'verify_backup',
        lambda *_a, **_k: pytest.fail('Oversized/invalid outer identity reached payload verification.'))
    destination = tmp_path / 'rejected-source'
    with pytest.raises(ValueError, match='original identity'):
        create_incremental_backup(app, destination, base=BackupReference(base.path, checksum))
    assert not destination.exists()
