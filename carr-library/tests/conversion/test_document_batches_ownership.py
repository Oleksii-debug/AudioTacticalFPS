"""Native cleanup and publication status on the existing document/Studio owners."""
from hashlib import sha256
from pathlib import Path
import runpy
from threading import Event, Thread

from filelock import FileLock, Timeout
import pytest

from carr_library.application import LibraryApp
from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.models import AudioCleanupPending
from carr_library.conversion import batches, service as conversion
from carr_library.conversion.batches import DocumentBatches
from carr_library.conversion.planning import SplitOptions
from carr_library.library_backup import create_backup


_fixture = runpy.run_path(str(Path(__file__).with_name('test_document_batches.py')))


def test_pending_native_conversion_preserves_the_exact_owner_stage_and_snapshot_exclusion(tmp_path, monkeypatch):
    library = LibraryApp(tmp_path / 'library')
    source = tmp_path / 'source.txt'
    source.write_text('A retained original.\nSecond line.', encoding='utf-8')
    service = DocumentBatches(library.root)
    identity = service.create([source])
    ready, held, stages = Event(), [], []
    def attempt():
        if not ready.is_set():
            return False
        for stream in held:
            stream.close()
        return True
    owner = CleanupOwner(attempt)
    pending = AudioCleanupPending('Native child has not confirmed exit.', cleanup_owner=owner)
    original_process = conversion.run_process
    def unconfirmed(*args, **kwargs):
        assert kwargs['owned_tree'] is True
        stage = Path(kwargs['cwd'])
        assert (stage / 'source.txt').read_bytes() == source.read_bytes()
        stages.append(stage)
        held.append((stage / 'owned-handle.fixture').open('wb'))
        raise pending
    monkeypatch.setattr(conversion, 'run_process', unconfirmed)
    try:
        with pytest.raises(AudioCleanupPending) as raised:
            service.prepare(identity)
        assert raised.value is pending and raised.value.cleanup_owner is owner
        assert len(stages) == 1 and stages[0].is_dir()
        assert service.get(identity)['sources'][0]['prepared'] is None
        lock = FileLock(service.root / '.document-batches.lock', timeout=0)
        with pytest.raises(Timeout):
            lock.acquire()
        with pytest.raises(RuntimeError):
            service.prepare(identity)
        with pytest.raises(RuntimeError):
            create_backup(library, tmp_path / 'blocked-backup')
        assert not (tmp_path / 'blocked-backup').exists()
    finally:
        ready.set()
        thread = Thread(target=owner.close)
        thread.start(); thread.join(5)
        assert not thread.is_alive() and owner.quiescent
    assert not stages[0].exists()
    with lock:
        pass
    monkeypatch.setattr(conversion, 'run_process', original_process)
    assert service.prepare(identity)['status'] == 'prepared'
    assert create_backup(library, tmp_path / 'ready-backup')


@pytest.mark.parametrize('failure', ['false', 'oserror', 'interrupt'])
def test_actual_studio_completion_survives_owned_close_failure_and_retains_retry(tmp_path, monkeypatch, failure):
    from carr_library.studio import render
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    _fixture['reviewed'](batch, action='studio', voice_id='synthetic-tone')
    production = render.ProductionService(service.library / 'state' / 'studio', voices=_fixture['ToneVoices']())
    actual_close = production.close
    can_close = [False]
    def interrupted_close():
        if can_close[0]:
            return actual_close()
        if failure == 'false':
            return False
        raise (OSError if failure == 'oserror' else KeyboardInterrupt)('Owned cleanup was not confirmed.')
    monkeypatch.setattr(production, 'close', interrupted_close)
    monkeypatch.setattr(render, 'ProductionService', lambda *args, **kwargs: production)
    try:
        result = service.run(identity)
        assert result['status'] == 'complete'
        assert service.get(identity)['status'] == 'complete'
        assert 'retained for cleanup' in result['message']
        assert service._owned_production is production
        assert service.close() is False and service._owned_production is production
        child = production.get(result['outputs'][0]['studio_job'])
        before = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest() for row in child['artifacts']}
        can_close[0] = True
        assert service.close() is True and service._owned_production is None
        assert not service.cleanup_notice
        assert {path: sha256(Path(path).read_bytes()).hexdigest() for path in before} == before
    finally:
        can_close[0] = True
        service.close()
        actual_close()


def test_borrowed_studio_service_is_not_closed_by_document_batch(tmp_path, monkeypatch):
    from carr_library.studio import render
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    production = render.ProductionService(service.library / 'state' / 'studio', voices=_fixture['ToneVoices']())
    actual_close = production.close
    service.production = production
    monkeypatch.setattr(production, 'close', lambda: pytest.fail('Borrowed Studio ownership changed.'))
    try:
        _fixture['reviewed'](batch, action='studio', voice_id='synthetic-tone')
        assert service.run(identity)['status'] == 'complete'
        assert service.close() is True
    finally:
        actual_close()


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_exact_completed_metadata_readback_retains_status_after_late_writer_error(tmp_path, monkeypatch, failure):
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    _fixture['reviewed'](batch)
    actual_save = service._save
    def late(record):
        actual_save(record)
        if record['status'] == 'complete':
            raise failure('The final atomic status writer did not acknowledge completion.')
    monkeypatch.setattr(service, '_save', late)
    result = service.run(identity)
    assert result['status'] == 'complete' and service.get(identity)['status'] == 'complete'
    assert 'present and checked' in result['message'] and 'acknowledgement was interrupted' in result['message']
    assert _fixture['receipts'](service, identity, output) == source.read_bytes()


def test_different_final_metadata_is_not_promoted_after_writer_error(tmp_path, monkeypatch):
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    _fixture['reviewed'](batch)
    actual_save = service._save
    def changed(record):
        actual_save(record)
        if record['status'] == 'complete':
            foreign = dict(record, status='paused', message='A different saved status.')
            batches.atomic_json(service.store.path(identity), foreign, replace=True)
            raise OSError('Final saved status differs.')
    monkeypatch.setattr(service, '_save', changed)
    with pytest.raises(OSError, match='differs'):
        service.run(identity)
    assert service.get(identity)['status'] == 'paused'
    assert _fixture['receipts'](service, identity, output) == source.read_bytes()


def test_late_progress_interrupt_cannot_demote_verified_batch(tmp_path):
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    _fixture['reviewed'](batch)
    def observer(message):
        if message.startswith('Every reviewed output'):
            raise KeyboardInterrupt('Late observer interruption.')
    assert service.run(identity, progress=observer)['status'] == 'complete'
    assert _fixture['receipts'](service, identity, output) == source.read_bytes()


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_final_lock_release_warning_keeps_only_the_exact_present_completed_status(tmp_path, monkeypatch, failure):
    from contextlib import contextmanager
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    _fixture['reviewed'](batch)
    original = batches.retained_state_lock
    @contextmanager
    def released(*args, **kwargs):
        with original(*args, **kwargs):
            yield
        raise failure('The real batch lock released without an acknowledgement.')
    monkeypatch.setattr(batches, 'retained_state_lock', released)
    result = service.run(identity)
    assert result['status'] == 'complete'
    assert service.get(identity)['status'] == 'complete'
    assert 'releasing the batch state locks could not be confirmed' in result['message']
    assert _fixture['receipts'](service, identity, output) == source.read_bytes()


def test_final_lock_error_cannot_adopt_a_different_completed_record(tmp_path, monkeypatch):
    from contextlib import contextmanager
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    _fixture['reviewed'](batch)
    original = batches.retained_state_lock
    @contextmanager
    def released(*args, **kwargs):
        with original(*args, **kwargs):
            yield
        current = service.get(identity)
        current['message'] = 'An independent changed completion record.'
        batches.atomic_json(service.store.path(identity), current, replace=True)
        raise OSError('Different record after release.')
    monkeypatch.setattr(batches, 'retained_state_lock', released)
    with pytest.raises(OSError, match='Different record'):
        service.run(identity)
    assert service.get(identity)['message'] == 'An independent changed completion record.'
    assert _fixture['receipts'](service, identity, output) == source.read_bytes()


def test_prepare_exposes_the_actual_converter_saved_output_cleanup_notice(tmp_path, monkeypatch):
    from dataclasses import replace
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    actual = conversion.ConversionService.extract
    def saved(*args, **kwargs):
        result = actual(*args, **kwargs)
        assert result.path.is_file() and result.report_path.is_file()
        return replace(result, cleanup_warning='Synthetic converter cleanup acknowledgement.')
    monkeypatch.setattr(conversion.ConversionService, 'extract', saved)
    notices = []
    result = service.prepare(identity, progress=notices.append)
    assert result['status'] == 'prepared'
    assert 'converter cleanup acknowledgement' in result['message']
    assert any('converter cleanup acknowledgement' in item for item in notices)
    assert service.get(identity)['message'] == result['message']


def test_split_outputs_preserve_both_actual_writer_cleanup_notices(tmp_path, monkeypatch):
    from dataclasses import replace
    batch = _fixture['batch'].__wrapped__(tmp_path)
    service, identity, source, output = batch
    _fixture['reviewed'](batch)
    actual = batches.write_export
    def saved(path, *args, **kwargs):
        result = actual(path, *args, **kwargs)
        assert result.path.is_file()
        kind = 'sidecar' if str(path).endswith('.provenance.json') else 'text'
        return replace(result, cleanup_warning='Synthetic ' + kind + ' cleanup acknowledgement.')
    monkeypatch.setattr(batches, 'write_export', saved)
    result = service.run(identity)
    assert result['status'] == 'complete'
    assert 'text cleanup acknowledgement' in result['message']
    assert 'sidecar cleanup acknowledgement' in result['message']
    assert _fixture['receipts'](service, identity, output) == source.read_bytes()
