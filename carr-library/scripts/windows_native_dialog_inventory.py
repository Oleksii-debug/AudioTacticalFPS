"""Inspect only this process's synthetic native Windows message dialog.

DIAGNOSTIC ONLY: EndDialog is explicit fixture cleanup, never proof that a
native No button, product workflow, keyboard route or cleanup contract passed.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time

if os.name != 'nt':
    raise RuntimeError('Actual Windows is required.')
import wx

output = Path(sys.argv[1]).resolve()
if output.exists():
    raise FileExistsError(output)
output.parent.mkdir(parents=True, exist_ok=True)
finished = threading.Event()
payload = {'status': 'DIAGNOSTIC_ONLY', 'product_qualification': 'NOT_RUN',
           'keyboard_qualification': 'NOT_RUN', 'native_no_button': 'NOT_RUN',
           'wx': wx.version(), 'python': sys.version, 'pid': os.getpid()}
user = ctypes.WinDLL('user32', use_last_error=True)
CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user.EnumWindows.argtypes = [CALLBACK, wintypes.LPARAM]
user.EnumChildWindows.argtypes = [wintypes.HWND, CALLBACK, wintypes.LPARAM]
user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user.GetDlgCtrlID.argtypes = [wintypes.HWND]
user.GetDlgCtrlID.restype = ctypes.c_int
user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user.GetWindow.restype = wintypes.HWND
user.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
user.GetDlgItem.restype = wintypes.HWND
user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user.FindWindowW.restype = wintypes.HWND
user.IsWindowVisible.argtypes = [wintypes.HWND]
user.IsWindowVisible.restype = wintypes.BOOL
user.EndDialog.argtypes = [wintypes.HWND, ctypes.c_ssize_t]
user.EndDialog.restype = wintypes.BOOL

def owner_pid(handle):
    value = wintypes.DWORD()
    user.GetWindowThreadProcessId(handle, ctypes.byref(value))
    return value.value

def text(handle, function):
    value = ctypes.create_unicode_buffer(512)
    function(handle, value, len(value))
    return value.value

def row(handle):
    return dict(handle=int(handle), pid=owner_pid(handle),
        title=text(handle, user.GetWindowTextW), window_class=text(handle, user.GetClassNameW),
        control_id=user.GetDlgCtrlID(handle), owner=int(user.GetWindow(handle, 4) or 0),
        visible=bool(user.IsWindowVisible(handle)))

def save():
    encoded = json.dumps(payload, ensure_ascii=True, indent=2).encode('utf-8') + b'\n'
    if len(encoded) > 256 * 1024:
        raise ValueError('Bounded owned-window inventory exceeded its limit.')
    output.write_bytes(encoded)
    print(json.dumps(payload, ensure_ascii=True), flush=True)

def watchdog():
    if not finished.wait(25):
        print('DIAGNOSTIC: synthetic UI cleanup exceeded25 seconds; no product outcome.', flush=True)
        os._exit(74)

threading.Thread(target=watchdog, daemon=True).start()
gui = wx.App(False)
gui.SetExitOnFrameDelete(False)
frame = wx.Frame(None, title='Owned native dialog inspection')
outer = wx.Dialog(frame, title='Owned outer inspection fixture')
inner = wx.Dialog(outer, title='Owned inner inspection fixture')
frame.Show()
timers = []
errors = []

def inspect_question():
    try:
        windows = []
        @CALLBACK
        def visit(handle, unused):
            if owner_pid(handle) == os.getpid():
                windows.append(row(handle))
            return len(windows) < 32
        user.EnumWindows(visit, 0)
        candidate = user.FindWindowW(None, 'Restore complete')
        payload['owned_windows'] = windows
        payload['exact_find_handle'] = int(candidate or 0)
        payload['known_fixture_handles'] = [frame.GetHandle(), outer.GetHandle(), inner.GetHandle()]
        if not candidate or owner_pid(candidate) != os.getpid():
            save()
            raise AssertionError('Exact synthetic question was not found in this process.')
        owners = set(payload['known_fixture_handles'])
        chain, current = [], user.GetWindow(candidate, 4)
        for unused in range(16):
            if not current:
                break
            chain.append(int(current))
            if int(current) in owners:
                break
            current = user.GetWindow(current, 4)
        if not chain or chain[-1] not in owners:
            raise AssertionError('The synthetic question is not owned by the fixture.')
        children = []
        @CALLBACK
        def child(handle, unused):
            if owner_pid(handle) == os.getpid():
                children.append(row(handle))
            return len(children) < 128
        user.EnumChildWindows(candidate, child, 0)
        payload['question'] = row(candidate)
        payload['owner_chain'] = chain
        payload['children'] = children
        payload['direct_button_handles'] = {str(code): int(user.GetDlgItem(candidate, code) or 0)
                                           for code in (6, 7, int(wx.ID_YES), int(wx.ID_NO))}
        save()
        # This direct fixture dismissal is intentionally NOT a tested button
        # interaction. The original required native workflow remains failed.
        payload['cleanup_method'] = 'EndDialog_for_synthetic_fixture_only'
        payload['cleanup_api_return'] = bool(user.EndDialog(candidate, 7))
        save()
    except BaseException as error:
        errors.append(repr(error))
        payload['diagnostic_errors'] = list(errors)
        save()

def open_inner():
    timers.append(wx.CallLater(250, inspect_question))
    answer = wx.MessageBox('Controlled inspection only; no action is authorized.',
                          'Restore complete', wx.YES_NO | wx.NO_DEFAULT, inner)
    payload['messagebox_return'] = int(answer)
    inner.EndModal(wx.ID_CANCEL)

def open_outer():
    timers.append(wx.CallLater(25, open_inner))
    payload['inner_modal_return'] = int(inner.ShowModal())
    outer.EndModal(wx.ID_CANCEL)

def begin():
    timers.append(wx.CallLater(25, open_outer))
    payload['outer_modal_return'] = int(outer.ShowModal())
    inner.Destroy()
    outer.Destroy()
    frame.Destroy()
    gui.ExitMainLoop()

wx.CallAfter(begin)
gui.MainLoop()
for timer in timers:
    timer.Stop()
finished.set()
payload['synthetic_loop_returned'] = True
save()
if errors:
    raise SystemExit(2)
