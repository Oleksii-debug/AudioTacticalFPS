"""Installed desktop progress smoke with actual wx controls and menu events.

Installed wheel: python -I tests/ui/probe_operation_progress_native.py --native
Checkout debugging: add --source /absolute/path/to/carr-library
The desktop must install its own progress panel; this probe never adds one.
Physical sound/SAPI and NVDA are NOT_RUN. A recording adapter observes only the
independent feedback worker during the explicit media-suppression checks.
"""
from argparse import ArgumentParser
import io
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread, get_ident
from time import monotonic
import wave


def not_run(reason):
    print(json.dumps(dict(status='NOT_RUN', reason=reason, nvda='NOT_RUN', physical_audio='NOT_RUN')), flush=True)
    return 77


def run_native(source=None):
    if sys.platform != 'win32' and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('A native display is unavailable.')
    try:
        import wx
    except (ImportError, OSError):
        return not_run('Native wxPython is unavailable.')
    if source is not None:
        sys.path.insert(0, str(source / 'src'))
    from carr_library import application as application_module
    from carr_library import progress as progress_module
    from carr_library.ui import desktop_app, operation_progress
    from carr_library.progress import FeedbackPreferences, ProgressPreferenceStore

    errors, checks, timers = [], [], []

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append('An unhandled native callback failed.')
            return False

    try:
        application = ProbeApp(False)
    except (Exception, SystemExit):
        return not_run('A native wx application could not open.')

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def pump(predicate, timeout=5):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            application.Yield()
            if errors:
                raise AssertionError(errors[0])
            if predicate():
                return True
            wx.MilliSleep(5)
        return False

    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (application_module, progress_module, desktop_app, operation_progress)}
    if source is None:
        checkout = Path(__file__).resolve().parents[2]
        check('all probe modules come from the installed distribution',
              all(not Path(path).is_relative_to(checkout)
                  and any(part in ('site-packages', 'dist-packages') for part in Path(path).parts)
                  for path in origins.values()))

    with TemporaryDirectory(prefix='carr-native-progress-') as temporary:
        root = Path(temporary)
        library = application_module.LibraryApp(root / 'first-library')
        library.create_workspace('Native progress library')
        other = application_module.LibraryApp(root / 'second-library')
        other.create_workspace('Native progress second library')
        frame = desktop_app.DesktopFrame(library)
        frame.SetSize((1000, 900))
        frame.Show()
        frame.Raise()
        panel = None
        closed = False
        ui_thread = get_ident()

        def descendant(window, parent):
            seen = set()
            while window is not None and id(window) not in seen:
                if window is parent:
                    return True
                seen.add(id(window))
                window = window.GetParent()
            return False

        def dismiss_one_modal():
            windows = []
            for window in wx.GetTopLevelWindows():
                try:
                    if isinstance(window, wx.Dialog) and window.IsModal() and descendant(window, frame):
                        depth, current = 0, window
                        while current is not frame:
                            depth += 1
                            current = current.GetParent()
                        windows.append((depth, window))
                except RuntimeError:
                    continue
            if windows:
                max(windows, key=lambda item: item[0])[1].EndModal(wx.ID_CANCEL)
                return True
            return False

        def choose_preferences():
            deadline = monotonic() + 8

            def answer():
                try:
                    dialog = next((window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.Dialog) and window.IsModal()
                        and window.GetTitle() == 'Operation progress feedback' and descendant(window, frame)), None)
                    if dialog is None:
                        if monotonic() > deadline:
                            raise AssertionError('The real feedback preference dialog did not appear.')
                        timers.append(wx.CallLater(20, answer))
                        return
                    controls = {}
                    def collect(parent):
                        for child in parent.GetChildren():
                            controls[child.GetName()] = child
                            collect(child)
                    collect(dialog)
                    controls['Feedback mode'].SetSelection(1)  # Beep, muted below.
                    controls['Minimum interval in seconds (1–120)'].SetValue(7)
                    controls['Notification volume (0–100)'].SetValue(21)
                    controls['Mute operation audio feedback'].SetValue(True)
                    controls['Allow feedback while this app is in the background'].SetValue(False)
                    button = dialog.FindWindowById(wx.ID_OK)
                    check('preferences use a real enabled native OK control', button is not None and button.IsEnabled())
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_OK)
                    event.SetEventObject(button)
                    check('the native OK event saves preferences', button.GetEventHandler().ProcessEvent(event))
                except BaseException as error:
                    errors.append(str(error))
                    dismiss_one_modal()
            timers.append(wx.CallLater(20, answer))

        def menu_event(label):
            options = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'options'), None)
            check('the desktop exposes its Options menu', options is not None)
            item = next((item for item in options.GetMenuItems()
                         if item.GetItemLabelText().replace('&', '') == label), None)
            check('native menu item exists: ' + label, item is not None)
            event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
            check('native menu event handled: ' + label, frame.GetEventHandler().ProcessEvent(event))
            if errors:
                raise AssertionError(errors[0])

        try:
            actions = getattr(frame, 'progress_actions', None)
            check('DesktopFrame installs the real progress actions', isinstance(actions, operation_progress.ProgressActions))
            panel = actions.panel
            check('progress has a real parent-owned native panel, gauge and read-only text',
                  isinstance(panel.window, wx.Panel) and panel.window.GetParent() is frame.host
                  and isinstance(panel.gauge, wx.Gauge) and isinstance(panel.text, wx.TextCtrl)
                  and not panel.text.IsEditable())
            check('progress controls have explicit accessible names', all(control.GetName().strip()
                  for control in (panel.window, panel.gauge, panel.text)))
            check('notification preferences start Off', actions.preferences == FeedbackPreferences())
            frame.choice.SetFocus()
            check('probe starts with focus in the library selector', pump(lambda: wx.Window.FindFocus() is frame.choice))
            handle = panel.start('native-counts', 'Native counted task', 'Processing synthetic units', total=100)
            stable = True
            for completed in range(100):
                panel.update(handle, completed=completed)
                application.Yield()
                stable = stable and wx.Window.FindFocus() is frame.choice
                wx.MilliSleep(3)
            check('100 producer updates leave keyboard focus unchanged', stable)
            check('real native gauge reaches the observed 99 percent', pump(lambda: panel.gauge.GetValue() == 99))
            panel.update(handle, completed=100)
            check('all units wait for final verification', pump(lambda: 'final verification pending' in panel.text.GetValue()))
            check('unit completion alone never reports 100 percent',
                  panel.hub.snapshot()[-1].percent is None and '100%; verified complete' not in panel.text.GetValue())
            panel.finish(handle, verified=True)
            check('verified finish reaches the real 100-percent gauge', pump(lambda: panel.gauge.GetValue() == 100
                  and '100%; verified complete' in panel.text.GetValue()))
            check('timer-rendered completion keeps selector focus', wx.Window.FindFocus() is frame.choice)

            unknown = panel.start('native-unknown', 'Native unknown task', 'Discovering synthetic inputs')
            check('unknown total is named honestly in the native text', pump(lambda: 'Total unknown' in panel.text.GetValue()))
            panel.pause(unknown)
            check('pause becomes visible and stops the indeterminate gauge', pump(lambda: 'Paused' in panel.text.GetValue()
                  and panel.gauge.GetValue() == 0))
            panel.resume(unknown)
            check('resume is visible without an invented percentage', pump(lambda: 'Resumed' in panel.text.GetValue())
                  and panel.hub.snapshot()[-1].percent is None)
            panel.finish(unknown, state='cancelled')
            check('cancellation remains distinct from verified completion', pump(lambda: 'Cancelled' in panel.text.GetValue())
                  and panel.hub.snapshot()[-1].state == 'cancelled')
            menu_event('Query current operation progress')
            check('explicit query moves focus to readable progress text', pump(lambda: wx.Window.FindFocus() is panel.text))

            choose_preferences()
            menu_event('Operation feedback preferences...')
            desired = FeedbackPreferences('Beep', 7, 21, True, False)
            saved = (library.root / progress_module.FILENAME).read_bytes()
            check('native preferences persist exact validated settings',
                  ProgressPreferenceStore(library.root).load() == desired)
            old_feedback = panel.feedback
            actions.reload()
            check('explicit preference reload replaces the old audio worker',
                  old_feedback._closed and panel.feedback is not old_feedback and actions.preferences == desired)

            class RecordingAdapter:
                def __init__(self):
                    self.calls = []
                def play(self, raw, *, cancelled):
                    if not cancelled():
                        with wave.open(io.BytesIO(raw), 'rb') as stream:
                            assert stream.getframerate() == 24000 and stream.getnchannels() == 1
                            assert stream.getnframes() == 3840
                        self.calls.append(get_ident())
                def speak(self, *args, **kwargs):
                    raise AssertionError('Speech is not part of this native control smoke.')

            adapter = RecordingAdapter()
            panel.feedback.adapter = adapter
            panel.feedback.configure(FeedbackPreferences('Beep', 1, 20, False, True))
            media = panel.start('native-media', 'Native media suppression', total=10)
            current = panel.update(media, completed=1)
            panel.set_media_state(playback=True)
            check('playback flag suppresses feedback before worker dispatch', not panel.feedback.submit(current, manual=True))
            panel.set_media_state(recording=True)
            check('recording flag suppresses feedback before worker dispatch', not panel.feedback.submit(current, manual=True))
            check('suppressed notifications produce no captured output', adapter.calls == [])
            panel.set_media_state()
            check('cleared media state permits independent feedback', panel.feedback.submit(current, manual=True))
            check('notification runs in its own worker and carries actual bounded PCM', pump(lambda: bool(adapter.calls))
                  and all(identity != ui_thread for identity in adapter.calls))
            panel.feedback.configure(desired)

            old_handle = panel.start('native-root-change', 'Old data folder task')
            old_feedback = panel.feedback
            check('real desktop opens another existing library data folder', frame._open_data_root(other.root))
            check('data-folder switch withdraws old progress handles',
                  panel.update(old_handle, completed=1) is None and panel.hub.snapshot() == ())
            check('new folder starts with its own quiet preferences and worker',
                  old_feedback._closed and actions.preferences == FeedbackPreferences()
                  and (library.root / progress_module.FILENAME).read_bytes() == saved
                  and not (other.root / progress_module.FILENAME).exists())
            late = panel.start('native-destroy', 'Pending task during forced destruction')
            panel.window.Destroy()
            check('native destruction stops the progress timer and audio worker', pump(lambda: not panel.alive)
                  and not panel.timer.IsRunning() and panel.feedback._closed)
            check('late producer callback after native destruction is ignored', panel.update(late, completed=5) is None)
        finally:
            for timer in timers:
                timer.Stop()
            while dismiss_one_modal():
                application.Yield()
            if panel is not None:
                panel.dispose()
            if not closed:
                frame.Close()
                application.Yield()
                closed = True

    check('no native callback errors were hidden', not errors)
    print(json.dumps(dict(status='PASS', checks=checks, python=platform.python_version(), wx=wx.version(),
        modules=origins, physical_audio='NOT_RUN', sapi='NOT_RUN', nvda='NOT_RUN',
        audio_adapter='Recording adapter only for media suppression; real PCM and worker'), ensure_ascii=True), flush=True)
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    if not args.native:
        return not_run('Pass --native to request actual native execution.')
    finished = Event()

    def watchdog():
        if not finished.wait(60):
            print(json.dumps(dict(status='FAIL', error='Native progress probe exceeded its 60-second watchdog',
                                  nvda='NOT_RUN')), flush=True)
            os._exit(1)  # Standalone synthetic probe only, never production.

    Thread(target=watchdog, name='native-progress-watchdog', daemon=True).start()
    try:
        return run_native(args.source.resolve() if args.source else None)
    except BaseException as error:
        print(json.dumps(dict(status='FAIL', error=f'{type(error).__name__}: {error}',
                              nvda='NOT_RUN', physical_audio='NOT_RUN')), flush=True)
        return 1
    finally:
        finished.set()


if __name__ == '__main__':
    raise SystemExit(main())
