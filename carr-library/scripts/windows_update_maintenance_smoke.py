"""Installed native signed-update review; synthetic authority only.

Actual wx controls, keyboard events, Desktop queue and TUF metadata/cache are
used. Standard installer execution is qualified separately by
packaging/probe_standard_update_windows.py. No production trust is provisioned.
"""
from hashlib import sha256
import argparse
import faulthandler
import json
import os
from pathlib import Path
import runpy
import sys
from contextlib import contextmanager
from tempfile import mkdtemp
import shutil
import threading
import time
import traceback


@contextmanager
def _fixture_root():
    root = Path(mkdtemp(prefix='carr-update-native-')).resolve()
    try:
        yield root
    except BaseException:
        print('Retained native maintenance fixture for diagnosis: ' + str(root), flush=True)
        raise
    else:
        shutil.rmtree(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Use native Windows and the installed interpreter with -I -B -X utf8.')
    if not args.native_child:
        from carr_library.audio.models import AudioBudget
        from carr_library.audio.process import run_process
        command = [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child']
        if args.report is not None:
            command.extend(['--report', str(args.report.absolute())])
        outcome = run_process(command, budget=AudioBudget(timeout=240, max_log_bytes=1024 * 1024),
                              owned_tree=True, allow_failure=True)
        sys.stdout.write(outcome.stdout.decode('utf-8', 'replace'))
        sys.stderr.write(outcome.stderr.decode('utf-8', 'replace'))
        return outcome.returncode
    faulthandler.dump_traceback_later(210, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        result = run_native()
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()
    if args.report:
        with args.report.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, indent=2); stream.write('\n')
    print(json.dumps(result, indent=2))
    return 0


def run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.security import maintenance, standard_handoff, handoff, https, updates, installation, download_state, _https_worker
    from carr_library.security._files import json_bytes
    from carr_library import portable_entry, installation_lease
    from carr_library.audio import windows_job
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import update_maintenance as ui
    from carr_library.ui import progress_panel
    from carr_library import progress

    checkout = Path(__file__).resolve().parents[1]
    verify_sources = runpy.run_path(str(checkout / 'scripts/windows_publishing_smoke.py'))['verify_installed_sources']
    modules = (ui, maintenance, standard_handoff, handoff, https, updates, installation,
               portable_entry, installation_lease, windows_job, download_state, _https_worker, progress, progress_panel)
    bindings = verify_sources(*modules)
    fixture = runpy.run_path(str(checkout / 'tests/security/conftest.py'))
    checks, errors, releases, owners = [], [], [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    class Application(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False

    gui = Application(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL

    def pump(predicate, seconds=20):
        until = time.monotonic() + seconds
        while not predicate():
            if errors:
                raise AssertionError(errors[-1])
            if time.monotonic() >= until:
                raise TimeoutError('Native maintenance acceptance did not reach its bounded condition.')
            gui.Yield()
            wx.MilliSleep(10)

    def press(control):
        top = wx.GetTopLevelParent(control)
        def foreground():
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
            handle, pid = user32.GetForegroundWindow(), wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            focused = wx.Window.FindFocus()
            return (pid.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                    and focused is not None and focused.GetId() == control.GetId())
        pump(foreground, 5)
        check('Focused native Space key is accepted', simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield(); wx.MilliSleep(30)
        finally:
            simulator.KeyUp(wx.WXK_SPACE)
        gui.Yield()

    def captured_id(dialog):
        identity, events = dialog.GetId(), []
        def observe(event):
            if event.GetId() == identity:
                events.append(event.GetId())
            event.Skip()
        dialog.Bind(wx.EVT_WINDOW_DESTROY, observe)
        return identity, events

    with _fixture_root() as root:
        repository = fixture['Repository'](root / 'synthetic-source')
        repository.publish(fixture['bundle']())
        library = LibraryApp(root / 'research')
        verifier = checkout / 'packaging/install_windows.py'
        bootstrap = root / 'root.json'; bootstrap.write_bytes(repository.bootstrap)
        config_value = dict(schema='carr-update-owner/1', product='carr-test', channel='stable', current_build=1,
            bootstrap=dict(path=str(bootstrap), sha256=sha256(repository.bootstrap).hexdigest()),
            repository=dict(kind='local', path=str(repository.root)), target=repository.target_path,
            cache=str(root / 'cache'), installation=dict(kind='standard',
                program=str(root / 'program/versions/111111111111-1'), work=str(root / 'work'),
                manifest_sha256='a' * 64, setup_sha256='b' * 64, version='0.1.0'),
            anchor=dict(profile_id='synthetic-native-update', binding='c' * 64,
                        reference='carr/provider/synthetic-native-update/' + 'a' * 32),
            verifier=dict(path=str(verifier), sha256=sha256(verifier.read_bytes()).hexdigest()),
            helper_python=None)
        config_path = root / 'configuration.json'
        config_raw = json_bytes(config_value); config_path.write_bytes(config_raw)
        config = maintenance.UpdateConfiguration.read(config_path, sha256(config_raw).hexdigest())
        frame = DesktopFrame(library)
        frame.Show()  # A real native owner must exist before MainLoop starts.
        session = maintenance.MaintenanceSession(library, config)
        dialog = ui.MaintenanceDialog(frame, session)
        owners.append(dialog); dialog.Show()
        original_library = {path.relative_to(library.root): path.read_bytes()
                            for path in library.root.rglob('*') if path.is_file()}
        observed = {}

        class QueueOwner(wx.Frame):
            def __init__(self):
                super().__init__(frame, title='Actual maintenance lifetime owner')
                self.library, self.cancel_event = library, frame.cancel_event
                self.delivered = []
            @property
            def working(self):
                return frame.working
            def run_job(self, label, operation, *, done, failed, with_progress=False):
                def completed(value):
                    self.delivered.append('done')
                    done(value)
                def rejected(error):
                    self.delivered.append('failed')
                    failed(error)
                return frame.run_job(label, operation, done=completed, failed=rejected, with_progress=with_progress)


        def exercise_pause_resume():
            # A separate authored signed target and cache keep the old native
            # check/download/lifetime scenarios byte-for-byte meaningful.
            resumed_source = fixture['Repository'](root / 'resumable-source')
            target_bytes = bytes(range(256)) * 4096
            target_info = resumed_source.publish(target_bytes)
            source_file = resumed_source.root / 'targets' / (
                target_info.hashes['sha256'] + '.' + resumed_source.target_path)
            selected_root = root / 'resume-bootstrap.json'
            selected_root.write_bytes(resumed_source.bootstrap)

            def selected_config(label):
                value = dict(config_value)
                value['bootstrap'] = dict(path=str(selected_root),
                    sha256=sha256(resumed_source.bootstrap).hexdigest())
                value['repository'] = dict(kind='local', path=str(resumed_source.root))
                value['cache'] = str(root / ('cache-' + label))
                path = root / ('configuration-' + label + '.json')
                raw = json_bytes(value); path.write_bytes(raw)
                return maintenance.UpdateConfiguration.read(path, sha256(raw).hexdigest())

            held = {}
            actual_report = download_state.TargetDownload.report
            def report(transfer, *args):
                actual_report(transfer, *args)
                if (held and not held['seen'] and transfer.received >= 65536
                        and os.path.samefile(transfer.downloads, held['downloads'])):
                    held['seen'] = True
                    held['received'] = transfer.received
                    held['entered'].set()
                    if not held['release'].wait(20):
                        raise TimeoutError('Release this authored native download prefix barrier.')

            def hold_prefix(configuration):
                entered, release = threading.Event(), threading.Event()
                releases.append(release)
                held.clear()
                held.update(entered=entered, release=release, seen=False,
                            downloads=Path(configuration.checked()['cache']) / 'downloads')
                return entered, release

            download_state.TargetDownload.report = report
            try:
                selection = selected_config('resume')
                paused_session = maintenance.MaintenanceSession(library, selection)
                view = ui.MaintenanceDialog(frame, paused_session)
                owners.append(view); view.Show()
                press(view.actions[0])
                pump(lambda: paused_session.release is not None and not frame.working and not view._busy)
                entered, release = hold_prefix(selection)
                press(view.actions[1])
                pump(entered.is_set)
                task = frame.progress_panel.active_task
                check('Actual signed bytes are pending on the existing Desktop task',
                      task is not None and frame.working and view._downloading
                      and view.download_actions[0].IsEnabled())
                press(view.download_actions[0])
                check('Native Pause requests only this transfer', view._pause.is_set()
                      and not view._cancel.is_set() and not frame.cancel_event.is_set())
                release.set()
                pump(lambda: paused_session.download_state is not None and not frame.working and not view._busy)
                partial = paused_session.download_state
                check('Actual paused checkpoint is not an installable package or100 percent',
                      partial.received == held['received'] and paused_session.package is None
                      and task.snapshot().state.value == 'paused'
                      and task.snapshot().completed == partial.received
                      and task.snapshot().percent < 100 and not view.actions[2].IsEnabled())
                folder = next(Path(selection.checked()['cache']).glob('downloads/.paused-*'))
                retained = {path.name: path.read_bytes() for path in folder.iterdir()}
                view.Destroy(); pump(lambda: not view)
                check('Closing a paused review preserves the exact inert checkpoint',
                      retained == {path.name: path.read_bytes() for path in folder.iterdir()})
                reopened = maintenance.MaintenanceSession(library, selection)
                view = ui.MaintenanceDialog(frame, reopened)
                owners.append(view); view.Show()
                check('A new window neither resumes nor activates automatically',
                      reopened.release is None and reopened.package is None and not frame.working)
                press(view.actions[0])
                pump(lambda: reopened.release is not None and not frame.working and not view._busy)
                check('Fresh signed review exposes explicit Resume', reopened.download_state == partial
                      and view.download_actions[1].IsEnabled() and not view.actions[1].IsEnabled())
                press(view.download_actions[1])
                pump(lambda: reopened.package is not None and not frame.working and not view._busy)
                check('Native Resume reaches100 only after the same full signed target verifies',
                      reopened.package.path.read_bytes() == target_bytes
                      and reopened.download_state is None and not folder.exists()
                      and frame.progress_panel.active_task.snapshot().state.value == 'succeeded'
                      and frame.progress_panel.active_task.snapshot().percent == 100
                      and reopened.last_result is None and not (root / 'program').exists()
                      and not (root / 'work').exists())
                view.Destroy(); pump(lambda: not view)

                selection = selected_config('cancel')
                cancelled_session = maintenance.MaintenanceSession(library, selection)
                view = ui.MaintenanceDialog(frame, cancelled_session)
                owners.append(view); view.Show()
                press(view.actions[0])
                pump(lambda: cancelled_session.release is not None and not frame.working and not view._busy)
                entered, release = hold_prefix(selection)
                press(view.actions[1]); pump(entered.is_set)
                task = frame.progress_panel.active_task
                press(view.download_actions[2])
                check('Actual Cancel is local to its selected transfer', view._cancel.is_set()
                      and not frame.cancel_event.is_set())
                release.set()
                pump(lambda: not frame.working and not view._busy)
                check('Native Cancel waits for cleanup and cannot leave an installable result',
                      cancelled_session.package is None and cancelled_session.download_state is None
                      and task.snapshot().state.value == 'cancelled'
                      and not tuple(Path(selection.checked()['cache']).glob('downloads/.paused-*'))
                      and not tuple(Path(selection.checked()['cache']).glob('downloads/*.zip'))
                      and source_file.read_bytes() == target_bytes)
                view.Destroy(); pump(lambda: not view)
            finally:
                for release in releases:
                    release.set()
                download_state.TargetDownload.report = actual_report

        def exercise():
            try:
                check('Maintenance sequence runs inside actual MainLoop', gui.IsMainLoopRunning())
                check('Signed review uses named read-only native text',
                      isinstance(dialog.status, wx.TextCtrl)
                      and dialog.status.GetName() == 'Signed update review and recovery details'
                      and bool(dialog.status.GetWindowStyleFlag() & wx.TE_READONLY))
                check('Apply is unavailable before signed check and download', not dialog.actions[2].IsEnabled())
                press(dialog.actions[0])
                pump(lambda: session.release is not None and not frame.working and not dialog._busy)
                check('Actual TUF check enables only explicit download', dialog.actions[1].IsEnabled()
                      and not dialog.actions[2].IsEnabled() and session.package is None)
                press(dialog.actions[1])
                pump(lambda: session.package is not None and not frame.working and not dialog._busy)
                check('Prepared exact package enables separate standard apply',
                      dialog.actions[2].IsEnabled()
                      and sha256(session.package.path.read_bytes()).hexdigest() == session.release.sha256)
                check('No installation, anchor or backup was created by review/download',
                      not (root / 'program').exists() and not (root / 'work').exists()
                      and session.last_result is None)
                dialog.Destroy()
                pump(lambda: not dialog)

                exercise_pause_resume()

                for death in ('direct', 'native-parent'):
                    owner = QueueOwner(); owner.Show(); owners.append(owner)
                    current = maintenance.MaintenanceSession(library, config)
                    target = ui.MaintenanceDialog(owner, current)
                    owners.append(target); target.Show()
                    identity, events = captured_id(target)
                    ready, release = threading.Event(), threading.Event()
                    releases.append(release)
                    actual = current.check
                    def held(*, cancelled, original=actual, ready=ready, release=release):
                        value = original(cancelled=cancelled)
                        ready.set()
                        if not release.wait(20):
                            raise TimeoutError('Actual signed review was not released after native deletion.')
                        return value
                    current.check = held
                    press(target.actions[0])
                    pump(ready.is_set)
                    check('Held work already has a real verified release: ' + death,
                          current.release is not None and frame.working)
                    if death == 'direct':
                        check('Direct Destroy detaches immediately', target.Destroy() and not target._alive)
                    else:
                        def forbidden_override():
                            raise AssertionError('Parent deletion must bypass the Python Destroy override.')
                        target.Destroy = forbidden_override
                        wx.Window.DestroyChildren(owner)
                    pump(lambda: not target and events == [identity])
                    check('Actual deletion observed before releasing verified work: ' + death,
                          not target and not target._alive and target._cancel.is_set() and events == [identity])
                    forbidden_access = []
                    def invalid(*args, **kwargs):
                        forbidden_access.append(True)
                        raise AssertionError('Late completion touched a deleted native control.')
                    target.status.SetValue = target.status.SetFocus = invalid
                    for action in (*target.actions, *target.download_actions):
                        action.Enable = invalid
                    release.set()
                    pump(lambda: owner.delivered == ['done'] and not frame.working)
                    delivered = []
                    def replay(target=target):
                        target.on_check(); target.on_download(); target.on_apply(); target.on_recover(); target.on_command()
                        target.on_pause_download(); target.on_resume_download(); target.on_cancel_download()
                        delivered.append(True)
                    wx.CallAfter(replay)
                    pump(lambda: delivered == [True])
                    check('Actual callback and queued actions are inert after deletion: ' + death,
                          forbidden_access == [] and not frame.cancel_event.is_set())
                    check('Business review is retained independently of the view: ' + death,
                          current.release.sha256 == session.release.sha256 and current.package is None)
                    owner.Destroy()
                    pump(lambda: not owner)
                check('User library and independently supplied configuration remain unchanged',
                      config_path.read_bytes() == config_raw
                      and original_library == {path.relative_to(library.root): path.read_bytes()
                           for path in library.root.rglob('*') if path.is_file()})
                check('Installed update source bindings remain exact', verify_sources(*modules) == bindings)
                observed['complete'] = True
            except BaseException:
                errors.append(traceback.format_exc())
            finally:
                for release in releases:
                    release.set()
                gui.ExitMainLoop()

        watchdog = wx.CallLater(180000, lambda: (errors.append('Native maintenance exceeded 180 seconds.'),
                                                [event.set() for event in releases], gui.ExitMainLoop()))
        try:
            wx.CallAfter(exercise)
            gui.MainLoop()
        finally:
            watchdog.Stop()
            for release in releases:
                release.set()
            if frame:
                deadline = time.monotonic() + 25
                while frame.working and time.monotonic() < deadline:
                    gui.Yield(); wx.MilliSleep(10)
                if frame.working:
                    raise RuntimeError('Native maintenance worker cleanup is unconfirmed; retain its workspace.')
            for owner in reversed(owners):
                if owner:
                    wx.Window.Destroy(owner)
            if frame:
                frame.Close()
            gui.ProcessPendingEvents()
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('All native review and lifetime cases executed', observed.get('complete') is True)
    return dict(status='PASS', checks=checks, installed_sources=bindings,
                authority='synthetic TUF fixture only', actual_native_installer='SEPARATE_GATE',
                physical_keyboard='NOT_RUN; UIActionSimulator supplied native events', nvda='NOT_RUN')


if __name__ == '__main__':
    raise SystemExit(main())
