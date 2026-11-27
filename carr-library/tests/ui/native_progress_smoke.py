"""Installed native gauge, real busy-time keys and actual worker-state acceptance.

Run: python -I -B tests/ui/native_progress_smoke.py --native
Explicit checkout debugging only: append --source path/to/carr-library.
Synthetic worker units and files are disclosed. Key dispatch uses the actual
wx.UIActionSimulator. This does not claim NVDA output or audible listening.
"""
from argparse import ArgumentParser
import hashlib
import json
import os
from pathlib import Path
import platform
from queue import Empty, Queue
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason,
                      'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None, *, timeout=100):
    if sys.platform not in ('win32', 'darwin') and not os.environ.get('DISPLAY'):
        return not_run('A supported native display is unavailable; native keys require X11 on Linux.')
    if os.environ.get('GDK_BACKEND') == 'wayland':
        return not_run('wx.UIActionSimulator does not support GTK Wayland.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    if not hasattr(wx, 'UIActionSimulator'):
        return not_run('The wx build has no native input simulator.')
    if source is not None:
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'src'))

    import carr_library.progress as progress_module
    import carr_library.import_progress as import_progress_module
    import carr_library.progress_preferences as preferences_module
    import carr_library.ui.progress_panel as panel_module
    import carr_library.ui.desktop_app as desktop_module
    from carr_library.application import LibraryApp
    from carr_library.progress import current_progress
    from carr_library.import_progress import import_outcome
    from carr_library.progress_preferences import ProgressPreferenceStore
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.progress_panel import ProgressSettingsDialog

    failures, checks = [], []
    deadline = monotonic() + timeout

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False

    try:
        application = ProbeApp(False)
    except (Exception, SystemExit) as exc:
        return not_run('A native wx application could not open: ' + str(exc))
    # Windows UIActionSimulator dispatch requires an active GUI event loop.
    # Keep both objects alive while this bounded probe manually pumps Yield.
    event_loop = wx.GUIEventLoop()
    event_loop_activation = wx.EventLoopActivator(event_loop)
    simulator = wx.UIActionSimulator()
    frame = None
    commands = Queue()
    release_unknown = Event()
    release_late = Event()
    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (progress_module, import_progress_module, preferences_module, panel_module, desktop_module)}
    checkout = Path(__file__).resolve().parents[2]

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def healthy():
        if failures:
            raise AssertionError('; '.join(failures))
        if monotonic() >= deadline:
            raise TimeoutError('The native progress probe exceeded its bounded timeout.')

    def pump(predicate, *, seconds=8):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            application.Yield()
            healthy()
            if predicate():
                return True
            wx.MilliSleep(10)
        healthy()
        return False

    def settle(seconds=0.3):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            application.Yield()
            healthy()
            wx.MilliSleep(10)

    def focus(control):
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() == control, seconds=3):
            raise AssertionError('Keyboard focus did not reach ' + control.GetName())

    def press(key, modifiers=wx.MOD_NONE):
        if not simulator.Char(key, modifiers):
            raise RuntimeError('The OS input backend could not inject the requested key.')
        settle()

    def source_identity():
        if source is not None:
            return
        check('progress and desktop modules are imported from the installed package',
              all(not Path(path).is_relative_to(checkout) for path in origins.values()))
        for name, origin in origins.items():
            relative = Path('src') / Path(*name.split('.')).with_suffix('.py')
            expected = checkout / relative
            check('installed source matches the exact acceptance checkout: ' + name,
                  expected.is_file() and hashlib.sha256(Path(origin).read_bytes()).digest()
                  == hashlib.sha256(expected.read_bytes()).digest())

    with TemporaryDirectory(prefix='carr-native-progress-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic native progress library')
        durable_result = root / 'completed-synthetic-steps.txt'
        started, completed_callback = Event(), Event()
        worker_binding = []

        try:
            frame = DesktopFrame(library)
            application.SetTopWindow(frame)
            frame.SetSize((1100, 900))
            frame.Show()
            frame.Raise()
            check('production desktop opens the synthetic workspace',
                  pump(lambda: frame.active == (workspace, None) and not frame.working))
            source_identity()
            panel = frame.progress_panel
            check('progress is a named native gauge with a percentage range',
                  isinstance(panel.gauge, wx.Gauge) and panel.gauge.GetRange() == 100
                  and bool(panel.gauge.GetName().strip()))
            check('phase and units use a named read-only native text control',
                  isinstance(panel.text, wx.TextCtrl)
                  and bool(panel.text.GetWindowStyleFlag() & wx.TE_READONLY)
                  and panel.text.GetName() == 'Current operation progress and phase')
            check('query remains an explicitly named native button',
                  isinstance(panel.query_button, wx.Button)
                  and panel.query_button.GetName() == 'Query current progress')
            check('application feedback defaults to Off', panel.preferences.mode == 'off')

            def measured_worker():
                task = current_progress()
                if task is None:
                    raise AssertionError('The production worker did not bind its progress task.')
                worker_binding.append(task.operation_id)
                units = []
                task.update(phase='Processing synthetic steps')
                started.set()
                while monotonic() < deadline:
                    if frame.cancel_event.is_set():
                        raise InterruptedError('Synthetic measured work cancelled.')
                    try:
                        action, value = commands.get(timeout=0.05)
                    except Empty:
                        continue
                    if action == 'advance':
                        if not len(units) <= value <= 100:
                            raise AssertionError('Invalid probe unit count.')
                        units.extend(range(len(units), value))
                        task.update(completed=len(units), phase='Processing synthetic steps')
                    elif action == 'pause':
                        task.pause()
                    elif action == 'resume':
                        task.resume()
                    elif action == 'finish':
                        if len(units) != 100:
                            raise AssertionError('Synthetic durable output is incomplete.')
                        with durable_result.open('x', encoding='ascii', newline='\n') as output:
                            output.write('\n'.join(map(str, units)) + '\n')
                            output.flush()
                            os.fsync(output.fileno())
                        return len(units)
                raise TimeoutError('The synthetic worker did not receive its bounded finish signal.')

            def completed(value):
                if value != 100 or len(durable_result.read_text(encoding='ascii').splitlines()) != 100:
                    raise AssertionError('Completion callback did not verify the durable result.')
                completed_callback.set()

            frame.run_job('Native measured progress', measured_worker, completed,
                          progress_total=100, progress_unit='synthetic steps')
            check('the actual desktop worker receives the shared task',
                  pump(started.is_set) and frame.working
                  and worker_binding == [panel.active_task.operation_id])
            check('known work initially displays zero measured units',
                  pump(lambda: panel.gauge.GetValue() == 0 and '0 of 100' in panel.text.GetValue()))
            check('only the progress query remains available through busy Main commands',
                  frame.keyboard_commands.available('progress.query')
                  and not frame.keyboard_commands.available('library.new')
                  and not frame.keyboard_commands.available('options.progress')
                  and panel.query_button.IsEnabled())

            for units, expected_percent in ((25, 25), (50, 50), (75, 75), (100, 99)):
                focus(panel.query_button)
                commands.put(('advance', units))
                check(f'actual worker reports {units} measured units as {expected_percent} percent',
                      pump(lambda: panel.gauge.GetValue() == expected_percent
                           and f'{units} of 100' in panel.text.GetValue()))
                check(f'automatic native update at {units} units preserves keyboard focus',
                      wx.Window.FindFocus() == panel.query_button)
                if units == 25:
                    press(ord('Q'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                    check('actual query shortcut works while the operation is busy',
                          frame.working and pump(lambda: wx.Window.FindFocus() == panel.text)
                          and '25' in panel.text.GetValue())
                if units == 50:
                    commands.put(('pause', None))
                    check('pause is a native status distinct from completion',
                          pump(lambda: 'paused' in panel.text.GetValue().casefold())
                          and panel.gauge.GetValue() == 50)
                    check('native mute remains available while the worker is paused',
                          isinstance(panel.mute, wx.CheckBox) and panel.mute.IsEnabled()
                          and panel.mute.GetName() == 'Mute extra progress feedback while work continues')
                    focus(panel.mute)
                    press(wx.WXK_SPACE)
                    check('actual busy-time mute key persists the explicit user preference',
                          frame.working and panel.preferences.muted
                          and ProgressPreferenceStore(panel.store.path).load().muted)
                    press(wx.WXK_SPACE)
                    check('actual busy-time unmute key restores the preference without enabling sound',
                          not panel.preferences.muted and panel.preferences.mode == 'off'
                          and not ProgressPreferenceStore(panel.store.path).load().muted)
                    commands.put(('resume', None))
                    check('resume returns to the measured running operation',
                          pump(lambda: panel.active_task.snapshot().state == 'running'
                               and 'running' in panel.text.GetValue().casefold()))

            check('100 processed units do not claim durable success prematurely',
                  frame.working and panel.gauge.GetValue() == 99 and not durable_result.exists())
            commands.put(('finish', None))
            check('only actual durable success produces the native 100 value',
                  pump(lambda: not frame.working and panel.active_task.snapshot().state == 'succeeded'
                       and panel.gauge.GetValue() == 100)
                  and completed_callback.is_set() and durable_result.is_file())

            unknown_started = Event()

            def unknown_worker():
                task = current_progress()
                if task is None:
                    raise AssertionError('Unknown-duration worker has no shared task.')
                task.update(phase='Unknown-size operation')
                unknown_started.set()
                while monotonic() < deadline and not release_unknown.wait(0.02):
                    if frame.cancel_event.is_set():
                        raise InterruptedError('Synthetic unknown-duration cancellation acknowledged.')
                if frame.cancel_event.is_set():
                    raise InterruptedError('Synthetic unknown-duration cancellation acknowledged.')
                raise TimeoutError('Native probe released its unknown-duration worker during cleanup.')

            frame.run_job('Native unknown progress', unknown_worker)
            check('unknown-duration work remains explicitly indeterminate',
                  pump(lambda: unknown_started.is_set() and 'indeterminate' in panel.text.GetValue().casefold())
                  and panel.active_task.snapshot().percent is None)
            focus(panel.query_button)
            press(ord('Q'), wx.MOD_CONTROL | wx.MOD_SHIFT)
            check('busy query also reports unknown-duration work without a made-up percentage',
                  pump(lambda: wx.Window.FindFocus() == panel.text)
                  and panel.active_task.snapshot().percent is None)
            focus(frame.cancel_button)
            press(wx.WXK_SPACE)
            check('actual native cancel button reaches confirmed cancelled state',
                  pump(lambda: not frame.working and panel.active_task.snapshot().state == 'cancelled'
                       and 'cancelled' in panel.text.GetValue().casefold())
                  and panel.active_task.snapshot().percent != 100)

            late_committed = Event()
            late_output = root / 'late-cancellation-committed.txt'

            def late_cancellation_worker():
                task = current_progress()
                with late_output.open('x', encoding='ascii') as output:
                    output.write('Durable synthetic result preceded the cancellation request.\n')
                    output.flush()
                    os.fsync(output.fileno())
                task.advance()
                late_committed.set()
                if not release_late.wait(8):
                    raise TimeoutError('Probe did not release its already committed worker.')
                return True  # Cancellation arrived after actual durable work.

            frame.run_job('Native late cancellation', late_cancellation_worker, progress_total=1)
            check('late-cancel fixture has actually committed before requesting cancellation',
                  pump(late_committed.is_set) and late_output.is_file()
                  and panel.active_task.snapshot().percent == 99)
            focus(frame.cancel_button)
            press(wx.WXK_SPACE)
            check('the late cancellation request remains distinct from acknowledgement',
                  panel.active_task.snapshot().state == 'cancelling' and frame.working)
            release_late.set()
            check('a successfully committed late-cancel result is reported truthfully as success',
                  pump(lambda: not frame.working and panel.active_task.snapshot().state == 'succeeded'
                       and panel.gauge.GetValue() == 100)
                  and 'before cancellation' in panel.text.GetValue().casefold())

            cancelled_source = root / 'cancelled-native-import.txt'
            cancelled_bytes = b'Exact native cancelled import source.\r\n'
            cancelled_source.write_bytes(cancelled_bytes)
            returned = []
            frame.run_job('Native returned import cancellation', lambda: library.import_file(
                workspace, cancelled_source, cancelled=lambda: True), returned.append,
                progress_result=import_outcome)
            check('an actual returned import cancellation never becomes native 100-percent success',
                  pump(lambda: not frame.working and panel.active_task.snapshot().state == 'cancelled')
                  and len(returned) == 1 and returned[0]['state'] == 'cancelled'
                  and panel.gauge.GetValue() != 100 and cancelled_source.read_bytes() == cancelled_bytes
                  and not library.search(workspace, 'cancelled'))

            quarantined = {'state': 'quarantined', 'message': 'Synthetic review-required import fixture.'}
            returned.clear()
            frame.run_job('Native returned page-review result', lambda: quarantined, returned.append,
                          progress_result=import_outcome)
            check('a returned review-required result keeps its callback and shows incomplete progress',
                  pump(lambda: not frame.working and panel.active_task.snapshot().state == 'failed')
                  and returned == [quarantined] and panel.gauge.GetValue() != 100
                  and 'page review' in panel.text.GetValue().casefold())

            def failing_worker():
                current_progress().update(phase='Synthetic publication')
                raise ValueError('Synthetic native publication failure')

            frame.run_job('Native failed progress', failing_worker)
            check('failed publication cannot reuse the preceding success value',
                  pump(lambda: not frame.working and panel.active_task.snapshot().state == 'failed'
                       and 'failed' in panel.text.GetValue().casefold())
                  and panel.active_task.snapshot().percent != 100 and panel.gauge.GetValue() != 100)
            check('normal commands are restored after the operation ends',
                  frame.keyboard_commands.available('library.new')
                  and frame.keyboard_commands.available('options.progress'))

            def preferences_session(edit):
                settings = ProgressSettingsDialog(frame, panel)
                errors = []

                def interact():
                    try:
                        check('feedback preferences use a real modal native dialog', settings.dialog.IsModal())
                        edit(settings)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        if settings.dialog.IsModal() and settings.dialog.IsShown():
                            settings.dialog.EndModal(wx.ID_CANCEL)
                wx.CallAfter(interact)
                result = settings.show()
                if errors:
                    raise errors[0]
                return result

            previous_preferences = panel.preferences
            previous_preference_bytes = panel.store.path.read_bytes() if panel.store.path.exists() else None

            def cancel_settings(settings):
                settings.interval.SetValue(3)
                focus(settings.interval)
                press(wx.WXK_ESCAPE)

            check('actual Escape cancels the native preference draft', not preferences_session(cancel_settings))
            check('cancelling preferences does not persist or change the active profile',
                  panel.preferences == previous_preferences
                  and (panel.store.path.read_bytes() if panel.store.path.exists() else None) == previous_preference_bytes)

            def save_settings(settings):
                # Values are controlled synthetic inputs; Save is a real OS key
                # event. Keep mode Off so CI does not pretend to listen to audio.
                settings.interval.SetValue(4)
                settings.volume.SetValue(45)
                settings.muted.SetValue(True)
                save = next(control for control in settings.dialog.GetChildren()
                            if isinstance(control, wx.Button) and control.GetId() == wx.ID_OK)
                focus(save)
                press(wx.WXK_SPACE)

            check('actual native Save commits the feedback preference draft', preferences_session(save_settings))
            persisted = ProgressPreferenceStore(panel.store.path).load()
            check('native preferences persist exact explicit settings without enabling sound',
                  persisted == panel.preferences and persisted.mode == 'off'
                  and persisted.interval_seconds == 4 and persisted.volume == 45 and persisted.muted)
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                              'platform': platform.system(), 'origins': origins,
                              'source_override': str(source) if source else None,
                              'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, sort_keys=True), flush=True)
        finally:
            release_unknown.set()
            release_late.set()
            if frame is not None:
                frame.cancel_event.set()
                commands.put(('finish', None))
                if getattr(frame, 'progress_panel', None) is not None:
                    frame.progress_panel.close()
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
                application.Yield()
            # Destruction restores the prior active loop after all windows and
            # queued callbacks have finished; these locals stay alive until now.
            del event_loop_activation
            del event_loop
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, default=None)
    args = parser.parse_args()
    return run_native(args.source.resolve() if args.source is not None else None)


if __name__ == '__main__':
    raise SystemExit(main())
