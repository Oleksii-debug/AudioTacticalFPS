"""Actual installed Windows snapshot controls and complete local round trip.

Exercises native widgets and bound handlers with a real DesktopFrame worker.
The controlled folder fields use synthetic temporary data. File-picker
interaction, manual keyboard operation and NVDA are not claimed.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
from time import monotonic
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    import wx
    from carr_library.native_runtime import activate_bundled_runtime
    activate_bundled_runtime()
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.incremental_backup import build_snapshot_dialog

    checks, failures, timers, receipts = [], [], [], {}
    gui = wx.App(False)
    main_thread = get_ident()
    worker_threads = []
    state = {'phase': 'initial', 'active': True, 'dialog': None}
    deadline = monotonic() + 100
    simulator = wx.UIActionSimulator()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('Expected an enabled native snapshot action.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The actual native snapshot handler did not run.')

    with TemporaryDirectory(prefix='carr-native-snapshots-') as temporary:
        root = Path(temporary).resolve(strict=True)
        app = LibraryApp(root / 'library')
        workspace = app.create_workspace('Synthetic native snapshot library')
        app.add_text(workspace, 'Native original', 'Точна native snapshot quotation.\r\nSecond line.')
        original_files = {p.name: sha256(p.read_bytes()).hexdigest() for p in app.assets_dir.iterdir()}
        frame = DesktopFrame(app)
        frame.Show()
        gui.SetTopWindow(frame)
        executor = frame.executor

        class ObservedExecutor:
            def submit(self, function, *args, **kwargs):
                def execute():
                    worker_threads.append(get_ident())
                    return function(*args, **kwargs)
                return executor.submit(execute)
            def shutdown(self, *args, **kwargs):
                return executor.shutdown(*args, **kwargs)

        frame.executor = ObservedExecutor()
        dialog = build_snapshot_dialog(frame)
        state['dialog'] = dialog
        actual_ready = dialog.ready

        def ready(action, result):
            receipts[action] = result
            actual_ready(action, result)

        dialog.ready = ready
        dialog.repository.SetValue(str(root / 'snapshots'))
        dialog.destination.SetValue(str(root / 'restored'))
        dialog.Show()
        dialog.Raise()

        def schedule():
            timers.append(wx.CallLater(25, poll))

        def stop():
            state['active'] = False
            dialog.alive = False
            dialog.Destroy()
            frame.Close()
            gui.ExitMainLoop()

        def poll():
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native snapshot sequence exceeded its deadline.')
                phase = state['phase']
                if phase == 'initial':
                    check('three named native snapshot input controls',
                          all(isinstance(field, wx.TextCtrl) and field.GetName() for field in dialog.fields)
                          and len(dialog.fields) == 3)
                    check('four named native snapshot action buttons',
                          len(dialog.actions) == 4 and all(isinstance(button, wx.Button) and button.GetName()
                                                         for button in dialog.actions.values()))
                    check('native snapshot result is read only and named',
                          isinstance(dialog.status, wx.TextCtrl) and not dialog.status.IsEditable()
                          and dialog.status.GetName() == 'Snapshot operation result and checksum')
                    dialog.repository.SetFocus()
                    state['phase'] = 'repository-focus'
                elif phase == 'repository-focus':
                    if wx.Window.FindFocus() != dialog.repository:
                        schedule()
                        return
                    check('native focus reaches snapshot repository', True)
                    simulator.KeyDown(wx.WXK_TAB)
                    simulator.KeyUp(wx.WXK_TAB)
                    state['phase'] = 'checksum-focus'
                elif phase == 'checksum-focus':
                    if wx.Window.FindFocus() != dialog.checksum:
                        schedule()
                        return
                    check('real Tab moves to the separately retained checksum', True)
                    press(dialog.actions['create'])
                    check('snapshot capture disables conflicting controls and keeps Close',
                          dialog.pending and frame.working and dialog.close.IsEnabled()
                          and all(not control.IsEnabled() for control in (*dialog.fields, *dialog.actions.values())))
                    state['phase'] = 'created'
                elif phase == 'created':
                    if dialog.pending or frame.working:
                        schedule()
                        return
                    created = receipts.get('create')
                    check('actual background worker returns complete snapshot receipt',
                          created is not None and created.status == 'complete'
                          and created.snapshot.file_count >= 2 and created.new_objects > 0
                          and worker_threads and all(thread != main_thread for thread in worker_threads))
                    check('created checksum is readable and result receives native focus',
                          dialog.checksum.GetValue() == created.snapshot.snapshot_sha256
                          and created.snapshot.snapshot_sha256 in dialog.status.GetValue()
                          and wx.Window.FindFocus() == dialog.status)
                    press(dialog.actions['verify'])
                    state['phase'] = 'verified'
                elif phase == 'verified':
                    if dialog.pending or frame.working:
                        schedule()
                        return
                    check('actual typed full snapshot verification succeeds',
                          receipts.get('verify') == receipts['create'].snapshot
                          and 'Complete snapshot verified:' in dialog.status.GetValue())
                    press(dialog.actions['restore'])
                    state['phase'] = 'restored'
                elif phase == 'restored':
                    if dialog.pending or frame.working:
                        schedule()
                        return
                    result = receipts.get('restore')
                    check('actual restore returns a fresh complete library',
                          result is not None and result.status == 'complete'
                          and result.path == root / 'restored'
                          and not (result.path / '.restore-incomplete').exists())
                    restored = LibraryApp(result.path)
                    hit, = restored.search(workspace, 'Точна')
                    check('restored quotation and original bytes equal the source',
                          restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Точна'
                          and {p.name: sha256(p.read_bytes()).hexdigest() for p in restored.assets_dir.iterdir()}
                          == original_files
                          and {p.name: sha256(p.read_bytes()).hexdigest() for p in app.assets_dir.iterdir()}
                          == original_files)
                    press(dialog.actions['create'])
                    state['phase'] = 'reused'
                elif phase == 'reused':
                    if dialog.pending or frame.working:
                        schedule()
                        return
                    second = receipts['create']
                    check('second actual snapshot reuses verified stored bytes',
                          second.reused_files > 0 and second.reused_bytes > 0
                          and second.cleanup_warning is None)
                    press(dialog.actions['list'])
                    state['phase'] = 'listed'
                elif phase == 'listed':
                    if dialog.pending or frame.working:
                        schedule()
                        return
                    check('native history explicitly distinguishes metadata from full verification',
                          len(receipts.get('list', ())) == 2
                          and dialog.status.GetValue().startswith('Snapshot metadata only;')
                          and wx.Window.FindFocus() == dialog.status)
                    check('finished controls are enabled and remain cancellable',
                          all(control.IsEnabled() for control in (*dialog.fields, *dialog.actions.values()))
                          and dialog.close.IsEnabled() and not frame.working)
                    stop()
                    return
            except BaseException:
                failures.append(traceback.format_exc())
                frame.cancel_event.set()
                stop()
                return
            schedule()

        schedule()
        try:
            gui.MainLoop()
        finally:
            state['active'] = False
            for timer in timers:
                timer.Stop()
            executor.shutdown(wait=True, cancel_futures=True)
        report = {'status': 'FAIL' if failures else 'PASS', 'checks': checks,
                  'native_controls': 'PASS' if not failures else 'FAIL',
                  'real_keyboard_tab': 'PASS' if not failures else 'FAIL',
                  'actual_background_roundtrip': 'PASS' if not failures else 'FAIL',
                  'native_file_pickers': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN',
                  'failures': failures}
        if not failures:
            report['snapshot_sha256'] = receipts['create'].snapshot.snapshot_sha256
            report['restored_snapshot_sha256'] = receipts['restore'].snapshot.snapshot_sha256
            report['new_bytes'] = receipts['create'].new_bytes
            report['reused_bytes'] = receipts['create'].reused_bytes
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
