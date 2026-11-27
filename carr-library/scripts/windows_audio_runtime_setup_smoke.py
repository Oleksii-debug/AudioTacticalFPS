"""Installed native audio setup using the already prepared exact Windows pack.

No network acquisition or neural inference is performed. A controlled progress
observer holds an actual verification boundary long enough to dispatch Cancel;
the verifier, settings publication and application worker remain production code.
"""
import argparse
import faulthandler
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def _run_probe(args, report):
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import local_runtime, windows_setup, runtime
    from carr_library.runtime_settings import load_settings, settings_path
    from carr_library.ui import audio_runtime_setup, desktop_app
    from carr_library.audio.render import tool_path

    checks = report['checks']
    failures, timers, source_hashes = [], [], {}
    frame = None
    original_prepare = audio_runtime_setup.prepare_local_audio_runtime
    deadline = monotonic() + report['independent_deadline_seconds']
    state = {}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def abort():
        for window in reversed(wx.GetTopLevelWindows()):
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return False

    gui = ProbeApp(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    simulator = wx.UIActionSimulator()

    def guarded(callback):
        if failures:
            abort()
            return
        try:
            if monotonic() > deadline:
                raise TimeoutError('Native local audio setup exceeded its deadline.')
            callback()
        except BaseException:
            failures.append(traceback.format_exc())
            abort()

    def later(callback, milliseconds=25):
        timer = wx.CallLater(milliseconds, lambda: guarded(callback))
        timers.append(timer)

    def wait(predicate, done, label, seconds=90):
        until = min(deadline, monotonic() + seconds)
        def tick():
            if predicate():
                done()
            elif monotonic() >= until:
                raise TimeoutError(label)
            else:
                later(tick)
        later(tick)

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def named(name):
        return next(child for child in children(state['dialog']) if child.GetName() == name)

    def key(control, code, done=lambda: None):
        control.SetFocus()
        def send():
            check('native key dispatch ' + str(code), simulator.Char(code))
            later(done)
        wait(lambda: wx.Window.FindFocus() is control, send, 'Native focus was not acquired.', seconds=8)

    def status():
        return named('Local audio setup status').GetValue()

    def cancel_verified_operation():
        check('actual shared worker reached a verification boundary', state['reached'].is_set() and frame.working)
        check('progress query stays enabled while setup is busy', named('Current operation progress').IsEnabled())
        def queried():
            check('modal progress query presents actual current verification',
                  'Verifying' in status() and frame.working)
            named('Local audio setup status').ChangeValue('Waiting for the configured progress shortcut.')
            check('configured default progress shortcut is native',
                  simulator.Char(ord('G'), wx.MOD_CONTROL | wx.MOD_ALT))
            def queried_by_shortcut():
                check('configured shortcut updates the modal progress result', 'Verifying' in status())
                key(named('Cancel current local audio setup'), wx.WXK_SPACE,
                    lambda: wait(lambda: not frame.working, cancelled, 'Verification cancellation did not finish.'))
            later(queried_by_shortcut)
        key(named('Current operation progress'), wx.WXK_SPACE, queried)

    def cancelled():
        check('cancelled verification retains exact settings bytes',
              state['settings_path'].read_bytes() == state['settings_bytes'])
        check('cancelled verification retains the previous active owner',
              frame.audio_runtime_binding is state['binding'])
        check('cancellation has a readable native result', 'cancel' in status().casefold())
        check('selected FFmpeg remains the same actual executable',
              Path(tool_path('ffmpeg')) == args.runtime.absolute() / 'bin/ffmpeg.exe')
        audio_runtime_setup.prepare_local_audio_runtime = original_prepare
        close = next(child for child in children(state['dialog']) if child.GetId() == wx.ID_CLOSE)
        key(close, wx.WXK_SPACE)

    def selected():
        check('native setup saved and activated the complete runtime', 'verified and active' in status())
        value = load_settings(frame.library.root / 'state')
        manifest = args.runtime / runtime.MANIFEST
        check('selected manifest is bound to actual bytes',
              value[local_runtime.SEAL] == sha256(manifest.read_bytes()).hexdigest())
        check('native setup uses the current library binding',
              frame.audio_runtime_binding.directory == args.runtime.absolute())
        check('shared operation progress is installed', hasattr(frame, 'operation_progress'))
        state['binding'] = frame.audio_runtime_binding
        state['settings_path'] = settings_path(frame.library.root / 'state')
        state['settings_bytes'] = state['settings_path'].read_bytes()
        state['reached'] = threading.Event()
        def observing_prepare(*args, **kwargs):
            real_progress = kwargs.get('progress')
            cancel = kwargs['cancelled']
            def observed(phase, completed, total):
                if real_progress:
                    real_progress(phase, completed, total)
                if not state['reached'].is_set():
                    state['reached'].set()
                    until = monotonic() + 10
                    while not cancel() and monotonic() < until:
                        threading.Event().wait(.02)
                    if not cancel():
                        raise TimeoutError('The native Cancel control was not dispatched.')
            return original_prepare(*args, **{**kwargs, 'progress': observed})
        audio_runtime_setup.prepare_local_audio_runtime = observing_prepare
        key(named('Verify current registration'), wx.WXK_SPACE,
            lambda: wait(state['reached'].is_set, cancel_verified_operation,
                         'Actual inventory verification was not observed.'))

    def select_source():
        check('declined download does not start the worker', not frame.working)
        check('declined download has a readable result', 'not requested' in status())
        check('declined download did not publish settings', not settings_path(frame.library.root / 'state').exists())
        named('Local audio setup source path').ChangeValue(str(args.runtime.absolute()))
        named('Local audio setup source type').SetSelection(0)
        key(named('Verify and select local source'), wx.WXK_SPACE,
            lambda: wait(lambda: not frame.working and 'verified and active' in status(),
                         selected, 'The actual runtime was not selected.'))

    def opened():
        state['dialog'] = next(window for window in wx.GetTopLevelWindows()
                               if isinstance(window, wx.Dialog) and window.GetTitle() == 'Local audio runtime setup')
        check('native setup has keyboard reachable source and cancel controls',
              named('Local audio setup source path').AcceptsFocus()
              and named('Verify and select local source').AcceptsFocus())
        # Escape declines the native Yes/No confirmation. No network test hook
        # or replacement downloader is installed.
        later(lambda: check('native confirmation Escape dispatched', simulator.Char(wx.WXK_ESCAPE)), 500)
        key(named('Download and install reviewed runtime'), wx.WXK_SPACE,
            lambda: wait(lambda: 'not requested' in status(), select_source,
                         'Native download confirmation was not declined.', seconds=10))

    try:
        checkout = Path(__file__).resolve().parents[1]
        for module in (local_runtime, windows_setup, runtime, audio_runtime_setup, desktop_app):
            installed = Path(module.__file__).resolve()
            source = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
            check('installed exact source ' + module.__name__,
                  not installed.is_relative_to(checkout)
                  and sha256(installed.read_bytes()).digest() == sha256(source.read_bytes()).digest())
            source_hashes[module.__name__] = sha256(installed.read_bytes()).hexdigest()
        check('real native Windows controls', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='resonance-local-audio-') as temporary:
            app = LibraryApp(Path(temporary) / 'library')
            frame = desktop_app.DesktopFrame(app)
            gui.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            menu = next(frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                        if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Audio Studio')
            action = next(item for item in menu.GetMenuItems()
                          if item.GetItemLabelText() == 'Local audio runtime setup...')
            later(opened)
            # Dispatch the actual installed menu handler, then use real native
            # focus and keyboard events inside the modal product window.
            event = wx.CommandEvent(wx.EVT_MENU.typeId, action.GetId())
            frame.GetEventHandler().ProcessEvent(event)
            if failures:
                raise AssertionError('\n'.join(failures))
            check('closing setup keeps the frame-owned runtime active', frame.audio_runtime_binding is state['binding'])
            check('actual fixed native version commands passed', state['binding'].report is not None)
            frame.Close()
            gui.Yield()
            check('frame close releases its audio binding', state['binding']._closed)
            frame = None
        report.update(status='PASS', actual_native_keyboard=True, actual_windows_ffmpeg=True,
                      source_hashes=source_hashes)
    except BaseException:
        report['failure'] = traceback.format_exc()
    finally:
        audio_runtime_setup.prepare_local_audio_runtime = original_prepare
        for timer in timers:
            timer.Stop()
        if frame is not None:
            binding = getattr(frame, 'audio_runtime_binding', None)
            if binding is not None:
                binding.close()
            frame.Close()
            gui.Yield()
        if native_handle is not None:
            native_handle.close()
    report['count'] = len(checks)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    args.report.write_text(payload + '\n', encoding='utf-8')
    print(payload)
    return 0 if report['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('This probe requires actual installed Windows execution.')
    report = {'status': 'FAILED', 'checks': [], 'network_download': 'NOT_REQUESTED',
              'neural_inference': 'NOT_RUN', 'human_listening': 'NOT_RUN', 'nvda': 'NOT_RUN',
              'independent_deadline_seconds': 240}
    # A failed initial receipt survives an unhandled native modal or cleanup.
    # This watchdog is independent of all wx callback-based waits.
    initial = {**report, 'count': 0, 'failure': 'The native audio setup probe did not reach final reporting. '
               'An independent 240-second watchdog records timeout stacks in stderr and exits nonzero.'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(initial, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Independent native audio setup deadline: 240 seconds; incomplete execution remains FAILED.', flush=True)
    faulthandler.dump_traceback_later(240, exit=True)
    try:
        return _run_probe(args, report)
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
