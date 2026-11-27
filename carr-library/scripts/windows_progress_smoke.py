"""Installed canonical progress, real Windows keys and durable job outcomes.

Synthetic work/files are intentional. This records native dispatch and control
behavior, not NVDA speech or listening acceptance. Run with python -I -B.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    import wx
    import carr_library.ui.progress_panel as progress_module
    import carr_library.progress as progress_contract
    import carr_library.progress_feedback as feedback_module
    import carr_library.ui.desktop_app as desktop_module
    from carr_library.application import LibraryApp

    errors, checks = [], []
    deadline = monotonic() + 100
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            return False
    app = ProbeApp(False)
    # Actual MSW simulator dispatch needs an active event loop even while this
    # bounded probe pumps it manually.
    event_loop = wx.GUIEventLoop()
    event_loop_activation = wx.EventLoopActivator(event_loop)
    simulator = wx.UIActionSimulator()

    def pump(predicate, seconds=6):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            app.Yield()
            if errors:
                raise AssertionError('; '.join(errors))
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def focus(control):
        control.SetFocus()
        check('native focus reaches ' + control.GetName(), pump(lambda: wx.Window.FindFocus() == control))

    checkout = Path(__file__).resolve().parents[1]
    for module in (progress_module, progress_contract, feedback_module, desktop_module):
        installed = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('installed exact source: ' + module.__name__, not installed.is_relative_to(checkout)
              and sha256(installed.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    frame = None
    release = Event()
    commands = Queue()
    with TemporaryDirectory(prefix='resonance-native-progress-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic progress acceptance')
        try:
            frame = desktop_module.DesktopFrame(library)
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            check('canonical desktop opens', pump(lambda: frame.active == (workspace, None) and not frame.working))
            progress = frame.progress_panel
            check('named native gauge with bounded range', isinstance(progress.gauge, wx.Gauge)
                  and progress.gauge.GetRange() == 100 and bool(progress.gauge.GetName()))
            check('progress label has an accessible name', bool(progress.text.GetName()))
            check('notification defaults to Off', progress.preferences.mode == 'off')
            output = root / 'verified-result.txt'
            completed = []

            def measured(report):
                report(0, 4, 'Processing synthetic units')
                while monotonic() < deadline:
                    if frame.cancel_event.is_set():
                        raise InterruptedError('Synthetic measured operation cancelled')
                    try:
                        value = commands.get(timeout=.05)
                    except Empty:
                        continue
                    if value == 'finish':
                        with output.open('x', encoding='utf-8') as stream:
                            stream.write('Verified synthetic durable result\n')
                            stream.flush()
                            os.fsync(stream.fileno())
                        return {'state': 'complete'}
                    report(value, 4, 'Processing synthetic units')
                raise TimeoutError('Probe worker exceeded its bounded deadline')

            def done(result):
                check('100 waits for final publication validation', progress.active_task.snapshot().percent == 99)
                check('completion callback sees durable output', output.read_text(encoding='utf-8').startswith('Verified'))
                completed.append(result)

            frame.run_job('Measured operation', measured, done, with_progress=True)
            check('actual worker reports zero of four', pump(lambda: progress.active_task.snapshot().total == 4
                  and progress.gauge.GetValue() == 0) and frame.working)
            first_task = progress.active_task
            for count, percent in ((1, 25), (2, 50), (3, 75), (4, 99)):
                focus(frame.cancel_button)
                commands.put(count)
                check(f'measured {count} of four displays {percent} percent', pump(
                    lambda: progress.active_task.snapshot().completed == count and progress.gauge.GetValue() == percent))
                check(f'automatic update {count} preserves focus', wx.Window.FindFocus() == frame.cancel_button)
                if count == 2:
                    check('OS injects current-progress shortcut', simulator.Char(ord('Q'), wx.MOD_CONTROL | wx.MOD_SHIFT))
                    check('real busy-time query dispatch', pump(lambda: wx.Window.FindFocus() == progress.text
                          and '50 percent' in progress.text.GetValue()) and frame.working)
            commands.put('finish')
            check('verified operation reaches 100 after callback', pump(lambda: not frame.working)
                  and progress.active_task.snapshot().state == 'succeeded' and progress.active_task.snapshot().percent == 100
                  and completed == [{'state': 'complete'}])

            started = Event()
            def cancellable():
                started.set()
                if not frame.cancel_event.wait(8):
                    raise TimeoutError('Cancel key was not dispatched')
                raise InterruptedError('Synthetic worker acknowledged cancellation')
            frame.run_job('Cancellable operation', cancellable)
            check('indeterminate worker starts', pump(started.is_set) and progress.active_task.snapshot().percent is None)
            try:
                first_task.update(completed=4, total=4, phase='Old worker callback')
            except progress_contract.ProgressError:
                pass
            else:
                raise AssertionError('A completed task accepted a stale worker update')
            check('stale worker cannot mutate current operation', progress.active_task.snapshot().title == 'Cancellable operation'
                  and progress.active_task.snapshot().percent is None)
            focus(frame.cancel_button)
            check('OS injects cancel button Space', simulator.Char(wx.WXK_SPACE))
            check('actual cancellation is distinct from success', pump(lambda: not frame.working)
                  and progress.active_task.snapshot().state == 'cancelled' and progress.active_task.snapshot().percent is None)

            committed = Event()
            late_output = root / 'late-cancel-result.txt'
            def committed_work():
                with late_output.open('x', encoding='utf-8') as stream:
                    stream.write('Already committed\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                committed.set()
                if not release.wait(8):
                    raise TimeoutError('Late cancellation fixture was not released')
                return {'state': 'complete'}
            frame.run_job('Late cancellation', committed_work)
            check('actual output commits before cancellation request', pump(committed.is_set) and late_output.is_file())
            focus(frame.cancel_button)
            check('OS injects late cancellation request', simulator.Char(wx.WXK_SPACE))
            check('late cancellation request arrives', pump(frame.cancel_event.is_set))
            release.set()
            check('committed success remains success after late request', pump(lambda: not frame.working)
                  and progress.active_task.snapshot().state == 'succeeded' and progress.active_task.snapshot().percent == 100)
            check('durable output survives late request', late_output.read_text(encoding='utf-8') == 'Already committed\n')
        finally:
            release.set()
            if frame is not None:
                frame.cancel_event.set()
                pump(lambda: not frame.working)
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
                app.Yield()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'actual_native_keys': True,
        'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
