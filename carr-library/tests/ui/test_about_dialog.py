"""Real worker-thread and native-handler contracts; headless tests are not NVDA proof."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import base64
import csv
from io import StringIO
from pathlib import Path
from queue import Queue
import sys
import threading
from types import SimpleNamespace as N

import pytest

from carr_library import component_notices as inventory
from carr_library.ui import about_dialog as about


class Host:
    """The real asynchronous run_job callback contract, including its None return."""
    def __init__(self, root):
        self.library = N(root=root)
        self._closing = self.working = False
        self.cancel_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending = Queue()
        self.futures = []
        self.calls, self.messages = [], []
        self.duplicate_callback = False
        self.admission_error = None

    def report(self, text):
        self.messages.append(text)

    def run_job(self, title, operation, done=None, failed=None):
        if self.admission_error is not None:
            raise self.admission_error
        assert not self.working and not self._closing
        self.working = True
        self.cancel_event.clear()
        self.calls.append(title)
        future = self.executor.submit(operation)
        self.futures.append(future)
        def complete(result):
            def apply():
                self.working = False
                if self._closing:
                    return
                try:
                    value = result.result()
                except Exception as exc:
                    if failed is not None:
                        failed(exc)
                else:
                    if done is not None:
                        done(value)
                        if self.duplicate_callback:
                            done(value)
            self.pending.put(apply)
        future.add_done_callback(complete)
        return None

    def finish(self):
        self.pending.get(timeout=5)()

    def close(self):
        self.cancel_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)


def actual_metadata(root):
    directory = root / 'component-1.dist-info'
    directory.mkdir(parents=True)
    path = directory / 'licenses' / 'LICENSE.txt'
    path.parent.mkdir()
    text = 'Copyright Component Authors\nRead every original condition.\n'
    path.write_bytes(text.encode('utf-8'))
    (directory / 'METADATA').write_text('Metadata-Version: 2.4\nName: component\nVersion: 1\n'
        'License-File: LICENSE.txt\n\n', encoding='utf-8')
    output = StringIO(newline='')
    csv.writer(output).writerow((path.relative_to(root).as_posix(),
        'sha256=' + base64.urlsafe_b64encode(sha256(path.read_bytes()).digest()).decode().rstrip('='),
        str(path.stat().st_size)))
    (directory / 'RECORD').write_text(output.getvalue(), encoding='utf-8')
    return text


@pytest.fixture
def session(tmp_path, monkeypatch):
    original = inventory.catalogue
    text = actual_metadata(tmp_path / 'site-packages')
    main_thread, reading_threads = threading.get_ident(), []
    def catalogue(**kwargs):
        reading_threads.append(threading.get_ident())
        return original(distribution_roots=(tmp_path / 'site-packages',), bundle_root=None, **kwargs)
    monkeypatch.setattr(inventory, 'catalogue', catalogue)
    host = Host(tmp_path / 'library')
    changed, displayed, selections = [], [], []
    def choose(component, files):
        selections.append((component, files))
        return 0
    controller = about.AboutSession(host, changed=lambda value: changed.append((value.busy, value.message)),
        choose_notice=choose, display_notice=lambda title, value: displayed.append((title, value)))
    try:
        yield N(host=host, controller=controller, changed=changed, displayed=displayed,
                selections=selections, text=text, main_thread=main_thread, reading_threads=reading_threads)
    finally:
        controller.close()
        host.close()


def test_actual_metadata_and_notice_reads_use_the_existing_async_none_returning_host(session):
    assert session.controller.refresh()
    session.host.finish()
    assert session.controller.catalogue.components[0].name == 'component'
    assert session.reading_threads and session.reading_threads[0] != session.main_thread
    session.controller.read_selected()
    session.host.finish()  # Select the actual file inventory; starts the next existing-host job.
    session.host.finish()  # Read only that file and show its unchanged upstream text.
    assert len(session.host.calls) == 3
    assert len(session.selections) == 1 and len(session.displayed) == 1
    assert session.displayed[0][1].endswith(session.text)
    assert not session.controller.busy


def test_duplicate_host_callbacks_do_not_read_or_display_the_notice_twice(session):
    session.host.duplicate_callback = True
    session.controller.refresh()
    session.host.finish()
    session.controller.read_selected()
    session.host.finish()
    session.host.finish()
    assert len(session.host.calls) == 3 and len(session.displayed) == 1


@pytest.mark.parametrize('change', ['object', 'root', 'closing'])
def test_late_catalogue_is_not_displayed_in_a_replaced_or_closing_library(session, change):
    session.controller.refresh()
    session.host.futures[-1].result(timeout=5)
    if change == 'object':
        session.host.library = N(root=session.host.library.root)
    elif change == 'root':
        session.host.library.root = Path('another-library')
    else:
        session.host._closing = True
    session.host.finish()
    assert session.controller.catalogue is None and session.displayed == []


def test_close_cancels_a_real_active_worker_and_discards_its_late_callback(session, monkeypatch):
    entered = threading.Event()
    original = inventory.catalogue
    def blocked(*, cancelled):
        entered.set()
        assert session.controller.cancelled.wait(timeout=3)
        return original(cancelled=cancelled)
    monkeypatch.setattr(inventory, 'catalogue', blocked)
    session.controller.refresh()
    assert entered.wait(timeout=3)
    session.controller.close()
    session.host.finish()
    with pytest.raises(InterruptedError):
        session.host.futures[-1].result(timeout=3)
    assert session.controller.closed and not session.controller.busy
    assert session.controller.catalogue is None and session.displayed == []


@pytest.mark.parametrize('failure', ['busy', 'admission'])
def test_busy_or_failed_admission_never_starts_io_and_restores_dialog_controls(session, failure):
    if failure == 'busy':
        session.host.working = True
        assert not session.controller.refresh()
    else:
        session.host.admission_error = RuntimeError('Executor is stopping')
        assert session.controller.refresh()
    assert session.reading_threads == [] and not session.controller.busy
    assert session.changed[-1][0] is False
    assert 'running' in session.controller.message or 'Executor is stopping' in session.controller.message


def test_selected_inventory_failure_is_explicit_and_never_displays_fabricated_text(session, monkeypatch):
    session.controller.refresh()
    session.host.finish()
    def corrupt(*args, **kwargs):
        raise ValueError('Changed RECORD')
    monkeypatch.setattr(inventory, 'notices', corrupt)
    session.controller.read_selected()
    session.host.finish()
    assert not session.controller.busy and session.displayed == []
    assert 'Changed RECORD' in session.controller.message


def test_scope_changes_after_notice_picker_prevent_payload_job(session):
    session.controller.refresh()
    session.host.finish()
    def choose(component, files):
        session.host.library = N(root=Path('new-library'))
        return 0
    session.controller.choose_notice = choose
    session.controller.read_selected()
    session.host.finish()
    assert len(session.host.calls) == 2 and session.displayed == []


def menu_host():
    class Item:
        next_id = 100
        def __init__(self, label):
            self.label = label
            self.identity = Item.next_id
            Item.next_id += 1
        def GetId(self):
            return self.identity
    class Menu:
        def __init__(self):
            self.items = []
        def Append(self, identity, title):
            item = Item(title)
            self.items.append(item)
            return item
    class Bar:
        def __init__(self):
            self.menus = [('&Help', Menu()), ('S&ettings', Menu())]
        def GetMenuCount(self):
            return len(self.menus)
        def GetMenuLabel(self, index):
            return self.menus[index][0]
        def GetMenu(self, index):
            return self.menus[index][1]
    bar, bindings = Bar(), []
    return N(GetMenuBar=lambda: bar, Bind=lambda *args: bindings.append(args)), bar, bindings


@pytest.mark.parametrize('keyboard_ready', [False, True])
def test_help_installer_registers_one_semantic_command_before_or_after_keyboard_manager(monkeypatch, keyboard_ready):
    host, bar, bindings = menu_host()
    registered = []
    if keyboard_ready:
        def register(spec, item, *, fallback_unassigned=False):
            assert fallback_unassigned is False
            registered.append((spec, item))
        host.keyboard_commands = N(register_menu_command=register)
    calls = []
    monkeypatch.setitem(sys.modules, 'wx', N(ID_ANY=-1, EVT_MENU='menu'))
    monkeypatch.setattr(about, 'show_about', lambda frame, event: calls.append((frame, event)))
    about.install_about_actions(host)
    about.install_about_actions(host)
    pending = registered if keyboard_ready else host._pending_keyboard_commands
    assert len(pending) == len(bindings) == len(bar.GetMenu(0).items) == 1
    assert pending[0][0].command_id == 'help.about_components'
    assert 'About Resonance' in bar.GetMenu(0).items[0].label
    assert bar.GetMenu(1).items == []
    marker = object()
    bindings[0][1](marker)
    assert calls == [(host, marker)]


def test_missing_help_host_is_rejected_before_menu_or_binding_mutation(monkeypatch):
    host, bar, bindings = menu_host()
    bar.menus = [('S&ettings', bar.GetMenu(1))]
    monkeypatch.setitem(sys.modules, 'wx', N(ID_ANY=-1, EVT_MENU='menu'))
    with pytest.raises(ValueError, match='existing Help'):
        about.install_about_actions(host)
    assert bindings == [] and bar.GetMenu(0).items == []
    assert not hasattr(host, '_pending_keyboard_commands')


def test_native_control_names_readonly_text_and_close_cancel_use_actual_dialog_handlers(monkeypatch, tmp_path):
    class Widget:
        def __init__(self, parent=None, identity=None, label='', value='', choices=(), **kwargs):
            self.parent, self.value, self.choices = parent, value, list(choices)
            self.name, self.bindings, self.enabled = '', {}, True
            self.selection = -1
            self.style = kwargs.get('style', 0)
        def SetName(self, name): self.name = name
        def Bind(self, event, handler): self.bindings[event] = handler
        def SetValue(self, value): self.value = value
        def Set(self, values): self.choices = list(values)
        def SetSelection(self, value): self.selection = value
        def GetSelection(self): return self.selection
        def Enable(self, enabled=True): self.enabled = enabled
        def SetFocus(self): pass
        def SetSizer(self, sizer): pass
        def SetEscapeId(self, identity): self.escape = identity
        def IsModal(self): return True
        def EndModal(self, identity): self.ended = identity
        def Destroy(self): self.destroyed = True
    class Sizer:
        def __init__(self, *args): pass
        def Add(self, *args): pass
    wx = N(Dialog=Widget, TextCtrl=Widget, Choice=Widget, StaticText=Widget, Button=Widget,
        BoxSizer=Sizer, DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, TE_MULTILINE=4, TE_READONLY=8,
        VERTICAL=1, HORIZONTAL=2, EXPAND=4, ALL=8, ALIGN_RIGHT=16, ID_CANCEL=2,
        EVT_CHOICE='choice', EVT_BUTTON='button', EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    host = Host(tmp_path)
    try:
        dialog = about.create_about_dialog(host)
        assert dialog.about.style & wx.TE_READONLY and dialog.details.style & wx.TE_READONLY
        assert dialog.about.name == 'About Resonance, version and notice availability'
        assert dialog.components.name == 'Installed and delivered component'
        assert dialog.read_button.name == 'Read selected component upstream notices'
        assert dialog.close_button.name == 'Close About Resonance'
        assert not dialog.read_button.enabled and dialog.refresh_button.enabled
        dialog.close_button.bindings['button'](None)
        assert dialog.session.cancelled.is_set() and dialog.session.closed
        assert dialog.ended == wx.ID_CANCEL and not dialog.alive
    finally:
        host.close()
