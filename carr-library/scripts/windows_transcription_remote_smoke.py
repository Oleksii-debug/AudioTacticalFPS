"""Actual Windows explicit remote-STT controls with a synthetic isolated provider.

The real DesktopFrame, selected-recording dialog, current Gateway, retained
job/usage receipts, owned Windows Job and serializers execute. Only the system
file-picker answer and test HTTPS/vault implementation are synthetic. No real
recording, paid endpoint, acoustic inference or manual screen-reader claim.
"""
import argparse
import ctypes
from contextlib import ExitStack
from ctypes import wintypes
from hashlib import sha1, sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep
import traceback
from unittest.mock import patch

_cleanup_complete = threading.Event()
FIXTURE_BLOB = '49b5a4b63c16f8ebada412864f7ce66c4474f0b7'
MODULES = (
    'carr_library.application', 'carr_library.transcription',
    'carr_library.assistant.models', 'carr_library.assistant.profiles',
    'carr_library.assistant.providers', 'carr_library.assistant.credentials',
    'carr_library.assistant.transcription_provider',
    'carr_library.assistant.transcription_worker',
    'carr_library.integrations.transcription_remote',
    'carr_library.integrations.transcription_remote_values',
    'carr_library.integrations.transcription_values',
    'carr_library.backup_transcription', 'carr_library.backup_producers',
    'carr_library.ui.transcription_dialog',
    'carr_library.ui.transcription_remote_dialog', 'carr_library.ui.desktop_app',
    'carr_library.ui.worker_lifetime', 'carr_library.operations.broker',
    'carr_library.audio.process', 'carr_library.audio.windows_job',
    'carr_library.audio.voice_transport', 'carr_library.native_runtime',
    'carr_library.durable_state',
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, type=Path)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        raise RuntimeError('Actual Windows is required for the native remote-STT control proof.')
    source_root = args.source_root.resolve(strict=True)
    fixture_path = source_root.parent / 'tests/transcription/remote_fixture.py'
    raw = fixture_path.read_bytes()
    if len(raw) > 32768 or sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != FIXTURE_BLOB:
        raise ValueError('The synthetic remote-STT companion differs from its exact reviewed source.')
    fixture_digest = sha256(raw).hexdigest()
    fixture_spec = importlib.util.spec_from_file_location('native_remote_stt_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.audio import windows_job
    from carr_library.ui import desktop_app, worker_lifetime
    from carr_library.ui import transcription_remote_dialog as view
    from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
    from carr_library.durable_state import read_json

    checks, errors, origins, installed_paths, pressed = [], [], {}, {}, set()
    modal_loops, failure_control, native_children, controllers = [], {}, [], []
    process_calls, host_calls, timers = [], [], []
    frame = host_handle = host_executor = host_cancel = None
    unwind_pending = False
    ui_thread = threading.get_ident()
    deadline = monotonic() + 240
    queue_release, queue_started = threading.Event(), threading.Event()
    hold_review = False
    outcomes = {}

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
    user32.GetFocus.argtypes, user32.GetFocus.restype = [], wintypes.HWND
    user32.SetFocus.argtypes, user32.SetFocus.restype = [wintypes.HWND], wintypes.HWND
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
            raise AssertionError('The selected remote transcription modal has no running owned event loop.')
        handle = window.GetHandle()
        if not any(old is window and native == handle for old, native, _ in modal_loops):
            modal_loops.append((window, handle, loop))

    def owned_question():
        handle = user32.FindWindowW(None, 'Send selected recording')
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
            raise AssertionError('Refusing a remote transcription question outside this probe process and owner chain.')
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
                        raise AssertionError('The owned remote transcription No control disappeared.')
                    user32.SetActiveWindow(question)
                    if not user32.PostMessageW(no_button, 0x00F5, 0, 0):  # BM_CLICK
                        raise OSError(ctypes.get_last_error(), 'Could not queue the owned remote transcription No answer.')
                    if errors:
                        wx.CallLater(25, unwind)
                    return
                for window, handle, loop in reversed(modal_loops):
                    if window and user32.IsWindow(handle) and window.IsModal() and loop.IsRunning():
                        window.EndModal(wx.ID_CANCEL)
                        if errors:
                            wx.CallLater(25, unwind)
                        return
                active = wx.EventLoopBase.GetActive()
                if errors and active is not None and active.IsMain():
                    active.ScheduleExit(1)
            except Exception:
                detail = traceback.format_exc()
                errors.append(detail)
                print('FAIL: native remote transcription cleanup\n' + detail, file=sys.stderr, flush=True)
        wx.CallAfter(close_active)


    def remember_failure():
        exception = sys.exc_info()[1]
        window, handle = failure_control.get('window'), failure_control.get('handle')
        if (failure_control.get('injecting') and type(exception) is RuntimeError
                and str(exception) == 'controlled remote-STT modal failure'
                and window and user32.IsWindow(handle) and window.IsModal()):
            failure_control['handled'] = True
        else:
            detail = traceback.format_exc()[:8192]
            errors.append(detail)
            print('FAIL: native remote-STT callback\n' + detail, file=sys.stderr, flush=True)
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

    def named(parent, name):
        controls = [child for child in descendants(parent) if child.GetName() == name]
        if len(controls) != 1:
            raise AssertionError('Expected one actual named remote-STT control: ' + name)
        return controls[0]

    def failure_cleanup_control():
        outer = wx.Dialog(frame, title='Remote-STT owned outer cleanup fixture')
        inner = wx.Dialog(outer, title='Remote-STT owned inner cleanup fixture')
        failure_control.update(window=inner, handle=inner.GetHandle(), injecting=False, handled=False)
        answered = []
        until = monotonic() + 5
        def inject():
            try:
                if not owned_question():
                    if monotonic() >= until:
                        raise TimeoutError('The controlled remote-STT question did not appear.')
                    wx.CallLater(25, inject)
                    return
                failure_control['injecting'] = True
                try:
                    raise RuntimeError('controlled remote-STT modal failure')
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
                answered.append(wx.MessageBox('Controlled failure; no recording may be transmitted.',
                    'Send selected recording', wx.YES_NO | wx.NO_DEFAULT, inner))
                if answered != [wx.NO] or not inner.IsModal() or not outer.IsModal():
                    raise AssertionError('Failure cleanup closed a suspended remote-STT parent.')
                unwind()
            except Exception:
                remember_failure()
        def outer_opened():
            try:
                own_modal(outer)
                wx.CallLater(25, inner_opened)
                outcome = inner.ShowModal()
                check('owned failure answers native No before closing only the active inner modal',
                    outcome == wx.ID_CANCEL and answered == [wx.NO] and failure_control['handled']
                    and outer.IsModal() and not frame.working and not process_calls)
                failure_control['passed'] = True
                outer.EndModal(wx.ID_CANCEL)
            except Exception:
                remember_failure()
        try:
            wx.CallLater(25, outer_opened)
            if outer.ShowModal() != wx.ID_CANCEL:
                raise AssertionError('The controlled remote-STT outer modal did not return Cancel.')
        finally:
            failure_control['injecting'] = False
            for window in (inner, outer):
                if window and user32.IsWindow(window.GetHandle()):
                    window.Destroy()
            gui.Yield()

    def later(milliseconds, action):
        def invoke():
            try:
                action()
            except BaseException:
                remember_failure()
        timer = wx.CallLater(milliseconds, invoke)
        timers.append(timer)
        return timer

    def wait_for(predicate, message, timeout=40):
        return predicate, message, timeout

    def modal(title, parent):
        matches = [window for window in wx.GetTopLevelWindows()
                   if isinstance(window, wx.Dialog) and window.IsShown()
                   and window.IsModal() and window.GetTitle() == title
                   and window.GetParent() is parent]
        if len(matches) > 1:
            raise AssertionError('Ambiguous owned transcription modal: ' + title)
        return matches[0] if matches else None

    def focus(control):
        top = wx.GetTopLevelParent(control)
        handle = top.GetHandle()
        top.Raise()
        user32.SetForegroundWindow(handle)
        control.SetFocus()
        yield wait_for(lambda: user32.IsWindow(handle)
            and user32.GetForegroundWindow() == handle and wx.Window.FindFocus() is control,
            'Native transcription focus did not reach the explicitly selected control.', 5)

    key_state = {'pending': False}

    def key(control, code=wx.WXK_SPACE):
        yield from focus(control)
        key_state['pending'] = True
        if not simulator.KeyDown(code):
            raise AssertionError('Native transcription key press failed.')
        pressed.add(code)
        def release():
            try:
                if code in pressed and not simulator.KeyUp(code):
                    raise AssertionError('Native transcription key release failed.')
            finally:
                pressed.discard(code)
                key_state['pending'] = False
        later(40, release)
        yield wait_for(lambda: not key_state['pending'], 'Native transcription key release did not settle.', 5)

    def text_input(control, value):
        value.encode('ascii')
        yield from focus(control)
        for code in (wx.WXK_CONTROL, ord('A')):
            if not simulator.KeyDown(code):
                raise AssertionError('Native transcription select-all press failed.')
            pressed.add(code)
        for code in (ord('A'), wx.WXK_CONTROL):
            if not simulator.KeyUp(code):
                raise AssertionError('Native transcription select-all release failed.')
            pressed.discard(code)
        if not simulator.Text(value.encode('ascii')):
            raise AssertionError('Native transcription text entry failed.')
        yield wait_for(lambda: control.GetValue() == value,
                       'Native transcription text differs from the explicit input.', 5)

    def answer_transfer(yes):
        yield wait_for(lambda: owned_question() is not None,
                       'The explicit native transfer question did not appear.', 5)
        question = owned_question()
        no_button = native_question_button(question, 'No')
        yield wait_for(lambda: user32.GetForegroundWindow() == question
            and no_button and user32.GetFocus() == no_button,
            'Native remote transcription did not settle on default No.', 5)
        check('transfer confirmation defaults to the actual owned No button',
              user32.GetWindow(question, 4) == controllers[-1][0].GetHandle()
              and user32.GetFocus() == no_button)
        if yes:
            yes_button = native_question_button(question, 'Yes')
            if not yes_button:
                raise AssertionError('The explicit owned transfer Yes button is unavailable.')
            user32.SetFocus(yes_button)
            yield wait_for(lambda: user32.GetFocus() == yes_button,
                           'Explicit transfer Yes focus did not settle.', 5)
        if not simulator.KeyDown(wx.WXK_SPACE):
            raise AssertionError('Native transfer answer press failed.')
        pressed.add(wx.WXK_SPACE)
        def release():
            if not simulator.KeyUp(wx.WXK_SPACE):
                raise AssertionError('Native transfer answer release failed.')
            pressed.discard(wx.WXK_SPACE)
        later(40, release)
        yield wait_for(lambda: wx.WXK_SPACE not in pressed and not user32.IsWindow(question),
                       'Native transfer answer did not return.', 5)


    def idle(controller):
        return wait_for(lambda: not controller.busy and not frame.working,
                        'The actual remote-STT host worker did not settle.', 45)

    with TemporaryDirectory(prefix='carr-native-remote-stt-') as temporary:
        root = Path(temporary).resolve(strict=True)
        selected = fixture.prepared(root)
        library, workspace, service, broker = (
            selected.app, selected.workspace, selected.service, selected.broker)
        original = selected.selected.read_bytes()
        ProgressPreferenceStore(library.root / 'progress-preferences.json').save(ProgressPreferences(muted=True))
        actual_builder = view.build_remote_transcription_dialog
        actual_job = windows_job.JobProcess
        picker_calls = []

        def capture_builder(*args, **kwargs):
            dialog = actual_builder(*args, **kwargs)
            controllers.append((dialog, dialog.remote_transcription_actions))
            return dialog

        def observed_job(*args, **kwargs):
            child = actual_job(*args, **kwargs)
            native_children.append(child)
            return child

        class Patches:
            def __init__(self, stack):
                self.stack = stack
            def setattr(self, target, name, value):
                self.stack.enter_context(patch.object(target, name, value))

        class ExplicitPicker:
            def __init__(self, parent, message, **kwargs):
                if (not controllers or parent is not controllers[-1][0]
                        or message != 'Choose one recording for explicit remote transcription'):
                    raise AssertionError('Unexpected remote-STT source picker or owner.')
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def ShowModal(self):
                picker_calls.append(True)
                return wx.ID_OK
            def GetPath(self):
                return str(selected.selected)

        def workflow():
            nonlocal hold_review
            yield wait_for(lambda: modal('Транскрипція — локальний Whisper', frame) is not None,
                           'The existing transcription menu did not open its actual parent.')
            parent = modal('Транскрипція — локальний Whisper', frame)
            own_modal(parent)
            check('registered transcription keeps its existing local default controls',
                  named(parent, 'Локальна модель').GetValue() == 'small'
                  and named(parent, 'Часові мітки кожного слова').GetValue()
                  and service.list() == () and not process_calls)
            remote_button = named(parent, 'Віддалене розпізнавання з дозволом')
            yield from key(remote_button)
            yield wait_for(lambda: bool(controllers) and controllers[-1][0].IsModal(),
                           'The explicit remote transcription child did not open.')
            child, c = controllers[-1]
            own_modal(child)
            check('remote child borrows exact local parent and original shared executor',
                  child.GetParent() is parent and c.frame is frame and c.service.app is library
                  and isinstance(host_executor, worker_lifetime.DesktopExecutor))
            check('opening remote transcription grants no transfer consent or implicit work',
                  not c.consent.GetValue() and c.reviewed is None and not c.send_button.IsEnabled()
                  and c.file.GetValue() == '' and not process_calls and not native_children)
            yield from key(named(child, 'Choose recording'))
            yield from text_input(c.cost, '0.50')
            yield from key(c.review_button)
            yield idle(c)
            check('actual local review binds original bytes and normal retention without a child call',
                  c.reviewed is not None and c.reviewed['audio']['sha256'] == sha256(original).hexdigest()
                  and c.reviewed['source_path'] == str(selected.selected)
                  and 'normal service retention' in c.preview.GetValue()
                  and 'not a price quote or a provider-enforced billing cap' in c.preview.GetValue()
                  and not process_calls and not native_children)
            check('review alone leaves actual native consent unchecked and Send disabled',
                  not c.consent.GetValue() and not c.send_button.IsEnabled())
            yield from key(c.consent)
            check('actual native Space grants only the reviewed selected-recording checkbox',
                  c.consent.GetValue() and c.send_button.IsEnabled())
            yield from key(c.send_button)
            yield from answer_transfer(False)
            yield idle(c)
            check('observed default No retains no remote job or usage reservation',
                  service.list() == () and not process_calls
                  and not list((library.root / 'state/providers').rglob('*.usage.json')))
            yield from key(c.send_button)
            yield from answer_transfer(True)
            yield wait_for(lambda: not c.busy and not frame.working,
                           'The explicit synthetic Gateway child did not settle.', 60)
            if c.last_result is None:
                print('DIAG: ' + json.dumps({'status': c.status.GetValue()[:2048],
                    'process_calls': len(process_calls), 'native_children': len(native_children),
                    'frame_working': frame.working}, ensure_ascii=True), flush=True)
                raise AssertionError('The explicitly approved native remote job has no completed receipt.')
            saved = service.verify(c.last_result['id'])
            check('one explicit Yes runs one current Gateway child on the shared host worker',
                  len(process_calls) == 1 and len(native_children) == 1
                  and len([row for row in host_calls if row['title'] == 'Transcribe explicitly approved recording']) == 1
                  and all(row['thread'] != ui_thread for row in host_calls))
            check('actual retained transcript has exact source and inactive public provider snapshot',
                  saved['status'] == 'completed' and saved['version'] == 2
                  and saved['protocol'] == 'elevenlabs-stt-v1'
                  and saved['sources'][0]['sha256'] == sha256(original).hexdigest()
                  and saved['remote']['profile']['credential_ref'] is None
                  and saved['remote']['profile']['enabled'] is False)
            receipt = read_json(service._path(saved['id']) / saved['receipts'][0])
            check('real usage and retained remote receipt expose provider timestamps without local inference',
                  receipt['local'] is False and receipt['actual_cost'] is None
                  and receipt['provenance']['timestamp_source'] == 'provider reported; not independently aligned'
                  and service.text(saved['id'], saved['sources'][0]['filename']).strip() == fixture.TEXT)
            check('completed child settles its actual Windows Job before returning the transcript',
                  native_children[0].poll() is not None and native_children[0]._job is None
                  and not native_children[0]._process.hProcess and not native_children[0]._process.hThread
                  and not Path(process_calls[0][0][-2]).parent.exists())
            check('native completion clears review and permission before another transfer',
                  c.reviewed is None and not c.consent.GetValue() and not c.send_button.IsEnabled())
            yield from key(c.close_button)
            yield wait_for(lambda: not user32.IsWindow(child.GetHandle()) if child else True,
                           'The completed remote child did not close.')
            yield wait_for(lambda: not frame.working
                and named(parent, 'Текст: редагування зберігається окремою версією').IsEnabled(),
                'The existing retained transcript editor did not load.')
            editor = named(parent, 'Текст: редагування зберігається окремою версією')
            check('existing transcription editor displays the verified remote text',
                  editor.GetValue().strip() == fixture.TEXT)
            yield from text_input(editor, 'Native reviewed derivative.')
            yield from key(named(parent, 'Зберегти правки'))
            yield wait_for(lambda: not frame.working and not named(parent, 'Зберегти правки').IsEnabled(),
                           'The existing transcript edit did not settle.')
            check('native edit keeps original audio and recognition text unchanged',
                  service.edits(saved['id'])[0]['text'] == 'Native reviewed derivative.'
                  and service.text(saved['id'], saved['sources'][0]['filename']).strip() == fixture.TEXT
                  and selected.selected.read_bytes() == original)
            yield from key(remote_button)
            yield wait_for(lambda: len(controllers) == 2 and controllers[-1][0].IsModal(),
                           'A second explicit remote child did not open.')
            queued_window, queued = controllers[-1]
            own_modal(queued_window)
            yield from key(named(queued_window, 'Choose recording'))
            yield from text_input(queued.cost, '0.50')
            hold_review = True
            yield from key(queued.review_button)
            yield wait_for(lambda: queue_started.is_set() and frame.working and queued.busy,
                           'The controlled queued review did not enter the original shared worker.')
            before_status = queued.status.GetValue()
            yield from key(queued.close_button)
            check('busy native Close requests cancellation and retains the current modal until settlement',
                  queued.cancel.is_set() and queued.alive and queued.busy and queued_window.IsModal()
                  and frame.working and len(process_calls) == 1)
            queue_release.set()
            yield idle(queued)
            check('cancelled queued review creates no second job or provider attempt',
                  len(service.list()) == 1 and len(process_calls) == 1 and queued.last_result is None
                  and queued.reviewed is None and not broker.snapshot()['leases'])
            yield from key(queued.close_button)
            yield wait_for(lambda: not user32.IsWindow(queued_window.GetHandle()) if queued_window else True,
                           'The cancelled remote child did not close after worker settlement.')
            yield from key(named(parent, 'Закрити'))
            yield wait_for(lambda: not user32.IsWindow(parent.GetHandle()) if parent else True,
                           'The original transcription parent did not close.')
            check('all explicit picker answers remain bound to the selected original',
                  len(picker_calls) == 2 and selected.selected.read_bytes() == original)
            outcomes.update(native_keyboard='PASS', explicit_approval='PASS',
                            retained_remote_receipt='PASS', queued_cancel_close='PASS')

        workflow_state = {'iterator': None, 'pending': None, 'done': False, 'until': 0}
        def drive():
            if errors:
                return
            later(25, drive)
            if monotonic() >= deadline:
                raise TimeoutError('Native remote-STT workflow exceeded 240 seconds.')
            pending = workflow_state['pending']
            if pending is not None and not pending[0]():
                if monotonic() >= workflow_state['until']:
                    raise TimeoutError(pending[1])
                return
            try:
                pending = next(workflow_state['iterator'])
            except StopIteration:
                workflow_state.update(done=True, pending=None)
                return
            workflow_state['pending'] = pending
            workflow_state['until'] = monotonic() + pending[2]

        patches = ExitStack()
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
            check('actual remote transcription controls use native Windows wx', wx.Platform == '__WXMSW__')
            frame = desktop_app.DesktopFrame(library)
            host_handle, host_executor, host_cancel = frame.GetHandle(), frame.executor, frame.cancel_event
            frame.Show()
            gui.SetTopWindow(frame)
            check('existing host registry retains its explicit transcription command',
                  'library.transcription' in {spec.command_id for spec in frame.keyboard_commands.registry.commands('main')})
            items = [item for index in range(frame.menu_bar.GetMenuCount())
                     for item in frame.menu_bar.GetMenu(index).GetMenuItems()]
            entries = [item for item in items if item.GetItemLabel().replace('&', '') ==
                       'Transcription: local audio and video...']
            check('one existing host menu opens the shared transcription workflow', len(entries) == 1)
            actual_run_job = frame.run_job
            def observed_run_job(title, operation, *args, **kwargs):
                def work(*work_args, **work_kwargs):
                    host_calls.append({'title': title, 'thread': threading.get_ident()})
                    if hold_review and title == 'Review selected remote transcription':
                        queue_started.set()
                        if not queue_release.wait(30):
                            raise TimeoutError('Controlled queued review was not released.')
                    return operation(*work_args, **work_kwargs)
                return actual_run_job(title, work, *args, **kwargs)
            patches.enter_context(patch.object(frame, 'run_job', observed_run_job))
            patches.enter_context(patch.object(view, 'build_remote_transcription_dialog', capture_builder))
            patches.enter_context(patch.object(wx, 'FileDialog', ExplicitPicker))
            patches.enter_context(patch.object(windows_job, 'JobProcess', observed_job))
            process_calls = fixture.real_child(Patches(patches))

            def close_host():
                if host_executor is None:
                    return
                host_cancel.set()
                if user32.IsWindow(host_handle):
                    frame.Close(force=True)
                pump(lambda: not user32.IsWindow(host_handle),
                     'Actual remote-STT host Close did not release native owners.', cleanup=True)
                host_executor.shutdown(wait=True, cancel_futures=True)

            def run_steps():
                try:
                    failure_cleanup_control()
                    workflow_state['iterator'] = workflow()
                    later(25, drive)
                    event = wx.CommandEvent(wx.EVT_MENU.typeId, entries[0].GetId())
                    event.SetEventObject(frame)
                    if not frame.GetEventHandler().ProcessEvent(event):
                        raise AssertionError('The actual transcription menu dispatch was not handled.')
                    pump(lambda: workflow_state['done'], 'Native remote transcription did not complete.', 245)
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                except BaseException:
                    remember_failure()
                finally:
                    queue_release.set()
                    # The original main loop is still active for real host Close.
                    try:
                        close_host()
                    except BaseException:
                        errors.append(traceback.format_exc()[:8192])
                    active = wx.EventLoopBase.GetActive()
                    if active is not None and active.IsMain():
                        active.ScheduleExit(1 if errors else 0)
                    elif errors:
                        unwind()
            wx.CallAfter(run_steps)
            gui.MainLoop()
        except BaseException:
            errors.append(traceback.format_exc()[:8192])
        finally:
            queue_release.set()
            for timer in timers:
                timer.Stop()
            for code in tuple(pressed):
                try:
                    simulator.KeyUp(code)
                finally:
                    pressed.discard(code)
            def cleanup_check(name, predicate):
                try:
                    check(name, predicate())
                except BaseException:
                    errors.append(traceback.format_exc()[:8192])
            if host_executor is not None:
                host_cancel.set()
                try:
                    if user32.IsWindow(host_handle):
                        frame.Close(force=True)
                    pump(lambda: not user32.IsWindow(host_handle),
                         'Native remote-STT final Close did not release the host.', cleanup=True)
                except BaseException:
                    errors.append(traceback.format_exc()[:8192])
                finally:
                    host_executor.shutdown(wait=True, cancel_futures=True)
                cleanup_check('actual host Close drains original shared worker and finalizers',
                    lambda: not user32.IsWindow(host_handle) and not host_executor.cleanup_failures)
            cleanup_check('all actual STT Job handles are settled before fallback cleanup',
                lambda: all(child.poll() is not None and child._job is None
                    and not child._process.hProcess and not child._process.hThread for child in native_children))
            cleanup_check('no STT resource lease survives actual host cleanup',
                          lambda: not broker.snapshot()['leases'])
            for child in native_children:
                if child._job or child._process.hProcess or child._process.hThread:
                    try:
                        child.terminate()
                        child.wait(timeout=5)
                        child.close()
                    except BaseException:
                        errors.append(traceback.format_exc()[:8192])
            try:
                patches.close()
            except BaseException:
                errors.append(traceback.format_exc()[:8192])
            cleanup_check('synthetic STT fixture remains byte-identical through owner cleanup',
                          lambda: sha256(fixture_path.read_bytes()).hexdigest() == fixture_digest)
            for name, actual in installed_paths.items():
                cleanup_check('unchanged installed source after cleanup: ' + name,
                    lambda name=name, actual=actual: sha256(actual.read_bytes()).hexdigest() == origins[name]
                        == sha256((source_root / Path(*name.split('.')).with_suffix('.py')).read_bytes()).hexdigest())
            if native_handle is not None:
                native_handle.close()
            outcomes['native_host_cleanup'] = 'PASS' if not errors else 'FAIL'
        if errors:
            raise RuntimeError('\n'.join(errors))
    check('actual remote-STT nested failure control completed', bool(failure_control.get('passed')))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'origins': origins, **outcomes,
        'native_failure_cleanup': 'PASS', 'synthetic_transport': True,
        'fixture_sha256': fixture_digest, 'paid_provider_call': 'NOT_RUN',
        'acoustic_quality': 'NOT_RUN', 'language_coverage': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
        'picker': 'explicit fixture file selection; system picker not automated'},
        ensure_ascii=True, indent=2))
    _cleanup_complete.set()
    return 0


if __name__ == '__main__':
    import faulthandler
    faulthandler.enable(all_threads=True)
    def watchdog():
        if not _cleanup_complete.wait(300):
            os.write(2, b'FAIL: native remote-STT or owned cleanup exceeded 300 seconds.\n')
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True, name='remote-stt-native-watchdog').start()
    raise SystemExit(main())
