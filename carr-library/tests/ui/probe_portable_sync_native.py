"""Actual wx desktop/snapshot events with synthetic local data, never NVDA claims.

Installed wheel: python -I tests/ui/probe_portable_sync_native.py --native
Checkout debugging: add --source /absolute/path/to/carr-library
Add --encrypted to exercise real encrypted publication and password re-entry.
Missing native prerequisites return NOT_RUN (77), not PASS.
"""
from argparse import ArgumentParser
import importlib.metadata
import importlib.util
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


# TaskDialog can draw virtual DirectUI buttons without child HWNDs. UIA must
# execute outside the wx UI thread so native accessibility callbacks keep moving.
# https://learn.microsoft.com/windows/win32/winauto/uiauto-threading
# https://learn.microsoft.com/dotnet/framework/ui-automation/invoke-a-control-using-ui-automation
# This helper sees only the already ownership-checked synthetic confirmation.
_UIA_REPLY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$stage = 'assemblies'
$reason = 'native_exception'
$observed = @()
$root_checks = @{}
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ConfirmationOwner {
    [DllImport("user32.dll")]
    public static extern IntPtr GetWindow(IntPtr window, uint command);
}
'@
    $stage = 'dialog_identity'
    $dialog = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]([long]$request.hwnd))
    $current = $dialog.Current
    $root_checks = @{
        pid=($current.ProcessId -eq $request.pid); title=($current.Name -ceq $request.title)
        hwnd=($current.NativeWindowHandle -eq $request.hwnd); class=($current.ClassName -eq '#32770')
        enabled=[bool]$current.IsEnabled; offscreen=[bool]$current.IsOffscreen
    }
    if ($current.ProcessId -ne $request.pid -or $current.Name -cne $request.title -or
            $current.NativeWindowHandle -ne $request.hwnd -or $current.ClassName -ne '#32770' -or
            -not $current.IsEnabled -or $current.IsOffscreen) {
        $reason = 'dialog_identity_changed'
        throw 'The accessible dialog identity changed'
    }
    $stage = 'owner_chain'
    $seen = @{}
    $owned = $false
    $parent = [ConfirmationOwner]::GetWindow([IntPtr]([long]$request.hwnd), 4)
    for ($depth = 0; $parent -ne [IntPtr]::Zero -and $depth -lt 32; $depth++) {
        $value = $parent.ToInt64()
        if ($seen.ContainsKey($value)) { break }
        $seen[$value] = $true
        if ($value -eq $request.owner) { $owned = $true; break }
        $parent = [ConfirmationOwner]::GetWindow($parent, 4)
    }
    if (-not $owned) { $reason = 'owner_changed'; throw 'The accessible dialog owner changed' }
    $stage = 'button_lookup'
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)
    $buttons = $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($buttons.Count -gt 32) { $reason = 'too_many_buttons'; throw 'Too many accessible confirmation buttons' }
    foreach ($button in $buttons) {
        $candidate = $button.Current
        # Boolean role comparisons expose no dialog text or password contents.
        $observed += @{
            pid=[long]$candidate.ProcessId; hwnd=[long]$candidate.NativeWindowHandle
            yes=($candidate.Name -ceq $request.yes); no=($candidate.Name -ceq $request.no)
            enabled=[bool]$candidate.IsEnabled; offscreen=[bool]$candidate.IsOffscreen
            type=[int]$candidate.ControlType.Id
        }
    }
    $answers = @{}
    foreach ($label in @($request.yes, $request.no)) {
        $matches = @()
        foreach ($button in $buttons) {
            $info = $button.Current
            if ($info.ProcessId -eq $request.pid -and $info.Name -ceq $label -and
                    $info.IsEnabled -and -not $info.IsOffscreen) {
                $matches += $button
            }
        }
        if ($matches.Count -ne 1) { $reason = 'answer_pair_not_unique'; throw 'The owned dialog has no unique accessible Yes/No pair' }
        $answers[$label] = $matches[0]
    }
    $stage = 'invoke_pattern'
    $selected = $answers[$request.answer]
    $info = $selected.Current
    $pattern = $null
    if (-not $selected.TryGetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        $reason = 'invoke_unavailable'
        throw 'The owned answer button cannot be invoked'
    }
    $receipt = @{
        status='NATIVE_DIALOG'; method='UIA_Invoke'; pid=[long]$request.pid
        hwnd=[long]$request.hwnd; owner=[long]$request.owner; title=$request.title
        button_name=$info.Name; button_id=$info.AutomationId
        button_hwnd=[long]$info.NativeWindowHandle; button_type=$info.ControlType.Id
        enabled=$info.IsEnabled; offscreen=$info.IsOffscreen
    }
    # Invoke the actual accessible control; never manufacture a modal result.
    $stage = 'invoke'
    ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
    $receipt | ConvertTo-Json -Compress
    exit 0
} catch {
    $failure = $_.Exception
    for ($depth = 0; $failure.InnerException -and $depth -lt 4; $depth++) {
        $failure = $failure.InnerException
    }
    @{
        status='UIA_FAILURE'; pid=[long]$request.pid; hwnd=[long]$request.hwnd
        owner=[long]$request.owner; stage=$stage; reason=$reason
        hresult=[int]$failure.HResult; controls=$observed; root=$root_checks
    } | ConvertTo-Json -Depth 4 -Compress
    exit 1
}
"""


class AccessibleAnswer:
    """One bounded native UIA operation, polled without blocking the wx loop."""
    def __init__(self, request):
        import base64
        import subprocess
        self.request = request
        payload = base64.b64encode(json.dumps(request).encode('utf-8')).decode('ascii')
        program = ("$request = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString("
                   "[Convert]::FromBase64String('" + payload + "')))\n" + _UIA_REPLY)
        command = base64.b64encode(program.encode('utf-16-le')).decode('ascii')
        executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self.process = subprocess.Popen(
            [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta',
             '-EncodedCommand', command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.deadline = monotonic() + 8

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.communicate(timeout=1)

    def poll(self):
        if self.process.poll() is None:
            if monotonic() >= self.deadline:
                self.close()
                raise AssertionError('Owned accessible confirmation lookup exceeded eight seconds')
            return False
        output, unused_error = self.process.communicate(timeout=1)
        if self.process.returncode or len(output) > 16384:
            report_accessible_failure(output, self.request, self.process.returncode)
            raise AssertionError('Owned accessible confirmation interaction failed')
        try:
            receipt = json.loads(output.decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            raise AssertionError('Invalid owned accessible confirmation receipt') from None
        expected = dict(status='NATIVE_DIALOG', method='UIA_Invoke',
                        pid=self.request['pid'], hwnd=self.request['hwnd'],
                        owner=self.request['owner'], title=self.request['title'],
                        button_name=self.request['answer'], button_type=50000,
                        enabled=True, offscreen=False)
        if (type(receipt) is not dict
                or any(type(receipt.get(key)) is not type(value) or receipt[key] != value
                       for key, value in expected.items())
                or type(receipt.get('button_hwnd')) is not int or receipt['button_hwnd'] < 0
                or type(receipt.get('button_id')) is not str or len(receipt['button_id']) > 256):
            raise AssertionError('Owned accessible confirmation receipt does not match the request')
        print(json.dumps(receipt), flush=True)
        return True


def report_accessible_failure(output, request, returncode):
    """Emit only fixed failure codes, integer native metadata and role booleans."""
    report = dict(status='NATIVE_DIALOG_FAILED', helper_exit=returncode)
    try:
        if len(output) > 16384:
            raise ValueError('oversized helper output')
        value = json.loads(output.decode('utf-8-sig'))
        stages = {'assemblies', 'dialog_identity', 'owner_chain', 'button_lookup',
                  'invoke_pattern', 'invoke'}
        reasons = {'native_exception', 'dialog_identity_changed', 'owner_changed',
                   'too_many_buttons', 'answer_pair_not_unique', 'invoke_unavailable'}
        if (type(value) is not dict or value.get('status') != 'UIA_FAILURE'
                or any(type(value.get(key)) is not int or value[key] != request[key]
                       for key in ('pid', 'hwnd', 'owner'))
                or value.get('stage') not in stages or value.get('reason') not in reasons
                or type(value.get('hresult')) is not int
                or type(value.get('controls')) is not list or len(value['controls']) > 32):
            raise ValueError('uncorrelated helper failure')
        root = value.get('root', {})
        if (type(root) is not dict or set(root) not in (set(), {'pid', 'title', 'hwnd', 'class', 'enabled', 'offscreen'})
                or any(type(flag) is not bool for flag in root.values())):
            raise ValueError('invalid root diagnostic')
        controls = []
        for control in value['controls']:
            if (type(control) is not dict
                    or any(type(control.get(key)) is not int for key in ('pid', 'hwnd', 'type'))
                    or any(type(control.get(key)) is not bool
                           for key in ('yes', 'no', 'enabled', 'offscreen'))):
                raise ValueError('invalid helper control metadata')
            controls.append({key: control[key] for key in
                             ('pid', 'hwnd', 'type', 'yes', 'no', 'enabled', 'offscreen')})
        report.update(stage=value['stage'], reason=value['reason'],
                      hresult=value['hresult'], controls=controls, root=root)
    except (TypeError, ValueError, UnicodeError):
        report['reason'] = 'invalid_helper_diagnostic'
    print(json.dumps(report), flush=True)


def answer_successor_password(responses, *, confirmation_gone, find_dialog, answer):
    """Keep the real successor modal moving while UIA Invoke is still returning.

    Some providers block Invoke until the next modal is dismissed. The original
    confirmation remains pending until its own successful native receipt arrives.
    """
    if (not confirmation_gone or len(responses) < 2
            or responses[1][0] != 'Snapshot password'):
        return False
    dialog = find_dialog('Snapshot password')
    if dialog is None:
        return False
    answer(dialog, responses[1][1])
    responses.pop(1)
    return True



class TaskDialogAnswer:
    """Post a native common-button click, then observe the owned dialog close.

    TDM_CLICK_BUTTON delivers TDN_BUTTON_CLICKED and honours a callback veto.
    Posting is not a completion receipt: a dialog that stays open fails the
    deadline, and the caller still verifies actual publication/restore effects.
    https://learn.microsoft.com/windows/win32/controls/tdm-click-button
    """
    def __init__(self, confirmations, request):
        self.confirmations, self.request = confirmations, request
        self.deadline = monotonic() + 5
        confirmations._validate_task_dialog(request)
        confirmations.api.SetActiveWindow(request['hwnd'])
        # CommCtrl.h: WM_USER + 102; wxMSW's wx.YES_NO maps to IDYES/IDNO.
        if not confirmations.api.PostMessageW(
                request['hwnd'], 0x0400 + 102, request['button_id'], 0):
            raise AssertionError('Could not dispatch the owned TaskDialog answer button')

    def poll(self):
        if monotonic() >= self.deadline:
            raise AssertionError('The owned TaskDialog did not close after its button click')
        if self.confirmations.api.IsWindow(self.request['hwnd']):
            self.confirmations._validate_task_dialog(self.request)
            return False
        print(json.dumps(dict(status='NATIVE_DIALOG', method='TDM_CLICK_BUTTON',
                              pid=self.request['pid'], hwnd=self.request['hwnd'],
                              owner=self.request['owner'], title=self.request['title'],
                              button_id=self.request['button_id'], closed=True)), flush=True)
        return True

    def close(self):
        # No helper process or native handle is owned by this observer.
        pass


class WindowsConfirmations:
    """Click only this process's owned native message/task dialog buttons.

    wxMSW MessageDialog does not run wx.Dialog's modal loop. IsModal/EndModal
    cannot be used to discover or dismiss its TaskDialogIndirect window.
    """
    def __init__(self, *, yes_label='Yes', no_label='No', task_dialog_titles=()):
        self.yes_label, self.no_label = yes_label, no_label
        self.task_dialog_titles = frozenset(task_dialog_titles)
        self.pending = None
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
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'IsChild': ([wintypes.HWND, wintypes.HWND], wintypes.BOOL),
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

    def close(self):
        if self.pending is not None:
            operation, unused_key = self.pending
            operation.close()
            self.pending = None

    def _validate_task_dialog(self, request):
        """Recheck the exact owned wx.YES_NO TaskDialog before native dispatch."""
        window, pane = request['hwnd'], request['pane']
        if (request['pid'] != os.getpid() or request['title'] not in self.task_dialog_titles
                or request['button_id'] not in (6, 7)
                or not self.api.IsWindow(window) or not self.api.IsWindow(pane)
                or not self.api.IsChild(window, pane)
                or self._string(self.api.GetClassNameW, window) != '#32770'
                or self._string(self.api.GetWindowTextW, window) != request['title']
                or self._string(self.api.GetClassNameW, pane).casefold() != 'directuihwnd'):
            raise AssertionError('The owned TaskDialog identity changed')
        for handle in (window, pane):
            pid = self.types.DWORD()
            self.api.GetWindowThreadProcessId(handle, self.ctypes.byref(pid))
            if (pid.value != request['pid'] or not self.api.IsWindowVisible(handle)
                    or not self.api.IsWindowEnabled(handle)):
                raise AssertionError('The owned TaskDialog identity changed')
        seen, current = set(), self.api.GetWindow(window, 4)
        while current and current not in seen and len(seen) < 32:
            if current == request['owner']:
                return
            seen.add(current)
            current = self.api.GetWindow(current, 4)
        raise AssertionError('The TaskDialog owner changed')

    def pending_dialog_is_gone(self, owner, title, *, accept):
        if self.pending is None:
            return False
        operation, previous_key = self.pending
        if previous_key != (owner, title, accept):
            raise AssertionError('A different owned confirmation operation is still pending')
        return not bool(self.api.IsWindow(operation.request['hwnd']))

    def reply(self, owner, title, *, accept):
        key = (owner, title, accept)
        if self.pending is not None:
            operation, previous_key = self.pending
            if key != previous_key:
                raise AssertionError('A different owned confirmation operation is still pending')
            try:
                complete = operation.poll()
            except BaseException:
                try:
                    operation.close()
                except Exception:
                    pass  # Retain the handle so final cleanup can retry.
                else:
                    self.pending = None
                raise
            if complete:
                self.pending = None
            return complete
        candidates = []

        @self.callback
        def visit(window, unused):
            pid = self.types.DWORD()
            self.api.GetWindowThreadProcessId(window, self.ctypes.byref(pid))
            if (pid.value != os.getpid() or not self.api.IsWindowVisible(window)
                    or not self.api.IsWindowEnabled(window)
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
        buttons, direct_ui, controls = [], [], []

        @self.callback
        def child(control, unused):
            kind = self._string(self.api.GetClassNameW, control)
            control_id = self.api.GetDlgCtrlID(control)
            visible = bool(self.api.IsWindowVisible(control))
            enabled = bool(self.api.IsWindowEnabled(control))
            if len(controls) < 32:
                controls.append(dict(hwnd=control, kind=kind, id=control_id,
                                     visible=visible, enabled=enabled))
            if visible and enabled:
                if control_id == identifier and kind.casefold() == 'button':
                    buttons.append(control)
                if kind.casefold() == 'directuihwnd':
                    direct_ui.append(control)
            return True

        self.api.EnumChildWindows(window, child, 0)
        if not buttons and len(direct_ui) == 1:
            if title in self.task_dialog_titles:
                # These two production confirmations use wx.YES_NO, with the
                # standard enabled IDYES/IDNO pair. Some Windows runner images
                # expose their DirectUI pane but no UIA descendants.
                self.pending = (TaskDialogAnswer(self, dict(
                    hwnd=window, pane=direct_ui[0], owner=owner, pid=os.getpid(),
                    title=title, button_id=identifier)), key)
                return False
            self.pending = (AccessibleAnswer(dict(
                hwnd=window, owner=owner, pid=os.getpid(), title=title,
                yes=self.yes_label, no=self.no_label,
                answer=self.yes_label if accept else self.no_label)), key)
            return False
        if len(buttons) != 1:
            print(json.dumps(dict(status='NATIVE_DIALOG_LOOKUP_FAILED',
                                  hwnd=window, owner_chain=chain, controls=controls)), flush=True)
            raise AssertionError('The owned confirmation has no unique native answer button')
        print(json.dumps(dict(status='NATIVE_DIALOG', pid=os.getpid(), hwnd=window,
                              owner_chain=chain, title=title, button=buttons[0], button_id=identifier)), flush=True)
        self.api.SetActiveWindow(window)
        # BM_CLICK sends actual native button notifications. It neither replaces
        # ShowModal nor changes its result artificially.
        if not self.api.PostMessageW(buttons[0], 0x00F5, 0, 0):
            raise AssertionError('Could not dispatch the owned native answer button')
        return True


def run_native(source=None, *, encrypted=False, report=None, report_path=None, evidence=None):
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the native Windows probe with Python -I -B.')
    checkout = Path(__file__).resolve(strict=True).parents[2]
    if source is not None and source.resolve(strict=True) != checkout:
        raise ValueError('The source override must be this exact checkout.')
    report = report if report is not None else {}
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
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import portable_sync
    from carr_library.sync import service as service_module

    try:
        application = wx.App(False)
    except (Exception, SystemExit):
        return not_run('A native wx application could not open.')
    checks, errors = report.setdefault('checks', []), []
    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (portable_sync, service_module)}
    timers = []
    state = {'finished': False}
    confirmation_titles = ('Publish all saved libraries in this data folder?',
                           'Restore the verified snapshot into a new folder?')
    windows = WindowsConfirmations(
        yes_label=wx.GetStockLabel(wx.ID_YES).replace('&', ''),
        no_label=wx.GetStockLabel(wx.ID_NO).replace('&', ''),
        task_dialog_titles=confirmation_titles) if sys.platform == 'win32' else None

    def check(label, condition):
        report['phase'] = label
        if report_path is not None:
            evidence._checkpoint(report_path, report)
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        if report_path is not None:
            evidence._checkpoint(report_path, report)
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

    from carr_library.sync import archive, crypto, vault, admission
    from carr_library import library_backup, backup_compat, backup_sidecars
    from carr_library.ui import worker_lifetime
    package = Path(carr_library.__file__).resolve(strict=True).parent
    distribution = None
    if source is None:
        owners = [candidate for candidate in importlib.metadata.distributions(name='universal-research-library')
                  if any(str(member).replace('\\', '/') == 'carr_library/__init__.py'
                         and Path(candidate.locate_file(member)).resolve(strict=True) ==
                             Path(carr_library.__file__).resolve(strict=True)
                         for member in candidate.files or ())]
        if len(owners) != 1:
            raise RuntimeError('Exactly one installed distribution must own this Sync package.')
        distribution = owners[0]
        check('modules are loaded from the installed distribution',
              all(not Path(path).is_relative_to(checkout) for path in origins.values()))
    report['module_evidence'] = evidence._module_evidence(checkout, package, (
        ('ui/portable_sync.py', portable_sync), ('sync/service.py', service_module),
        ('sync/archive.py', archive), ('sync/crypto.py', crypto),
        ('sync/vault.py', vault), ('sync/admission.py', admission),
        ('library_backup.py', library_backup), ('backup_compat.py', backup_compat),
        ('backup_sidecars.py', backup_sidecars), ('ui/worker_lifetime.py', worker_lifetime)),
        source_mode=source is not None, distribution=distribution)

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

        def owned_wx_dialog(title):
            return next((window for window in wx.GetTopLevelWindows()
                         if isinstance(window, wx.Dialog) and window.IsModal()
                         and window.GetTitle() == title and descendant(window, native.window)), None)

        def answer_password(dialog, password):
            fields = [child for child in dialog.GetChildren() if isinstance(child, wx.TextCtrl)]
            check('password entry uses named native masked controls', bool(fields) and all(
                field.GetWindowStyleFlag() & wx.TE_PASSWORD and field.GetName() for field in fields))
            for field in fields:
                field.SetValue(password or '')
            dialog.EndModal(wx.ID_OK)

        def dismiss_one_modal():
            # A successor wx modal may itself keep UIA Invoke waiting. Release it
            # before polling or changing the pending confirmation operation.
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
            if windows is not None and native is not None and native._window_alive:
                if windows.pending is not None:
                    unused_operation, (owner, title, accept) = windows.pending
                    return not windows.reply(owner, title, accept=accept)
                for title in confirmation_titles:
                    answered = windows.reply(native.window.GetHandle(), title, accept=False)
                    if answered or windows.pending is not None:
                        # Do not retarget a just-started async No operation to the
                        # other confirmation title in the same cleanup pass.
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
                        answer_successor_password(
                            responses, confirmation_gone=windows.pending_dialog_is_gone(
                                native.window.GetHandle(), title, accept=True),
                            find_dialog=owned_wx_dialog, answer=answer_password)
                        if windows.reply(native.window.GetHandle(), title, accept=True):
                            responses.pop(0)
                            if responses:
                                timers.append(wx.CallLater(20, answer))
                        elif monotonic() >= deadline:
                            raise AssertionError('Expected owned Windows confirmation was not shown')
                        else:
                            timers.append(wx.CallLater(20, answer))
                        return
                    dialog = owned_wx_dialog(title)
                    if dialog is None:
                        if monotonic() >= deadline:
                            raise AssertionError('Expected native dialog was not shown: ' + title)
                        timers.append(wx.CallLater(20, answer))
                        return
                    if title == 'Snapshot password':
                        answer_password(dialog, password)
                    else:
                        dialog.EndModal(wx.ID_YES)
                    responses.pop(0)
                    if responses:
                        timers.append(wx.CallLater(20, answer))
                except BaseException as error:
                    errors.append(type(error).__name__ + ': native modal interaction failed')
                    print(json.dumps(dict(status='FAIL', error=errors[-1], nvda='NOT_RUN')), flush=True)
                    # Only this already-failed standalone synthetic probe can be
                    # terminated; production windows and results are not involved.
                    def abort_failed_modal():
                        Event().wait(5)
                        if not state['finished']:
                            print('FAIL: native confirmation could not be released after failure', flush=True)
                            os._exit(1)
                    Thread(target=abort_failed_modal, name='failed-modal-watchdog', daemon=True).start()
                    if windows is not None:
                        try:
                            windows.close()
                        except Exception:
                            print('Native confirmation helper cleanup failed', flush=True)
                    try:
                        dismiss_one_modal()
                    except BaseException:
                        # Preserve the original failed check instead of raising
                        # a second exception from the timer's cleanup callback.
                        print('Native confirmation cleanup also failed', flush=True)

            timers.append(wx.CallLater(20, answer))

        def click(button):
            check('native command is enabled: ' + button.GetLabel(), button.IsEnabled())
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native button event: ' + button.GetLabel(), button.GetEventHandler().ProcessEvent(event))
            if errors:
                raise AssertionError(errors[0])

        def finish_job():
            check('snapshot worker finishes through the event loop', pump(lambda: not native.controller.running
                  and (windows is None or windows.pending is None)))

        password = 'Synthetic native snapshot password!' if encrypted else None
        try:
            library_menu = next(frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                                if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Library')
            item = next(item for item in library_menu.GetMenuItems()
                        if item.GetItemLabelText() == 'Portable snapshots and transfer…')
            event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
            check('real desktop Library menu opens the snapshot window', frame.GetEventHandler().ProcessEvent(event))
            native = actions.window
            check('snapshot borrows the actual existing desktop worker',
                  native.controller.executor is frame.executor)
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

            # Direct native destruction must revoke callbacks without closing
            # the borrowed desktop executor or losing completion ownership.
            actions.open()
            native = actions.window
            native.folder.SetValue(str(root / 'portable'))
            native.token.SetValue(token)
            release.clear()
            entered.clear()
            native.controller.service.inspect = held_inspect
            answer_modals([('Snapshot password', password)])
            click(native.verify_button)
            check('worker reaches actual forced-destruction boundary', pump(entered.is_set))
            owner = native.controller
            native.window.Destroy()
            application.Yield()
            check('forced native destruction revokes scope and callbacks',
                  not native._window_alive and not owner.alive and owner.cancel_event.is_set())
            release.set()
            check('closed owner acknowledges exact worker completion',
                  pump(lambda: not owner.running and owner._pending_epoch is None))
            check('borrowed desktop executor remains usable after dialog destruction',
                  frame.executor.submit(lambda: 17).result(timeout=10) == 17)
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
            if windows is not None:
                windows.close()
            frame.Close()
            application.Yield()
            password = None
    report.update(status='PASS', phase='completed', python=platform.python_version(), wx=wx.version(),
                  encrypted=encrypted, modules=origins, nvda='NOT_RUN')
    print(json.dumps(report), flush=True)
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--encrypted', action='store_true')
    parser.add_argument('--expect-installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not args.native:
        return not_run('Pass --native to request actual native execution.')
    report = dict(status='FAIL', phase='starting', checks=[], nvda='NOT_RUN',
                  python=sys.version, encrypted=args.encrypted)
    path = Path(__file__).resolve(strict=True).parents[2] / 'scripts/windows_clipboard_reader_smoke.py'
    spec = importlib.util.spec_from_file_location('_portable_native_evidence', path)
    evidence = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = evidence
    captured = path.read_bytes()
    if len(captured) > 128 * 1024:
        raise RuntimeError('The native evidence helper exceeds its source bound.')
    exec(compile(captured, str(path), 'exec'), evidence.__dict__)
    evidence._checkpoint(args.report, report)
    if (args.source is None) != args.expect_installed:
        raise ValueError('Select either this exact --source or --expect-installed.')
    finished = Event()

    def watchdog():
        if not finished.wait(180):
            print(json.dumps(dict(status='FAIL', error='Native probe exceeded its 180-second watchdog',
                                  nvda='NOT_RUN')), flush=True)
            os._exit(1)  # Standalone synthetic probe only; never terminate production.

    Thread(target=watchdog, name='native-probe-watchdog', daemon=True).start()
    try:
        return run_native(args.source.resolve() if args.source else None, encrypted=args.encrypted,
                          report=report, report_path=args.report, evidence=evidence)
    except BaseException as error:
        # Synthetic passwords must not appear in diagnostics, even on failure.
        report.update(status='FAIL', error=type(error).__name__)
        print(json.dumps(report), flush=True)
        return 1
    finally:
        finished.set()
        evidence._checkpoint(args.report, report)


if __name__ == '__main__':
    raise SystemExit(main())
