"""Execute the composed host lifecycle methods with real serial owner cleanup.

Native window controls are explicit stand-ins here; the separate Windows probe
qualifies actual wx/Win32 destruction and global-key release.
"""
import ast
from importlib.util import find_spec
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.worker_lifetime import DesktopExecutor


def host_type(events):
    source = Path(find_spec('carr_library.ui.desktop_app').origin).read_text(encoding='utf-8')
    tree = ast.parse(source)
    original = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    names = {'on_close', '_open_data_root'}
    methods = [node for node in original.body
               if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    wrapper = ast.parse('class Host(Base):\n    pass\n')
    wrapper.body[0].body = methods
    class Base:
        def on_close(self, event):
            events.append('base-close')
            event.Skip()
    namespace = {'__name__': __name__, 'Base': Base, 'Path': Path, 'LibraryApp': LibraryApp}
    exec(compile(ast.fix_missing_locations(wrapper), '<composed desktop lifecycle>', 'exec'), namespace)
    return namespace['Host']


class CloseEvent:
    def __init__(self, can_veto=True):
        self.can_veto, self.veto, self.skipped = can_veto, False, False
    def CanVeto(self): return self.can_veto
    def GetVeto(self): return self.veto
    def Veto(self): self.veto = True
    def Skip(self): self.skipped = True


def frame(events):
    owner = host_type(events)()
    owner.working, owner._closing = False, False
    owner.cancel_event, owner.executor = Event(), DesktopExecutor()
    owner.progress_panel = SimpleNamespace(close=lambda: events.append('progress-close'))
    owner.report = Mock()
    return owner


def test_normal_host_close_releases_native_owner_before_real_worker_finalizers():
    events, thread_ids = [], []
    owner = frame(events)
    ui_thread = get_ident()
    def native_close():
        assert get_ident() == ui_thread and not owner._closing
        events.append('native-close')
    owner.desktop_integration = SimpleNamespace(close=native_close)
    owner.executor.register_cleanup(
        lambda: (events.append('worker-cleanup'), thread_ids.append(get_ident())))
    event = CloseEvent()
    try:
        owner.on_close(event)
        owner.executor.shutdown(wait=True, cancel_futures=True)
        assert event.skipped and not event.veto
        assert owner._closing and owner.cancel_event.is_set()
        assert events.index('base-close') < events.index('native-close') < events.index('worker-cleanup')
        assert 'progress-close' in events and len(thread_ids) == 1
        assert thread_ids[0] != ui_thread and not owner.executor.cleanup_failures
    finally:
        owner.executor.shutdown(wait=True, cancel_futures=True)


def test_unconfirmed_native_close_vetoes_without_stopping_worker_then_retries():
    events, attempt = [], []
    owner = frame(events)
    def native_close():
        attempt.append(True)
        if len(attempt) == 1:
            raise RuntimeError('Native key release remains unconfirmed.')
        events.append('native-close')
    owner.desktop_integration = SimpleNamespace(close=native_close)
    owner.executor.register_cleanup(lambda: events.append('worker-cleanup'))
    try:
        first = CloseEvent()
        owner.on_close(first)
        assert first.veto and not owner._closing and not owner.cancel_event.is_set()
        assert 'worker-cleanup' not in events and 'progress-close' not in events
        owner.report.assert_called_once()
        second = CloseEvent()
        owner.on_close(second)
        owner.executor.shutdown(wait=True, cancel_futures=True)
        assert not second.veto and owner._closing and len(attempt) == 2
        assert events.index('native-close') < events.index('worker-cleanup')
        assert not owner.executor.cleanup_failures
    finally:
        owner.executor.shutdown(wait=True, cancel_futures=True)


def test_forced_host_close_keeps_actual_worker_cleanup_after_native_refusal():
    events = []
    owner = frame(events)
    def native_close():
        events.append('native-refused')
        raise RuntimeError('Retained native owner requires a retry.')
    owner.desktop_integration = SimpleNamespace(close=native_close)
    owner.executor.register_cleanup(lambda: events.append('worker-cleanup'))
    try:
        event = CloseEvent(can_veto=False)
        owner.on_close(event)
        owner.executor.shutdown(wait=True, cancel_futures=True)
        assert not event.veto and owner._closing and owner.cancel_event.is_set()
        assert events.index('native-refused') < events.index('worker-cleanup')
        assert 'progress-close' in events and not owner.executor.cleanup_failures
        owner.report.assert_not_called()
    finally:
        owner.executor.shutdown(wait=True, cancel_futures=True)


def test_optional_desktop_rebind_failure_does_not_leave_host_on_old_catalog(tmp_path):
    events, reports = [], []
    old = LibraryApp(tmp_path / 'old')
    old.create_workspace('Previous library')
    target = LibraryApp(tmp_path / 'new')
    selected = target.create_workspace('New library')
    owner = host_type(events)()
    owner.library, owner.working, owner.remember_data_dir = old, False, None
    owner._confirm = lambda: True
    owner._drop = lambda: events.append('drop-old')
    owner.keyboard_commands = SimpleNamespace(change_root=lambda root: events.append('keymap-new'))
    owner.progress_panel = SimpleNamespace(change_root=lambda root: events.append('progress-new'))
    def refused_rebind():
        events.append('desktop-revoked')
        raise ValueError('Saved desktop choices are malformed.')
    owner.desktop_integration = SimpleNamespace(change_root=refused_rebind)
    owner.refresh_scopes = lambda: setattr(owner, 'scopes', [(selected, None)])
    owner.choice = SimpleNamespace(SetSelection=Mock())
    owner.open_selected = lambda: events.append('open-new')
    owner.report = reports.append
    assert owner._open_data_root(target.root)
    assert owner.library.root == target.root and owner.library is not old
    assert owner.list_scopes.__self__ is owner.library
    assert events == ['drop-old', 'keymap-new', 'progress-new', 'desktop-revoked', 'open-new']
    assert len(reports) == 1 and 'Opened library folder:' in reports[0]
    assert 'Desktop integration needs attention:' in reports[0]
