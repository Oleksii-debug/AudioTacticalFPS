"""Actual wx desktop/snapshot events with synthetic local data, never NVDA claims.

Installed wheel: python -I tests/ui/probe_portable_sync_native.py --native
Checkout debugging: add --source /absolute/path/to/carr-library
Add --encrypted to exercise real encrypted publication and password re-entry.
Missing native prerequisites return NOT_RUN (77), not PASS.
"""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic


def not_run(reason):
    print(json.dumps(dict(status='NOT_RUN', reason=reason, nvda='NOT_RUN')), flush=True)
    return 77


class WindowsConfirmations:
    """Click only this process's owned native message/task dialog buttons.

    wxMSW MessageDialog does not run wx.Dialog's modal loop. IsModal/EndModal
    cannot be used to discover or dismiss its TaskDialogIndirect window.
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.types = ctypes, wintypes
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        declarations = {
            'EnumWindows': ([self.callback, wintypes.LPARAM], wintypes.BOOL),
            'EnumChildWindows': ([wintypes.HWND, self.callback, wintypes.LPARAM], wintypes.BOOL),
            'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetWindow': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            'GetDlgCtrlID': ([wintypes.HWND], ctypes.c_int),
            'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
            'IsWindowEnabled': ([wintypes.HWND], wintypes.BOOL),
            'SetActiveWindow': ([wintypes.HWND], wintypes.HWND),
            'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    def _string(self, function, window):
        value = self.ctypes.create_unicode_buffer(2048)
        function(window, value, len(value))
        return value.value

    def reply(self, owner, title, *, accept):
        candidates = []

        @self.callback
        def visit(window, unused):
            pid = self.types.DWORD()
            self.api.GetWindowThreadProcessId(window, self.ctypes.byref(pid))
            if (pid.value != os.getpid() or not self.api.IsWindowVisible(window)
                    or self._string(self.api.GetClassNameW, window) != '#32770'
                    or self._string(self.api.GetWindowTextW, window) != title):
                return True
            chain, current = [], self.api.GetWindow(window, 4)  # GW_OWNER
            while current and current not in chain and len(chain) < 32:
                chain.append(current)
                if current == owner:
                    candidates.append((window, chain))
                    break
                current = self.api.GetWindow(current, 4)
            return True

        self.api.EnumWindows(visit, 0)
        if not candidates:
            return False
        if len(candidates) != 1:
            raise AssertionError('Ambiguous owned Windows confirmation dialog')
        window, chain = candidates[0]
        identifier = 6 if accept else 7  # Native IDYES/IDNO, not wx.ID_YES.
        buttons = []

        @self.callback
        def child(control, unused):
            if (self.api.GetDlgCtrlID(control) == identifier
                    and self._string(self.api.GetClassNameW, control).casefold() == 'button'
                    and self.api.IsWindowEnabled(control) and self.api.IsWindowVisible(control)):
                buttons.append(control)
            return True

        self.api.EnumChildWindows(window, child, 0)
        if len(buttons) != 1:
            raise AssertionError('The owned confirmation has no unique native answer button')
        print(json.dumps(dict(status='NATIVE_DIALOG', pid=os.getpid(), hwnd=window,
                              owner_chain=chain, title=title, button=buttons[0], button_id=identifier)), flush=True)
        self.api.SetActiveWindow(window)
        # BM_CLICK sends actual native button notifications. It neither replaces
        # ShowModal nor changes its result artificially.
        if not self.api.PostMessageW(buttons[0], 0x00F5, 0, 0):
            raise AssertionError('Could not dispatch the owned native answer button')
        return True


def run_native(source=None, *, encrypted=False):
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('A native display is unavailable.')
    try:
        import wx
    except (ImportError, OSError):
        return not_run('Native wxPython is unavailable.')
    if source is not None:
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'src'))
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import portable_sync
    from carr_library.sync import service as service_module

    try:
        application = wx.App(False)
    except (Exception, SystemExit):
        return not_run('A native wx application could not open.')
    checks, errors = [], []
    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (portable_sync, service_module)}
    timers = []
    state = {'finished': False}
    windows = WindowsConfirmations() if sys.platform == 'win32' else None
    confirmation_titles = ('Publish all saved libraries in this data folder?',
                           'Restore the verified snapshot into a new folder?')

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def pump(predicate, timeout=20):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            application.Yield()
            if errors:
                raise AssertionError(errors[0])
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    checkout = Path(__file__).resolve().parents[2]
    if source is None:
        check('modules are loaded from the installed distribution',
              all(not Path(path).is_relative_to(checkout) for path in origins.values()))

    with TemporaryDirectory(prefix='carr-native-portable-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native snapshot current library')
        archived = library.create_workspace('Native snapshot archived library')
        library.add_text(workspace, 'Synthetic portable source', 'Exact synthetic source for portable transfer.')
        library.archive_workspace(archived)
        frame = DesktopFrame(library)
        frame.Show()
        actions = portable_sync.install_portable_sync_actions(frame)
        active, notebook = frame.active, frame.book
        native = None
        release = Event()

        def descendant(window, parent):
            current = window.GetParent()
            while current is not None:
                if current is parent:
                    return True
                current = current.GetParent()
            return False

        def dismiss_one_modal():
            if windows is not None and native is not None and native._window_alive:
                for title in confirmation_titles:
                    if windows.reply(native.window.GetHandle(), title, accept=False):
                        return True
            modals = []
            for window in wx.GetTopLevelWindows():
                try:
                    if isinstance(window, wx.Dialog) and window.IsModal() and descendant(window, frame):
                        depth, current = 0, window
                        while current is not frame:
                            depth += 1
                            current = current.GetParent()
                        modals.append((depth, window))
                except RuntimeError:
                    continue
            if modals:
                max(modals, key=lambda item: item[0])[1].EndModal(wx.ID_CANCEL)
                return True
            return False

        def answer_modals(responses):
            responses = list(responses)
            deadline = monotonic() + 12

            def answer():
                if state['finished'] or not responses:
                    return
                try:
                    title, password = responses[0]
                    if windows is not None and title in confirmation_titles:
                        if windows.reply(native.window.GetHandle(), title, accept=True):
                            responses.pop(0)
                            if responses:
                                timers.append(wx.CallLater(20, answer))
                        elif monotonic() >= deadline:
                            raise AssertionError('Expected owned Windows confirmation was not shown')
                        else:
                            timers.append(wx.CallLater(20, answer))
                        return
                    dialog = next((window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.Dialog) and window.IsModal()
                        and window.GetTitle() == title and descendant(window, native.window)), None)
                    if dialog is None:
                        if monotonic() >= deadline:
                            raise AssertionError('Expected native dialog was not shown: ' + title)
                        timers.append(wx.CallLater(20, answer))
                        return
                    if title == 'Snapshot password':
                        fields = [child for child in dialog.GetChildren() if isinstance(child, wx.TextCtrl)]
                        check('password entry uses named native masked controls', bool(fields) and all(
                            field.GetWindowStyleFlag() & wx.TE_PASSWORD and field.GetName() for field in fields))
                        for field in fields:
                            field.SetValue(password or '')
                        dialog.EndModal(wx.ID_OK)
                    else:
                        dialog.EndModal(wx.ID_YES)
                    responses.pop(0)
                    if responses:
                        timers.append(wx.CallLater(20, answer))
                except BaseException as error:
                    errors.append(type(error).__name__ + ': native modal interaction failed')
                    dismiss_one_modal()

            timers.append(wx.CallLater(20, answer))

        def click(button):
            check('native command is enabled: ' + button.GetLabel(), button.IsEnabled())
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native button event: ' + button.GetLabel(), button.GetEventHandler().ProcessEvent(event))
            if errors:
                raise AssertionError(errors[0])

        def finish_job():
            check('snapshot worker finishes through the event loop', pump(lambda: not native.controller.running))

        password = 'Synthetic native snapshot password!' if encrypted else None
        try:
            library_menu = next(frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                                if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Library')
            item = next(item for item in library_menu.GetMenuItems()
                        if item.GetItemLabelText() == 'Portable snapshots and transfer…')
            event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
            check('real desktop Library menu opens the snapshot window', frame.GetEventHandler().ProcessEvent(event))
            native = actions.window
            check('snapshot window is modeless and parent-owned', native is not None
                  and native.window.GetParent() is frame and not native.window.IsModal())
            check('paths, code, history, preview and status have accessible names', all(control.GetName()
                  for control in (native.folder, native.token, native.destination, native.history,
                                  native.preview, native.status)))
            native.folder.SetValue(str(root / 'portable'))
            native.label.SetValue('Native portable checkpoint')
            native.encrypt.SetValue(encrypted)
            click(native.refresh_button)
            finish_job()
            dialogs = [('Publish all saved libraries in this data folder?', None)]
            if encrypted:
                dialogs.append(('Snapshot password', password))
            answer_modals(dialogs)
            click(native.publish_button)
            finish_job()
            token = native.controller.expected_head
            check('real service publishes a transfer receipt', isinstance(token, str) and len(token) == 64
                  and token in native.preview.GetValue())
            check('publication and listing never autofill the trust code', native.token.GetValue() == '')
            native.token.SetValue(token)  # Explicitly enter the separately retained publication receipt.
            answer_modals([('Snapshot password', password)])
            click(native.verify_button)
            finish_job()
            check('verified scope includes the archived library', native.controller.inspection is not None
                  and 'Native snapshot archived library' in native.preview.GetValue())
            check('restore becomes available only after verification', native.restore_button.IsEnabled())
            native.destination.SetValue(str(root / 'restored'))
            dialogs = [('Restore the verified snapshot into a new folder?', None)]
            if encrypted:
                dialogs.append(('Snapshot password', password))
            answer_modals(dialogs)
            click(native.restore_button)
            finish_job()
            check('real verified library exists at the new destination', (root / 'restored' / 'library.sqlite').is_file())
            restored = LibraryApp(root / 'restored')
            check('restored library retains archived scope', len(restored.workspaces(include_archived=True)) == 2)
            check('current library and notebook never switch', frame.library is library
                  and frame.active == active and frame.book is notebook)
            check('main frame stays available', frame.choice.IsEnabled() and frame.book.IsEnabled() and not frame.working)

            # A deterministic pause before the real service establishes the
            # cancellation boundary; no synthetic result is ever returned.
            entered = Event()
            real_inspect = native.controller.service.inspect

            def held_inspect(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('Native cancellation probe timed out')
                if kwargs['cancelled']():
                    raise InterruptedError('Native cancellation probe')
                return real_inspect(*args, **kwargs)

            native.controller.service.inspect = held_inspect
            answer_modals([('Snapshot password', password)])
            click(native.verify_button)
            check('worker reaches cancellation boundary', pump(entered.is_set))
            click(native.cancel_button)
            release.set()
            finish_job()
            check('cancelled work never accepts a verification preview', native.controller.inspection is None)

            release.clear()
            entered.clear()
            answer_modals([('Snapshot password', password)])
            click(native.verify_button)
            check('second worker reaches close boundary', pump(entered.is_set))
            click(native.close_button)
            check('normal close waits for worker cancellation', native._window_alive and native.controller.running
                  and native.controller.cancel_event.is_set())
            release.set()
            check('window closes after safe worker completion', pump(lambda: not native._window_alive))
            check('parent drops the closed modeless window', actions.window is None)
        finally:
            state['finished'] = True
            for timer in timers:
                timer.Stop()
            while dismiss_one_modal():
                application.Yield()
            release.set()
            if native is not None:
                native.controller.cancel_event.set()
                deadline = monotonic() + 10
                while native.controller.running and monotonic() < deadline:
                    application.Yield()
                    wx.MilliSleep(10)
                native.close_window()
            frame.Close()
            application.Yield()
            password = None
    print(json.dumps(dict(status='PASS', checks=checks, python=platform.python_version(), wx=wx.version(),
                          encrypted=encrypted, modules=origins, nvda='NOT_RUN')), flush=True)
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--encrypted', action='store_true')
    args = parser.parse_args()
    if not args.native:
        return not_run('Pass --native to request actual native execution.')
    finished = Event()

    def watchdog():
        if not finished.wait(60):
            print(json.dumps(dict(status='FAIL', error='Native probe exceeded its 60-second watchdog',
                                  nvda='NOT_RUN')), flush=True)
            os._exit(1)  # Standalone synthetic probe only; never terminate production.

    Thread(target=watchdog, name='native-probe-watchdog', daemon=True).start()
    try:
        return run_native(args.source.resolve() if args.source else None, encrypted=args.encrypted)
    except BaseException as error:
        # Synthetic passwords must not appear in diagnostics, even on failure.
        print(json.dumps(dict(status='FAIL', error=type(error).__name__, nvda='NOT_RUN')), flush=True)
        return 1
    finally:
        finished.set()


if __name__ == '__main__':
    raise SystemExit(main())
