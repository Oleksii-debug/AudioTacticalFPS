"""Installed native progress, real Windows keys and UIA range verification.

Synthetic work and files are deliberate. This is not manual NVDA or listening
acceptance. Run after installing the exact application wheel with python -I -B.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback


_UIA = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$request = ConvertFrom-Json ([Console]::In.ReadToEnd())
$element = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]([Int64]$request.handle))
$range = $element.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern)
[ordered]@{
    name = $element.Current.Name
    control = $element.Current.ControlType.ProgrammaticName
    value = $range.Current.Value
    minimum = $range.Current.Minimum
    maximum = $range.Current.Maximum
    read_only = $range.Current.IsReadOnly
} | ConvertTo-Json -Compress
'''


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    import wx
    import carr_library.progress as progress_module
    import carr_library.progress_audio as audio_module
    import carr_library.ui.progress_view as lifecycle_module
    import carr_library.ui.operation_progress as view_module
    import carr_library.ui.desktop_app as desktop_module
    from carr_library.application import LibraryApp

    errors, checks = [], []
    deadline = monotonic() + 120

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            return False

    app = ProbeApp(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    simulator = wx.UIActionSimulator()

    def pump(predicate, seconds=8):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            app.Yield()
            if errors:
                raise AssertionError('; '.join(errors))
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def check(name, valid):
        if not valid:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def uia_range(control):
        # Query from a separate process while the GUI continues pumping.
        result = Queue()
        handle = int(control.GetHandle())
        executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        def inspect():
            try:
                child = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Sta', '-Command', _UIA],
                    input=json.dumps({'handle': handle}), capture_output=True, text=True,
                    encoding='utf-8', timeout=20)
                if child.returncode or len(child.stdout) > 16384:
                    raise RuntimeError('Native UIA query failed: ' + child.stderr[:2000])
                result.put(json.loads(child.stdout.lstrip('\ufeff')))
            except BaseException as error:
                result.put(error)
        Thread(target=inspect, name='native-uia-query', daemon=True).start()
        check('UIA observer completes while GUI stays responsive', pump(lambda: not result.empty(), 22))
        value = result.get_nowait()
        if isinstance(value, BaseException):
            raise value
        return value

    checkout = Path(__file__).resolve().parents[1]
    prefix = Path(sys.prefix).resolve()
    for module in (progress_module, audio_module, lifecycle_module, view_module, desktop_module):
        installed = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('installed exact source: ' + module.__name__,
              installed.is_relative_to(prefix) and not installed.is_relative_to(checkout)
              and sha256(installed.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    frame = None
    view = None
    commands = Queue()
    release = Event()
    with TemporaryDirectory(prefix='resonance-shared-progress-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic native progress')
        try:
            frame = desktop_module.DesktopFrame(library)
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            check('current desktop opens', pump(lambda: frame.active == (workspace, None) and not frame.working))
            view = frame.operation_progress
            check('same shared hub and named native gauge',
                  view is frame.progress_view and view.hub is frame.progress_hub and isinstance(view.gauge, wx.Gauge)
                  and view.gauge.GetRange() == 100 and bool(view.gauge.GetName()))
            check('notification default is Off without an audio thread',
                  view.preferences.mode == 'off' and view.notifications._thread is None)
            frame.keyboard_commands.apply_overrides({'progress.query': 'Ctrl+Shift+Q',
                                                       'progress.mute': 'Ctrl+Alt+Shift+F8'})
            output = root / 'verified-result.txt'
            completed = []

            def measured():
                reporter = progress_module.current_progress()
                if reporter is None:
                    raise AssertionError('The actual worker lost its shared progress binding.')
                reporter.update(total=4, completed=0, phase='Retaining synthetic units')
                while monotonic() < deadline:
                    if frame.cancel_event.is_set():
                        raise InterruptedError('Synthetic operation cancelled')
                    try:
                        value = commands.get(timeout=.05)
                    except Empty:
                        continue
                    if value == 'finish':
                        with output.open('x', encoding='utf-8') as stream:
                            stream.write('Verified synthetic durable result\n')
                            stream.flush()
                            os.fsync(stream.fileno())
                        return {'status': 'completed'}
                    reporter.update(completed=value, phase='Retaining synthetic units')
                raise TimeoutError('The synthetic worker exceeded its bounded deadline')

            def done(result):
                check('completion callback sees committed bytes',
                      output.read_text(encoding='utf-8') == 'Verified synthetic durable result\n')
                completed.append(result)

            frame.run_job('Measured operation', measured, done, progress_unit='files')
            check('actual background operation starts with one reporter',
                  pump(lambda: len(view.hub.snapshot()) == 1 and view.hub.snapshot()[0].total == 4))
            task_id = view.hub.snapshot()[0].operation_id
            for count, percent in ((1, 25), (2, 50), (3, 75), (4, 99)):
                frame.cancel_button.SetFocus()
                check('cancel control receives native focus', pump(lambda: wx.Window.FindFocus() == frame.cancel_button))
                commands.put(count)
                check(f'actual measured progress displays {percent} percent',
                      pump(lambda: view.hub.get(task_id).completed == count and view.gauge.GetValue() == percent))
                check(f'timed update {count} preserves focus', wx.Window.FindFocus() == frame.cancel_button)
                if count == 2:
                    data = uia_range(view.gauge)
                    check('native UIA exposes named read-only progress range and 50 percent',
                          bool(data['name']) and data['control'] == 'ControlType.ProgressBar'
                          and data['minimum'] == 0 and data['maximum'] == 100
                          and data['read_only'] and data['value'] == 50)
                    check('OS injects configured query shortcut',
                          simulator.Char(ord('Q'), wx.MOD_CONTROL | wx.MOD_SHIFT))
                    check('busy-time semantic query focuses current progress',
                          pump(lambda: wx.Window.FindFocus() == view.text and '50%' in view.text.GetValue()))
                    check('OS injects configured mute shortcut',
                          simulator.Char(wx.WXK_F8, wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT))
                    check('busy-time mute persists without stopping work',
                          pump(lambda: view.preferences.muted) and frame.working
                          and progress_module.load_preferences(library.root).muted)
            check('99 does not claim publication', not output.exists() and not view.hub.get(task_id).verified)
            commands.put('finish')
            check('verified publication reaches native 100',
                  pump(lambda: not frame.working and view.gauge.GetValue() == 100)
                  and view.hub.get(task_id).verified and completed == [{'status': 'completed'}])
            data = uia_range(view.gauge)
            check('UIA confirms 100 only after committed success', data['value'] == 100 and output.exists())

            started = Event()
            def cancellable():
                started.set()
                if not frame.cancel_event.wait(8):
                    raise TimeoutError('Cancel key did not dispatch')
                raise InterruptedError('Synthetic child acknowledged Stop')
            frame.run_job('Unknown total', cancellable)
            check('unknown work is visibly indeterminate',
                  pump(lambda: started.is_set() and 'indeterminate' in view.gauge.GetName())
                  and view.selected_event().percent is None)
            frame.cancel_button.SetFocus()
            check('cancel focus is ready', pump(lambda: wx.Window.FindFocus() == frame.cancel_button))
            check('OS injects cancel button Space', simulator.Char(wx.WXK_SPACE))
            check('cancelled work stays distinct from completion',
                  pump(lambda: not frame.working and view.selected_event().state == 'cancelled')
                  and not view.selected_event().verified)

            committed = Event()
            late_output = root / 'late-cancel.txt'
            def committed_work():
                with late_output.open('x', encoding='utf-8') as stream:
                    stream.write('Already committed\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                committed.set()
                if not release.wait(8):
                    raise TimeoutError('Late-cancel fixture was not released')
                return {'status': 'completed'}
            frame.run_job('Late cancellation', committed_work)
            check('real output commits before late cancellation', pump(committed.is_set) and late_output.is_file())
            frame.cancel_button.SetFocus()
            check('late cancel focus is ready', pump(lambda: wx.Window.FindFocus() == frame.cancel_button))
            check('OS injects late cancellation request', simulator.Char(wx.WXK_SPACE))
            check('late cancellation reaches the current operation', pump(frame.cancel_event.is_set))
            release.set()
            check('accepted committed result survives late request',
                  pump(lambda: not frame.working and view.selected_event().verified)
                  and late_output.read_text(encoding='utf-8') == 'Already committed\n')

            settings_done = []
            settings_deadline = min(deadline, monotonic() + 12)
            def save_settings():
                if monotonic() >= settings_deadline:
                    errors.append('Native progress settings exceeded its bounded deadline.')
                    for window in wx.GetTopLevelWindows():
                        if isinstance(window, wx.Dialog) and window.IsModal():
                            window.EndModal(wx.ID_CANCEL)
                    return
                dialog = next((window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.GetTitle() == 'Progress feedback'), None)
                if dialog is None:
                    wx.CallLater(25, save_settings)
                    return
                try:
                    interval = wx.FindWindowByName('Notification interval in seconds, 0.5 to 60', dialog)
                    volume = wx.FindWindowByName('Notification volume, 0 to 1', dialog)
                    mode = wx.FindWindowByName('Progress audio mode', dialog)
                    check('native settings controls expose accessible names', all((interval, volume, mode)))
                    mode.SetSelection(0)  # Preserve quiet native acceptance; no listening claim.
                    interval.SetValue('2.5')
                    volume.SetValue('0.17')
                    button = dialog.FindWindowById(wx.ID_OK)
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                    event.SetEventObject(button)
                    button.GetEventHandler().ProcessEvent(event)
                    settings_done.append(True)
                except BaseException:
                    errors.append(traceback.format_exc())
                    dialog.EndModal(wx.ID_CANCEL)
            wx.CallLater(25, save_settings)
            view.settings()
            check('native settings save exact preferences',
                  settings_done and not errors and progress_module.load_preferences(library.root)
                  == progress_module.ProgressPreferences(interval_seconds=2.5, volume=.17, muted=True))
            check('default-off probe never starts notification audio', view.notifications._thread is None)
        finally:
            primary_error = sys.exc_info()[0] is not None
            release.set()
            if frame is not None:
                try:
                    frame.cancel_event.set()
                    pump(lambda: not frame.working)
                    frame.executor.shutdown(wait=True, cancel_futures=True)
                    frame.Destroy()
                    app.Yield()
                    if view is not None:
                        check('native destruction closes progress lifecycle',
                              view.closed and view.notifications._closed)
                except BaseException:
                    if not primary_error:
                        raise
                    print('Secondary native cleanup failure:\n' + traceback.format_exc(), file=sys.stderr)
    print(json.dumps({'status': 'PASS', 'checks': checks, 'actual_native_keys': True,
        'native_UIA_range': True, 'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
