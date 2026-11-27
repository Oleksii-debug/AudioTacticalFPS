"""Actual installed wx draft-timer lifetime probe; no manual NVDA claim."""
import argparse
import faulthandler
from hashlib import sha256
import json
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
import time
import traceback

faulthandler.enable(all_threads=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run this probe on Windows with the installed Python -I -B.')
    failures, checks, cases, callbacks = [], [], [], []
    started = time.monotonic()
    report = {'status': 'FAIL', 'platform': sys.platform, 'manual_nvda': 'NOT_RUN'}

    def checkpoint(phase):
        # Keep the last completed boundary available even after a native crash.
        # Never inspect a possibly destroyed native object while reporting it.
        report.update(phase=phase, elapsed_seconds=round(time.monotonic() - started, 3),
            checks=list(checks), failures=list(failures),
            actual_timer_events={case['label']: case['ticks'] for case in cases},
            destroyed={case['label']: case['destroyed'] for case in cases})
        raw = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output is not None:
            args.output.write_text(raw, encoding='utf-8')
        print('DRAFT_TIMER_PHASE ' + json.dumps(report, ensure_ascii=False, allow_nan=False), flush=True)

    checkpoint('starting')
    # wx.CallLater cannot diagnose a blocked native MainLoop or cleanup.
    faulthandler.dump_traceback_later(35, repeat=False, exit=True)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        checkpoint('native runtime active; importing wx')
        import wx
        import wx.siplib
        from carr_library.application import LibraryApp
        from carr_library.ui.desktop_app import DesktopFrame
        from carr_library.ui import save_actions
        checkout = Path(__file__).resolve().parents[1]
        installed = Path(save_actions.__file__).resolve()
        purelib = Path(sysconfig.get_path('purelib')).resolve()
        if installed.is_relative_to(checkout) or not installed.is_relative_to(purelib):
            raise AssertionError('The probe must import the installed application.')
        expected = checkout / 'src' / 'carr_library' / 'ui' / 'save_actions.py'
        if installed.read_bytes() != expected.read_bytes():
            raise AssertionError('The installed draft timer differs from the reviewed checkout.')
        report.update(installed_save_actions=str(installed),
                      installed_save_actions_sha256=sha256(installed.read_bytes()).hexdigest())
        active = [True]

        class ProbeApp(wx.App):
            def OnExceptionInMainLoop(self):
                failures.append(traceback.format_exc())
                checkpoint('exception in native event loop')
                self.ExitMainLoop()
                return False

        app = ProbeApp(False)
        app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
        app.SetExitOnFrameDelete(False)
        # The two product windows are the objects under test, not the probe's
        # application root. Keep an independent native top window alive while
        # pumping late events after BOTH product windows have been destroyed.
        keeper = wx.Frame(None, title='Draft timer lifetime acceptance')
        app.SetTopWindow(keeper)
        keeper.Show()
        checkpoint('native event-loop keeper ready')

        def failed(message):
            failures.append(message)
            checkpoint('callback failed')
            app.ExitMainLoop()

        def guarded(callback):
            def invoke():
                if not active[0]:
                    return
                try:
                    callback()
                except BaseException:
                    failed(traceback.format_exc())
            return invoke

        def later(delay, callback):
            pending = wx.CallLater(delay, guarded(callback))
            callbacks.append(pending)
            return pending

        with TemporaryDirectory(prefix='carr-native-save-timer-') as temporary:
            try:
                for label in ('Close', 'Destroy'):
                    library = LibraryApp(Path(temporary) / label)
                    frame = DesktopFrame(library)
                    case = {'label': label, 'frame': frame,
                            'timer': frame._draft_save_timer, 'ticks': 0,
                            'destroyed': False, 'executor': frame.executor,
                            'cancel': frame.cancel_event}
                    cases.append(case)

                    def dirty(state=case):
                        if state['destroyed']:
                            raise AssertionError('A queued autosave accessed its destroyed owner.')
                        return False

                    def tick(event, state=case):
                        state['ticks'] += 1
                        event.Skip()

                    def destroyed(event, state=case):
                        if event.GetEventObject() is state['frame']:
                            state['destroyed'] = True
                            checkpoint(state['label'] + ': owner destroy event')
                        event.Skip()

                    frame._dirty = dirty
                    frame.Bind(wx.EVT_TIMER, tick, case['timer'])
                    frame.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                    frame.Show()
                    assert case['timer'].IsRunning()
                    assert case['timer'].GetInterval() == 3000
                    assert case['timer'].GetOwner() is frame
                    wx.Panel(frame).Destroy()
                    checkpoint(label + ': shown; child destroyed; timer owner verified')

                checks.append('Both actual draft timers initially target their own DesktopFrame')

                def finish():
                    assert all(case['destroyed'] for case in cases)
                    assert all(not case['timer'].IsRunning() for case in cases)
                    assert all(case['ticks'] == case['stopped_ticks'] for case in cases)
                    checks.append('MainLoop remained usable without new ticks beyond the original timer period')
                    checkpoint('late-event interval complete')
                    app.ExitMainLoop()

                def after_destroy():
                    if not all(case['destroyed'] for case in cases):
                        later(50, after_destroy)
                        return
                    assert all(wx.siplib.isdeleted(case['frame']) for case in cases)
                    assert all(not wx.siplib.isdeleted(case['timer']) for case in cases)
                    assert all(not case['timer'].IsRunning() for case in cases)
                    for case in cases:
                        case['stopped_ticks'] = case['ticks']
                    checks.append('Close and direct Destroy stopped their native draft timers')
                    checkpoint('both owners deleted; stopped timers retained for late-event observation')
                    later(3400, finish)

                def wait_for_live_tick():
                    assert all(case['timer'].IsRunning() for case in cases)
                    if not all(case['ticks'] > 0 for case in cases):
                        later(50, wait_for_live_tick)
                        return
                    checks.append('Child destruction preserved actual live 3000 ms timer delivery')
                    checkpoint('live ticks observed; closing first owner')
                    assert cases[0]['frame'].Close()
                    checkpoint('Close returned; destroying second owner')
                    cases[1]['frame'].Destroy()
                    checkpoint('Destroy returned')
                    later(50, after_destroy)

                later(20000, lambda: failed('Native timer probe exceeded its 20 second allowance.'))
                later(50, wait_for_live_tick)
                checkpoint('entering native MainLoop')
                app.MainLoop()
                checkpoint('native MainLoop returned')
            except BaseException:
                failures.append(traceback.format_exc())
                checkpoint('probe exception')
            finally:
                active[0] = False
                checkpoint('stopping probe callbacks')
                for callback in callbacks:
                    callback.Stop()
                for case in cases:
                    case['cancel'].set()
                    case['executor'].shutdown(wait=False, cancel_futures=True)
                    if not case['destroyed'] and not wx.siplib.isdeleted(case['frame']):
                        case['frame'].Destroy()
                app.ProcessPendingEvents()
                checkpoint('product cleanup complete; destroying event-loop keeper')
                if not wx.siplib.isdeleted(keeper):
                    keeper.Destroy()
                app.ProcessPendingEvents()

        report['status'] = 'FAIL' if failures or len(checks) != 4 else 'PASS'
        checkpoint('complete')
        raw = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output is not None:
            args.output.write_text(raw, encoding='utf-8')
        print(raw, end='', flush=True)
        if report['status'] != 'PASS':
            raise RuntimeError('Native draft timer lifetime acceptance failed.')
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
