"""Publication integration uses the existing controller and captured destination."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from carr_library.audio.models import digest
from carr_library.audio.publishing import PublicationService
from carr_library.ui.audio_studio import StudioController


def test_controller_binds_the_current_plan_and_existing_managed_project(tmp_path):
    from carr_library.application import LibraryApp
    app = LibraryApp(tmp_path / 'library')
    studio = StudioController(private_root=app.root)
    studio.new('Exact literal [source].', title='Current title')
    studio.save_in_library()
    service = studio._publication_service(bind_plan=True)
    assert isinstance(service, PublicationService)
    assert service.root == studio.output_dir
    assert service.expected_plan_sha256 == digest(studio.plan().to_dict())
    assert service.ffmpeg is None and service.broker is None


def test_publication_controller_forwards_captured_approval_and_does_not_render(tmp_path, monkeypatch):
    studio = StudioController(private_root=tmp_path / 'private')
    studio.new('Exact source.')
    studio.result = {'run_key': 'a' * 64}
    recorded, approval = [], object()
    service = SimpleNamespace(export_epub=lambda *a, **k: recorded.append((a, k)) or {'status': 'complete'})
    monkeypatch.setattr(studio, '_publication_service', lambda **kw: service)
    monkeypatch.setattr(studio, 'render', lambda **kw: pytest.fail('Publication launched inference'))
    stop, update = lambda: False, lambda value: None
    result = studio.publish_book(tmp_path / 'book.epub', book_format='epub', language='uk',
                                 approval=approval, cancelled=stop, on_status=update)
    assert result['status'] == 'complete'
    assert recorded == [(('a' * 64, tmp_path / 'book.epub'),
        {'language': 'uk', 'creator': '', 'approval': approval, 'cancelled': stop, 'progress': update})]
    assert studio._render_lock.acquire(blocking=False)
    studio._render_lock.release()


def test_busy_studio_cannot_start_a_second_publication(tmp_path, monkeypatch):
    studio = StudioController(private_root=tmp_path / 'private')
    studio._render_lock.acquire()
    monkeypatch.setattr(studio, '_publication_service', lambda **kw: pytest.fail('Concurrent service opened'))
    try:
        with pytest.raises(RuntimeError, match='current Studio'):
            studio.publish_book(tmp_path / 'book.epub', book_format='epub', language='uk')
    finally:
        studio._render_lock.release()


def test_publication_failure_releases_existing_controller_worker_guard(tmp_path, monkeypatch):
    studio = StudioController(private_root=tmp_path / 'private')
    studio.result = {'run_key': 'a' * 64}
    def failed(*a, **k):
        raise ValueError('No valid completed render')
    monkeypatch.setattr(studio, '_publication_service', lambda **kw: SimpleNamespace(export_epub=failed))
    with pytest.raises(ValueError, match='valid completed'):
        studio.publish_book(tmp_path / 'book.epub', book_format='epub', language='uk')
    assert studio._render_lock.acquire(blocking=False)
    studio._render_lock.release()


def test_publication_receipt_discovery_can_stop_before_any_filesystem_work(tmp_path, monkeypatch):
    studio = StudioController(private_root=tmp_path / 'private')
    monkeypatch.setattr(studio, '_publication_service', lambda **kw: pytest.fail('Cancelled discovery opened storage'))
    with pytest.raises(InterruptedError):
        studio.publication_receipts(cancelled=lambda: True)


def test_verify_uses_explicit_existing_output_and_never_reinterprets_current_script(tmp_path, monkeypatch):
    studio = StudioController(private_root=tmp_path / 'private')
    verify = Mock(return_value={'status': 'complete'})
    monkeypatch.setattr(studio, '_publication_service', lambda **kw: SimpleNamespace(verify=verify))
    monkeypatch.setattr(studio, 'plan', lambda: pytest.fail('Offline verification parsed a new plan'))
    path, stop = tmp_path / 'restored.epub', lambda: False
    studio.verify_publication('1' * 32, path, cancelled=stop)
    verify.assert_called_once_with('1' * 32, output=path, cancelled=stop)
