"""Actual acquisition publication and shared admission boundaries; no live network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
import sys
from threading import Event

import pytest
import carr_library

from carr_library.acquisition import service as module
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest
from carr_library.operations.models import ResourceCancelled, ResourceUnavailable


_fixture = runpy.run_path(str(Path(__file__).with_name('test_acquisition_workflow.py')))
lookup = _fixture['lookup']


@pytest.fixture
def acquired_source(tmp_path):
    app, workspace, transfers = _fixture['sample'].__wrapped__(tmp_path)
    transfers.pdf = _fixture['pdf_bytes']()
    preview = lookup(app, workspace, oa=True)
    return app, workspace, transfers, preview


def acquire(sample, target, **options):
    app, workspace, _transfers, preview = sample
    return app.acquire_doi_source(workspace, preview, 0, target,
        online_allowed=True, rights_confirmed=True, **options)


@pytest.mark.parametrize('failure', ['directory_sync', 'parent_sync', 'staging_cleanup', 'marker_unlink'])
def test_committed_pdf_and_provenance_survive_late_cleanup_error(acquired_source, tmp_path, monkeypatch, failure):
    target = tmp_path / 'completed'
    marker = target / '.acquisition-incomplete'
    observed = []
    actual_sync, actual_unlink = module._sync_directory, Path.unlink
    def sync(path):
        if (failure in ('directory_sync', 'parent_sync')
                and Path(path) == (target if failure == 'directory_sync' else target.parent)
                and target.exists() and not marker.exists()):
            observed.append(failure)
            raise OSError('Synthetic post-completion directory sync failure')
        return actual_sync(path)
    def unlink(path, *args, **kwargs):
        result = actual_unlink(path, *args, **kwargs)
        if failure == 'marker_unlink' and path == marker:
            observed.append(failure)
            raise OSError('Synthetic error after the completion marker was removed')
        return result
    actual_temporary = module.TemporaryDirectory
    class Temporary(actual_temporary):
        def cleanup(self):
            super().cleanup()
            if failure == 'staging_cleanup':
                observed.append(failure)
                raise OSError('Synthetic post-completion staging cleanup failure')
    monkeypatch.setattr(module, '_sync_directory', sync)
    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(module, 'TemporaryDirectory', Temporary)
    result = acquire(acquired_source, target)
    app, workspace, transfers, _preview = acquired_source
    assert observed and transfers.downloads and len(transfers.downloads) == 1
    assert result.path.read_bytes() == transfers.pdf and not marker.exists()
    receipt = json.loads(result.receipt_path.read_text(encoding='utf-8'))
    assert receipt['sha256'] == result.sha256 == sha256(transfers.pdf).hexdigest()
    assert receipt['bytes'] == len(transfers.pdf) and receipt['source_text_verified'] is False
    assert result.state == 'complete' and result.edition_id and result.reference_id
    assert app.assets.resolve(result.sha256).read_bytes() == transfers.pdf
    assert 'saved' in result.message.lower() or 'completion was published' in result.message.lower()
    assert list(tmp_path.glob('.carr-acquire-*')) == []


@pytest.mark.parametrize('replacement', ['document.pdf', '.acquisition-incomplete'])
def test_failed_pair_publication_preserves_foreign_replacement(acquired_source, tmp_path, monkeypatch, replacement):
    target = tmp_path / 'contended'
    actual_publish = module._publish_file
    foreign = b'Independent writer replacement; acquisition must not delete it.'
    def publish(source, destination, cancelled):
        if Path(destination).name == 'provenance.json':
            incoming = target / 'replacement.tmp'
            incoming.write_bytes(foreign)
            incoming.replace(target / replacement)
            raise OSError('Synthetic second-file publication failure')
        return actual_publish(source, destination, cancelled)
    monkeypatch.setattr(module, '_publish_file', publish)
    with pytest.raises(OSError, match='second-file publication'):
        acquire(acquired_source, target)
    assert (target / replacement).read_bytes() == foreign
    assert (target / '.acquisition-incomplete').is_file()
    assert not (target / 'provenance.json').exists()
    assert not acquired_source[0].browse(acquired_source[1])


def test_native_resource_release_error_precedes_completion(acquired_source, tmp_path, monkeypatch):
    from carr_library.operations.broker import ResourceLease
    target = tmp_path / 'unconfirmed-release'
    actual = ResourceLease.release
    def release(lease):
        first = not lease._released
        actual(lease)
        if first:
            raise OSError('Synthetic native ownership release error')
    monkeypatch.setattr(ResourceLease, 'release', release)
    with pytest.raises(OSError, match='native ownership release'):
        acquire(acquired_source, target)
    assert not target.exists()
    assert not acquired_source[0].browse(acquired_source[1])


def test_shared_policy_denies_before_download_or_staging(acquired_source, tmp_path):
    app, _workspace, transfers, _preview = acquired_source
    broker = ResourceBroker.for_app(app.root,
        limits=ResourceLimits(cpu_slots=1, memory_bytes=64 * 1024 * 1024,
                              min_free_disk_bytes=0))
    app._acquisition_service._resource_broker = broker
    target = tmp_path / 'policy-denied'
    with pytest.raises(ResourceUnavailable):
        acquire(acquired_source, target)
    assert not transfers.downloads and not target.exists()
    assert not list(tmp_path.glob('.carr-acquire-*'))
    assert not broker.snapshot()['leases']


def test_download_holds_declared_resources_and_releases_before_import(acquired_source, tmp_path, monkeypatch):
    app, _workspace, transfers, _preview = acquired_source
    broker = ResourceBroker.for_app(app.root)
    app._acquisition_service._resource_broker = broker
    observed = []
    def during_transfer():
        snapshot = broker.snapshot()
        assert len(snapshot['leases']) == 1
        lease = snapshot['leases'][0]
        assert lease['operation_id'].startswith('acquisition:download:')
        assert lease['request']['cpu_slots'] == 1
        assert lease['request']['disk_bytes'] >= 2 * module.MAX_DOWNLOAD_BYTES
        assert lease['request']['additional_disks'][0][1] == module.MAX_DOWNLOAD_BYTES
        observed.append('transfer')
    transfers.on_download = during_transfer
    actual_import = app.import_file
    def importing(*args, **kwargs):
        assert not broker.snapshot()['leases']
        observed.append('import')
        return actual_import(*args, **kwargs)
    monkeypatch.setattr(app, 'import_file', importing)
    result = acquire(acquired_source, tmp_path / 'admitted')
    assert result.state == 'complete' and observed == ['transfer', 'import']
    assert not broker.snapshot()['leases']


def test_cancelled_resource_wait_preserves_the_other_owner(acquired_source, tmp_path, monkeypatch):
    app, _workspace, transfers, _preview = acquired_source
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    app._acquisition_service._resource_broker = broker
    cancellation, waiting = Event(), Event()
    actual = broker.reserve
    @contextmanager
    def reserve(request, **kwargs):
        waiting.set()
        with actual(request, **kwargs) as lease:
            yield lease
    with actual(ResourceRequest(cpu_slots=1), operation_id='fixture:other-owner') as other:
        monkeypatch.setattr(broker, 'reserve', reserve)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(acquire, acquired_source, tmp_path / 'cancelled-wait',
                                 cancelled=cancellation.is_set)
            assert waiting.wait(5)
            cancellation.set()
            with pytest.raises(ResourceCancelled):
                future.result(10)
        assert not other.cancelled()
        assert [row['id'] for row in broker.snapshot()['leases']] == [other.id]
    assert not transfers.downloads and not (tmp_path / 'cancelled-wait').exists()
    assert not list(tmp_path.glob('.carr-acquire-*')) and not broker.snapshot()['leases']


@pytest.mark.parametrize('boundary, code, incomplete', [
    ('before_marker', 74, True), ('after_marker', 73, False),
])
def test_actual_abrupt_child_preserves_publication_pair_and_releases_admission(
        tmp_path, boundary, code, incomplete):
    package = Path(carr_library.__file__).resolve()
    source = Path(module.__file__).resolve()
    digest = sha256(source.read_bytes()).hexdigest()
    child = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8',
        str(Path(__file__).with_name('acquisition_crash_probe.py').resolve()),
        boundary, str(tmp_path), str(package), str(source), digest],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', timeout=30)
    assert child.returncode == code, child.stdout + child.stderr
    witness = json.loads((tmp_path / 'witness.json').read_text(encoding='utf-8'))
    assert witness['boundary'] == boundary and witness['downloads'] == 1
    assert Path(witness['package']) == package and Path(witness['service']) == source
    assert witness['service_sha256'] == digest and witness['active_leases'] == []
    target = tmp_path / 'completed'
    assert (target / '.acquisition-incomplete').exists() is incomplete
    document, provenance = target / 'document.pdf', target / 'provenance.json'
    assert document.read_bytes() == (tmp_path / 'expected.pdf').read_bytes()
    receipt = json.loads(provenance.read_text(encoding='utf-8'))
    assert receipt['sha256'] == witness['sha256'] == sha256(document.read_bytes()).hexdigest()
    assert receipt['bytes'] == witness['bytes']
    assert receipt['source_text_verified'] is False
    assert document.stat().st_nlink == provenance.stat().st_nlink == 1
    assert all(not tuple(path.iterdir()) for path in tmp_path.glob('.carr-acquire-*'))
    assert not ResourceBroker.for_app(Path(witness['library'])).snapshot()['leases']
    from carr_library.application import LibraryApp
    reopened = LibraryApp(Path(witness['library']))
    assert not reopened.browse(witness['workspace'])
    with reopened.read(witness['workspace']) as db:
        assert db.execute('SELECT count(*) FROM academic_reference').fetchone()[0] == 0
