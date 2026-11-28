"""Real configured updater A-to-B acceptance in an isolated Windows account.

A and B are exact compiled trusted-client distributions. Synthetic publisher
keys and one temporary Windows-vault anchor authorize only this owned fixture.
The current installed entrypoint, actual wx controls, separate offline host,
standard setup, library backup and checked restart all execute unchanged.
Nothing here qualifies a production publisher, private rollout or NVDA.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid

from carr_library.security._files import TrustError, file_info, json_bytes, json_value, read_bytes

HERE = Path(__file__).resolve().parent
# Failed native ownership survives this function until the enclosing Job exits.
_RETAINED_NATIVE_FAILURES = []
ORIGINS = (
    'carr_library.__main__', 'carr_library.application',
    'carr_library.installation_lease', 'carr_library.native_runtime',
    'carr_library.security.controller', 'carr_library.security.update_runtime',
    'carr_library.security.update_handoff', 'carr_library.security.update_host',
    'carr_library.security.updates', 'carr_library.security.managed_installation',
    'carr_library.security.installation', 'carr_library.security.anchors',
    'carr_library.ui.desktop_app', 'carr_library.ui.trust_status',
    'carr_library.ui.worker_lifetime',
)
UI_CHECKS = (
    'actual installed A entrypoint owns the selected library and explicit recipe',
    'actual native updater command is registered exactly once',
    'offline configuration grants no idle network permission',
    'preparation question defaults to the actual owned No button',
    'observed No creates no host or configuration',
    'explicit native Yes prepares the separate exact A runtime',
    'prepared controller retains original library and requires explicit review',
    'native signed release review has exact product build source and target',
    'downloaded target is verified before the native Apply review',
    'Apply opens with restart unchecked and Cancel as default',
    'native Cancel leaves A open and creates no handoff',
    'explicit native Space selects the optional checked restart',
    'actual Close precedes the exact durable COMMIT',
    'source identity remains exact through the real application lease',
    'actual entrypoint returns after normal Close and shared worker settlement',
)
PARENT_CHECKS = (
    'both compiled source receipts and full owned A payload are exact',
    'temporary actual Windows vault anchor begins at A',
    'real separate host records completed B and protected build two',
    'complete prior A wrapper and exact original research are retained',
    'actual backup restores the original synthetic research',
    'restarted process and window belong to the retained Windows Job',
    'restarted actual installed entrypoint retains the configured Apply route',
    'restarted actual application holds the installation lease',
    'normal restarted Close settles the complete native process family',
    'actual standard uninstaller leaves research and prior recovery intact',
)
NOT_RUN = {
    'production_publisher': 'NOT_CONFIGURED',
    'production_rollout': 'NOT_RUN',
    'real_user_installation': 'NOT_RUN',
    'authenticode': 'NOT_CLAIMED',
    'nvda': 'NOT_RUN',
    'physical_audio': 'NOT_RUN',
}


def digest(path):
    with Path(path).open('rb') as stream:
        from hashlib import file_digest
        return file_digest(stream, 'sha256').hexdigest()


def output(path, value):
    raw = json_bytes(value)
    if len(raw) > 65536:
        raise RuntimeError('Configured update evidence exceeds 64 KiB.')
    with Path(path).open('xb') as stream:
        stream.write(raw + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class Windows:
    """Only bounded, PID/job-bound native observations and ordinary messages."""
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        ptr = ctypes.c_void_p
        self._bind(self.user, {
            'EnumWindows': ([self.callback, wintypes.LPARAM], wintypes.BOOL),
            'EnumChildWindows': ([wintypes.HWND, self.callback, wintypes.LPARAM], wintypes.BOOL),
            'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
            'IsWindowEnabled': ([wintypes.HWND], wintypes.BOOL),
            'GetWindow': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            'GetForegroundWindow': ([], wintypes.HWND),
            'SetForegroundWindow': ([wintypes.HWND], wintypes.BOOL),
            'SetActiveWindow': ([wintypes.HWND], wintypes.HWND),
            'GetFocus': ([], wintypes.HWND),
            'SetFocus': ([wintypes.HWND], wintypes.HWND),
            'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            'SendMessageTimeoutW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
            'GetMenu': ([wintypes.HWND], ptr),
            'GetSubMenu': ([ptr, ctypes.c_int], ptr),
            'GetMenuItemCount': ([ptr], ctypes.c_int),
            'GetMenuItemID': ([ptr, ctypes.c_int], wintypes.UINT),
            'GetMenuStringW': ([ptr, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT], ctypes.c_int),
        })
        self._bind(self.kernel, {
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'IsProcessInJob': ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        })

    @staticmethod
    def _bind(library, table):
        for name, (arguments, result) in table.items():
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def pid(self, handle):
        value = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.value

    def text(self, handle, *, content=False):
        buffer = ctypes.create_unicode_buffer(16385 if content else 513)
        if content:
            result = ctypes.c_size_t()
            ctypes.set_last_error(0)
            if not self.user.SendMessageTimeoutW(handle, 0x000D, len(buffer),
                    ctypes.addressof(buffer), 0x0002 | 0x0020, 1000, ctypes.byref(result)):
                raise RuntimeError('The exact owned native text did not respond within one second.')
            if result.value >= len(buffer) - 1:
                raise RuntimeError('The exact owned native text exceeded its observation bound.')
        else:
            self.user.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    def windows(self, parent=None):
        found, failure = [], []
        @self.callback
        def visit(handle, parameter):
            if len(found) >= 256:
                failure.append(True)
                return False
            found.append(int(handle))
            return True
        if parent is None:
            self.user.EnumWindows(visit, 0)
        else:
            self.user.EnumChildWindows(parent, visit, 0)
        if failure:
            raise RuntimeError('Native window inventory exceeds its fixed bound.')
        return found

    def post(self, handle, message, wparam=0, lparam=0):
        if not self.user.IsWindow(handle) or not self.user.PostMessageW(handle, message, wparam, lparam):
            raise RuntimeError('The exact owned native window refused its ordinary message.')

    def process(self, pid, job):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            member = wintypes.BOOL()
            if not self.kernel.IsProcessInJob(handle, job, ctypes.byref(member)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not member.value:
                return None
            buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return Path(buffer.value).resolve(strict=True)
        finally:
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def question_button(self, question, caption):
        expected = caption.casefold()
        matches = []
        for handle in self.windows(question):
            buffer = ctypes.create_unicode_buffer(64)
            self.user.GetClassNameW(handle, buffer, len(buffer))
            if (self.pid(handle) == os.getpid() and buffer.value.casefold() == 'button'
                    and self.user.IsWindowVisible(handle) and self.user.IsWindowEnabled(handle)
                    and self.text(handle).replace('&', '').strip().casefold() == expected):
                matches.append(handle)
        if len(matches) != 1:
            raise RuntimeError('The owned native question has no unique actual answer button.')
        return matches[0]

    def update_menu(self, frame):
        menu, matches = self.user.GetMenu(frame), []
        count = self.user.GetMenuItemCount(menu)
        if count < 1 or count > 32:
            raise RuntimeError('The restarted native menu is not bounded.')
        for top in range(count):
            child = self.user.GetSubMenu(menu, top)
            length = self.user.GetMenuItemCount(child)
            if length < 0 or length > 256:
                raise RuntimeError('The restarted native submenu is not bounded.')
            for index in range(length):
                text = ctypes.create_unicode_buffer(513)
                self.user.GetMenuStringW(child, index, text, len(text), 0x400)
                if text.value.replace('&', '').split('\t', 1)[0] == 'Check for updates / Update':
                    identity = self.user.GetMenuItemID(child, index)
                    if identity == 0xFFFFFFFF:
                        raise RuntimeError('The restarted update command is not an ordinary menu entry.')
                    matches.append(identity)
        if len(matches) != 1:
            raise RuntimeError('The restarted actual update command is missing or ambiguous.')
        return matches[0]


def wait(predicate, message, seconds, *, alive=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if alive is not None and not alive():
            raise RuntimeError(message + ' The required native owner exited.')
        time.sleep(.05)
    raise TimeoutError(message)


def assert_checks(values, expected):
    if type(values) is not list or len(values) != len(expected) or len(set(values)) != len(values):
        raise RuntimeError('Native configured-update check inventory is incomplete or duplicated.')
    if set(values) != set(expected):
        raise RuntimeError('Native configured-update check inventory differs from its required contract.')


def ui_child(plan_path, expected_plan, probe_sha256):
    """Run the real installed A main; observe existing methods without changing them."""
    if os.name != 'nt' or digest(__file__) != probe_sha256:
        raise RuntimeError('Use this exact probe through the actual installed Windows interpreter.')
    plan_raw = read_bytes(plan_path, 65536)
    if sha256(plan_raw).hexdigest() != expected_plan:
        raise RuntimeError('The independently supplied native plan changed.')
    plan = json_value(plan_raw, maximum=65536)
    root, library, repository = (Path(plan[name]).resolve(strict=True)
                                 for name in ('installation', 'library', 'repository'))
    if Path(sys.executable).resolve(strict=True) != (root / 'app/.venv/Scripts/python.exe').resolve(strict=True):
        raise RuntimeError('The native UI is not running the selected installed A interpreter.')
    if Path(os.environ.get('CARR_PROGRAM_DIR', '')).resolve(strict=True) != root / 'app':
        raise RuntimeError('The native entrypoint has no exact installed program scope.')
    raw_owner = read_bytes(root / 'owned-files.json', 32 * 1024**2)
    if sha256(raw_owner).hexdigest() != plan['owner_sha256']:
        raise RuntimeError('Installed A ownership changed before the native entrypoint.')
    owner = json_value(raw_owner, maximum=32 * 1024**2)
    from carr_library.native_runtime import activate_bundled_runtime
    from carr_library.installation_lease import application_lease
    from unittest.mock import patch
    native_runtime = activate_bundled_runtime()
    import wx
    from carr_library import __main__ as entry
    from carr_library.ui import desktop_app, trust_status
    from carr_library.security import update_handoff

    api, checks, errors, timers, pressed = Windows(), [], [], [], set()
    captured, frames, menu_items, commits = {}, [], [], []
    operation_errors, operations = [], []
    original_init = desktop_app.DesktopFrame.__init__
    original_commit = update_handoff.UpdateHandoff.commit_after_close
    origins = {}
    stop_watchdog = threading.Event()
    deadline = time.monotonic() + 2700
    cleanup_deadline = None
    state = {'iterator': None, 'pending': None, 'until': 0, 'ended': False}
    owned_modals, requested_modals = [], set()
    unwind_timer = None
    patches = ExitStack()
    main_result = None

    def check(name, condition):
        if name not in UI_CHECKS or name in checks or not condition:
            raise AssertionError(name)
        checks.append(name)
        print('CHECK: ' + name, flush=True)

    def source_identity():
        actual = {}
        for name in ORIGINS:
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve(strict=True)
            relative = path.relative_to(root / 'app').as_posix()
            file_info(path)
            if not relative.startswith('.venv/Lib/site-packages/carr_library/'):
                raise AssertionError('Actual A module is outside its installed payload: ' + name)
            current = digest(path)
            if owner['files'].get(relative) != current:
                raise AssertionError('Actual A module differs from its owned inventory: ' + name)
            actual[name] = current
        return actual

    def later(delay, callback):
        def invoke():
            try:
                callback()
            except BaseException as error:
                fail(error)
        timer = wx.CallLater(delay, invoke)
        timers.append(timer)
        return timer

    def fail(error):
        nonlocal cleanup_deadline
        if not errors:
            errors.append(type(error).__name__ + ': ' + str(error)[:4000])
            print('NATIVE_UPDATE_ERROR ' + ''.join(traceback.format_exception(
                type(error), error, error.__traceback__))[:12000], flush=True)
            cleanup_deadline = time.monotonic() + 300
            if frames:
                frames[0].cancel_event.set()
        schedule_unwind()

    def owned_question():
        if not frames:
            return None
        known = {row[1] for row in owned_modals} | {captured['handle']}
        found = [handle for handle in api.windows()
                 if api.user.IsWindowVisible(handle) and api.pid(handle) == os.getpid()
                 and api.text(handle) == 'Prepare update host'
                 and int(api.user.GetWindow(handle, 4) or 0) in known]
        if len(found) > 1:
            raise RuntimeError('The preparation question is ambiguous.')
        return found[0] if found else None

    def schedule_unwind():
        nonlocal unwind_timer
        if unwind_timer is None:
            unwind_timer = wx.CallLater(25, unwind)

    def unwind():
        nonlocal unwind_timer
        unwind_timer = None
        if not frames:
            return
        if cleanup_deadline is not None and time.monotonic() >= cleanup_deadline:
            print('NATIVE_UPDATE_ERROR owned Close did not settle within 300 seconds.', flush=True)
            return
        try:
            question = owned_question()
            if question is not None:
                api.user.SetActiveWindow(question)
                api.post(api.question_button(question, 'No'), 0x00F5)
                schedule_unwind()
                return
            active = wx.EventLoop.GetActive()
            for window, handle, loop in reversed(owned_modals):
                if (api.user.IsWindow(handle) and window.GetHandle() == handle
                        and window.IsModal() and loop is active and loop.IsRunning()):
                    key = (id(window), handle, id(loop))
                    if key not in requested_modals:
                        requested_modals.add(key)
                        window.EndModal(wx.ID_CANCEL)
                    schedule_unwind()
                    return
            # A suspended recorded loop must resume before its own EndModal.
            # Never close the host through a still-live modal owned by another
            # loop, and never touch a foreign native window.
            if any(api.user.IsWindow(handle) and window.GetHandle() == handle
                   and window.IsModal() for window, handle, _loop in owned_modals):
                schedule_unwind()
                return
            frame = frames[0]
            if api.user.IsWindow(captured['handle']):
                if frame.working:
                    frame.cancel_event.set()
                    schedule_unwind()
                else:
                    frame.Close()
                    schedule_unwind()
        except BaseException as error:
            errors.append('Cleanup: ' + type(error).__name__ + ': ' + str(error)[:2000])

    def modal(title):
        found = [window for window in wx.GetTopLevelWindows()
                 if isinstance(window, wx.Dialog) and window
                 and window.GetTitle() == title and window.GetParent() is frames[0]
                 and window.IsModal()]
        if len(found) > 1:
            raise AssertionError('The native updater modal is ambiguous: ' + title)
        if not found:
            return None
        window = found[0]
        handle, loop = window.GetHandle(), wx.EventLoop.GetActive()
        if not any(row[0] is window and row[1] == handle for row in owned_modals):
            if api.pid(handle) != os.getpid() or loop is None or not loop.IsRunning():
                raise AssertionError('The current updater modal has no actual owned loop.')
            owned_modals.append((window, handle, loop))
        return window

    def control(parent, name):
        found, visited = [], 0
        pending = list(parent.GetChildren())
        while pending:
            window = pending.pop()
            visited += 1
            if visited > 256:
                raise AssertionError('Native updater controls exceed their fixed bound.')
            if window.GetName() == name:
                found.append(window)
            pending.extend(window.GetChildren())
            if len(pending) + len(found) > 256:
                raise AssertionError('Native updater controls exceed their fixed bound.')
        if len(found) != 1:
            raise AssertionError('No unique actual updater control: ' + name)
        return found[0]

    def wait_for(predicate, message, seconds=45):
        return predicate, message, seconds

    def focus(window):
        top = wx.GetTopLevelParent(window)
        handle = top.GetHandle()
        if not window.IsShown() or not window.IsEnabled() or api.pid(handle) != os.getpid():
            raise AssertionError('The actual updater control is not currently available.')
        top.Raise()
        api.user.SetForegroundWindow(handle)
        window.SetFocus()
        yield wait_for(lambda: api.user.GetForegroundWindow() == handle
                       and wx.Window.FindFocus() is window,
                       'Actual updater focus did not settle: ' + window.GetName()[:128], 5)

    simulator = wx.UIActionSimulator()
    key_pending = {'value': False}
    def key(window, code=wx.WXK_SPACE):
        yield from focus(window)
        key_pending['value'] = True
        if not simulator.KeyDown(code):
            raise AssertionError('The actual updater key press failed.')
        pressed.add(code)
        def release():
            try:
                if code in pressed and not simulator.KeyUp(code):
                    raise AssertionError('The actual updater key release failed.')
            finally:
                pressed.discard(code)
                key_pending['value'] = False
        later(40, release)
        yield wait_for(lambda: not key_pending['value'], 'Actual updater key release did not settle.', 5)

    def open_status():
        frame = frames[0]
        yield wait_for(lambda: not frame.working and not frame._closing,
                       'The actual updater shared worker did not settle.', 45)
        event = wx.CommandEvent(wx.EVT_MENU.typeId, menu_items[0].GetId())
        event.SetEventObject(frame)
        wx.PostEvent(frame, event)
        yield wait_for(lambda: modal('License and signed updates') is not None,
                       'The actual registered updater menu did not open.')
        return modal('License and signed updates')

    def answer_preparation(yes):
        yield wait_for(lambda: owned_question() is not None,
                       'The actual preparation question did not appear.', 5)
        question = owned_question()
        no = api.question_button(question, 'No')
        yield wait_for(lambda: api.user.GetForegroundWindow() == question
                       and api.user.GetFocus() == no,
                       'The actual preparation question did not retain default No.', 5)
        if not yes:
            check('preparation question defaults to the actual owned No button', True)
        else:
            chosen = api.question_button(question, 'Yes')
            api.user.SetFocus(chosen)
            yield wait_for(lambda: api.user.GetFocus() == chosen,
                           'Explicit preparation Yes focus did not settle.', 5)
        if not simulator.KeyDown(wx.WXK_SPACE):
            raise AssertionError('The actual preparation answer key press failed.')
        pressed.add(wx.WXK_SPACE)
        def release():
            try:
                if not simulator.KeyUp(wx.WXK_SPACE):
                    raise AssertionError('The actual preparation answer key release failed.')
            finally:
                pressed.discard(wx.WXK_SPACE)
        later(40, release)
        yield wait_for(lambda: wx.WXK_SPACE not in pressed and not api.user.IsWindow(question),
                       'The actual preparation question did not return.', 5)

    class ExplicitRepository:
        def __init__(self, parent, message, **kwargs):
            if parent is not modal('License and signed updates') or message != 'Choose the signed offline repository folder':
                raise AssertionError('The controlled picker is not the actual updater repository picker.')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def ShowModal(self):
            return wx.ID_OK
        def GetPath(self):
            return str(repository)

    def workflow():
        frame, controller = frames[0], frames[0].trust_controller
        check('actual installed A entrypoint owns the selected library and explicit recipe',
              frame.library.root == library and controller.update_library is frame.library
              and controller.provisioning is not None
              and controller.provisioning.sha256 == plan['recipe_sha256'])
        check('actual native updater command is registered exactly once', len(menu_items) == 1)
        check('offline configuration grants no idle network permission',
              controller.update_endpoint is None and not controller.background_update_checks)
        status = yield from open_status()
        yield from key(control(status, 'Prepare update host'))
        yield from answer_preparation(False)
        recipe = controller.provisioning
        check('observed No creates no host or configuration',
              not recipe.destination.exists() and not recipe.manifest.exists()
              and not recipe.configuration.exists() and not frame.working)
        yield from key(control(status, 'Prepare update host'))
        yield from answer_preparation(True)
        yield wait_for(lambda: not frame.working and controller.provisioning is None
                       and controller.handoff is not None,
                       'Actual offline host preparation did not complete.', 2100)
        configuration = controller.handoff.configuration
        check('explicit native Yes prepares the separate exact A runtime',
              configuration.runtime == Path(plan['external_runtime'])
              and configuration.path == Path(plan['configuration'])
              and configuration.python.is_file()
              and configuration.manifest_path.is_file())
        check('prepared controller retains original library and requires explicit review',
              controller.update_library is frame.library and configuration.library == library
              and frame.library.root == library
              and 'Apply is available after package verification' in controller.status_text()
              and not list((configuration.work / 'handoffs').glob('*/request.json')))
        for attempt in range(2):
            status = yield from open_status()
            yield from key(control(status, 'Check offline update repository'))
            yield wait_for(lambda: modal('Review signed update') is not None,
                           'The verified signed release review did not open.', 120)
            review = modal('Review signed update')
            details = control(review, 'Verified update details').GetValue()
            if attempt == 0:
                check('native signed release review has exact product build source and target',
                      all(str(value) in details for value in (
                          plan['release']['product'], plan['release']['build'],
                          plan['release']['source_commit'], plan['release']['sha256'])))
            yield from key(control(review, 'Download or resume verified package'))
            yield wait_for(lambda: modal('Apply reviewed update') is not None,
                           'The verified target did not reach the explicit Apply dialog.', 180)
            apply = modal('Apply reviewed update')
            restart = control(apply, 'Restart after verified update')
            cancel = apply.FindWindow(wx.ID_CANCEL)
            if attempt == 0:
                check('downloaded target is verified before the native Apply review',
                      plan['release']['sha256'] in control(apply, 'Reviewed update and backup handoff').GetValue()
                      and not list((configuration.work / 'handoffs').glob('*/request.json')))
                check('Apply opens with restart unchecked and Cancel as default',
                      not restart.GetValue() and apply.GetDefaultItem() is cancel)
                yield from key(cancel)
                yield wait_for(lambda: modal('Apply reviewed update') is None and not frame.working,
                               'Native Apply Cancel did not return to the current application.')
                check('native Cancel leaves A open and creates no handoff',
                      not frame._closing and api.user.IsWindow(captured['handle'])
                      and not list((configuration.work / 'handoffs').glob('*/request.json')))
            else:
                yield from key(restart)
                check('explicit native Space selects the optional checked restart', restart.GetValue())
                yield from key(control(apply, 'Close application and apply this update'))
                # Normal Close ends the real main loop. Its final checks run
                # after the current entrypoint and shared executor settle.
                yield wait_for(lambda: bool(commits),
                               'The real Close/COMMIT handoff did not complete.', 360)

    def drive():
        if errors or state['ended']:
            return
        if operation_errors:
            raise RuntimeError('Actual update worker failed: ' + operation_errors[0])
        later(25, drive)
        if time.monotonic() >= deadline:
            raise TimeoutError('The actual configured updater UI exceeded 2700 seconds.')
        pending = state['pending']
        if pending is not None and not pending[0]():
            if time.monotonic() >= state['until']:
                raise TimeoutError(pending[1])
            return
        try:
            pending = next(state['iterator'])
        except StopIteration:
            state['pending'] = None
            return
        state['pending'], state['until'] = pending, time.monotonic() + pending[2]

    def begin():
        frame = frames[0]
        if not hasattr(frame, 'trust_controller') or not frame.IsShown():
            raise AssertionError('The actual entrypoint did not load its configured updater.')
        frame.Raise()
        api.user.SetForegroundWindow(captured['handle'])
        items = [item for index in range(frame.menu_bar.GetMenuCount())
                 for item in frame.menu_bar.GetMenu(index).GetMenuItems()]
        menu_items.extend(item for item in items
                          if item.GetItemLabel().replace('&', '') == 'Check for updates / Update')
        state['iterator'] = workflow()
        later(25, drive)

    def observed_init(frame, *args, **kwargs):
        original_init(frame, *args, **kwargs)
        if frames:
            raise AssertionError('The actual entrypoint created more than one desktop.')
        frames.append(frame)
        captured.update(handle=frame.GetHandle(), executor=frame.executor)
        original_run_job = frame.run_job
        def observed_job(title, operation, *job_args, **job_options):
            def work(*arguments, **options):
                operations.append({'title': str(title)[:128], 'thread': threading.get_ident()})
                try:
                    return operation(*arguments, **options)
                except BaseException as error:
                    detail = ''.join(traceback.format_exception(type(error), error, error.__traceback__))[:12000]
                    operation_errors.append(detail)
                    print('NATIVE_UPDATE_WORKER_ERROR ' + detail, flush=True)
                    raise
            return original_run_job(title, work, *job_args, **job_options)
        patches.enter_context(patch.object(frame, 'run_job', observed_job))
        later(100, begin)

    def observed_commit(owner, *, host_closed):
        frame = frames[0]
        check('actual Close precedes the exact durable COMMIT',
              host_closed is True and frame._closing and owner.request['restart'] is True
              and owner.request['release'] == plan['release'] and not owner.transferred)
        if source_identity() != origins:
            raise AssertionError('Installed A modules changed before durable COMMIT.')
        receipt = original_commit(owner, host_closed=host_closed)
        if source_identity() != origins:
            raise AssertionError('Installed A modules changed while its application lease was held.')
        check('source identity remains exact through the real application lease', True)
        commits.append(receipt)
        return receipt

    def settle_host():
        # Retain the actual application lease, observers and DLL owner through
        # this settlement, including an exceptional return from entry.main.
        nonlocal cleanup_deadline
        if not captured:
            return True
        try:
            frames[0].cancel_event.set()
            if cleanup_deadline is None:
                cleanup_deadline = time.monotonic() + 300
            if api.user.IsWindow(captured['handle']):
                schedule_unwind()
                desktop = wx.GetApp()
                while (api.user.IsWindow(captured['handle'])
                       and time.monotonic() < cleanup_deadline):
                    if desktop is not None:
                        desktop.Yield(True)
                    time.sleep(.01)
        finally:
            # Original owner finalizers execute on the existing worker even if
            # a GUI observation failed; no alternate cleanup thread is added.
            captured['executor'].shutdown(wait=True, cancel_futures=True)
        failures = captured['executor'].cleanup_failures
        if failures:
            errors.append('Actual shared-worker owner settlement failed: ' +
                          '; '.join(type(error).__name__ + ': ' + str(error)[:512]
                                    for _callback, error in failures[:4]))
        closed = not api.user.IsWindow(captured['handle'])
        if not closed:
            errors.append('Actual native host closure remains unconfirmed.')
        return closed and not failures

    def watchdog():
        if not stop_watchdog.wait(3030):
            print('NATIVE_UPDATE_ERROR actual main/cleanup watchdog expired.', flush=True)
            os._exit(124)

    guard = threading.Thread(target=watchdog, name='configured-update-watchdog', daemon=True)
    guard.start()
    host_settled = False
    try:
        # Enter first, leave last: the real installed application lease covers
        # observer/executor settlement even when unchanged entry.main raises.
        patches.enter_context(application_lease())
        origins = source_identity()
        patches.enter_context(patch.object(desktop_app.DesktopFrame, '__init__', observed_init))
        patches.enter_context(patch.object(update_handoff.UpdateHandoff, 'commit_after_close', observed_commit))
        patches.enter_context(patch.object(wx, 'DirDialog', ExplicitRepository))
        main_result = entry.main(['--data-dir', str(library),
            '--update-configuration', plan['recipe'],
            '--update-configuration-sha256', plan['recipe_sha256']])
        state['ended'] = True
        for code in tuple(pressed):
            if not simulator.KeyUp(code):
                errors.append('Final native key release failed.')
            pressed.discard(code)
        if captured:
            captured['executor'].shutdown(wait=True, cancel_futures=True)
        if frames and not errors:
            check('actual entrypoint returns after normal Close and shared worker settlement',
                  main_result == 0 and frames[0]._closing
                  and not api.user.IsWindow(captured['handle']) and len(commits) == 1
                  and not captured['executor'].cleanup_failures
                  and getattr(frames[0], 'update_handoff_receipt', None) == commits[0])
    except BaseException as error:
        errors.append(type(error).__name__ + ': ' + str(error)[:4000])
        raise
    finally:
        state['ended'] = True
        try:
            host_settled = settle_host()
        except BaseException as error:
            errors.append('Host settlement: ' + type(error).__name__ + ': ' + str(error)[:2000])
        # The owned unwind timer remains live while pumping deferred modal
        # exits. Stop observers only after the final original-worker drain.
        for timer in [*timers, unwind_timer]:
            if timer is not None:
                try:
                    if timer.IsRunning():
                        timer.Stop()
                except BaseException as error:
                    errors.append('Timer release: ' + type(error).__name__ + ': ' + str(error)[:512])
        for code in tuple(pressed):
            try:
                if not simulator.KeyUp(code):
                    errors.append('Final native key release failed.')
            except BaseException as error:
                errors.append('Key release: ' + type(error).__name__ + ': ' + str(error)[:512])
            finally:
                pressed.discard(code)
        if host_settled:
            try:
                patches.close()
                native_runtime.close()
            except BaseException as error:
                host_settled = False
                errors.append('Native ownership release: ' + type(error).__name__ + ': ' + str(error)[:2000])
        if not host_settled:
            # Keep the lease, patches and native owner strongly held. The
            # existing watchdog/parent Job still bounds this failed process.
            _RETAINED_NATIVE_FAILURES.append((patches, native_runtime, captured, tuple(frames)))
        else:
            stop_watchdog.set()
            guard.join(5)
        value = dict(schema=1, status='PASS' if not errors and len(checks) == len(UI_CHECKS) else 'FAIL',
            checks=checks, errors=errors[:12], origins=origins, origin_count=len(origins),
            main_returncode=main_result, host_settled=host_settled,
            operations=operations[:16], commit=commits[0] if len(commits) == 1 else None,
            library=str(library), source_a=plan['source_a'],
            configuration=str(plan['configuration']),
            configuration_sha256=(digest(plan['configuration']) if Path(plan['configuration']).is_file() else None),
            picker='explicit owned synthetic offline-repository selection; no file-picker qualification')
        output(plan['ui_result'], value)
    assert_checks(checks, UI_CHECKS)
    if errors:
        raise RuntimeError(errors[0])
    return 0


def restarted_window(api, child, root, expected_pid):
    if child._job is None or api.process(expected_pid, child._job) is None:
        raise RuntimeError('The reported restart process does not belong to the retained native Job.')
    permitted = {(root / path).resolve(strict=True) for path in
                 ('app/.venv/Scripts/python.exe', 'app/runtime/python.exe')}
    matches = []
    for handle in api.windows():
        if not api.user.IsWindowVisible(handle) or api.text(handle) != 'Resonance':
            continue
        pid = api.pid(handle)
        image = api.process(pid, child._job)
        if image is not None and image in permitted:
            matches.append((handle, pid, str(image)))
    if len(matches) > 1:
        raise RuntimeError('More than one installed restart window belongs to the controlled family.')
    return matches[0] if matches else None


def restarted_status(api, frame, pid):
    found = [handle for handle in api.windows()
             if api.user.IsWindowVisible(handle) and api.pid(handle) == pid
             and api.text(handle) == 'License and signed updates'
             and int(api.user.GetWindow(handle, 4) or 0) == frame]
    if len(found) > 1:
        raise RuntimeError('Restarted configured status is ambiguous.')
    return found[0] if found else None


def native_text(api, window, pid):
    parts = []
    for handle in api.windows(window):
        if api.pid(handle) != pid:
            raise RuntimeError('A configured status control belongs to a different process.')
        kind = ctypes.create_unicode_buffer(128)
        api.user.GetClassNameW(handle, kind, len(kind))
        if 'edit' in kind.value.casefold():
            if len(parts) >= 16:
                raise RuntimeError('The configured status has too many native text controls.')
            parts.append(api.text(handle, content=True))
    return '\n'.join(parts)


def run_family(standard, command, *, root, work, log, plan, native, completed):
    """Retain the existing Windows Job through detached host and checked restart."""
    from carr_library.audio.models import AudioCleanupPending
    environment = standard._environment()
    environment['CARR_PROGRAM_DIR'] = str(root / 'app')
    api = Windows()
    child, threads, reader_errors = None, [], []
    lock, used = threading.Lock(), [0]
    family_settled, restart_observation = False, None
    with log.open('xb') as stream:
        def drain(pipe):
            try:
                while True:
                    data = pipe.read(65536)
                    if not data:
                        return
                    with lock:
                        remaining = max(0, 4 * 1024**2 - used[0])
                        if len(data) > remaining and not reader_errors:
                            reader_errors.append('Actual updater diagnostics exceeded four MiB.')
                        if remaining:
                            stream.write(data[:remaining])
                            used[0] += min(len(data), remaining)
            except Exception as error:
                reader_errors.append('Native diagnostic reader: ' + type(error).__name__)
            finally:
                pipe.close()

        try:
            try:
                child = standard.JobProcess([str(part) for part in command], cwd=work,
                    env=environment, stdin_pipe=False)
            except AudioCleanupPending as error:
                standard.wait_cleanup_owner(error.cleanup_owner)
                raise
            for pipe in (child.stdout, child.stderr):
                thread = threading.Thread(target=drain, args=(pipe,), daemon=True)
                threads.append(thread)
                thread.start()
            deadline = time.monotonic() + 5400
            if child.wait(timeout=3060) != 0:
                raise RuntimeError('The actual configured A desktop failed; retain its bounded diagnostic.')
            ui = json_value(read_bytes(plan['ui_result'], 65536), maximum=65536)
            assert_checks(ui.get('checks'), UI_CHECKS)
            if (ui.get('status') != 'PASS' or ui.get('source_a') != plan['source_a']
                    or ui.get('library') != plan['library'] or ui.get('main_returncode') != 0
                    or set(ui.get('origins', {})) != set(ORIGINS)
                    or ui.get('origin_count') != len(ORIGINS)):
                raise RuntimeError('Actual installed A UI identity/controls are incomplete.')
            receipt = ui['commit']
            result_path = Path(receipt['report'])
            parent = Path(plan['recovery']) / 'handoffs'
            if (receipt.get('status') != 'HANDED_OFF' or receipt.get('activation') != 'NOT_ESTABLISHED'
                    or receipt.get('installation') != str(root) or receipt.get('release') != plan['release']
                    or result_path.name != 'result.json' or result_path.parent.parent != parent
                    or not re.fullmatch('[0-9a-f]{32}', result_path.parent.name)):
                raise RuntimeError('The UI did not retain the exact private committed handoff.')
            wait(lambda: result_path.is_file(), 'Actual external host did not publish its bounded result.',
                 min(2160, max(1, deadline - time.monotonic())))
            result = json_value(read_bytes(result_path, 32768), maximum=32768)
            restart = result.get('restart', {})
            if (result.get('status') != 'COMPLETE' or result.get('release') != plan['release']
                    or result.get('transaction_phase') != 'complete'
                    or result.get('data_retargeted') is not False
                    or restart.get('status') != 'STARTED_NOT_QUALIFIED'
                    or type(restart.get('pid')) is not int or restart['pid'] <= 0):
                raise RuntimeError('Actual host activation/restart is incomplete: ' +
                                   json.dumps({key: result.get(key) for key in
                                       ('status', 'transaction_phase', 'error_type', 'restart')})[:4000])
            marker = native.verify_current(root)
            if any(result['installation_identity'][key] != marker[key]
                   for key in ('bundle_sha256', 'setup_sha256', 'owner_sha256')):
                raise RuntimeError('The completed B identity differs from actual native ownership.')
            shown = wait(lambda: restarted_window(api, child, root, restart['pid']),
                         'The checked installed restart did not open its actual owned desktop.', 90)
            handle, pid, image = shown
            completed('restarted process and window belong to the retained Windows Job', True)
            try:
                with native.maintenance.installation_lease(root, exclusive=True):
                    raise AssertionError('A restarted installed application failed to hold its real lease.')
            except RuntimeError as error:
                if 'Close Resonance' not in str(error):
                    raise
            completed('restarted actual application holds the installation lease', True)
            api.post(handle, 0x0111, api.update_menu(handle))
            dialog = wait(lambda: restarted_status(api, handle, pid),
                          'The restarted real entrypoint has no configured update status.', 45)
            text = native_text(api, dialog, pid)
            completed('restarted actual installed entrypoint retains the configured Apply route',
                      'Signed offline update checks are available.' in text
                      and 'Apply is available after package verification' in text
                      and 'Apply is unavailable' not in text)
            api.post(dialog, 0x0010)
            wait(lambda: not api.user.IsWindow(dialog), 'Restarted status Close did not settle.', 10)
            api.post(handle, 0x0010)
            wait(lambda: not api.user.IsWindow(handle), 'Normal restarted application Close did not settle.', 90)
            standard.wait_owned_tree(child, deadline=min(deadline, time.monotonic() + 90))
            family_settled = True
            restart_observation = dict(requested_pid=restart['pid'], actual_window_pid=pid,
                actual_image=image, actual_window=handle, status='PASS',
                configured_status_sha256=sha256(text.encode('utf-8')).hexdigest(),
                normal_close='PASS', owned_family='PASS')
            completed('normal restarted Close settles the complete native process family', True)
            if reader_errors:
                raise RuntimeError(reader_errors[0])
            return ui, result, restart_observation
        finally:
            if child is not None:
                try:
                    standard.stop_owned_process(child)
                finally:
                    for thread in threads:
                        if thread.ident is not None:
                            thread.join(10)
                    for index, pipe in enumerate((child.stdout, child.stderr)):
                        if index >= len(threads) or threads[index].ident is None:
                            pipe.close()
                    if any(thread.is_alive() for thread in threads):
                        raise RuntimeError('Owned updater diagnostic pipes did not settle.')
                    output(work / 'native-family-settlement.json', {
                        'schema': 1, 'direct_returncode': child.poll(),
                        'normal_family_quiescence': 'PASS' if family_settled else 'NOT_PROVEN',
                        'owned_cleanup_handles_closed': bool(child._job is None
                            and not child._process.hProcess and not child._process.hThread),
                        'restart_observation': restart_observation,
                        'forced_failure_cleanup_possible': not family_settled})


def probe(args):
    if os.name != 'nt':
        raise RuntimeError('Configured native update requires actual Windows; no skip qualifies it.')
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup, verify_backup
    from carr_library.security.anchors import Anchor, WindowsVaultAnchor
    from carr_library.security.managed_installation import WindowsManagedSetupAdapter
    from carr_library.security.updates import LocalRepositoryClient
    from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
    host_path = Path(carr_library.__file__).resolve(strict=True)
    if 'site-packages' not in host_path.parts or host_path.is_relative_to(HERE.parent / 'src'):
        raise RuntimeError('Run the proof through the exact isolated installed B host wheel.')
    standard = load_module('configured_standard_probe', HERE / 'windows_standard_installer_smoke.py')
    old = load_module('configured_managed_fixture', HERE / 'windows_managed_update_smoke.py')
    target_builder = load_module('configured_target_builder',
        HERE.parent / 'packaging/windows_installer/create_update_target.py')
    maintenance = target_builder.builder.maintenance
    record_a = old._evidence(args.setup_a, args.build_a, args.source_a)
    if args.source_a == args.source_b:
        raise RuntimeError('Choose distinct immutable A and current B source commits.')
    if standard.registry() is not None:
        raise RuntimeError('This account has a preexisting standard installation; it will not be changed.')
    menu = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Resonance'
    if os.path.lexists(menu):
        raise RuntimeError('This account has preexisting application shortcuts.')
    work = args.work.absolute().parent.resolve(strict=True) / args.work.name
    report = args.report.absolute().parent.resolve(strict=True) / args.report.name
    if work == report or work.is_relative_to(report) or report.is_relative_to(work):
        raise RuntimeError('The private native work and public evidence directories must be separate.')
    work.mkdir(parents=False, exist_ok=False)
    report.mkdir(parents=False, exist_ok=False)
    root = work / 'Configured Resonance Україна Ω'
    external = work / 'external updater'
    recovery = work / 'update recovery'
    recovery.mkdir()
    configuration = work / 'prepared-owner-configuration.json'
    recipe_path, manifest_path = work / 'owner-recipe.json', work / 'external-runtime-seal.json'
    target = work / 'prepared-target'
    target_report = target_builder.create_update_target(args.prepared_b, args.source_b, target)
    if target_report['bundle_manifest_sha256'] == record_a['bundle_manifest_sha256']:
        raise RuntimeError('A and B have the same distribution manifest; this is not an upgrade.')
    checks, anchor, provisioned, finished = [], None, False, False
    def completed(name, condition):
        if name not in PARENT_CHECKS or name in checks or not condition:
            raise AssertionError(name)
        checks.append(name)
        print('CHECK: ' + name, flush=True)
    common = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-']
    result = dict(schema=1, status='FAIL', source_a=args.source_a, source_b=args.source_b,
        checks=checks, ui_checks=[], actual_native_restart='NOT_RUN',
        actual_host_preparation='NOT_RUN', actual_update='NOT_RUN', **NOT_RUN)
    try:
        print('CONFIGURED_STAGE install-exact-A', flush=True)
        standard.owned_process([args.setup_a, *common, '/DIR=' + str(root),
            '/LOG=' + str(report / 'install-A.log')], cwd=work,
            log=report / 'install-A-process.txt', timeout=1800)
        native = WindowsManagedSetupAdapter(maintenance)
        marker_a = native.verify_current(root)
        old_marker = read_bytes(root / maintenance.MARKER, 8192)
        old_owner = read_bytes(root / maintenance.OWNER, maintenance.MAX_MANIFEST)
        old_inventory = maintenance.inventory(root / 'app')
        old_uninstall = {name: digest(root / name) for name in ('unins000.exe', 'unins000.dat')}
        old_registration = native.registration.snapshot(root)
        shortcut_work = work / 'private-shortcut-observation'
        shortcut_work.mkdir()
        standard.inspect_shortcuts(root, shortcut_work)
        # The reused Windows PowerShell observer writes UTF-8 with a BOM;
        # its own validated reader uses utf-8-sig. Normalize only that local report.
        shortcut_bytes = read_bytes(shortcut_work / 'shortcuts.json', 65536)
        output(report / 'A-shortcuts.json', json_value(
            shortcut_bytes.removeprefix(b'\xef\xbb\xbf'), maximum=65536))
        completed('both compiled source receipts and full owned A payload are exact',
                  marker_a['bundle_sha256'] == record_a['bundle_manifest_sha256']
                  and marker_a['setup_sha256'] == record_a['setup_sha256'])
        app = LibraryApp(work / 'original synthetic research')
        workspace = app.create_workspace('Configured updater native research')
        imported = app.add_text(workspace, 'Original immutable source',
            'Original immutable source. Незмінне джерело Україна Ω.')
        if imported.get('state') != 'complete':
            raise RuntimeError('The synthetic original research did not complete.')
        ProgressPreferenceStore(app.root / 'progress-preferences.json').save(ProgressPreferences(muted=True))
        old._check_source(app, workspace)
        repository = work / 'synthetic signed repository'
        policy = old._repository(repository, target / target_report['target'], target_report)
        client = LocalRepositoryClient(work / 'parent signed review', policy)
        release = client.check(repository, 'managed-update.zip')
        token = uuid.uuid4().hex
        profile = 'configured-native-' + token
        anchor = WindowsVaultAnchor('carr/provider/' + profile + '/' + token,
            sha256(str(root).encode('utf-8')).hexdigest(), product=policy.product, profile_id=profile)
        anchor.vault.put(anchor.profile_id, anchor.encode(Anchor(1, record_a['setup_sha256'], int(time.time()))),
                         reference=anchor.reference)
        provisioned = True
        completed('temporary actual Windows vault anchor begins at A', anchor.load().sequence == 1)
        publisher = asdict(policy)
        publisher['bootstrap'] = base64.b64encode(policy.bootstrap).decode('ascii')
        recipe = {'schema': 1, 'kind': 'carr-managed-update-provision',
            'runtime': {'root': str(external), 'manifest': str(manifest_path)},
            'installation': {'root': str(root), 'work': str(recovery)}, 'library': str(app.root),
            'publisher': publisher,
            'repository': {'endpoint': None, 'target': 'managed-update.zip', 'tls_ca_pem': None, 'timeout': 60},
            'anchor': {'reference': anchor.reference, 'binding': anchor.binding, 'profile_id': anchor.profile_id},
            'close_timeout': 120, 'operation_timeout': 2400,
            'source': {'bundle_sha256': marker_a['bundle_sha256'], 'owner_sha256': marker_a['owner_sha256'],
                       'setup_sha256': marker_a['setup_sha256']},
            'configuration': str(configuration)}
        output(recipe_path, recipe)
        plan = {'schema': 1, 'source_a': args.source_a, 'source_b': args.source_b,
            'installation': str(root), 'library': str(app.root), 'repository': str(repository),
            'recovery': str(recovery), 'recipe': str(recipe_path), 'recipe_sha256': digest(recipe_path),
            'configuration': str(configuration), 'external_runtime': str(external),
            'owner_sha256': marker_a['owner_sha256'], 'release': asdict(release),
            'ui_result': str(work / 'actual-A-ui-result.json')}
        plan_path = work / 'native-plan.json'
        output(plan_path, plan)
        command = [root / 'app/.venv/Scripts/python.exe', '-I', '-B', '-X', 'utf8', Path(__file__).resolve(),
                   '--ui-child', plan_path, '--plan-sha256', digest(plan_path), '--probe-sha256', digest(__file__)]
        print('CONFIGURED_STAGE actual-A-main-prepare-review-cancel-apply-restart', flush=True)
        ui, host, restart = run_family(standard, command, root=root, work=work,
            log=report / 'configured-native-child.txt', plan=plan, native=native, completed=completed)
        if anchor.load().sequence != 2:
            raise RuntimeError('The actual Windows-vault update anchor did not advance to reviewed B.')
        result.update(ui_checks=ui['checks'], ui_origin_count=ui['origin_count'], ui_origins=ui['origins'],
            actual_native_restart='PASS', actual_host_preparation='PASS', actual_update='PASS',
            actual_restart=restart, actual_ui_result_sha256=digest(plan['ui_result']))
        marker_b = native.verify_current(root)
        completed('real separate host records completed B and protected build two',
                  marker_b['bundle_sha256'] == target_report['bundle_manifest_sha256']
                  and marker_b['setup_sha256'] == target_report['setup_sha256'])
        transaction = host['transaction']
        if type(transaction) is not dict or transaction.get('status') != 'complete':
            raise RuntimeError('The exact original managed transaction did not record completion.')
        previous = Path(transaction['previous'])
        journal = json_value(read_bytes(recovery / 'managed-activation.json', 16384), maximum=16384)
        wrapper = native._read_wrapper(root, previous / 'wrapper', journal['wrapper_sha256'])
        completed('complete prior A wrapper and exact original research are retained',
                  maintenance.inventory(previous / 'app') == old_inventory
                  and read_bytes(previous / maintenance.MARKER, 8192) == old_marker
                  and read_bytes(previous / maintenance.OWNER, maintenance.MAX_MANIFEST) == old_owner
                  and {name: digest(previous / 'wrapper' / name) for name in old_uninstall} == old_uninstall
                  and wrapper['registration'] == old_registration
                  and (previous / 'maintenance/ResonanceSetup.exe').is_file()
                  and digest(previous / 'maintenance/ResonanceSetup.exe') == record_a['setup_sha256'])
        old._check_source(app, workspace)
        verify_backup(Path(transaction['backup']), transaction['backup_sha256'])
        restored = LibraryApp(restore_backup(Path(transaction['backup']), work / 'verified backup restore',
            transaction['backup_sha256']))
        old._check_source(restored, workspace)
        completed('actual backup restores the original synthetic research', True)
        standard.owned_process([root / 'app/ResearchLibrary.exe', '--check-installation'],
            cwd=work, log=report / 'verified-B-entrypoint.txt', timeout=300)
        standard.owned_process([root / 'unins000.exe', *common,
            '/LOG=' + str(report / 'uninstall-B.log')], cwd=work,
            log=report / 'uninstall-B-process.txt', timeout=900)
        old._check_source(app, workspace)
        completed('actual standard uninstaller leaves research and prior recovery intact',
                  not (root / 'app').exists() and standard.registry() is None and previous.is_dir()
                  and not any((menu / name).exists() for name in
                      ('Resonance.lnk', 'Repair Resonance.lnk', 'Uninstall Resonance.lnk')))
        assert_checks(checks, PARENT_CHECKS)
        result.update(status='PASS', ui_checks=ui['checks'], actual_native_restart='PASS',
            actual_host_preparation='PASS', actual_update='PASS',
            ui_origin_count=ui['origin_count'], ui_origins=ui['origins'],
            setup_a_sha256=record_a['setup_sha256'], setup_b_sha256=target_report['setup_sha256'],
            target_sha256=target_report['sha256'], actual_restart=restart,
            actual_host_result_sha256=digest(Path(ui['commit']['report'])),
            actual_ui_result_sha256=digest(plan['ui_result']),
            prepared_configuration_sha256=ui['configuration_sha256'],
            host_package=str(host_path), exact_publisher='synthetic owned TUF closure',
            picker=ui['picker'])
        finished = True
        return result
    except BaseException as error:
        result['error_type'] = type(error).__name__
        result['error'] = str(error)[:4000]
        raise
    finally:
        if provisioned and finished:
            anchor.vault.delete(anchor.profile_id, anchor.reference)
        elif provisioned:
            result['recovery'] = {'protected_anchor': 'RETAINED_FOR_REVIEW',
                'program': str(root), 'work': str(recovery), 'reference': anchor.reference,
                'profile_id': anchor.profile_id}
        output(report / 'configured-update.json', result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-child', type=Path)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--probe-sha256')
    parser.add_argument('--setup-a', type=Path)
    parser.add_argument('--build-a', type=Path)
    parser.add_argument('--prepared-b', type=Path)
    parser.add_argument('--source-a')
    parser.add_argument('--source-b')
    parser.add_argument('--work', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    if args.ui_child is not None:
        if not args.plan_sha256 or not args.probe_sha256:
            parser.error('The private native child requires both independent input digests.')
        return ui_child(args.ui_child, args.plan_sha256, args.probe_sha256)
    if any(getattr(args, name) is None for name in
           ('setup_a', 'build_a', 'prepared_b', 'source_a', 'source_b', 'work', 'report')):
        parser.error('The native parent requires both exact compiled versions and private work/evidence paths.')
    result = probe(args)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
