"""The total-disk wrapper must preserve an unconfirmed audio cleanup owner.

Native stop failure is injected at the existing runtime boundary; the broker,
journal, cross-thread render lock and staged bytes are real local resources.
"""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from filelock import FileLock, Timeout
import pytest

from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.models import AudioBudget, AudioCleanupPending
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.speech import VoiceRegistry
from carr_library.studio.render import ProductionService


def test_total_budget_keeps_pending_native_path_lock_and_admission_until_quiescence(tmp_path):
    broker = ResourceBroker.for_app(tmp_path,
        limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    permitted = threading.Event()
    owner = CleanupOwner(lambda: permitted.is_set())
    attempted = []
    budget = AudioBudget(threads=1, max_output_bytes=16 * 1024**2, reserve_disk_bytes=0)
    runtime = SimpleNamespace(budget=budget, ffmpeg=Path(sys.executable).resolve(),
        ffprobe=Path(sys.executable).resolve(), music_models={}, capabilities=lambda: {})
    runtime.inspect = lambda *a, **k: pytest.fail('An unconfirmed native result is not an accepted audio node.')

    def pending_mix(tracks, target, **options):
        assert not tracks
        assert runtime.budget.max_output_bytes < budget.max_output_bytes
        target.write_bytes(b'Native cleanup owner retains these unaccepted bytes.')
        attempted.append(target)
        owner.add_quiescent_callback(lambda: target.unlink(missing_ok=True))
        raise AudioCleanupPending('Synthetic native stop failure', cleanup_owner=owner)

    runtime.mix = pending_mix
    service = ProductionService(tmp_path / 'state/studio', voices=VoiceRegistry([]),
        broker=broker, runtime_factory=lambda *_a, **_k: runtime, total_disk_bytes=2 * 1024**2)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    job = service.submit(service.create('[Pause: 0.2]'), outputs, stems=False)
    competitor = FileLock(service.jobs_root / job / '.render.lock', timeout=0)
    try:
        with pytest.raises(AudioCleanupPending) as captured:
            service.run(job)
        assert captured.value.cleanup_owner is owner
        assert attempted and attempted[0].read_bytes().startswith(b'Native cleanup owner')
        assert runtime.budget is budget
        assert service.get(job)['status'] == 'failed'
        assert service.get(job)['nodes'] == {}
        leases = broker.snapshot()['leases']
        assert len(leases) == 1 and leases[0]['operation_id'] == 'studio:' + job
        with pytest.raises(Timeout):
            competitor.acquire()
        assert owner.close() is False and attempted[0].exists()
        permitted.set()
        thread = threading.Thread(target=owner.close)
        thread.start()
        thread.join(3)
        assert not thread.is_alive() and owner.quiescent
        assert not attempted[0].exists()
        assert broker.snapshot()['leases'] == []
        with competitor:
            pass
    finally:
        permitted.set()
        owner.close()


def test_retained_inventory_can_cancel_during_wide_directory_enumeration(tmp_path, monkeypatch):
    from carr_library.studio import render
    root = tmp_path / 'retained'
    root.mkdir()
    for index in range(12):
        (root / str(index)).mkdir()
    actual_scandir = render.os.scandir
    observed, closed = [], []

    class ObservedDirectory:
        def __init__(self, path):
            self.entries = actual_scandir(path)

        def __enter__(self):
            return self

        def __iter__(self):
            for entry in self.entries:
                observed.append(entry.name)
                yield entry

        def __exit__(self, *args):
            self.entries.close()
            closed.append(True)

    monkeypatch.setattr(render.os, 'scandir', ObservedDirectory)
    with pytest.raises(InterruptedError, match='disk accounting cancelled'):
        render._retained_bytes((root,), lambda: len(observed) >= 3)
    assert len(observed) == 3 and closed == [True]
    assert all((root / str(index)).is_dir() for index in range(12))


def test_retained_inventory_counts_empty_directories_toward_entry_bound(tmp_path, monkeypatch):
    from carr_library.studio import render
    root = tmp_path / 'retained'
    root.mkdir()
    for index in range(12):
        (root / str(index)).mkdir()
    monkeypatch.setattr(render, '_MAX_RETAINED_ENTRIES', 4)
    with pytest.raises(ValueError, match='entry limit'):
        render._retained_bytes((root,), lambda: False)
    assert all((root / str(index)).is_dir() for index in range(12))
