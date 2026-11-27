"""Installed wx evidence recovery timer lifetime; no manual NVDA claim."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import faulthandler
from hashlib import sha256
import json
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback

faulthandler.enable(all_threads=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run this probe on Windows with installed Python -I -B.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    failures, checks, cases = [], [], []
    try:
        import wx
        from carr_library.application import LibraryApp
        from carr_library.evidence_drafts import EvidenceDrafts
        from carr_library.ui.desktop_evidence_panel import DesktopEvidencePanel
        from carr_library.ui import desktop_evidence_panel
        checkout = Path(__file__).resolve().parents[1]
        installed = Path(desktop_evidence_panel.__file__).resolve()
        purelib = Path(sysconfig.get_path('purelib')).resolve()
        if installed.is_relative_to(checkout) or not installed.is_relative_to(purelib):
            raise AssertionError('The probe must import the installed application.')
        expected = checkout / 'src/carr_library/ui/desktop_evidence_panel.py'
        if installed.read_bytes() != expected.read_bytes():
            raise AssertionError('The installed recovery owner differs from the reviewed checkout.')

        class ProbeApp(wx.App):
            def OnExceptionInMainLoop(self):
                failures.append(traceback.format_exc())
                return True

        app = ProbeApp(False)
        app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
        app.SetExitOnFrameDelete(False)
        loop = wx.GUIEventLoop()
        activation = wx.EventLoopActivator(loop)

        def pump(predicate, timeout=8):
            deadline = monotonic() + timeout
            while monotonic() < deadline:
                app.Yield()
                if failures:
                    raise AssertionError(failures[-1])
                if predicate():
                    return
                wx.MilliSleep(10)
            raise AssertionError('Native recovery lifetime condition timed out.')

        class Host(wx.Frame):
            def __init__(self, library, workspace, topic):
                super().__init__(None, title='Native recovery timer acceptance', size=(800, 650))
                self.library = library
                self.active = workspace, topic
                self.working = self._closing = False
                self.cancel_event = Event()
                self.executor = ThreadPoolExecutor(max_workers=1)
                self.evidence = None
                layout = wx.BoxSizer(wx.VERTICAL)
                self.SetSizer(layout)
                self.evidence = DesktopEvidencePanel(self, self, workspace, topic,
                    lambda: library.evidence_transaction(workspace))
                layout.Add(self.evidence, 1, wx.EXPAND)
                self.Bind(wx.EVT_CLOSE, self.closing)
                self.Show()

            def report(self, message):
                failures.append(str(message))

            def closing(self, event):
                self._closing = True
                self.cancel_event.set()
                event.Skip()

            def run_job(self, title, operation, done=None, failed=None):
                self.working = True
                future = self.executor.submit(operation)
                def finished(result):
                    def apply():
                        self.working = False
                        if self._closing or not self:
                            return
                        try:
                            value = result.result()
                        except Exception as error:
                            if failed:
                                failed(error)
                            else:
                                failures.append(str(error))
                        else:
                            if done:
                                done(value)
                    wx.CallAfter(apply)
                future.add_done_callback(finished)

        with TemporaryDirectory(prefix='carr-native-save-timer-') as temporary:
            try:
                for label in ('Close', 'Destroy'):
                    library = LibraryApp(Path(temporary) / label)
                    workspace = library.create_workspace('Timer acceptance')
                    topic = library.create_topic(workspace, 'Recovery')
                    library.add_text(workspace, 'Synthetic source', 'Exact source for timer evidence.')
                    hit, = library.search(workspace, 'Exact')
                    evidence = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved baseline')
                    frame = Host(library, workspace, topic)
                    panel = frame.evidence
                    case = dict(label=label, frame=frame, panel=panel,
                        native_id=panel.GetId(), timer=panel._recovery_timer,
                        executor=frame.executor, cancel=frame.cancel_event,
                        store=EvidenceDrafts(library.root), key=(workspace, topic, evidence),
                        ticks=0, destroyed=False, destroy_events=[])
                    cases.append(case)
                    pump(lambda: panel.displayed is not None and not frame.working)

                    def tick(event, state=case):
                        if state['destroyed']:
                            raise AssertionError('A timer event reached a deleted recovery owner.')
                        state['ticks'] += 1
                        event.Skip()

                    def destroyed(event, state=case):
                        if event.GetId() == state['native_id']:
                            state['destroyed'] = True
                            state['destroy_events'].append(dict(window_id=event.GetId(),
                                original_wrapper=event.GetEventObject() is state['panel']))
                        event.Skip()

                    panel.Bind(wx.EVT_TIMER, tick, case['timer'])
                    panel.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                    panel.note.SetValue('First immediate edit')
                    pump(lambda: case['store'].get(*case['key']) is not None)
                    panel.note.SetValue('Coalesced edit after child destruction')
                    assert case['timer'].IsRunning() and case['timer'].GetInterval() == 1000
                    wx.Panel(panel).Destroy()
                    assert not panel._recovery_disposed

                pump(lambda: all(case['ticks'] > 0 and case['store'].get(*case['key']).text
                    == 'Coalesced edit after child destruction' for case in cases))
                checks.append('Child deletion preserves actual one-second coalesced timer delivery')
                for case in cases:
                    case['panel'].note.SetValue('Final cached edit before native owner deletion')
                    assert case['timer'].IsRunning()
                    assert case['store'].get(*case['key']).text == 'Coalesced edit after child destruction'
                assert cases[0]['frame'].Close()
                cases[1]['frame'].Destroy()
                pump(lambda: all(case['destroyed'] and not case['frame'] for case in cases))
                for case in cases:
                    assert len(case['destroy_events']) == 1
                    assert case['panel']._recovery_disposed
                    assert not case['timer'].IsRunning()
                    assert case['store'].get(*case['key']).text == 'Final cached edit before native owner deletion'
                    case['stopped_ticks'] = case['ticks']
                    before = case['store'].path.read_bytes()
                    assert case['panel']._flush_recovery(None) is False
                    assert case['store'].path.read_bytes() == before
                checks.append('Close and direct Destroy stop owned timers and flush cached edits without live controls')
                until = monotonic() + 1.4
                pump(lambda: monotonic() >= until, timeout=3)
                assert all(case['ticks'] == case['stopped_ticks'] for case in cases)
                checks.append('No late native timer or queued callback writes after owner deletion')
            except BaseException:
                failures.append(traceback.format_exc())
            finally:
                for case in cases:
                    case['cancel'].set()
                    case['executor'].shutdown(wait=True, cancel_futures=True)
                    if case['frame']:
                        case['frame']._closing = True
                        case['frame'].Destroy()
                app.Yield()

        report = dict(status='FAIL' if failures or len(checks) != 3 else 'PASS',
            platform=sys.platform, manual_nvda='NOT_RUN', checks=checks, failures=failures,
            actual_timer_events={case['label']: case['ticks'] for case in cases},
            native_destroy_events={case['label']: case['destroy_events'] for case in cases},
            installed_recovery_owner=str(installed),
            installed_recovery_owner_sha256=sha256(installed.read_bytes()).hexdigest())
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
        return 0 if report['status'] == 'PASS' else 1
    finally:
        native_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
