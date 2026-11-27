"""Actual byte counts, authenticating phases and cancellation publication gates."""
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.progress import ProgressHub
from carr_library.sync import archive
from carr_library.sync.service import SyncService
from carr_library.sync.vault import Vault


PASSWORD = 'Synthetic transfer password 123'


class Observed:
    def __init__(self):
        self.hub, self.events, self.errors = ProgressHub(), [], []
        self.handle = self.hub.start('snapshot', 'Portable snapshot')

    def __call__(self, **values):
        try:
            self.events.append(self.hub.update(self.handle, **values))
        except Exception as error:
            self.errors.append(error)

    def phase(self, label):
        return [event for event in self.events if event.phase == label]

    def measured_total(self, label):
        events = self.phase(label)
        assert events and all(event.total == events[0].total for event in events)
        assert events[-1].completed == events[-1].total > 0
        assert all(event.percent != 100 and not event.verified for event in events)
        return events[-1].total


@pytest.fixture
def app(tmp_path):
    value = LibraryApp(tmp_path / 'library')
    workspace = value.create_workspace('Synthetic library')
    value.add_text(workspace, 'Synthetic source', 'A checked original source. ' * 200)
    return value


def test_archive_counters_match_actual_uncompressed_bytes_and_hash_length(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'CHUNK', 1024)
    source = tmp_path / 'source'
    source.mkdir()
    (source / archive.INVENTORY).write_bytes(b'{}')
    (source / 'data.bin').write_bytes(b'A' * 7000)
    (source / 'empty').mkdir()
    packed, unpacked, hashed = [], [], []
    target = tmp_path / 'snapshot.zip'
    receipt = archive.pack_directory(source, target, on_progress=lambda *counts: packed.append(counts))
    archive.unpack_archive(target, tmp_path / 'unpacked', on_progress=lambda *counts: unpacked.append(counts))
    assert archive.hash_file(target, on_progress=lambda *counts: hashed.append(counts)) == receipt['sha256']
    assert packed[0] == unpacked[0] == (0, 7002)
    assert packed[-1] == unpacked[-1] == (7002, 7002)
    assert hashed[-1] == (target.stat().st_size, target.stat().st_size)
    assert all(a[0] <= b[0] for values in (packed, unpacked, hashed) for a, b in zip(values, values[1:]))
    assert len(packed) > 3 and len(unpacked) > 3


@pytest.mark.parametrize('encrypted', [False, True])
def test_real_snapshot_progress_uses_phase_units_and_never_declares_completion(app, tmp_path, encrypted):
    service, folder = SyncService(app), tmp_path / 'vault'
    password = PASSWORD if encrypted else None
    published, inspected, restored = Observed(), Observed(), Observed()
    receipt = service.publish(folder, 'Checkpoint', None, password, on_progress=published)
    preview = service.inspect(folder, receipt['token'], password, on_progress=inspected)
    destination = tmp_path / 'recovered'
    service.restore(folder, receipt['token'], destination, password, on_progress=restored)
    source_bytes = published.measured_total('Packing the checkpoint')
    assert inspected.measured_total('Unpacking and checking the checkpoint') == source_bytes
    assert published.measured_total('Publishing verified checkpoint chunks') == receipt['bytes']
    assert inspected.measured_total('Checking and copying snapshot chunks') == receipt['bytes']
    assert restored.measured_total('Copying the verified library into private staging') == preview['bytes']
    if encrypted:
        plain_size = published.measured_total('Encrypting the checkpoint')
        assert inspected.measured_total('Authenticating and decrypting the checkpoint') == plain_size
        assert plain_size < receipt['bytes']
    for observed in (published, inspected, restored):
        assert not observed.errors
        assert all(event is not None and event.state == 'running' and event.percent != 100
                   for event in observed.events)
        assert any(event.total is None for event in observed.events)
        assert PASSWORD not in '\n'.join(event.describe() for event in observed.events)
    assert LibraryApp(destination).workspaces()[0].name == 'Synthetic library'


def test_encryption_count_cancellation_cannot_publish_a_checkpoint(app, tmp_path):
    service, cancel = SyncService(app), Event()
    events = []
    def progress(**event):
        events.append(event)
        if event['phase'] == 'Encrypting the checkpoint' and event['completed']:
            cancel.set()
    folder = tmp_path / 'vault'
    with pytest.raises(InterruptedError):
        service.publish(folder, 'Cancel encryption', None, PASSWORD,
                        cancelled=cancel.is_set, on_progress=progress)
    assert events and Vault(folder).head() is None
    assert not list(service.cache_root.glob('*/ready.json'))


def test_restore_copy_count_cancellation_never_publishes_destination(app, tmp_path):
    service, cancel = SyncService(app), Event()
    folder = tmp_path / 'vault'
    receipt = service.publish(folder, 'Checkpoint', None)
    def progress(**event):
        if event['phase'] == 'Copying the verified library into private staging' and event['completed']:
            cancel.set()
    destination = tmp_path / 'cancelled-restore'
    with pytest.raises(InterruptedError):
        service.restore(folder, receipt['token'], destination,
                        cancelled=cancel.is_set, on_progress=progress)
    assert not destination.exists()
    assert not list(tmp_path.glob('.carr-portable-restore-*'))


def test_progress_observer_error_cannot_hide_durable_receipts(app, tmp_path):
    service, folder = SyncService(app), tmp_path / 'vault'
    def broken(**event):
        raise RuntimeError('Unavailable progress consumer')
    receipt = service.publish(folder, 'Checkpoint', None, on_progress=broken)
    assert receipt['token'] == Vault(folder).head()
    assert service.inspect(folder, receipt['token'], on_progress=broken)['verified']
    result = service.restore(folder, receipt['token'], tmp_path / 'restored', on_progress=broken)
    assert result['verified'] and LibraryApp(result['destination']).workspaces()
