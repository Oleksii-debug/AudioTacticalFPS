"""Installed Book workspace -> Multiple books -> per-book native reviews.

Real wx controls, native default-No confirmations, selected SAPI file synthesis
and pinned FFmpeg are required. No acoustic, NVDA or physical-device claim.
"""
import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
import threading
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio-directory', required=True, type=Path)
    parser.add_argument('--audio-lock', type=Path,
        default=Path(__file__).resolve().parents[1] / 'packaging/windows-audio.lock.json')
    arguments = parser.parse_args()
    selected_audio = arguments.audio_directory.resolve(strict=True)
    audio_pin = json.loads(arguments.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library import runtime_settings, backup_book_batches
    from carr_library.audio import dsp, process
    from carr_library.operations import broker
    from carr_library.speech import NativeSapiProvider
    from carr_library.speech import source as source_module
    from carr_library.reader import reading_desk
    from carr_library.studio import book_audio, book_batch, book_cli, project, render
    from carr_library.ui import book_audio_dialog, book_batch_dialog, worker_lifetime, desktop_app

    checks, errors, pressed, audio_proof, spoken = [], [], set(), {}, []
    frame = primary = batch_dialog = child = service = production = registry = None
    close_primary = None
    host_handle = host_executor = host_cancel = None
    modal_loops, question_waits, failure_control = [], {}, {}
    abort_pending = False
    question_titles = ('Use reviewed book in batch', 'Prepare reviewed batch', 'Run reviewed book batch')
    ui_thread = threading.get_ident()
    stage, key_pending, finished, reviewing = -1, False, False, 0
    deadline = monotonic() + 300

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def remember_failure():
        exception = sys.exc_info()[1]
        fixture = failure_control.get('window')
        fixture_handle = failure_control.get('handle')
        if (failure_control.get('injecting') and type(exception) is RuntimeError
                and str(exception) == 'controlled native BookBatch confirmation failure'
                and fixture and user32.IsWindow(fixture_handle) and fixture.IsModal()):
            failure_control['handled'] = True
        else:
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native BookBatch callback\n' + detail, file=sys.stderr, flush=True)
        abort()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            remember_failure()
            return True

    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    gui.SetExitOnFrameDelete(False)
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

    user32.GetForegroundWindow.argtypes = []
    user32.GetFocus.argtypes = []
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.FindWindowW.argtypes, user32.FindWindowW.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
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
            raise AssertionError('The owned BookBatch modal has no running native event loop.')
        record = (window, window.GetHandle(), loop)
        if not any(item[0] is window and item[1] == record[1] for item in modal_loops):
            modal_loops.append(record)

    def owned_question(title):
        handle = user32.FindWindowW(None, title)
        if not handle:
            return None
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        owners = {native for window, native, _ in modal_loops
                  if window and user32.IsWindow(native)}
        parent = user32.GetWindow(handle, 4)  # GW_OWNER
        for _ in range(16):
            if not parent or parent in owners:
                break
            parent = user32.GetWindow(parent, 4)
        if process_id.value != os.getpid() or parent not in owners:
            raise AssertionError('Refusing a native confirmation not owned by this BookBatch probe.')
        if not native_question_button(handle, 'No'):
            return None
        return handle

    def dismiss_owned_question():
        # MessageBox has its own native modal loop. Answer only its exact
        # process/owner-bound question No before touching any suspended wx loop.
        for title in question_titles:
            handle = owned_question(title)
            if not handle:
                continue
            no_button = native_question_button(handle, 'No')
            if not no_button:
                raise AssertionError('The owned BookBatch confirmation has no native No button.')
            user32.SetActiveWindow(handle)
            if not user32.PostMessageW(no_button, 0x00F5, 0, 0):  # BM_CLICK, actual button
                raise OSError(ctypes.get_last_error(), 'Could not queue owned BookBatch No cleanup.')
            return True
        return False

    def abort():
        nonlocal abort_pending
        if abort_pending:
            return
        abort_pending = True

        def close_active():
            nonlocal abort_pending
            abort_pending = False
            try:
                if dismiss_owned_question():
                    # Product handlers return after No without calling the
                    # probe again. Continue real-failure cleanup only after the
                    # native question has yielded back to its owning wx loop.
                    if errors:
                        wx.CallLater(25, abort)
                    return
                for window, handle, loop in reversed(modal_loops):
                    if window and user32.IsWindow(handle) and window.IsModal() and loop.IsRunning():
                        window.EndModal(wx.ID_CANCEL)
                        if errors:
                            wx.CallLater(25, abort)
                        return
                active = wx.EventLoopBase.GetActive()
                if errors and active is not None and active.IsMain():
                    active.ScheduleExit(1)
            except Exception:
                detail = traceback.format_exc()
                errors.append(detail)
                print('FAIL: native BookBatch cleanup\n' + detail, file=sys.stderr, flush=True)
                # Never destroy a suspended parent or an unowned question.
                # The process watchdog remains active through cleanup.
        wx.CallAfter(close_active)

    def pump(predicate, message, timeout=20, *, cleanup=False):
        until = monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= until:
                raise TimeoutError(message)
            gui.Yield()
            sleep(.01)

    def guarded(callback):
        def invoke():
            if finished or errors:
                return
            try:
                callback()
            except Exception:
                remember_failure()
        return invoke

    def release(key, name):
        nonlocal key_pending
        try:
            if key in pressed:
                check('native key release: ' + name, simulator.KeyUp(key))
        finally:
            pressed.discard(key)
            key_pending = False

    def press(control, key=wx.WXK_SPACE):
        nonlocal key_pending
        key_pending = True
        top = wx.GetTopLevelParent(control)
        focus_deadline = min(deadline, monotonic() + 5)
        name = control.GetName() or control.GetLabel()
        def dispatch():
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
            if wx.Window.FindFocus() != control or user32.GetForegroundWindow() != top.GetHandle():
                if monotonic() >= focus_deadline:
                    raise TimeoutError('Native focus did not reach ' + name)
                wx.CallLater(25, guarded(dispatch))
                return
            check('owned native focus: ' + name, wx.Window.FindFocus() == control)
            check('native key down: ' + name, simulator.KeyDown(key))
            pressed.add(key)
            wx.CallLater(40, guarded(lambda: release(key, name)))
        wx.CallAfter(guarded(dispatch))

    def named(window, name):
        def descendants(window):
            for child in window.GetChildren():
                yield child
                yield from descendants(child)
        values = [control for control in descendants(window) if control.GetName() == name]
        if len(values) != 1:
            raise AssertionError('Unique accessible control missing: ' + name)
        return values[0]

    def confirm(owner, title, *, accepted):
        """Drive the product's actual default-No Windows MessageBox."""
        nonlocal key_pending
        handle = owned_question(title)
        if not handle:
            return False
        expected_parent = owner.GetHandle()
        if user32.GetWindow(handle, 4) != expected_parent:
            raise AssertionError('The actual BookBatch confirmation has a different direct parent.')
        current_focus = user32.GetFocus()
        no_button = native_question_button(handle, 'No')
        key_identity = (stage, title, handle)
        if key_identity not in question_waits:
            question_waits[key_identity] = min(deadline, monotonic() + 5)
            print('DIAG: BookBatch confirmation focus ' + json.dumps({
                'stage': stage, 'foreground': user32.GetForegroundWindow() == handle,
                'focused_hwnd': current_focus, 'no_button_hwnd': no_button,
                'control_id': user32.GetDlgCtrlID(current_focus)}), flush=True)
        # Appearance precedes focus settlement on some Windows builds. Observe
        # the actual default; never move focus to No to manufacture this proof.
        if not no_button or user32.GetForegroundWindow() != handle or current_focus != no_button:
            if monotonic() >= question_waits[key_identity]:
                raise AssertionError('Native BookBatch confirmation did not settle on its default No; '
                                     'observed focus/no HWNDs ' + str((current_focus, no_button)))
            return False
        check('confirmation belongs to the book dialog: ' + title,
              user32.GetWindow(handle, 4) == expected_parent)
        check('confirmation defaults to No: ' + title, bool(no_button) and current_focus == no_button)
        # Space activates the actually observed No button; Escape is not an
        # alternative proof of the selected default on a native TaskDialog.
        key = wx.WXK_SPACE
        if accepted:
            yes = native_question_button(handle, 'Yes')
            check('native Yes button exists: ' + title, bool(yes))
            user32.SetFocus(yes)
            check('native explicit Yes focus: ' + title, user32.GetFocus() == yes)
            key = wx.WXK_SPACE
        key_pending = True
        check('native confirmation key: ' + title, simulator.KeyDown(key))
        pressed.add(key)
        wx.CallLater(40, guarded(lambda: release(key, title)))
        return True


    def verify_audio(runtime):
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            check('explicit selected audio executable: ' + name,
                  binary.samefile(selected_audio / 'bin' / (name + '.exe')))
            check('pinned audio executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])
            audio_proof[name] = {'path': str(binary), 'sha256': digest}

    def top(name, *, exclude=None):
        return next((window for window in wx.GetTopLevelWindows()
                     if window is not exclude and window.GetName() == name
                     and isinstance(window, wx.Dialog) and window.IsShown() and window.IsModal()), None)

    def titled(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.GetTitle() == title
                     and window.IsShown() and window.IsModal()), None)

    def release_owned():
        try:
            if service is not None:
                service.close()
        finally:
            if production is not None:
                production.close()


    def live_book_selections(picker):
        # wxMultiChoiceDialog.SetSelections writes its live list/checklist.
        # GetSelections is a result cache until the actual OK transfer.
        if not isinstance(picker, wx.MultiChoiceDialog):
            raise AssertionError('The existing-book picker is not a native multi-choice dialog.')
        lists = [control for control in picker.GetChildren() if isinstance(control, wx.ListBox)]
        if len(lists) != 1:
            raise AssertionError('The native book picker has no unique live list.')
        choices = lists[0]
        if isinstance(choices, wx.CheckListBox):
            return tuple(index for index in range(choices.GetCount()) if choices.IsChecked(index))
        return tuple(index for index in range(choices.GetCount()) if choices.IsSelected(index))

    def native_failure_control():
        fixture = wx.Dialog(primary, title='Native BookBatch failure cleanup fixture')
        refused = []
        failure_control.update(window=fixture, handle=fixture.GetHandle(),
                               handled=False, injecting=False)
        until = monotonic() + 5

        def inject_failure():
            try:
                if not owned_question(question_titles[0]):
                    if monotonic() >= until:
                        raise TimeoutError('The controlled native Book question did not appear.')
                    wx.CallLater(25, inject_failure)
                    return
                failure_control['injecting'] = True
                try:
                    raise RuntimeError('controlled native BookBatch confirmation failure')
                except RuntimeError:
                    gui.OnExceptionInMainLoop()
                finally:
                    failure_control['injecting'] = False
            except Exception:
                remember_failure()

        def fixture_opened():
            try:
                own_modal(fixture)
                wx.CallLater(25, inject_failure)
                refused.append(wx.MessageBox('Synthetic failure cleanup; no book jobs are authorized.',
                    question_titles[0], wx.YES_NO | wx.NO_DEFAULT, fixture))
                if (refused != [wx.NO] or not fixture.IsModal() or not primary.IsModal()
                        or not failure_control.get('handled')):
                    raise AssertionError('Native Book question cleanup touched a suspended parent.')
                abort()
            except Exception:
                remember_failure()

        try:
            wx.CallLater(25, fixture_opened)
            if fixture.ShowModal() != wx.ID_CANCEL:
                raise AssertionError('The controlled Book modal did not unwind through Cancel.')
            check('native batch failure answers only owned question No before unwinding active modal',
                  refused == [wx.NO] and failure_control.get('handled')
                  and primary.IsModal() and not frame.working and not spoken
                  and production.jobs() == () and all(not list(output.iterdir()) for output in outputs))
            failure_control['passed'] = True
        finally:
            failure_control['injecting'] = False
            if user32.IsWindow(failure_control['handle']):
                fixture.Destroy()


    with TemporaryDirectory(prefix='resonance-native-book-batch-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        try:
            checkout = Path(__file__).resolve().parents[1]
            modules = (book_batch, book_audio, book_cli, book_audio_dialog, book_batch_dialog,
                       backup_book_batches, worker_lifetime, render, project, reading_desk,
                       source_module, runtime_settings, broker, dsp, process, desktop_app)
            for module in modules:
                actual = Path(module.__file__).resolve()
                expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
                check('exact installed source: ' + module.__name__,
                      actual.is_relative_to(Path(sys.prefix).resolve())
                      and not actual.is_relative_to(checkout)
                      and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())
            check('real native Windows batch controls', wx.Platform == '__WXMSW__')
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Native ordered BookBatch')
            originals = ('First literal book. [Music: calm] remains source text.',
                         'The second selected book keeps its own reviewed settings.')
            imports = tuple(library.add_text(workspace, title, text) for title, text in zip(
                ('First selected book', 'Second selected book'), originals))
            assets = {row['asset']: text.encode('utf-8') for row, text in zip(imports, originals)}
            outputs = (root / 'first-output', root / 'second-output')
            for output in outputs:
                output.mkdir()
            settings = runtime_settings.load_settings(library.root / 'state')
            settings.update(ffmpeg=str(selected_audio / 'bin/ffmpeg.exe'),
                            ffprobe=str(selected_audio / 'bin/ffprobe.exe'))
            runtime_settings.save_settings(library.root / 'state', settings)
            production = render.ProductionService(library.root / 'state/studio')
            verify_audio(production._runtime())
            service = book_audio.BookAudioService(library, workspace, production=production)
            groups = book_batch.BookBatchService(service)
            frame = desktop_app.DesktopFrame(library)
            host_handle, host_executor, host_cancel = frame.GetHandle(), frame.executor, frame.cancel_event
            frame.register_worker_cleanup(release_owned)
            frame.Show()
            gui.SetTopWindow(frame)
            check('existing Book command uses canonical command_id',
                  'studio.book_audio' in {command.command_id for command in
                      frame.keyboard_commands.registry.commands('main')})
            check('existing host owns one serial worker and original scope',
                  isinstance(frame.executor, worker_lifetime.DesktopExecutor)
                  and frame.active == (workspace, None))
            primary, close_primary = book_audio_dialog.create_book_audio_dialog(frame, service=service)
            accepted, expected_roles = {}, []

            def drive():
                nonlocal stage, registry, batch_dialog, child, reviewing
                if monotonic() >= deadline:
                    raise TimeoutError('Native BookBatch deadline at stage ' + str(stage))
                try:
                    if key_pending:
                        return
                    if stage == -1:
                        own_modal(primary)
                        native_failure_control()
                        stage = 0
                    elif stage == 0:
                        stage = 1
                        press(named(primary, 'Multiple books...'))
                    elif stage == 1:
                        found = top('Multiple book audio workspace')
                        if found is not None:
                            batch_dialog = found
                            own_modal(batch_dialog)
                            check('real Multiple books action opens the existing batch workspace', True)
                            stage = 2
                            press(named(batch_dialog, 'Choose library books'))
                    elif stage == 2:
                        picker = titled('Existing library books')
                        if picker is not None:
                            own_modal(picker)
                            picker.SetSelections([0, 1])
                            check('actual native library chooser selects two existing books',
                                  live_book_selections(picker) == (0, 1))
                            stage = 3
                            press(wx.FindWindowById(wx.ID_OK, picker))
                    elif stage == 3 and not frame.working:
                        choice = named(batch_dialog, 'Ordered books; review each book before Preview')
                        if choice.GetCount() == 2:
                            check('choosing books does not prepare or synthesize', not production.jobs() and not spoken)
                            choice.SetSelection(reviewing)
                            stage = 4
                            press(named(batch_dialog, 'Review selected book'))
                    elif stage == 4:
                        found = top('Book audio workspace', exclude=primary)
                        if found is not None:
                            child = found
                            own_modal(child)
                            if not frame.working and 'reading positions; revision' in named(child, 'Selected library source').GetValue():
                                stage = 5
                                press(named(child, 'Discover local voices'))
                    elif stage == 5 and not frame.working and named(child, 'Installed local voice').GetCount():
                        registry = production.voices
                        voices = tuple(registry.voices())
                        selected = next((index for index, voice in enumerate(voices)
                            if isinstance(registry._owners[voice.id], NativeSapiProvider)), None)
                        check('actual installed SAPI is available for reviewed book ' + str(reviewing + 1),
                              selected is not None)
                        if not spoken and not accepted.get('observed'):
                            actual_synthesize = registry.synthesize
                            def observed(text, voice_id, destination, **options):
                                spoken.append((text, voice_id, options['settings'].rate,
                                               options['settings'].volume, threading.get_ident()))
                                return actual_synthesize(text, voice_id, destination, **options)
                            registry.synthesize = observed
                            accepted['observed'] = True
                        named(child, 'Installed local voice').SetSelection(selected)
                        rate, volume, format = ((1, 75, 'wav'), (-1, 55, 'flac'))[reviewing]
                        expected_roles.append((voices[selected].id, rate, volume, format))
                        named(child, 'Voice rate, -10 to 10').ChangeValue(str(rate))
                        named(child, 'Voice volume, 0 to 100').ChangeValue(str(volume))
                        named(child, 'Audio format').SetSelection(book_audio.FORMATS.index(format))
                        named(child, 'Reviewed output folder').ChangeValue(str(outputs[reviewing]))
                        stage = 6
                        press(named(child, 'Preview split and names'))
                    elif stage == 6 and not frame.working and named(child, 'Book split preview and saved job status').GetValue().startswith('PREVIEW ONLY'):
                        check('per-book native review remains nonexecuting', not production.jobs() and not spoken)
                        check('per-book review has native status focus',
                              wx.Window.FindFocus() == named(child, 'Book split preview and saved job status'))
                        stage = 7
                        press(named(child, 'Use reviewed book'))
                    elif stage == 7 and confirm(child, 'Use reviewed book in batch', accepted=True):
                        stage = 8
                    elif stage == 8 and top('Book audio workspace', exclude=primary) is None and not frame.working:
                        check('nested review returns to its owning batch without jobs', not production.jobs() and not spoken)
                        reviewing += 1
                        if reviewing < 2:
                            stage = 3
                        else:
                            named(batch_dialog, 'Ordered books; review each book before Preview').SetSelection(1)
                            stage = 9
                            press(named(batch_dialog, 'Move selected book up'))
                    elif stage == 9 and not frame.working:
                        stage = 10
                        press(named(batch_dialog, 'Preview entire batch'))
                    elif stage == 10 and not frame.working:
                        status = named(batch_dialog, 'Multiple book review and existing Studio checkpoints')
                        if status.GetValue().startswith('BATCH PREVIEW'):
                            check('whole batch preview contains immutable order and source identities',
                                  'Batch review SHA-256:' in status.GetValue()
                                  and status.GetValue().count('Retained original SHA-256:') == 2)
                            check('whole batch preview still starts no jobs or speech', not production.jobs() and not spoken)
                            stage = 11
                            press(named(batch_dialog, 'Prepare batch items'))
                    elif stage == 11 and confirm(batch_dialog, 'Prepare reviewed batch', accepted=False):
                        stage = 12
                    elif stage == 12 and not frame.working:
                        check('actual default-No batch confirmation creates no jobs', not production.jobs()
                              and all(not tuple(output.iterdir()) for output in outputs))
                        stage = 13
                        press(named(batch_dialog, 'Prepare batch items'))
                    elif stage == 13 and confirm(batch_dialog, 'Prepare reviewed batch', accepted=True):
                        stage = 14
                    elif stage == 14 and not frame.working:
                        status = named(batch_dialog, 'Multiple book review and existing Studio checkpoints')
                        if status.GetValue().startswith('PREPARED'):
                            saved, = groups.saved()
                            plans = groups.plans(saved)
                            accepted.update(batch=saved, plans=plans)
                            check('Prepare retains two existing schema2 Studio jobs without speech',
                                  len(production.jobs()) == 2 and not spoken
                                  and all(job['project']['schema'] == 2 for job in production.jobs()))
                            check('native reordered plans retain exact independent voice controls and formats',
                                  [(p.role.voice_id, p.role.rate, p.role.volume, p.format) for p in plans]
                                  == list(reversed(expected_roles)))
                            check('all reviewed output folders are still empty',
                                  all(not tuple(output.iterdir()) for output in outputs))
                            stage = 15
                            press(named(batch_dialog, 'Run or resume batch'))
                    elif stage == 15 and confirm(batch_dialog, 'Run reviewed book batch', accepted=True):
                        stage = 16
                    elif stage == 16 and not frame.working:
                        status = named(batch_dialog, 'Multiple book review and existing Studio checkpoints')
                        if status.GetValue().startswith('Batch invocation: complete'):
                            plans = accepted['plans']
                            states = [production.get(plan.job_id(1)) for plan in plans]
                            check('actual native Run completes both ordered source-bound jobs',
                                  all(state['status'] == 'complete' for state in states))
                            check('actual SAPI follows the exact reviewed order and controls',
                                  len(spoken) == 2 and
                                  [(voice, rate, volume) for _, voice, rate, volume, _ in spoken]
                                  == [(p.role.voice_id, p.role.rate, p.role.volume) for p in plans]
                                  and all(thread != ui_thread for *_, thread in spoken))
                            check('literal original texts are narrated without Studio cues',
                                  [text for text, *_ in spoken] == [p.source['text'] for p in plans]
                                  and all(not scene.cues and scene.music is None
                                      for plan in plans for scene in plan.project(1).scenes))
                            for index, (plan, state) in enumerate(zip(plans, states), 1):
                                destination = Path(state['destination'])
                                with wave.open(str(destination / 'master.wav'), 'rb') as audio:
                                    check('actual rendered batch PCM: ' + str(index),
                                          audio.getframerate() == plan.sample_rate and audio.getnchannels() == 2
                                          and audio.getnframes() > 0)
                                check('exact batch source receipt: ' + str(index),
                                      json.loads((destination / 'book-source.json').read_bytes()) == plan.provenance(1))
                            accepted['artifacts'] = {item['path']: (
                                sha256(Path(item['path']).read_bytes()).hexdigest(),
                                Path(item['path']).stat().st_mtime_ns)
                                for state in states for item in state['artifacts']}
                            check('completed batch returns actual native status focus', wx.Window.FindFocus() == status)
                            stage = 17
                            press(named(batch_dialog, 'Run or resume batch'))
                    elif stage == 17 and confirm(batch_dialog, 'Run reviewed book batch', accepted=True):
                        stage = 18
                    elif stage == 18 and not frame.working:
                        status = named(batch_dialog, 'Multiple book review and existing Studio checkpoints')
                        if status.GetValue().startswith('Batch invocation: complete'):
                            check('explicit native resume verifies completed bytes without synthesis', len(spoken) == 2
                                  and accepted['artifacts'] == {path: (
                                      sha256(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns)
                                      for path in accepted['artifacts']})
                            stage = 19
                            press(named(batch_dialog, 'New batch'))
                    elif stage == 19 and not frame.working:
                        if named(batch_dialog, 'Ordered books; review each book before Preview').GetCount() == 0:
                            stage = 20
                            press(named(batch_dialog, 'Saved batches'))
                    elif stage == 20:
                        picker = titled('Saved book batches')
                        if picker is not None:
                            own_modal(picker)
                            picker.SetSelection(0)
                            stage = 21
                            press(wx.FindWindowById(wx.ID_OK, picker))
                    elif stage == 21 and not frame.working:
                        status = named(batch_dialog, 'Multiple book review and existing Studio checkpoints')
                        if 'No saved batch is automatically resumed' in status.GetValue():
                            check('reopened batch has no automatic resume authority',
                                  not named(batch_dialog, 'Run or resume batch').IsEnabled() and len(spoken) == 2)
                            check('retained original assets remain byte-identical',
                                  all(library.assets.resolve(key).read_bytes() == raw for key, raw in assets.items()))
                            check('batch narration creates no quotation approval', library._selections == {})
                            stage = 22
                            press(named(batch_dialog, 'Close'), wx.WXK_ESCAPE)
                    elif stage == 22 and top('Multiple book audio workspace') is None:
                        check('native batch close preserves borrowed Book service', not service._closed.is_set()
                              and not production._closed)
                        stage = 23
                        press(named(primary, 'Close'), wx.WXK_ESCAPE)
                finally:
                    if not errors and not finished:
                        wx.CallLater(25, guarded(drive))

            def open_workspace():
                nonlocal finished
                try:
                    wx.CallLater(25, guarded(drive))
                    primary.ShowModal()
                    close_primary()
                    if not errors:
                        check('native Escape unwinds both existing Book workspaces', stage == 23)
                        finished = True
                except Exception:
                    remember_failure()
                finally:
                    active = wx.EventLoopBase.GetActive()
                    if active is not None and active.IsMain():
                        active.ScheduleExit(1 if errors else 0)
                    elif errors:
                        abort()
            wx.CallAfter(guarded(open_workspace))
            gui.MainLoop()
        finally:
            try:
                for key in tuple(pressed):
                    simulator.KeyUp(key)
                    pressed.discard(key)
                try:
                    if close_primary is not None:
                        close_primary()
                finally:
                    if host_executor is not None:
                        host_cancel.set()
                        try:
                            if user32.IsWindow(host_handle):
                                frame.Close(force=True)
                            pump(lambda: not user32.IsWindow(host_handle),
                                 'Actual BookBatch host Close did not release its native owners.', cleanup=True)
                        finally:
                            host_executor.shutdown(wait=True, cancel_futures=True)
                        check('native batch host shutdown retains no failed owner',
                              not user32.IsWindow(host_handle) and not host_executor.cleanup_failures)
                    else:
                        release_owned()
                gui.Yield()
            finally:
                native_handle.close()
        if errors:
            raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'audio_runtime': audio_proof,
        'native_keyboard': 'PASS', 'native_sapi_render': 'PASS', 'installed_modules': 'PASS',
        'native_failure_cleanup': 'PASS' if failure_control.get('passed') else 'FAIL',
        'entry': 'Existing Book audio workspace, actual Multiple books button and nested reviews',
        'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN', 'listening_quality': 'NOT_RUN',
        'physical_device': 'NOT_RUN', 'private_model_inference': 'NOT_RUN'}, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    def timeout():
        print('FAIL: native BookBatch watchdog expired during work or cleanup.', flush=True)
        os._exit(124)
    watchdog = threading.Timer(360, timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        raise SystemExit(main())
    finally:
        watchdog.cancel()
