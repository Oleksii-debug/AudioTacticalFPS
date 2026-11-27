"""Actual >64 MiB PDF import, original access and typed backup roundtrips."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.streaming_pdf import FileAsset, snapshot_original
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import load_request, prepare_request
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.source_file import prepare_source_file, launch_source_file
from tests.ingest.streaming_pdf_fixture import MIB, write_pdf


def _metadata():
    return dict(title='Large synthetic original', authors=['Fixture author'], language=None,
                edition=None, volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)


def test_large_pdf_real_worker_view_review_and_full_backup_restore(tmp_path, record_property):
    with TemporaryDirectory(prefix='complete-pdf-', dir=tmp_path) as temporary:
        root = Path(temporary)
        source = write_pdf(root / 'source.pdf')
        original = snapshot_original(root, source.name)
        assert isinstance(original, FileAsset) and original.size > 64 * MIB
        app = LibraryApp(root / 'library')
        workspace = app.create_workspace('Large PDF')
        result = app.import_file(workspace, source, title='Large source', timeout=120,
                                 pdf_max_bytes=96 * MIB)
        assert result['state'] == 'complete' and result['isolated'] is True, result
        assert result['verified'] is False and result['completed'] == 1
        request = load_request(app.root, result['request_id'])
        assert request['pdf_max_bytes'] == 96 * MIB
        assert request['asset_sha256'] == original.digest
        with app.read(workspace) as db:
            assert db.execute('SELECT size FROM catalog_asset WHERE digest=?',
                              (original.digest,)).fetchone()[0] == original.size
            assert db.execute('SELECT count(*) FROM import_publication WHERE revision=?',
                              (result['id'],)).fetchone()[0] == 1
        hit, = app.search(workspace, 'railway')
        assert not hit.verified
        with pytest.raises(ValueError, match='unverified'):
            app.copy(workspace, hit.ref, hit.revision, 'quote')
        with pytest.raises(ValueError):
            app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic independent reviewer')
        viewed = prepare_source_file(app, workspace, hit.ref, hit.revision)
        assert FileAsset.capture(viewed.path).manifest() == original.manifest()
        assert viewed.path.stat().st_nlink == 1
        launched = []
        launch_source_file(app, viewed, lambda path: launched.append(path) is None)
        assert launched == [str(viewed.path)]
        # This explicit synthetic review tests binding; successful parsing never calls it.
        app.review_pdf_page(workspace, hit.ref, hit.revision,
                            'Synthetic independent reviewer', confirmed=True)
        assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway'
        backup = root / 'backup'
        digest = create_backup(app, backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        entry, = manifest['assets']
        assert entry['size_bytes'] == original.size and entry['sha256'] == original.digest
        assert FileAsset.capture(backup / 'originals' / entry['filename']).manifest() == original.manifest()
        source.unlink()
        restored_path = restore_backup(backup, root / 'restored', digest)
        restored = LibraryApp(restored_path)
        restored_hit, = restored.search(workspace, 'railway')
        assert restored_hit.revision == hit.revision
        assert restored.copy(workspace, restored_hit.ref, restored_hit.revision, 'quote') == 'railway'
        assert FileAsset.capture(restored.assets.resolve(original.digest)).manifest() == original.manifest()
        record_property('large_original_bytes', original.size)
        record_property('large_original_sha256', original.digest)


def test_large_pending_request_restores_exact_original_and_resumes_real_worker(tmp_path):
    with TemporaryDirectory(prefix='pending-pdf-', dir=tmp_path) as temporary:
        root = Path(temporary)
        source = write_pdf(root / 'pending.pdf')
        original = snapshot_original(root, source.name)
        app = LibraryApp(root / 'library')
        workspace = app.create_workspace('Pending large PDF')
        request = prepare_request(app.root, workspace, source, _metadata(), Config(),
                                  expected_manifest=original.manifest(), pdf_max_bytes=96 * MIB)
        assert app.browse(workspace) == ()
        backup = root / 'backup'
        digest = create_backup(app, backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        pending = manifest['pending']
        asset, = pending['assets']
        assert asset['size_bytes'] == original.size and asset['sha256'] == original.digest
        assert pending['version'] == 1  # same typed component; no format fork
        source.unlink()
        restored = LibraryApp(restore_backup(backup, root / 'restored', digest))
        saved = load_request(restored.root, request['request_id'])
        assert saved['pdf_max_bytes'] == request['pdf_max_bytes']
        assert saved['expected_manifest'] == original.manifest()
        assert Path(saved['source']).is_relative_to(restored.root / 'originals')
        assert FileAsset.capture(saved['source']).manifest() == original.manifest()
        resumed = restored.resume_import(workspace, request['request_id'], timeout=120)
        assert resumed['state'] == 'complete' and resumed['isolated'] is True, resumed
        assert resumed['verified'] is False
        hit, = restored.search(workspace, 'railway')
        assert not hit.verified
        with pytest.raises(ValueError, match='unverified'):
            restored.copy(workspace, hit.ref, hit.revision, 'quote')


def test_large_pdf_backup_cancel_never_publishes_manifest(tmp_path):
    with TemporaryDirectory(prefix='cancel-pdf-', dir=tmp_path) as temporary:
        root = Path(temporary)
        source = write_pdf(root / 'source.pdf')
        app = LibraryApp(root / 'library')
        workspace = app.create_workspace('Cancelled backup')
        original = snapshot_original(root, source.name)
        prepare_request(app.root, workspace, source, _metadata(), Config(),
                        expected_manifest=original.manifest(), pdf_max_bytes=96 * MIB)
        backup = root / 'backup'
        observed = []
        def cancelled():
            # The real copied PDF lives inside the backup's owned staging directory.
            for file in root.rglob('*.pdf'):
                if file != source and file.is_file() and file.stat().st_size >= MIB:
                    observed.append(file.stat().st_size)
                    return True
            return False
        with pytest.raises(InterruptedError):
            create_backup(app, backup, cancelled=cancelled)
        assert observed
        assert not (backup / 'manifest.json').exists()
        assert app.browse(workspace) == ()
        assert FileAsset.capture(source).manifest() == original.manifest()


def test_pending_backup_refuses_source_exceeding_its_saved_request_budget(tmp_path):
    source = write_pdf(tmp_path / 'source.pdf', 1024)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Limited PDF request')
    request = prepare_request(app.root, workspace, source, _metadata(), Config(), pdf_max_bytes=512)
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match='request byte budget'):
        create_backup(app, destination)
    assert not (destination / 'manifest.json').exists()
    assert load_request(app.root, request['request_id'])['pdf_max_bytes'] == 512
    assert source.exists() and app.browse(workspace) == ()
