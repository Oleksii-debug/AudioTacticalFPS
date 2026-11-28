"""Real file snapshots across a controlled native overwrite confirmation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.research.export_file import capture_export_destination, write_binary_export, write_export
from carr_library.ui.export_destination import confirm_export_destination


@pytest.fixture
def native():
    wx = SimpleNamespace(YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, YES=8, NO=16)
    wx.MessageBox = Mock(return_value=wx.YES)
    return wx


def test_new_destination_needs_no_overwrite_confirmation(tmp_path, native):
    target = tmp_path / 'new.txt'
    captured = capture_export_destination(target, private_root=tmp_path / 'private')
    approval = confirm_export_destination(object(), captured, wx_module=native)
    assert approval is captured
    assert approval.require_absent
    native.MessageBox.assert_not_called()
    assert not target.exists()


def test_declined_confirmation_preserves_existing_bytes(tmp_path, native):
    target = tmp_path / 'previous.txt'
    target.write_bytes(b'Previous independent work')
    native.MessageBox.return_value = native.NO
    parent = object()
    captured = capture_export_destination(target, private_root=tmp_path / 'private')
    assert confirm_export_destination(parent, captured, wx_module=native) is None
    message, title, style, owner = native.MessageBox.call_args.args
    assert target.name in message and title == 'Replace export' and owner is parent
    assert style & native.NO_DEFAULT and style & native.YES_NO
    assert target.read_bytes() == b'Previous independent work'
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_file_changed_while_confirmation_is_open_is_not_approved(tmp_path, native, binary):
    target = tmp_path / 'selected'
    target.write_bytes(b'File inspected before confirmation')
    private = tmp_path / 'private'

    def confirm(*args):
        target.write_bytes(b'Independent edit during confirmation')
        return native.YES

    native.MessageBox.side_effect = confirm
    captured = capture_export_destination(target, private_root=private)
    approval = confirm_export_destination(object(), captured, wx_module=native)
    assert approval is captured
    writer = write_binary_export if binary else write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new export' if binary else 'new export',
               private_root=private, approval=approval)
    assert target.read_bytes() == b'Independent edit during confirmation'
    assert not list(tmp_path.glob('.research-export-*'))


def test_private_destination_is_rejected_before_confirmation(tmp_path, native):
    private = tmp_path / 'private'
    private.mkdir()
    target = private / 'original.txt'
    target.write_bytes(b'Original source')
    with pytest.raises(ValueError, match='outside the private library'):
        captured = capture_export_destination(target, private_root=private)
        confirm_export_destination(object(), captured, wx_module=native)
    native.MessageBox.assert_not_called()
    assert target.read_bytes() == b'Original source'


def test_confirmation_requires_a_snapshot_and_performs_no_filesystem_read(tmp_path, native, monkeypatch):
    from pathlib import Path
    target = tmp_path / 'selected.txt'
    target.write_bytes(b'Inspected existing bytes')
    captured = capture_export_destination(target, private_root=tmp_path / 'private')
    def forbidden(*args, **kwargs):
        raise AssertionError('Native confirmation must not inspect the filesystem.')
    with monkeypatch.context() as patches:
        patches.setattr(Path, 'open', forbidden)
        patches.setattr(Path, 'stat', forbidden)
        patches.setattr(Path, 'lstat', forbidden)
        assert confirm_export_destination(object(), captured, wx_module=native) is captured
        with pytest.raises(ValueError, match='existing worker'):
            confirm_export_destination(object(), target, wx_module=native)
    assert target.read_bytes() == b'Inspected existing bytes'


class _QueuedFrame:
    """The shared dispatcher contract with worker/UI completion kept separate."""
    def __init__(self, executor, thread):
        from threading import Event
        self.executor = executor
        self.main_thread = thread
        self.cancel_event = Event()
        self._closing = False
        self.scope_valid = True
        self.working = False
        self.pending = []
        self.started = []
        self.results = []
        self.errors = []

    def run_job(self, title, operation, done=None, failed=None):
        from threading import get_ident
        assert get_ident() == self.main_thread
        assert not self.working
        self.working = True
        self.cancel_event.clear()
        self.started.append(title)
        self.pending.append((self.executor.submit(operation), done, failed))

    def finish(self, *, one=False):
        from threading import get_ident
        assert get_ident() == self.main_thread
        while self.pending:
            future, done, failed = self.pending.pop(0)
            try:
                result = future.result(timeout=10)
                self.working = False
                if not self._closing and done is not None:
                    done(result)
            except Exception as error:
                self.working = False
                if not self._closing:
                    if failed is None:
                        raise
                    failed(error)
            if one:
                break

    def check_scope(self):
        if not self.scope_valid:
            raise ValueError('The selected export scope changed.')


def _start(frame, target, native):
    from carr_library.ui.export_destination import start_export
    writes = []
    def publish(approval, cancelled, check_live):
        from threading import get_ident
        assert get_ident() != frame.main_thread
        check_live()
        writes.append(approval)
        return write_export(target, 'Verified new content', private_root=target.parent / 'private',
                            approval=approval, cancelled=cancelled)
    start_export(frame, target, private_root=target.parent / 'private',
                 check_scope=frame.check_scope, operation=publish, done=frame.results.append,
                 title='Publishing chosen export', failed=frame.errors.append, wx_module=native)
    return writes


def test_delayed_capture_runs_only_on_worker_and_keeps_ui_confirmation_separate(
        tmp_path, native, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident
    from carr_library.ui import export_destination

    target = tmp_path / 'selected.txt'
    target.write_bytes(b'Previous exact content')
    entered, release = Event(), Event()
    captured = []
    original = export_destination.capture_export_destination
    main_thread = get_ident()
    def delayed(*args, **kwargs):
        assert get_ident() != main_thread
        entered.set()
        assert release.wait(10), 'The UI did not release the bounded capture barrier.'
        result = original(*args, **kwargs)
        captured.append(result)
        return result
    def confirm(*args):
        assert get_ident() == main_thread
        assert target.read_bytes() == b'Previous exact content'
        return native.YES
    native.MessageBox.side_effect = confirm
    monkeypatch.setattr(export_destination, 'capture_export_destination', delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        frame = _QueuedFrame(executor, main_thread)
        try:
            writes = _start(frame, target, native)
            assert entered.wait(10)
            # start_export already returned to this caller while capture waits.
            assert frame.working and frame.results == [] and writes == []
            native.MessageBox.assert_not_called()
        finally:
            release.set()
        frame.finish(one=True)
        native.MessageBox.assert_called_once()
        assert frame.started == ['Checking export destination', 'Publishing chosen export']
        # The publication worker may already be running. Its old-byte check
        # belongs to confirm(), before it can be dispatched, not to this race.
        frame.finish()
    assert frame.errors == [] and len(frame.results) == 1
    assert writes == captured and writes[0] is captured[0]
    assert target.read_bytes() == b'Verified new content'


@pytest.mark.parametrize('change', ['cancel', 'close'], ids=str)
def test_cancel_or_close_during_delayed_capture_never_opens_confirmation(
        tmp_path, native, monkeypatch, change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident
    from carr_library.ui import export_destination

    target = tmp_path / 'selected.txt'
    target.write_bytes(b'Keep independent work')
    entered, release = Event(), Event()
    original = export_destination.capture_export_destination
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(export_destination, 'capture_export_destination', delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        frame = _QueuedFrame(executor, get_ident())
        try:
            writes = _start(frame, target, native)
            assert entered.wait(10)
            if change == 'cancel':
                frame.cancel_event.set()
            else:
                frame._closing = True
        finally:
            release.set()
        frame.finish()
    native.MessageBox.assert_not_called()
    assert writes == frame.results == []
    assert frame.started == ['Checking export destination']
    assert target.read_bytes() == b'Keep independent work'
    if change == 'cancel':
        assert len(frame.errors) == 1 and isinstance(frame.errors[0], InterruptedError)


@pytest.mark.parametrize('change', ['cancel', 'close', 'scope', 'file'], ids=str)
def test_modal_changes_cannot_gain_fresh_authority_from_the_second_job(tmp_path, native, change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import get_ident
    target = tmp_path / 'selected.txt'
    target.write_bytes(b'Captured old content')
    with ThreadPoolExecutor(max_workers=1) as executor:
        frame = _QueuedFrame(executor, get_ident())
        def confirm(*args):
            assert get_ident() == frame.main_thread
            if change == 'cancel':
                frame.cancel_event.set()
            elif change == 'close':
                frame._closing = True
            elif change == 'scope':
                frame.scope_valid = False
            else:
                target.write_bytes(b'Independent changed content')
            return native.YES
        native.MessageBox.side_effect = confirm
        writes = _start(frame, target, native)
        frame.finish()
    native.MessageBox.assert_called_once()
    assert frame.results == []
    if change == 'file':
        assert len(writes) == 1
        assert len(frame.errors) == 1 and 'destination changed' in str(frame.errors[0])
        assert target.read_bytes() == b'Independent changed content'
    else:
        assert writes == []
        assert frame.started == ['Checking export destination']
        assert target.read_bytes() == b'Captured old content'
    if change == 'cancel':
        assert frame.cancel_event.is_set()
    if change == 'close':
        assert frame.errors == []  # No error callback touches a destroyed UI.


def test_real_timeout_is_not_relabelled_as_user_cancellation(tmp_path, native, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import get_ident
    from carr_library.ui import export_destination
    target = tmp_path / 'target'
    def timed_out(*args, **kwargs):
        raise TimeoutError('Independent storage deadline elapsed.')
    monkeypatch.setattr(export_destination, 'capture_export_destination', timed_out)
    with ThreadPoolExecutor(max_workers=1) as executor:
        frame = _QueuedFrame(executor, get_ident())
        writes = _start(frame, target, native)
        frame.finish()
    assert len(frame.errors) == 1 and type(frame.errors[0]) is TimeoutError
    assert not frame.cancel_event.is_set() and writes == []
    native.MessageBox.assert_not_called()
    assert not target.exists()
