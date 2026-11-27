"""Actual host-worker lifetime boundaries; native widgets are covered separately."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.ebook_audio_dialog import EbookAudioDialog, OverlayPlayback


class Host:
    """The test owns this real serial executor; the product dialog only borrows it."""
    def __init__(self, library):
        self.library = library
        self.cancel_event = Event()
        self._closing = self.working = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.callbacks = []
        self.callback_lock = Lock()
        self.delivered = Event()
        self.future = None
        self.progress_panel = None
        self.report = Mock()

    def run_job(self, title, operation, done, failed):
        if self.working or self._closing:
            return None
        self.working = True
        self.cancel_event.clear()
        self.delivered.clear()
        self.future = future = self.executor.submit(operation)
        def finished(result):
            def apply():
                if self._closing:
                    return
                self.working = False
                try:
                    value = result.result()
                except BaseException as error:
                    failed(error)
                else:
                    done(value)
            with self.callback_lock:
                self.callbacks.append(apply)
            self.delivered.set()
        future.add_done_callback(finished)
        return future

    def pump(self):
        assert self.delivered.wait(5), 'The actual host worker did not return.'
        with self.callback_lock:
            callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


@pytest.fixture
def host(tmp_path):
    value = Host(LibraryApp(tmp_path / 'library'))
    try:
        yield value
    finally:
        value.cancel_event.set()
        value.executor.shutdown(wait=True, cancel_futures=True)


class Owned:
    def __init__(self, path):
        self.path = path
        self.path.write_bytes(b'owned temporary result')
        self.closed = Event()
        self.close_count = 0

    def close(self):
        self.close_count += 1
        self.path.unlink(missing_ok=True)
        self.closed.set()


def dialog_for(host):
    value = object.__new__(EbookAudioDialog)
    value.host = host
    value.library, value.library_root = host.library, Path(host.library.root)
    value.guard = lambda: None
    value.cancelled = Event()
    value.closed = value.busy = value._closing = value._destroyed = False
    value._future = None
    value._take_result = lambda: None
    value._release_result = lambda: None
    value._cleanup_owners = []
    value._last_error = None
    value._pending = value.session = value.transport = value.player = None
    value.progress = None
    value.timer = Mock()
    value.status = Mock()
    value._enable = Mock()
    value._error = Mock(wraps=value._error)
    value.dialog = SimpleNamespace(IsModal=lambda: True, EndModal=Mock())
    value.wx = SimpleNamespace(ID_CANCEL=1, CallAfter=lambda fn: fn())
    return value


@pytest.mark.parametrize('change', ['same-path-new-library', 'root-changed', 'host-closing'])
def test_queued_recorded_work_rechecks_exact_host_scope_before_execution(host, tmp_path, change):
    release, entered = Event(), Event()
    def block():
        entered.set()
        assert release.wait(5)
    blocker = host.executor.submit(block)
    assert entered.wait(5)
    dialog = dialog_for(host)
    operation, presented = Mock(), Mock()
    assert dialog._submit(operation, presented, lambda value: value.close())
    dialog._enable.reset_mock()
    if change == 'same-path-new-library':
        host.library = LibraryApp(host.library.root)
    elif change == 'root-changed':
        host.library.root = tmp_path / 'different-root'
    else:
        host._closing = True
    release.set(); blocker.result(timeout=5)
    host.pump()
    operation.assert_not_called()
    presented.assert_not_called()
    dialog._enable.assert_not_called()
    dialog.status.ChangeValue.assert_not_called()
    dialog.status.SetFocus.assert_not_called()
    assert not hasattr(dialog, 'executor')


def test_close_disposes_late_actual_worker_result_without_host_callback(host, tmp_path):
    dialog = dialog_for(host)
    entered, release = Event(), Event()
    result = Owned(tmp_path / 'late.wav')
    def operation():
        entered.set()
        assert release.wait(5)
        return result
    presented = Mock()
    assert dialog._submit(operation, presented, lambda value: value.close())
    assert entered.wait(5)
    dialog.close()
    host._closing = True
    release.set()
    assert result.closed.wait(5)
    host.pump()
    presented.assert_not_called()
    assert result.close_count == 1 and not result.path.exists()
    assert dialog.closed
    # Closing the child never shuts down or replaces the borrowed host executor.
    assert host.executor.submit(lambda: 23).result(timeout=5) == 23


def test_completed_but_undelivered_result_is_closed_once_on_scope_change(host, tmp_path):
    dialog = dialog_for(host)
    result = Owned(tmp_path / 'ready.wav')
    presented = Mock()
    assert dialog._submit(lambda: result, presented, lambda value: value.close())
    assert host.delivered.wait(5)
    assert result.path.is_file() and not result.closed.is_set()
    dialog._enable.reset_mock()
    host.library = LibraryApp(host.library.root)
    host.pump()
    presented.assert_not_called()
    dialog._enable.assert_not_called()
    dialog.status.ChangeValue.assert_not_called()
    dialog.status.SetFocus.assert_not_called()
    assert result.closed.is_set() and result.close_count == 1
    dialog.close()
    assert result.close_count == 1


def test_refused_host_submission_never_starts_a_private_executor(host, tmp_path):
    dialog = dialog_for(host)
    host.run_job = Mock(return_value=None)
    operation, presented = Mock(), Mock()
    assert dialog._submit(operation, presented, lambda value: value.close()) is False
    operation.assert_not_called(); presented.assert_not_called()
    assert not dialog.busy and not hasattr(dialog, 'executor')


def test_successful_preparation_transfers_ownership_only_to_current_view(host, tmp_path):
    dialog = dialog_for(host)
    result = Owned(tmp_path / 'accepted.wav')
    received = []
    assert dialog._submit(lambda: result, received.append, lambda value: value.close())
    host.pump()
    assert received == [result] and result.path.is_file() and result.close_count == 0
    result.close()
    dialog.close()
    assert result.close_count == 1


def test_close_uses_original_disposer_for_borrowed_resume_input(host, tmp_path):
    dialog = dialog_for(host)
    result = Owned(tmp_path / 'playing.wav')
    entered, release = Event(), Event()
    def verify():
        entered.set()
        assert release.wait(5)
        return result
    # Resume verification borrows the PCM already owned by the playing transport.
    assert dialog._submit(verify, Mock(), lambda borrowed: None)
    assert entered.wait(5)
    dialog.close()
    release.set()
    assert host.delivered.wait(5)
    host.pump()
    assert result.path.is_file() and result.close_count == 0
    result.close()


def test_failed_native_close_retains_pcm_and_quiet_until_existing_owner_reaps(host, tmp_path):
    dialog = dialog_for(host)
    release = Event()
    prepared = Owned(tmp_path / 'native-held.wav')
    session = SimpleNamespace(close=Mock(), chapter=SimpleNamespace(cues=()), guard=lambda: None)
    class Player:
        state = 'playing'
        def close(self):
            if not release.is_set():
                raise RuntimeError('Controlled native close has not acknowledged silence.')
            self.state = 'stopped'
    player = Player()
    transport = OverlayPlayback(session, player)
    transport.prepared, transport.active = prepared, True
    dialog.session, dialog.player, dialog.transport = session, player, transport
    silence = Event()
    def unregistered(player, *, silence_confirmed):
        if silence_confirmed:
            silence.set()
    dialog.progress = SimpleNamespace(unregister_player=Mock(side_effect=unregistered), refresh_audio_state=Mock())
    dialog._destroyed = True
    dialog.close(destroyed=True)
    assert dialog.closed and prepared.path.is_file() and not prepared.closed.is_set()
    assert transport.cleanup_pending and transport.observed.state == 'closing'
    owner, = dialog._cleanup_owners
    assert not owner.quiescent
    assert dialog.progress.unregister_player.call_args_list[0].kwargs == {'silence_confirmed': False}
    dialog.status.ChangeValue.assert_not_called()
    release.set()
    assert owner.quiescent_event.wait(5) and silence.wait(5)
    assert prepared.closed.is_set() and not prepared.path.exists()
    assert not transport.cleanup_pending
    assert dialog.progress.unregister_player.call_args_list[-1].kwargs == {'silence_confirmed': True}
    session.close.assert_called_once()


def test_normal_close_keeps_window_and_pcm_if_native_silence_is_unconfirmed(host, tmp_path):
    dialog = dialog_for(host)
    result = Owned(tmp_path / 'held.wav')
    release = Event()
    class Player:
        state = 'playing'
        def close(self):
            if not release.is_set():
                raise RuntimeError('Controlled unconfirmed native closure.')
            self.state = 'stopped'
    session = SimpleNamespace(close=Mock(), chapter=SimpleNamespace(cues=()), guard=lambda: None)
    player = Player(); transport = OverlayPlayback(session, player)
    transport.prepared, transport.active = result, True
    dialog.session, dialog.player, dialog.transport = session, player, transport
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    dialog.close(event)
    assert not dialog.closed and dialog._closing and result.path.is_file()
    event.Veto.assert_called_once()
    dialog.dialog.EndModal.assert_not_called()
    release.set()
    dialog.close()
    assert dialog.closed and not result.path.exists()


@pytest.mark.parametrize('change', ['unchanged', 'host', 'selection', 'closed'])
def test_recorded_modal_return_rechecks_captured_reader_before_focus(host, monkeypatch, change):
    from carr_library.ui import ebook_audio_dialog
    from carr_library.ui.reading_desk import ReadingDialog

    reading = object.__new__(ReadingDialog)
    page = SimpleNamespace(ordinal=1)
    library = host.library
    def check():
        if host.library is not library:
            raise ValueError('The captured host library changed.')
    reading.controller = SimpleNamespace(check=check, current=page, desk=object())
    reading.parent, reading.dialog = host, object()
    reading.busy = reading.closed = False
    reading.text, reading._error = Mock(), Mock()
    reading._audio_dialog = None

    class Modal:
        closed = False
        def __init__(self, parent, desk, ordinal, guard, **kwargs):
            assert kwargs['host'] is host
            guard()
        def show(self):
            if change == 'host':
                host.library = LibraryApp(host.library.root)
            elif change == 'selection':
                reading.controller.current = SimpleNamespace(ordinal=2)
            elif change == 'closed':
                reading.closed = True
            self.closed = True

    monkeypatch.setattr(ebook_audio_dialog, 'EbookAudioDialog', Modal)
    reading.open_audio()
    assert reading._audio_dialog is None
    if change == 'unchanged':
        reading.text.SetFocus.assert_called_once_with()
    else:
        reading.text.SetFocus.assert_not_called()
    reading._error.assert_not_called()
