"""Installed Windows complete-backup capacity selection and actual restore.

Uses the real DesktopFrame, native keyboard controls, shared worker and canonical
backup/verify/restore APIs. Only system folder selections are fixture-supplied.
A small library proves this workflow, not the advertised maximum capacity.
"""
import argparse
import ctypes
from ctypes import wintypes
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep
import traceback
from unittest.mock import patch


_cleanup_complete = threading.Event()
MODULES = (
    'carr_library.application', 'carr_library.backup_capacity',
    'carr_library.library_backup', 'carr_library.pending_backup',
    'carr_library.backup_sidecars', 'carr_library.storage.revisions',
    'carr_library.ingest.catalog_asset_copy', 'carr_library.ui.backup_capacity',
    'carr_library.ui.desktop_app', 'carr_library.ui.worker_lifetime',
    'carr_library.durable_state', 'carr_library.native_runtime',
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'src')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        raise RuntimeError('Actual Windows is required for native complete-backup acceptance.')
    source_root = args.source_root.resolve(strict=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.backup_capacity import LARGE_SOURCE_PROFILE
    from carr_library import library_backup
    from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
    from carr_library.ui import backup_capacity as view
    from carr_library.ui import desktop_app, worker_lifetime

    checks, errors, origins, installed_paths, pressed = [], [], {}, {}, set()
    modal_loops, failure_control = [], {}
    frame = host_handle = host_executor = host_cancel = None
    unwind_pending = False
    ui_thread = threading.get_ident()
    command_epoch = 0
    backup_calls, restore_calls = [], []
    deadline = monotonic() + 150

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.FindWindowW.argtypes, user32.FindWindowW.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND
    user32.GetWindow.argtypes, user32.GetWindow.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetFocus.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL

    native_child_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumChildWindows.argtypes = [wintypes.HWND, native_child_callback, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.IsChild.argtypes, user32.IsChild.restype = [wintypes.HWND, wintypes.HWND], wintypes.BOOL
    user32.IsWindowVisible.argtypes, user32.IsWindowVisible.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsWindowEnabled.argtypes, user32.IsWindowEnabled.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.SetActiveWindow.argtypes, user32.SetActiveWindow.restype = [wintypes.HWND], wintypes.HWND

    def native_question_button(question, label):
        # wx 4.2.4 uses nested TaskDialog buttons with control ID zero.
        # The caller first binds the question title, process and owner chain.
        if label not in ('Yes', 'No'):
            raise ValueError('Only the explicit native Yes or No control is admitted.')
        if not question or not user32.IsWindow(question):
            return None
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(question, ctypes.byref(process_id))
        if process_id.value != os.getpid():
            raise AssertionError('Refusing native controls from another process.')
        matches, failures = [], []
        visited = 0
        @native_child_callback
        def visit(child, unused):
            nonlocal visited
            try:
                visited += 1
                if visited > 128:
                    raise AssertionError('The owned native question exceeds the descendant bound.')
                if (not user32.IsChild(question, child) or not user32.IsWindowVisible(child)
                        or not user32.IsWindowEnabled(child)):
                    return True
                child_process = wintypes.DWORD()
                user32.GetWindowThreadProcessId(child, ctypes.byref(child_process))
                if child_process.value != os.getpid():
                    raise AssertionError('A native question descendant belongs to another process.')
                class_name = ctypes.create_unicode_buffer(64)
                if user32.GetClassNameW(child, class_name, len(class_name)) <= 0:
                    raise OSError(ctypes.get_last_error(), 'Cannot inspect the native button class.')
                if class_name.value != 'Button':
                    return True
                caption = ctypes.create_unicode_buffer(64)
                length = user32.GetWindowTextW(child, caption, len(caption))
                if 0 < length < len(caption) - 1 and caption.value in (label, '&' + label):
                    matches.append(child)
                return True
            except BaseException as error:
                failures.append(error)
                return False
        user32.EnumChildWindows(question, visit, 0)
        if failures:
            raise failures[0]
        if len(matches) > 1:
            raise AssertionError('The owned native question has ambiguous ' + label + ' controls.')
        if not matches:
            return None
        button = matches[0]
        if not user32.IsWindow(button) or not user32.IsChild(question, button):
            raise AssertionError('The owned native question button disappeared.')
        return button

    def own_modal(window):
        loop = wx.EventLoopBase.GetActive()
        if loop is None or not loop.IsRunning() or loop.IsMain() or not window.IsModal():
            raise AssertionError('The selected backup modal has no running owned event loop.')
        handle = window.GetHandle()
        if not any(old is window and native == handle for old, native, _ in modal_loops):
            modal_loops.append((window, handle, loop))

    def owned_question():
        handle = user32.FindWindowW(None, 'Restore complete')
        if not handle:
            return None
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        owners = {native for window, native, _ in modal_loops
                  if window and user32.IsWindow(native)}
        if host_handle and user32.IsWindow(host_handle):
            owners.add(host_handle)
        owner = user32.GetWindow(handle, 4)  # GW_OWNER
        for _ in range(16):
            if not owner or owner in owners:
                break
            owner = user32.GetWindow(owner, 4)
        if process_id.value != os.getpid() or owner not in owners:
            raise AssertionError('Refusing a restore question outside this probe process and owner chain.')
        if not native_question_button(handle, 'No'):  # Distinguish the read-only wx result.
            return None
        return handle

    def unwind():
        nonlocal unwind_pending
        if unwind_pending:
            return
        unwind_pending = True
        def close_active():
            nonlocal unwind_pending
            unwind_pending = False
            try:
                question = owned_question()
                if question:
                    no_button = native_question_button(question, 'No')
                    if not no_button:
                        raise AssertionError('The owned restore No control disappeared.')
                    user32.SetActiveWindow(question)
                    if not user32.PostMessageW(no_button, 0x00F5, 0, 0):  # BM_CLICK
                        raise OSError(ctypes.get_last_error(), 'Could not queue the owned restore No answer.')
                    if errors:
                        wx.CallLater(25, unwind)
                    return
                if errors and host_handle and user32.IsWindow(host_handle):
                    # A failed restore confirmation can legitimately return No
                    # and then open its read-only result. Capture only this
                    # workflow's exact direct-child dialogs in their live loop.
                    for candidate in wx.GetTopLevelWindows():
                        if (isinstance(candidate, wx.Dialog) and candidate.IsShown()
                                and candidate.IsModal() and candidate.GetParent() is frame
                                and candidate.GetTitle() in ('Backup complete', 'Restore complete',
                                    'Complete-backup capacity', 'Verify backup')):
                            own_modal(candidate)
                for window, handle, loop in reversed(modal_loops):
                    if window and user32.IsWindow(handle) and window.IsModal() and loop.IsRunning():
                        window.EndModal(wx.ID_CANCEL)
                        return
                active = wx.EventLoopBase.GetActive()
                if errors and active is not None and active.IsMain():
                    active.ScheduleExit(1)
            except Exception:
                detail = traceback.format_exc()
                errors.append(detail)
                print('FAIL: native backup cleanup\n' + detail, file=sys.stderr, flush=True)
        wx.CallAfter(close_active)

    def remember_failure():
        exception = sys.exc_info()[1]
        window, handle = failure_control.get('window'), failure_control.get('handle')
        if (failure_control.get('injecting') and type(exception) is RuntimeError
                and str(exception) == 'controlled native backup modal failure'
                and window and user32.IsWindow(handle) and window.IsModal()):
            failure_control['handled'] = True
        else:
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native backup callback\n' + detail, file=sys.stderr, flush=True)
        unwind()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            remember_failure()
            return True

    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()

    def pump(predicate, message, timeout=40, *, cleanup=False):
        until = monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= until or (not cleanup and monotonic() >= deadline):
                raise TimeoutError(message)
            gui.Yield()
            sleep(.01)

    def descendants(parent):
        for child in parent.GetChildren():
            yield child
            yield from descendants(child)

    def failure_cleanup_control():
        outer = wx.Dialog(frame, title='Backup owned outer cleanup fixture')
        inner = wx.Dialog(outer, title='Backup owned inner cleanup fixture')
        failure_control.update(window=inner, handle=inner.GetHandle(),
                               injecting=False, handled=False)
        answered = []
        until = monotonic() + 5
        def inject():
            try:
                if not owned_question():
                    if monotonic() >= until:
                        raise TimeoutError('The controlled backup native question did not appear.')
                    wx.CallLater(25, inject)
                    return
                failure_control['injecting'] = True
                try:
                    raise RuntimeError('controlled native backup modal failure')
                except RuntimeError:
                    gui.OnExceptionInMainLoop()
                finally:
                    failure_control['injecting'] = False
            except Exception:
                remember_failure()
        def inner_opened():
            try:
                own_modal(inner)
                wx.CallLater(25, inject)
                answered.append(wx.MessageBox('Controlled cleanup only; no backup is authorized.',
                    'Restore complete', wx.YES_NO | wx.NO_DEFAULT, inner))
                if answered != [wx.NO] or not inner.IsModal() or not outer.IsModal():
                    raise AssertionError('Failure cleanup terminated a suspended backup parent.')
                unwind()
            except Exception:
                remember_failure()
        def outer_opened():
            try:
                own_modal(outer)
                wx.CallLater(25, inner_opened)
                outcome = inner.ShowModal()
                check('native failure answers owned No before closing only the active inner modal',
                      outcome == wx.ID_CANCEL and answered == [wx.NO]
                      and failure_control.get('handled') and outer.IsModal()
                      and not frame.working and not backup_calls and not restore_calls)
                failure_control['passed'] = True
                outer.EndModal(wx.ID_CANCEL)
            except Exception:
                remember_failure()
        try:
            wx.CallLater(25, outer_opened)
            if outer.ShowModal() != wx.ID_CANCEL:
                raise AssertionError('Controlled backup outer modal did not return Cancel.')
        finally:
            failure_control['injecting'] = False
            for window in (inner, outer):
                if window and user32.IsWindow(window.GetHandle()):
                    window.Destroy()
            gui.Yield()

    def command(item, *, selected=0, folders=(), checksum=None, cancel_choice=False):
        nonlocal command_epoch
        command_epoch += 1
        epoch = command_epoch
        paths, timers, output = list(folders), [], []
        visited = set()
        state = {'choice': False, 'selection_key': False, 'text': False,
                 'result': False, 'question': False, 'key_pending': False, 'text_input': False}
        question_until = text_until = result_until = None
        completed = False
        until = min(deadline, monotonic() + 50)
        expected_result = 'Restore complete' if checksum is not None else 'Backup complete'

        def later(milliseconds, callback, *, release=False):
            def invoke():
                if epoch != command_epoch:
                    return
                if errors and not release:
                    unwind()
                    return
                try:
                    callback()
                except Exception:
                    remember_failure()
            timers.append(wx.CallLater(milliseconds, invoke))

        def key_up(key):
            try:
                if key in pressed:
                    if not simulator.KeyUp(key):
                        raise AssertionError('Native backup key release failed.')
            finally:
                pressed.discard(key)
                state['key_pending'] = False

        def press(control, key=wx.WXK_SPACE):
            state['key_pending'] = True
            top = wx.GetTopLevelParent(control)
            handle = top.GetHandle()
            focus_until = monotonic() + 5
            def dispatch():
                if not user32.IsWindow(handle):
                    raise AssertionError('Native backup key target was destroyed.')
                top.Raise()
                user32.SetForegroundWindow(handle)
                control.SetFocus()
                if wx.Window.FindFocus() is not control or user32.GetForegroundWindow() != handle:
                    if monotonic() >= focus_until:
                        raise TimeoutError('Native backup focus did not reach the selected control.')
                    later(25, dispatch)
                    return
                if not simulator.KeyDown(key):
                    raise AssertionError('Native backup key press failed.')
                pressed.add(key)
                later(40, lambda: key_up(key), release=True)
            later(1, dispatch)

        def visit():
            nonlocal completed, question_until, text_until, result_until
            if monotonic() >= until:
                raise TimeoutError('Native backup command exceeded its finite deadline.')
            question = owned_question()
            if question and not state['question'] and not state['key_pending']:
                if checksum is None:
                    raise AssertionError('Unexpected restore confirmation during backup.')
                if question_until is None:
                    question_until = monotonic() + 5
                no_button = native_question_button(question, 'No')
                focused = user32.GetFocus()
                if user32.GetForegroundWindow() != question or not no_button or focused != no_button:
                    if monotonic() >= question_until:
                        raise AssertionError('Restore confirmation did not settle on default No.')
                else:
                    check('restored-library opening uses actual default-No native confirmation',
                          user32.GetWindow(question, 4) == host_handle and focused == no_button)
                    state['question'] = True
                    state['key_pending'] = True
                    if not simulator.KeyDown(wx.WXK_SPACE):
                        raise AssertionError('Native No key press failed.')
                    pressed.add(wx.WXK_SPACE)
                    later(40, lambda: key_up(wx.WXK_SPACE), release=True)
            elif not question and not state['key_pending']:
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsShown()
                           and window.IsModal() and window.GetParent() is frame]
                # Deferred Destroy from an earlier command cannot be selected:
                # its native loop is not running, and each command has an epoch.
                for dialog in reversed(dialogs):
                    loop = wx.EventLoopBase.GetActive()
                    if loop is None or loop.IsMain() or not loop.IsRunning():
                        continue
                    known = ((isinstance(dialog, wx.SingleChoiceDialog)
                              and dialog.GetTitle() == 'Complete-backup capacity')
                             or (isinstance(dialog, wx.TextEntryDialog)
                                 and dialog.GetTitle() == 'Verify backup')
                             or dialog.GetTitle() == expected_result)
                    if not known:
                        continue
                    own_modal(dialog)
                    handle = dialog.GetHandle()
                    if isinstance(dialog, wx.SingleChoiceDialog) and not state['choice']:
                        if handle not in visited:
                            check('capacity dialog starts with the ordinary allowance',
                                  dialog.GetName() == 'Complete-backup capacity'
                                  and dialog.GetSelection() == 0)
                            visited.add(handle)
                        if cancel_choice:
                            state['choice'] = True
                            press(dialog.FindWindowById(wx.ID_CANCEL))
                        elif selected == 1 and not state['selection_key']:
                            lists = [child for child in descendants(dialog) if isinstance(child, wx.ListBox)]
                            if len(lists) != 1:
                                raise AssertionError('The native capacity choice has no unique list.')
                            state['selection_key'] = True
                            press(lists[0], wx.WXK_DOWN)
                        else:
                            if dialog.GetSelection() != selected:
                                raise AssertionError('Native keyboard did not select the reviewed capacity.')
                            state['choice'] = True
                            press(dialog.FindWindowById(wx.ID_OK))
                        break
                    if isinstance(dialog, wx.TextEntryDialog) and not state['text']:
                        if checksum is None or dialog.GetTitle() != 'Verify backup':
                            raise AssertionError('Unexpected native checksum prompt.')
                        readers = [child for child in descendants(dialog) if isinstance(child, wx.TextCtrl)]
                        if len(readers) != 1:
                            raise AssertionError('The checksum prompt has no unique text control.')
                        if text_until is None:
                            text_until = monotonic() + 5
                        if not state['text_input']:
                            dialog.Raise()
                            user32.SetForegroundWindow(handle)
                            readers[0].SetFocus()
                            if (wx.Window.FindFocus() is not readers[0]
                                    or user32.GetForegroundWindow() != handle):
                                if monotonic() >= text_until:
                                    raise TimeoutError('Checksum control did not receive native focus.')
                                break
                            # UIActionSimulator.Text requires bytes in wxPython 4.2.
                            if not simulator.Text(checksum.encode('ascii')):
                                raise AssertionError('Native checksum input failed.')
                            state['text_input'] = True
                            break
                        if dialog.GetValue() != checksum:
                            if monotonic() >= text_until:
                                raise AssertionError('Native checksum does not equal the retained receipt.')
                            break
                        state['text'] = True
                        check('native checksum entry preserves the exact retained manifest identity',
                              dialog.GetValue() == checksum)
                        press(dialog.FindWindowById(wx.ID_OK))
                        break
                    if dialog.GetTitle() == expected_result and not state['result']:
                        readers = [child for child in descendants(dialog)
                                   if isinstance(child, wx.TextCtrl) and child.GetName() == expected_result]
                        if len(readers) != 1 or readers[0].IsEditable():
                            raise AssertionError('Completion has no unique read-only result.')
                        if result_until is None:
                            result_until = monotonic() + 5
                        if wx.Window.FindFocus() is not readers[0]:
                            if monotonic() >= result_until:
                                raise TimeoutError('Native completion did not focus its readable result.')
                            break
                        check('native completion retains focused readable result',
                              wx.Window.FindFocus() is readers[0])
                        output.append(readers[0].GetValue())
                        state['result'] = True
                        press(dialog.FindWindowById(wx.ID_CANCEL))
                        break
            if (cancel_choice and state['choice'] and not state['key_pending']
                    and not any(window and user32.IsWindow(handle) and window.IsModal()
                                for window, handle, _ in modal_loops)):
                completed = True
            if state['result'] and not state['key_pending']:
                completed = True
            if not completed:
                later(25, visit)

        def selected_folder(parent, title):
            if parent is not frame or not paths:
                raise AssertionError('Unexpected system folder selection.')
            return paths.pop(0)

        with patch.object(view, '_directory', side_effect=selected_folder):
            try:
                later(25, visit)
                event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
                event.SetEventObject(frame)
                if not frame.GetEventHandler().ProcessEvent(event):
                    raise AssertionError('Existing native backup menu dispatch was not handled.')
                pump(lambda: completed, 'Native backup result did not complete.', timeout=55)
                pump(lambda: not frame.working, 'Shared backup worker did not finish.')
                if errors:
                    raise RuntimeError('\n'.join(errors))
                if paths or not state['choice'] or (checksum is not None and not state['question']):
                    raise AssertionError('Native backup did not consume its exact reviewed inputs.')
                return output[0] if output else None
            finally:
                command_epoch += 1
                for timer in timers:
                    timer.Stop()
                for key in tuple(pressed):
                    simulator.KeyUp(key)
                    pressed.discard(key)
                gui.Yield()

    outcomes = {}
    with TemporaryDirectory(prefix='carr-native-backup-capacity-') as temporary:
        root = Path(temporary)
        try:
            for name in MODULES:
                module = importlib.import_module(name)
                actual = Path(module.__file__).resolve(strict=True)
                expected = source_root / Path(*name.split('.')).with_suffix('.py')
                digest = sha256(actual.read_bytes()).hexdigest()
                check('exact installed source: ' + name,
                      actual.is_relative_to(Path(sys.prefix).resolve())
                      and not actual.is_relative_to(source_root)
                      and digest == sha256(expected.read_bytes()).hexdigest())
                origins[name], installed_paths[name] = digest, actual
            check('real native Windows controls', wx.Platform == '__WXMSW__')
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Native complete backup')
            originals = ('Railway capacity original: Україна.', 'Railway capacity second original.')
            for index, original in enumerate(originals, 1):
                library.add_text(workspace, 'Оригінал ' + str(index), original)
            ProgressPreferenceStore(library.root / 'progress-preferences.json').save(ProgressPreferences(muted=True))
            frame = desktop_app.DesktopFrame(library)
            host_handle, host_executor, host_cancel = frame.GetHandle(), frame.executor, frame.cancel_event
            frame.Show()
            gui.SetTopWindow(frame)
            check('existing host executor owns backup work and finalizers',
                  isinstance(host_executor, worker_lifetime.DesktopExecutor))
            items = [item for index in range(frame.menu_bar.GetMenuCount())
                     for item in frame.menu_bar.GetMenu(index).GetMenuItems()]
            backup_item = next(item for item in items if item.GetItemLabel().replace('&', '') == 'Back up all libraries')
            restore_item = next(item for item in items if item.GetItemLabel().replace('&', '') == 'Restore backup to a new folder')
            ordinary_parent, large_parent, restored_parent = (root / name for name in ('ordinary', 'large', 'restored'))
            for directory in (ordinary_parent, large_parent, restored_parent):
                directory.mkdir()
            actual_backup, actual_restore = library.backup, library_backup.restore_backup
            def observed_backup(destination, **options):
                check('actual backup executes on the shared worker', threading.get_ident() != ui_thread)
                result = actual_backup(destination, **options)
                backup_calls.append((options.get('capacity_profile'), Path(destination), result))
                return result
            def observed_restore(source, destination, *args, **options):
                check('actual restore executes on the shared worker', threading.get_ident() != ui_thread)
                result = actual_restore(source, destination, *args, **options)
                restore_calls.append((options.get('capacity_profile'), Path(result)))
                return result

            def run_steps():
                try:
                    failure_cleanup_control()
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    check('native capacity cancellation opens no folder or worker',
                          command(backup_item, cancel_choice=True) is None
                          and not backup_calls and not restore_calls
                          and not list(ordinary_parent.iterdir()) and not list(large_parent.iterdir()))
                    ordinary_text = command(backup_item, folders=(ordinary_parent,))
                    large_text = command(backup_item, selected=1, folders=(large_parent,))
                    check('ordinary and large selections reach the real canonical backup',
                          [row[0] for row in backup_calls] == [None, LARGE_SOURCE_PROFILE])
                    check('backup result explicitly includes validated unfinished imports',
                          'unfinished imports' in ordinary_text and 'unfinished imports' in large_text)
                    manifest_hash = re.search(r'\b[0-9a-f]{64}\b', large_text)
                    check('native backup presents the independently retainable manifest checksum',
                          manifest_hash is not None and manifest_hash.group() == backup_calls[1][2])
                    for profile, destination, digest in backup_calls:
                        manifest = library_backup.verify_backup(destination, digest, capacity_profile=profile)
                        check('selected allowance preserves complete4 and pending1 wire',
                              manifest['version'] == 4 and manifest['pending']['version'] == 1
                              and 'capacity_profile' not in manifest
                              and 'capacity_profile' not in manifest['pending'])
                    result_text = command(restore_item, selected=1,
                        folders=(backup_calls[1][1], restored_parent), checksum=backup_calls[1][2])
                    check('explicit large restore reaches the same canonical fresh-destination API',
                          len(restore_calls) == 1 and restore_calls[0][0] == LARGE_SOURCE_PROFILE
                          and restore_calls[0][1].parent == restored_parent)
                    recovered = LibraryApp(restore_calls[0][1])
                    check('real restored library preserves both retained originals and search',
                          len(recovered.search(workspace, 'Railway capacity')) == 2
                          and all(recovered.assets.resolve(sha256(text.encode('utf-8')).hexdigest()).read_bytes()
                                  == library.assets.resolve(sha256(text.encode('utf-8')).hexdigest()).read_bytes()
                                  == text.encode('utf-8') for text in originals)
                          and 'Restored library folder:' in result_text)
                    check('default-No restore leaves the original LibraryApp active', frame.library is library)
                    check('native backup menus dispatch the existing host workflow',
                          len(backup_calls) == 2 and len(restore_calls) == 1)
                    outcomes.update(native_keyboard='PASS', native_backup_restore='PASS')
                except Exception:
                    remember_failure()
                finally:
                    active = wx.EventLoopBase.GetActive()
                    if active is not None and active.IsMain():
                        active.ScheduleExit(1 if errors else 0)
                    elif errors:
                        unwind()

            with patch.object(library, 'backup', side_effect=observed_backup), \
                 patch.object(library_backup, 'restore_backup', side_effect=observed_restore):
                wx.CallAfter(run_steps)
                gui.MainLoop()
        finally:
            owners_released = False
            try:
                for key in tuple(pressed):
                    simulator.KeyUp(key)
                    pressed.discard(key)
                if host_executor is not None:
                    host_cancel.set()
                    try:
                        if user32.IsWindow(host_handle):
                            frame.Close(force=True)
                        pump(lambda: not user32.IsWindow(host_handle),
                             'Actual backup host Close did not release its native owners.', cleanup=True)
                    finally:
                        host_executor.shutdown(wait=True, cancel_futures=True)
                    check('native host Close drains the actual executor without retained failure',
                          not user32.IsWindow(host_handle) and not host_executor.cleanup_failures)
                    outcomes['native_host_cleanup'] = 'PASS'
                gui.Yield()
                for name, actual in installed_paths.items():
                    check('unchanged installed source after cleanup: ' + name,
                          sha256(actual.read_bytes()).hexdigest() == origins[name]
                          == sha256((source_root / Path(*name.split('.')).with_suffix('.py')).read_bytes()).hexdigest())
                owners_released = True
            finally:
                if native_handle is not None:
                    native_handle.close()
                if not owners_released:
                    print('FAIL: native backup cleanup did not complete.', file=sys.stderr, flush=True)
        if errors:
            raise RuntimeError('\n'.join(errors))
    check('native failure cleanup control completed', bool(failure_control.get('passed')))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'origins': origins, **outcomes,
        'native_failure_cleanup': 'PASS', 'large_corpus_measurement': 'NOT_RUN',
        'manual_nvda': 'NOT_RUN', 'physical_media': 'NOT_RUN', 'power_loss': 'NOT_RUN',
        'folder_picker': 'explicit fixture selections; system picker not automated'},
        ensure_ascii=True, indent=2))
    _cleanup_complete.set()
    return 0


if __name__ == '__main__':
    import faulthandler
    faulthandler.enable(all_threads=True)
    def watchdog():
        if not _cleanup_complete.wait(180):
            os.write(2, b'FAIL: native complete-backup acceptance or cleanup exceeded 180 seconds.\n')
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True, name='backup-capacity-native-watchdog').start()
    raise SystemExit(main())
