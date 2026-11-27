"""Actual installed Windows menu/timer/worker scheduling over synthetic local data.

Folder chooser results are explicit fixture paths. Arming/default-No/Yes, native
controls, timer dispatch, isolated import and canonical backup/restore are real.
No audio device, external service, private model or ChatGPT automation is used.
"""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from unittest.mock import patch


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1] / 'src')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        raise RuntimeError('This native schedule acceptance requires actual Windows.')
    from carr_library.native_runtime import activate_bundled_runtime
    native = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library import incremental_backup
    from carr_library import local_schedules
    from carr_library.ingest.batches import ImportBatchStore
    from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import local_schedules as ui

    checks, errors, origins = [], [], {}
    modules = (
        'carr_library.local_schedule_model', 'carr_library.local_schedules',
        'carr_library.local_schedules_cli', 'carr_library.backup_local_schedules',
        'carr_library.ui.local_schedules', 'carr_library.incremental_backup',
        'carr_library.sync.vault', 'carr_library.ingest.batches',
        'carr_library.operations.broker', 'carr_library.operations._locks',
        'carr_library.durable_state', 'carr_library.ui.desktop_app',
    )
    deadline = monotonic() + 150
    def expired():
        print('FAIL: native local schedules or owned host cleanup exceeded 180 seconds', file=sys.stderr, flush=True)
        os._exit(1)
    watchdog = threading.Timer(180, expired)
    watchdog.daemon = True
    watchdog.start()
    frame = dialog = None
    pressed, calls = set(), []
    phase, pending_key, finished = 0, False, False
    imports, backups, selected_paths = [], [], []
    gui_thread = threading.get_ident()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def source_check(stage):
        for name in modules:
            installed = Path(importlib.import_module(name).__file__).resolve()
            expected = args.source_root / Path(*name.split('.')).with_suffix('.py')
            check('installed source origin ' + stage + ': ' + name,
                installed.is_relative_to(Path(sys.prefix).resolve())
                and not installed.is_relative_to(args.source_root.resolve()))
            actual = sha256(installed.read_bytes()).hexdigest()
            check('installed source bytes ' + stage + ': ' + name,
                  actual == sha256(expected.read_bytes()).hexdigest())
            origins[name] = actual

    source_check('before')
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            abort()
            return True
    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDlgItem.restype = wintypes.HWND
    user32.GetFocus.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetDlgCtrlID.restype = ctypes.c_int

    def abort():
        if frame is not None:
            frame.cancel_event.set()
            if getattr(frame, 'local_schedules', None) is not None:
                frame.local_schedules.close()
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    def guarded(function):
        def call():
            if finished or errors:
                return
            try:
                function()
            except BaseException:
                errors.append(traceback.format_exc())
                abort()
        return call

    def release(key):
        nonlocal pending_key
        try:
            if key in pressed:
                check('native keyboard key released', simulator.KeyUp(key))
        finally:
            pressed.discard(key)
            pending_key = False

    def press(control):
        nonlocal pending_key
        check('native action is enabled: ' + control.GetName(), control.IsEnabled())
        pending_key = True
        end = min(deadline, monotonic() + 5)
        def act():
            top = wx.GetTopLevelParent(control)
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
            if wx.Window.FindFocus() != control or user32.GetForegroundWindow() != top.GetHandle():
                if monotonic() >= end:
                    raise TimeoutError('Native focus could not reach ' + control.GetName())
                calls.append(wx.CallLater(25, guarded(act)))
                return
            check('native focused action: ' + control.GetName(), wx.Window.FindFocus() is control)
            check('native keyboard action sent', simulator.KeyDown(wx.WXK_SPACE))
            pressed.add(wx.WXK_SPACE)
            calls.append(wx.CallLater(40, guarded(lambda: release(wx.WXK_SPACE))))
        wx.CallAfter(guarded(act))

    def confirm(title, *, accepted):
        nonlocal pending_key
        handle = user32.GetForegroundWindow()
        label = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(handle, label, len(label))
        if label.value != title:
            return False
        check('native confirmation owner: ' + title, user32.GetWindow(handle, 4) == dialog.GetHandle())
        check('native confirmation defaults to No: ' + title,
              user32.GetDlgCtrlID(user32.GetFocus()) == 7)
        if accepted:
            yes = user32.GetDlgItem(handle, 6)
            check('native explicit Yes control', bool(yes))
            user32.SetFocus(yes)
            check('native explicit Yes focus', user32.GetFocus() == yes)
        pending_key = True
        check('native confirmation keyboard action', simulator.KeyDown(wx.WXK_SPACE))
        pressed.add(wx.WXK_SPACE)
        calls.append(wx.CallLater(40, guarded(lambda: release(wx.WXK_SPACE))))
        return True

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def named(name):
        rows = [child for child in descendants(dialog) if child.GetName() == name]
        check('unique accessible control: ' + name, len(rows) == 1)
        return rows[0]

    with TemporaryDirectory(prefix='carr-native-local-schedules-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native scheduled library')
        library.add_text(workspace, 'Original', 'Native retained railway original.')
        ProgressPreferenceStore(library.root / 'progress-preferences.json').save(ProgressPreferences(muted=True))
        watch = root / 'selected-watch'
        watch.mkdir()
        source = watch / 'Україна.txt'
        source.write_text('A genuine scheduled TXT import, unchanged Ω.', encoding='utf-8')
        source_raw = source.read_bytes()
        repository = root / 'repository'
        incremental_backup.initialize_repository(repository)
        frame = DesktopFrame(library)
        frame.Show()
        gui.Yield()
        check('actual host installed one local scheduling controller',
              isinstance(frame.local_schedules, ui.LocalScheduleController))
        check('actual command uses current canonical registry',
              frame.keyboard_commands.registry.get(ui.COMMAND_ID).command_id == ui.COMMAND_ID)
        check('scheduler owns no private executor or progress hub',
              not hasattr(frame.local_schedules, 'executor') and frame.progress_panel is not None)
        items = [item for index in range(frame.menu_bar.GetMenuCount())
                 for item in frame.menu_bar.GetMenu(index).GetMenuItems()]
        command = next(item for item in items if item.GetItemLabel().replace('&', '').startswith(
            'Local backup and folder schedules'))
        real_import, real_backup = ImportBatchStore.run, incremental_backup.create_snapshot
        def tracked_import(store, *args, **kwargs):
            thread = threading.get_ident()
            result = real_import(store, *args, **kwargs)
            imports.append((thread, result))
            return result
        def tracked_backup(*args, **kwargs):
            thread = threading.get_ident()
            result = real_backup(*args, **kwargs)
            backups.append((thread, result))
            return result
        def folder(parent, title):
            check('folder picker is requested by the actual local schedule dialog', parent is dialog)
            selected_paths.append(title)
            return repository if 'backup repository' in title else watch

        answered = set()
        snapshot_token = None

        def drive():
            nonlocal dialog, phase, snapshot_token
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native local schedule control flow exceeded its deadline.')
                if pending_key:
                    return
                controller = frame.local_schedules
                dialog = controller.dialog
                if dialog is None or not dialog.IsShown():
                    return
                # Actual text dialogs are filled with deterministic fixture input;
                # their native OK controls still receive real keyboard actions.
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.TextEntryDialog) and window.IsShown():
                        title = window.GetName()
                        key = (phase, title)
                        if key in answered:
                            continue
                        answered.add(key)
                        if title == 'Local rule title':
                            text = 'Native watched sources' if phase == 1 else 'Native scheduled backup'
                        elif title == 'Repeat interval in minutes':
                            text = '1'
                        elif title == 'First UTC start':
                            text = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
                        elif title == 'Watched-folder OCR languages':
                            text = 'eng'
                        elif title == 'Current snapshot token':
                            text = ''
                        else:
                            raise AssertionError('Unexpected native text dialog: ' + title)
                        window.SetValue(text)
                        press(window.FindWindowById(wx.ID_OK))
                        return
                if phase == 1 and confirm('Watch subfolders', accepted=False):
                    return
                if phase == 3 and confirm('Arm this exact local rule', accepted=False):
                    phase = 4
                    return
                if phase in (5, 11) and confirm('Arm this exact local rule', accepted=True):
                    phase += 1
                    return
                if frame.working:
                    return
                status = named('Local schedule status')
                if phase == 0:
                    check('initial saved rule inventory is empty and inactive',
                          dialog.views == [] and controller.session.armed == () and not controller.timer.IsRunning())
                    check('native review text is read-only', not named('Local schedule review').IsEditable())
                    phase = 1
                    press(named('New watched folder rule...'))
                elif phase == 1 and len(dialog.views) == 1:
                    check('native save creates no import or active timer',
                          imports == [] and len(library.browse(workspace)) == 1
                          and controller.session.armed == () and not controller.timer.IsRunning())
                    check('saved rule status has readable focus', wx.Window.FindFocus() is status)
                    phase = 3
                    press(named('Arm selected rule'))
                elif phase == 4:
                    check('default-No arm leaves actual source and services untouched',
                          imports == [] and backups == [] and controller.session.armed == ())
                    phase = 5
                    press(named('Arm selected rule'))
                elif phase == 6 and imports:
                    check('actual native timer dispatched one verified isolated import', len(imports) == 1
                          and imports[0][1]['counts']['complete'] == 1
                          and imports[0][1]['rows'][0]['result']['isolated']
                          and imports[0][1]['rows'][0]['result']['verified'])
                    check('scheduled import used the current worker off the GUI thread', imports[0][0] != gui_thread)
                    check('watched source bytes are unchanged', source.read_bytes() == source_raw)
                    check('native completed status remains readable and focused',
                          'Scheduled operation: complete' in status.GetValue() and wx.Window.FindFocus() is status)
                    phase = 7
                    press(named('Disarm this window'))
                elif phase == 7:
                    check('native Disarm stops repeat admission and native timer',
                          controller.session.armed == () and not controller.timer.IsRunning())
                    phase = 9
                    press(named('New backup rule...'))
                elif phase == 9 and len(dialog.views) == 2:
                    check('native backup save creates no snapshot', backups == []
                          and incremental_backup.list_snapshots(repository) == [])
                    check('native backup review displays exact selected repository',
                          str(repository) in named('Local schedule review').GetValue())
                    phase = 11
                    press(named('Arm selected rule'))
                elif phase == 12 and backups:
                    check('actual native timer dispatched one canonical complete snapshot',
                          len(backups) == 1 and backups[0][1]['published'])
                    check('both operations used the same current host worker',
                          backups[0][0] == imports[0][0] and backups[0][0] != gui_thread)
                    snapshot_token = backups[0][1]['snapshot_sha256']
                    check('native result exposes actual snapshot identity',
                          snapshot_token in status.GetValue() and wx.Window.FindFocus() is status)
                    phase = 13
                    press(named('Close local schedules'))
                elif phase == 20:
                    check('reopened native window is inactive despite saved completed rules',
                          len(dialog.views) == 2 and controller.session.armed == () and not controller.timer.IsRunning())
                    phase = 21
                    press(named('Close local schedules'))
            finally:
                if not finished and not errors:
                    calls.append(wx.CallLater(25, guarded(drive)))

        def show_first():
            nonlocal finished
            calls.append(wx.CallLater(25, guarded(drive)))
            event = wx.CommandEvent(wx.EVT_MENU.typeId, command.GetId())
            event.SetEventObject(frame)
            check('actual native menu command dispatched', frame.GetEventHandler().ProcessEvent(event))
            check('native Close ended the schedule window', phase == 13 and frame.local_schedules.dialog is None)
            finished = True
            gui.ExitMainLoop()

        try:
            with patch.object(ui, '_directory', side_effect=folder), \
                    patch.object(ImportBatchStore, 'run', tracked_import), \
                    patch.object(incremental_backup, 'create_snapshot', tracked_backup):
                wx.CallAfter(guarded(show_first))
                gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('one explicit source folder and one explicit repository were chosen', len(selected_paths) == 2)
            verified = incremental_backup.verify_snapshot(repository, snapshot_token)
            check('scheduled snapshot passes independent complete-backup verification', verified['payloads_verified'])
            restored_path = root / 'restored'
            incremental_backup.restore_snapshot(repository, snapshot_token, restored_path)
            restored = LibraryApp(restored_path)
            check('actual restored library contains both exact source revisions', len(restored.browse(workspace)) == 2)
            saved_store = local_schedules.ScheduleStore(restored)
            restored_session = local_schedules.ScheduleSession(saved_store)
            check('restoration arms no timer and runs no saved rule',
                  len(saved_store.list()) == 2 and restored_session.armed == () and restored_session.tick() is None)
            restored_session.close()
            check('restoration leaves the original current host library selected', frame.library is library)
            shutil.rmtree(watch)
            shutil.rmtree(repository)
            for call in calls:
                call.Stop()
            calls.clear()
            for key in tuple(pressed):
                release(key)
            # Reopen through the same actual menu. Listing saved metadata must
            # remain passive even after both recorded external folders vanish.
            phase, finished = 20, False
            def reopen():
                nonlocal finished
                calls.append(wx.CallLater(25, guarded(drive)))
                event = wx.CommandEvent(wx.EVT_MENU.typeId, command.GetId())
                event.SetEventObject(frame)
                check('same native menu reopens inactive declarations', frame.GetEventHandler().ProcessEvent(event))
                check('second native Close completed', phase == 21)
                finished = True
                gui.ExitMainLoop()
            wx.CallAfter(guarded(reopen))
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('passive reopening never recreated missing external folders', not watch.exists() and not repository.exists())
            source_check('after')
        finally:
            finished = True
            for call in calls:
                call.Stop()
            for key in tuple(pressed):
                simulator.KeyUp(key)
                pressed.discard(key)
            if frame is not None:
                frame.cancel_event.set()
                frame.local_schedules.close()
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                check('actual host shutdown has no retained worker cleanup failure', not frame.executor.cleanup_failures)
                frame.progress_panel.close()
                frame.Destroy()
                gui.Yield()
            if native is not None:
                native.close()
    watchdog.cancel()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'origins': origins,
        'native_keyboard': 'PASS', 'actual_timer': 'PASS', 'actual_watch_import': 'PASS',
        'canonical_backup_restore': 'PASS', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'unattended_background': 'NOT_IMPLEMENTED', 'physical_media': 'NOT_RUN',
        'power_loss': 'NOT_RUN', 'external_automations': 'NOT_USED'}, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
