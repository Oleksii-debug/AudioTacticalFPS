"""Actual Windows keyboard, worker, store and backup paths for passive language evidence.

All imported observations and reviewer fields are explicitly synthetic. They
exercise recorded-state transitions, not language quality, acoustic inference,
authenticated human identity, physical devices or NVDA. Only fixed file/directory
picker results are supplied; every observation question and review control is real.
"""
import argparse
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


_cleanup_complete = threading.Event()
MODULES = (
    'carr_library.language_qualification',
    'carr_library.language_qualification.catalog',
    'carr_library.language_qualification.evidence',
    'carr_library.language_qualification.backup',
    'carr_library.ui.language_matrix',
    'carr_library.application',
    'carr_library.library_backup',
    'carr_library.state_backup',
    'carr_library.backup_sidecars',
    'carr_library.durable_state',
    'carr_library.ui.desktop_app',
    'carr_library.ui.worker_lifetime',
    'carr_library.ui.command_registry',
    'carr_library.operations.broker',
)
FIXTURE_GIT_BLOB = '72944369f7304650e8742f97372d41415e3e35da'


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required',
                          'language_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--report', required=True, type=Path)
    arguments = parser.parse_args()
    checkout = Path(__file__).resolve(strict=True).parents[1]
    if not arguments.installed:
        sys.path.insert(0, str(checkout / 'src'))
    if os.path.lexists(arguments.report):
        raise ValueError('The native evidence report must be a new file.')
    arguments.report.parent.resolve(strict=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.durable_state import encode
    from carr_library.language_qualification import build_capability_matrix
    from carr_library.language_qualification.evidence import EvidenceStore, REPORT_SCHEMA
    from carr_library.library_backup import create_backup, restore_backup
    from carr_library.state_backup import snapshot_state, restore_state
    from carr_library.ui import language_matrix as ui
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, module_origins, source_digests, source_locations = [], [], {}, {}, {}
    modules = []
    mode = 'installed' if arguments.installed else 'source'

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    for name in MODULES:
        module = importlib.import_module(name)
        location = Path(module.__file__).resolve(strict=True)
        relative = Path(*name.split('.'))
        if module.__spec__.submodule_search_locations is not None:
            relative = relative / '__init__.py'
        else:
            relative = relative.with_suffix('.py')
        expected = (checkout / 'src' / relative).resolve(strict=True)
        digest = sha256(location.read_bytes()).hexdigest()
        module_origins[name] = str(location)
        source_locations[name] = str(expected)
        source_digests[name] = digest
        modules.append(module)
        admitted = (location.is_relative_to(Path(sys.prefix).resolve())
                    and 'site-packages' in location.parts
                    and not location.is_relative_to(checkout)) if arguments.installed else location == expected
        check('exact current module origin: ' + name,
              admitted and digest == sha256(expected.read_bytes()).hexdigest())

    fixture_path = checkout / 'tests' / 'language_qualification' / 'evidence_fixture.py'
    fixture_raw = fixture_path.read_bytes()
    fixture_blob = sha1(b'blob ' + str(len(fixture_raw)).encode('ascii') + b'\0' + fixture_raw).hexdigest()
    check('exact synthetic observation fixture source', fixture_blob == FIXTURE_GIT_BLOB)
    spec = importlib.util.spec_from_file_location('_native_language_evidence_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)

    pressed, modal_loops, captured_windows, destroyed = set(), [], [], set()
    questions, worker_threads, admissions = [], set(), []
    frame = dialog = host_executor = destroy_observer = None
    host_handle = host_id = None
    closing = {'scheduled': False, 'deadline': None, 'host_close': False,
               'complete': False, 'stalled': False}
    original_factory = ui._native_dialog_type
    original_file_picker, original_directory_picker = wx.FileDialog, wx.DirDialog
    initial_thread = threading.get_ident()
    picker = {'file': None, 'directory': None}
    library_roots, saved_snapshots = [], {}

    def remember_failure():
        detail = traceback.format_exc()
        errors.append(detail)
        print('FAIL: native language evidence\n' + detail, file=sys.stderr, flush=True)
        begin_shutdown()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            remember_failure()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    for name, parameters, result in (
        ('SetForegroundWindow', [wintypes.HWND], wintypes.BOOL),
        ('SetActiveWindow', [wintypes.HWND], wintypes.HWND),
        ('SetFocus', [wintypes.HWND], wintypes.HWND),
        ('GetForegroundWindow', [], wintypes.HWND),
        ('GetFocus', [], wintypes.HWND),
        ('IsWindow', [wintypes.HWND], wintypes.BOOL),
        ('IsWindowEnabled', [wintypes.HWND], wintypes.BOOL),
        ('IsWindowVisible', [wintypes.HWND], wintypes.BOOL),
        ('GetWindow', [wintypes.HWND, wintypes.UINT], wintypes.HWND),
        ('GetDlgItem', [wintypes.HWND, ctypes.c_int], wintypes.HWND),
        ('GetDlgCtrlID', [wintypes.HWND], ctypes.c_int),
        ('GetWindowThreadProcessId', [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        ('GetWindowTextW', [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ('SendMessageW', [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.LPARAM),
    ):
        function = getattr(user32, name)
        function.argtypes, function.restype = parameters, result
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes, user32.EnumWindows.restype = [enum_proc, wintypes.LPARAM], wintypes.BOOL

    def pump(predicate, description, timeout=45, *, cleanup=False):
        deadline = monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError(errors[0])
            if monotonic() > deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)

    def guarded(callback):
        def invoke():
            if errors:
                return
            try:
                callback()
            except BaseException:
                remember_failure()
        return invoke

    def native_control_current(control):
        top = wx.GetTopLevelParent(control)
        handle = top.GetHandle()
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner_pid))
        return (bool(user32.IsWindow(handle)) and owner_pid.value == os.getpid()
                and control.IsEnabled() and control.IsShownOnScreen())

    def focus(control):
        # A returned modal can precede the previous key-up callback. Never let
        # Escape/Space leak into the next dialog or bypass disabled controls.
        pump(lambda: not pressed and native_control_current(control),
             'Language evidence control is not current, enabled and free of held keys', timeout=5)
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
                     and user32.GetForegroundWindow() == top.GetHandle(),
             'Language evidence control did not receive foreground keyboard focus', timeout=5)
        check('keyboard focus: ' + control.GetName(), wx.Window.FindFocus() is control)

    def release(key, modifiers, name):
        try:
            if (key, modifiers) in pressed:
                if key == wx.WXK_SPACE:
                    check('Space key up: ' + name, simulator.KeyUp(key, modifiers))
                else:
                    check('native key up: ' + name, simulator.KeyUp(key, modifiers))
        except BaseException:
            remember_failure()
        finally:
            pressed.discard((key, modifiers))

    def press(control, key=wx.WXK_SPACE, modifiers=wx.MOD_NONE):
        focus(control)
        name = control.GetName()
        if key == wx.WXK_SPACE:
            check('Space key down: ' + name, simulator.KeyDown(key, modifiers))
        else:
            check('native key down: ' + name, simulator.KeyDown(key, modifiers))
        pressed.add((key, modifiers))
        # Schedule release before dispatch can enter a nested ShowModal loop.
        wx.CallLater(40, lambda: release(key, modifiers, name))
        gui.Yield()
        pump(lambda: (key, modifiers) not in pressed, 'Native language evidence key was not released', timeout=5)
        gui.Yield()


    def descendants(window):
        pending, result = list(window.GetChildren()), []
        while pending:
            if len(result) >= 128:
                raise AssertionError('Native evidence control inventory exceeds its bound.')
            current = pending.pop(0)
            result.append(current)
            pending.extend(current.GetChildren())
        return result

    def named(window, name, kind):
        rows = [item for item in descendants(window)
                if isinstance(item, kind) and item.GetName() == name]
        if len(rows) != 1:
            raise AssertionError('Expected one actual named evidence control: ' + name)
        return rows[0]

    def choose(control, label):
        focus(control)
        check('native choice keyboard input: ' + control.GetName(), simulator.Select(label))
        pump(lambda: control.GetStringSelection() == label,
             'Native choice did not retain its exact live selection', timeout=5)

    def enter(control, value):
        press(control, ord('A'), wx.MOD_CONTROL)
        check('native text keyboard input: ' + control.GetName(), simulator.Text(value.encode('ascii')))
        pump(lambda: control.GetValue() == value, 'Actual review text differs from keyboard input', timeout=5)

    def remember_window(window, label):
        handle, identifier = window.GetHandle(), window.GetId()
        if any(row['label'] == label for row in captured_windows):
            raise AssertionError('Duplicate captured native evidence window label.')
        captured_windows.append({'window': window, 'handle': handle, 'id': identifier, 'label': label})
        return handle, identifier

    def current_review():
        matches = [window for window in wx.GetTopLevelWindows()
                   if isinstance(window, wx.Dialog) and window.GetParent() is dialog
                   and window.GetTitle() == 'Record a human language assessment'
                   and window.IsModal() and user32.IsWindow(window.GetHandle())]
        if len(matches) > 1:
            raise AssertionError('Ambiguous owned review dialog.')
        if not matches:
            return None
        window = matches[0]
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window.GetHandle(), ctypes.byref(pid))
        loop = wx.EventLoopBase.GetActive()
        if pid.value != os.getpid() or loop is None or not loop.IsRunning() or loop.IsMain():
            raise AssertionError('Review dialog lacks its actual owned native child loop.')
        if not any(item[0] is window for item in modal_loops):
            modal_loops.append((window, window.GetHandle(), loop))
            remember_window(window, 'review-' + str(len(modal_loops)))
        return window

    def owner_reaches(handle, expected):
        owner = user32.GetWindow(handle, 4)
        for _ in range(16):
            if not owner or owner == expected:
                return owner == expected
            owner = user32.GetWindow(owner, 4)
        return False

    def owned_question(handle):
        if not handle or not user32.IsWindow(handle):
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(handle, title, len(title))
        return (pid.value == os.getpid() and title.value == 'Import passive language observation'
                and dialog is not None and owner_reaches(handle, dialog.GetHandle())
                and user32.IsWindowVisible(handle))

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


    def find_question():
        matches = []
        @enum_proc
        def visit(handle, unused):
            if owned_question(handle):
                matches.append(handle)
            return True
        user32.EnumWindows(visit, 0)
        if len(matches) > 1:
            raise AssertionError('Ambiguous owned passive observation question.')
        if not matches:
            return None
        handle = matches[0]
        if handle not in questions:
            questions.append(handle)
        return handle

    def answer_question(handle, accept):
        no, yes = native_question_button(handle, 'No'), native_question_button(handle, 'Yes')
        check('passive import question uses the actual owned native Yes and No controls',
              owned_question(handle) and no and yes
              and user32.IsWindowEnabled(no) and user32.IsWindowEnabled(yes))
        check('passive import confirmation initially focuses native No',
              not pressed and user32.GetFocus() == no)
        target = yes if accept else no
        user32.SetForegroundWindow(handle)
        user32.SetActiveWindow(handle)
        if accept:
            user32.SetFocus(target)
        pump(lambda: not pressed and owned_question(handle)
             and user32.GetFocus() == target and user32.GetForegroundWindow() == handle,
             'Owned import question did not retain its actual keyboard target', timeout=5)
        name = 'owned passive observation ' + ('Yes' if accept else 'No')
        check('native question Space key down: ' + name, simulator.KeyDown(wx.WXK_SPACE))
        pressed.add((wx.WXK_SPACE, wx.MOD_NONE))
        wx.CallLater(40, lambda: release(wx.WXK_SPACE, wx.MOD_NONE, name))
        pump(lambda: (wx.WXK_SPACE, wx.MOD_NONE) not in pressed,
             'Import confirmation key did not release', timeout=5)

    def settle(description):
        pump(lambda: not frame.working and not getattr(frame, 'busy', False)
             and dialog is not None and dialog._alive and not dialog.pending and not pressed,
             description, timeout=45)

    def import_selected(path, *, accept):
        picker['file'] = path
        state = {'answered': False}
        deadline = monotonic() + 45
        def visit():
            if state['answered']:
                return
            if monotonic() >= deadline:
                raise TimeoutError('Passive import did not show its actual owned confirmation.')
            question = find_question()
            if question is None or pressed:
                wx.CallLater(25, guarded(visit))
                return
            state['answered'] = True
            answer_question(question, accept)
        wx.CallLater(25, guarded(visit))
        press(dialog.import_button)
        pump(lambda: state['answered'] and not any(owned_question(item) for item in questions),
             'Passive import confirmation did not finish', timeout=45)
        settle('Saving the explicit passive import')

    reviewer_values = {
        'Reviewer identity': 'native-fixture-reviewer',
        'Reviewer provenance reference': 'Synthetic native workflow actor; no authenticated identity',
        'Reviewer role': 'synthetic independent workflow reviewer',
        'Assessment and limitations': 'Synthetic workflow assessment only; no language, acoustic or quality qualification.',
    }
    review_decisions = {'accept': 'Accept recorded test assessment',
                        'reject': 'Reject assessment', 'revoke': 'Revoke assessment'}

    def review_selected(decision='accept', *, action='confirm'):
        state = {'answered': False}
        deadline = monotonic() + 45
        def visit():
            if state['answered']:
                return
            if monotonic() >= deadline:
                raise TimeoutError('The actual review controls did not appear.')
            window = current_review()
            if window is None or pressed:
                wx.CallLater(25, guarded(visit))
                return
            exact = named(window, 'Confirm the exact displayed observation', wx.CheckBox)
            independent = named(window, 'Recorded independent assessment acknowledgement', wx.CheckBox)
            detail = named(window, 'Exact observation and prior reviews; no current-device test', wx.TextCtrl)
            selected = dialog.evidence_rows[dialog.evidence_list.GetSelection()]
            check('native review binds the exact displayed observation digest',
                  selected.fingerprint in detail.GetValue())
            check('native review acknowledgements are initially unchecked',
                  not exact.GetValue() and not independent.GetValue())
            if action == 'confirm':
                for label, value in reviewer_values.items():
                    enter(named(window, label, wx.TextCtrl), value)
                choose(named(window, 'Recorded decision', wx.Choice), review_decisions[decision])
                press(independent)
                press(exact)
                check('native review reads two explicit live acknowledgements',
                      exact.GetValue() and independent.GetValue())
            button = wx.Window.FindWindowById(wx.ID_CANCEL if action == 'cancel' else wx.ID_OK, window)
            if button is None:
                raise AssertionError('The actual native review confirmation button is absent.')
            state['answered'] = True
            press(button)
        wx.CallLater(25, guarded(visit))
        press(dialog.review_button)
        pump(lambda: state['answered'] and not any(
             window.IsModal() for window, handle, loop in modal_loops if user32.IsWindow(handle)),
             'Native review did not leave its owned modal', timeout=45)
        settle('Persisting the exact recorded assessment')

    def select_observation(value):
        indices = [index for index, row in enumerate(dialog.evidence_rows)
                   if row.fingerprint == value.fingerprint]
        if len(indices) != 1:
            raise AssertionError('Expected one exact saved observation in the live native list.')
        choose(dialog.evidence_list, dialog.evidence_list.GetString(indices[0]))
        check('native observation selection retains the exact digest',
              dialog.evidence_rows[dialog.evidence_list.GetSelection()].fingerprint == value.fingerprint
              and value.fingerprint in dialog.evidence_text.GetValue())

    def select_context(value):
        index = dialog.context_ids.index(value.context.fingerprint)
        choose(dialog.contexts, dialog.contexts.GetString(index))
        check('native context choice binds one exact recorded configuration',
              dialog.controller.recorded_context == value.context.fingerprint)

    def capability(snapshot, value):
        matrix = build_capability_matrix(evidence=snapshot,
                                         recorded_context=value.context.fingerprint)
        row = next(row for row in matrix.entries if row.language.id == value.language_id)
        return row.capability(value.capability).status

    def current_saved():
        result = EvidenceStore(frame.library.root).load()
        check('native view matches the exact persisted evidence and token',
              result == (dialog.controller.evidence, dialog.controller.evidence_token))
        return result

    def wait_host():
        pump(lambda: not frame.working and not getattr(frame, 'busy', False) and not pressed,
             'The current library host did not finish its actual worker', timeout=45)

    def menu(label):
        wait_host()
        rows = [item for index in range(frame.GetMenuBar().GetMenuCount())
                for item in frame.GetMenuBar().GetMenu(index).GetMenuItems()
                if not item.IsSeparator() and item.GetItemLabelText() == label]
        if len(rows) != 1 or not rows[0].IsEnabled():
            raise AssertionError('Expected one enabled real registered menu item: ' + label)
        check('actual registered menu item: ' + label, True)
        frame.ProcessWindowEvent(wx.CommandEvent(wx.EVT_MENU.typeId, rows[0].GetId()))

    def observe_destroy(event):
        if event.GetEventType() == wx.EVT_WINDOW_DESTROY.typeId:
            window = event.GetWindow()
            for row in captured_windows:
                if (event.GetId() == row['id'] and window is not None
                        and window.GetId() == row['id']):
                    destroyed.add((row['label'], row['handle']))
        return wx.EventFilter.Event_Skip

    class EvidenceDestroyObserver(wx.EventFilter):
        def FilterEvent(self, event):
            return observe_destroy(event)

    def begin_shutdown():
        if closing['scheduled'] or closing['complete'] or closing['stalled']:
            return
        closing['scheduled'] = True
        if closing['deadline'] is None:
            closing['deadline'] = monotonic() + 45
        wx.CallLater(25, shutdown_tick)

    def shutdown_tick():
        closing['scheduled'] = False
        try:
            if monotonic() >= closing['deadline']:
                raise TimeoutError('Language evidence windows or the shared worker did not confirm Close.')
            if frame is not None:
                frame.cancel_event.set()
            for key, modifiers in tuple(pressed):
                release(key, modifiers, 'owned evidence cleanup')
            for handle in reversed(questions):
                if owned_question(handle):
                    no = native_question_button(handle, 'No')
                    if not no or not user32.IsWindowEnabled(no):
                        raise AssertionError('Owned import question lacks its native No cleanup control.')
                    user32.SetActiveWindow(handle)
                    user32.SendMessageW(no, 0x00F5, 0, 0)
                    begin_shutdown()
                    return
            for window, handle, loop in reversed(modal_loops):
                if (user32.IsWindow(handle) and window.IsModal() and loop.IsRunning()
                        and wx.EventLoopBase.GetActive() is loop):
                    window.EndModal(wx.ID_CANCEL)
                    begin_shutdown()
                    return
            for row in reversed(captured_windows):
                if row['label'].startswith('evidence-') and user32.IsWindow(row['handle']):
                    window = row['window']
                    if window.IsModal():
                        window.close()
                    begin_shutdown()
                    return
            if frame is not None and frame.working:
                begin_shutdown()
                return
            if frame is not None and user32.IsWindow(host_handle):
                if not closing['host_close']:
                    closing['host_close'] = True
                    frame.Close()
                begin_shutdown()
                return
            check('actual evidence and host Destroy events precede original loop exit',
                  all((row['label'], row['handle']) in destroyed for row in captured_windows))
            check('all captured evidence native windows are closed',
                  all(not user32.IsWindow(row['handle']) for row in captured_windows))
            closing['complete'] = True
            gui.ExitMainLoop()
        except BaseException:
            errors.append(traceback.format_exc())
            print('FAIL: native evidence cleanup\n' + errors[-1], file=sys.stderr, flush=True)
            closing['stalled'] = True
            # Keep the original loop, picker fixtures, temporary files and native
            # runtime alive until the existing finite watchdog settles failure.

    check('real wx Windows controls and original event loop', wx.Platform == '__WXMSW__')
    native_class = original_factory()

    class CapturedEvidenceDialog(native_class):
        def __init__(self, parent, controller):
            nonlocal dialog
            super().__init__(parent, controller)
            dialog = self
            number = sum(row['label'].startswith('evidence-') for row in captured_windows) + 1
            remember_window(self, 'evidence-' + str(number))
            check('native evidence dialog belongs to the exact current host',
                  parent is frame and controller.store.root == Path(frame.library.root).resolve(strict=True)
                  and self.GetHandle() != host_handle and self.GetId() != host_id)

    class FixedFilePicker:
        def __init__(self, parent, message, *args, **kwargs):
            allowed = {
                'Choose one passive qualification observation': wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
                'Create a new passive evidence report outside the library': wx.FD_SAVE,
            }
            if parent is not dialog or message not in allowed or kwargs.get('style') != allowed[message]:
                raise AssertionError('Unexpected native evidence picker route.')
            if not isinstance(picker['file'], Path):
                raise AssertionError('The synthetic file picker has no explicitly selected fixture path.')
            self.path = picker['file']
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(self.path)
        def Destroy(self): pass

    class FixedDirectoryPicker:
        def __init__(self, parent, message, *args, **kwargs):
            if parent is not frame or message != 'Choose an existing research-library data folder':
                raise AssertionError('Unexpected library-folder picker route.')
            if not isinstance(picker['directory'], Path):
                raise AssertionError('The synthetic directory picker has no explicit restored path.')
            self.path = picker['directory']
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(self.path)
        def Destroy(self): pass

    def show_recorded_tab():
        check('native evidence dialog initially shows the canonical catalog section',
              dialog.tabs.GetSelection() == 0)
        press(dialog.search, wx.WXK_TAB, wx.MOD_CONTROL)
        pump(lambda: dialog.tabs.GetSelection() == 1, 'First native Ctrl+Tab did not select inventory', timeout=5)
        press(dialog.inventory, wx.WXK_TAB, wx.MOD_CONTROL)
        pump(lambda: dialog.tabs.GetSelection() == 2, 'Second native Ctrl+Tab did not select recorded evidence', timeout=5)
        check('native Ctrl+Tab reaches the recorded evidence section',
              dialog.tabs.GetPageText(dialog.tabs.GetSelection()) == 'Recorded qualification evidence')

    def with_native_dialog(body):
        before = sum(row['label'].startswith('evidence-') for row in captured_windows)
        ready = {'started': False}
        deadline = monotonic() + 45
        def visit():
            if ready['started']:
                return
            current = sum(row['label'].startswith('evidence-') for row in captured_windows)
            if monotonic() >= deadline:
                raise TimeoutError('The actual registered language dialog did not become current.')
            if current == before or not dialog.IsModal() or pressed:
                wx.CallLater(25, guarded(visit))
                return
            ready['started'] = True
            settle('Loading the exact current evidence through the shared worker')
            check('initial evidence loading uses the exact current store',
                  dialog.controller.evidence_loaded and dialog.controller.inventory is None)
            show_recorded_tab()
            body()
            settle('Finishing the actual language evidence operation')
            close = wx.Window.FindWindowById(wx.ID_CANCEL, dialog)
            if close is None:
                raise AssertionError('The actual language view Close button is absent.')
            press(close)
        wx.CallLater(25, guarded(visit))
        menu('Languages and capability evidence...')
        if errors:
            raise RuntimeError(errors[0])
        check('registered language menu entered one actual modal view',
              ready['started'] and sum(row['label'].startswith('evidence-') for row in captured_windows) == before + 1)
        current = [row for row in captured_windows if row['label'] == 'evidence-' + str(before + 1)][0]
        pump(lambda: not user32.IsWindow(current['handle'])
             and (current['label'], current['handle']) in destroyed,
             'Actual language view did not destroy after keyboard Close', timeout=5)
        wait_host()
        check('native language view Close preserves the current desktop owner',
              user32.IsWindow(host_handle) and not frame._closing)

    def worker(label, operation):
        results, failures = [], []
        wait_host()
        admitted = frame.run_job(label, operation, results.append, failures.append)
        if admitted is None:
            raise AssertionError('Actual host refused the explicitly requested evidence worker.')
        pump(lambda: not frame.working and (results or failures),
             'Actual evidence backup worker did not return', timeout=45)
        if failures:
            raise failures[0]
        if len(results) != 1:
            raise AssertionError('The actual evidence worker returned an ambiguous result.')
        return results[0]

    with TemporaryDirectory(prefix='carr-native-language-evidence-') as temporary:
        root = Path(temporary).resolve(strict=True)
        original_root, restored_root = root / 'library', root / 'restored-library'
        state_root = root / 'state-only-library'
        library_roots.extend((original_root, restored_root, state_root))
        first, second = fixture.observation('tts'), fixture.observation('unicode_text')
        first_path, second_path = root / 'first-observation.json', root / 'second-observation.json'
        first_path.write_bytes(encode(first.to_dict()))
        second_path.write_bytes(encode(second.to_dict()))
        imported_bytes = {first_path: first_path.read_bytes(), second_path: second_path.read_bytes()}
        report_path = root / 'passive-report.json'
        backup_directory, state_backup_directory = root / 'library-backup', root / 'state-backup'
        ui._native_dialog_type = lambda: CapturedEvidenceDialog
        wx.FileDialog, wx.DirDialog = FixedFilePicker, FixedDirectoryPicker
        try:
            destroy_observer = EvidenceDestroyObserver()
            wx.EvtHandler.AddFilter(destroy_observer)
            library = LibraryApp(original_root)
            workspace = library.create_workspace('Synthetic evidence workflow')
            original_text = 'Retained synthetic source. Unicode: \u0422\u0435\u043a\u0441\u0442.'
            library.add_text(workspace, 'Retained original', original_text, edition_id='original')
            frame = DesktopFrame(library)
            host_executor = frame.executor
            host_handle, host_id = remember_window(frame, 'host')
            frame.Show()
            gui.SetTopWindow(frame)
            actual_run_job = frame.run_job

            def observed_run_job(title, operation, *args, **kwargs):
                def observed_operation(*call_args, **call_kwargs):
                    worker_threads.add(threading.get_ident())
                    return operation(*call_args, **call_kwargs)
                result = actual_run_job(title, observed_operation, *args, **kwargs)
                admissions.append({'title': str(title), 'admitted': result is not None,
                                   'same_executor': frame.executor is host_executor})
                return result

            frame.run_job = observed_run_job

            def original_evidence_flow():
                store = dialog.controller.store
                check('fresh evidence load neither writes nor selects a recorded configuration',
                      not store.path.exists() and dialog.controller.evidence.observations == ()
                      and dialog.controller.evidence.reviews == ()
                      and dialog.controller.recorded_context is None
                      and dialog.controller.matrix.summary()['qualified_entries'] == 0)
                import_selected(first_path, accept=False)
                check('native default-No leaves the original evidence state absent',
                      not store.path.exists() and dialog.controller.evidence.observations == ())
                import_selected(first_path, accept=True)
                initial, initial_token = current_saved()
                check('native Yes imports only one passive observation without review authority',
                      initial.observations == (first,) and initial.reviews == ()
                      and dialog.controller.recorded_context is None
                      and capability(initial, first) == 'candidate')
                select_context(first)
                select_observation(first)
                before = store.path.read_bytes()
                review_selected(action='cancel')
                check('native review Cancel preserves the exact saved bytes and token',
                      store.path.read_bytes() == before and current_saved() == (initial, initial_token))
                review_selected(action='unchecked')
                check('native unchecked review refuses before any saved change',
                      store.path.read_bytes() == before and current_saved() == (initial, initial_token)
                      and 'Confirm the exact displayed observation' in dialog.status.GetValue())
                review_selected('accept')
                accepted, token = current_saved()
                review = accepted.reviews[-1]
                check('native explicit assessment persists exact reviewer and observation bindings',
                      len(accepted.reviews) == 1 and review.observation_sha256 == first.fingerprint
                      and review.decision == 'accept' and review.independent_assessment
                      and review.reviewer.identity == reviewer_values['Reviewer identity']
                      and review.reviewer.source == reviewer_values['Reviewer provenance reference']
                      and review.reviewer.role == reviewer_values['Reviewer role']
                      and review.statement == reviewer_values['Assessment and limitations']
                      and review.fingerprint in dialog.evidence_text.GetValue())
                check('recorded acceptance remains scoped and creates no complete language qualification',
                      capability(accepted, first) == 'verified'
                      and dialog.controller.matrix.summary()['qualified_entries'] == 0
                      and dialog.controller.inventory is None
                      and 'not authenticated' in dialog.evidence_text.GetValue())
                import_selected(second_path, accept=True)
                select_observation(second)
                review_selected('accept')
                second_accepted, _ = current_saved()
                check('second native assessment remains bound to the selected observation',
                      second_accepted.reviews[-1].observation_sha256 == second.fingerprint
                      and capability(second_accepted, second) == 'verified')
                select_observation(second)
                review_selected('reject')
                rejected, _ = current_saved()
                check('native Reject preserves history and marks the exact recorded capability failed',
                      rejected.reviews[:-1] == second_accepted.reviews
                      and rejected.reviews[-1].decision == 'reject'
                      and rejected.reviews[-1].observation_sha256 == second.fingerprint
                      and capability(rejected, second) == 'failed'
                      and capability(rejected, first) == 'verified')
                select_observation(second)
                review_selected('revoke')
                before_backup, before_token = current_saved()
                check('native Revoke appends exact history without removing earlier observations',
                      before_backup.observations == (first, second)
                      and before_backup.reviews[:-1] == rejected.reviews
                      and before_backup.reviews[-1].decision == 'revoke'
                      and capability(before_backup, second) == 'revoked'
                      and capability(before_backup, first) == 'verified')
                saved_snapshots['original'] = before_backup, before_token, store.path.read_bytes()
                picker['file'] = report_path
                press(dialog.export_button)
                settle('Creating a passive outside-library evidence report')
                report_raw = report_path.read_bytes()
                report = json.loads(report_raw)
                check('native Export creates an exact passive report outside private library state',
                      report['schema'] == REPORT_SCHEMA
                      and report['evidence'] == before_backup.to_dict()
                      and 'no automatic execution' in report['qualification']
                      and not report_path.is_relative_to(original_root)
                      and 'cannot be imported as review authority' in dialog.status.GetValue())
                press(dialog.export_button)
                settle('Refusing an existing passive report destination')
                check('native report export never overwrites an existing destination',
                      report_path.read_bytes() == report_raw and current_saved() == (before_backup, before_token))
                picker['file'] = report_path
                question_count = len(questions)
                press(dialog.import_button)
                settle('Refusing passive report import as observation authority')
                check('native import refuses a report before confirmation or state change',
                      len(questions) == question_count
                      and current_saved() == (before_backup, before_token)
                      and store.path.read_bytes() == saved_snapshots['original'][2])
                select_observation(first)
                check('original accepted observation remains visible after rejected report import',
                      first.fingerprint in dialog.evidence_text.GetValue()
                      and 'Recorded human decision: accept' in dialog.evidence_text.GetValue())

            def restored_evidence_flow():
                before, _, _ = saved_snapshots['original']
                restored, restored_token = current_saved()
                check('reopened restored view begins without selected or active recorded approval',
                      dialog.controller.recorded_context is None
                      and restored.observations == before.observations and restored.reviews == before.reviews
                      and restored.dormant_through == len(before.reviews)
                      and restored.revision == before.revision + 1)
                select_context(first)
                select_observation(first)
                check('native restored detail exposes dormant review and preserved revocation',
                      capability(restored, first) == 'candidate'
                      and capability(restored, second) == 'revoked'
                      and 'explicit local re-review is required' in dialog.evidence_text.GetValue()
                      and dialog.controller.matrix.summary()['qualified_entries'] == 0)
                review_selected('accept')
                rereviewed, _ = current_saved()
                check('native explicit re-review reactivates only the exact restored recorded capability',
                      rereviewed.reviews[:-1] == before.reviews
                      and rereviewed.reviews[-1].observation_sha256 == first.fingerprint
                      and rereviewed.reviews[-1].decision == 'accept'
                      and capability(rereviewed, first) == 'verified'
                      and capability(rereviewed, second) == 'revoked'
                      and dialog.controller.matrix.summary()['qualified_entries'] == 0)

            def scenario():
                try:
                    wait_host()
                    check('actual host bootstrap settles before native language admission',
                          not frame.working and not frame.busy and gui.GetMainLoop() is not None)
                    with_native_dialog(original_evidence_flow)
                    before, token, original_raw = saved_snapshots['original']

                    def full_backup_restore():
                        create_backup(frame.library, backup_directory)
                        manifest = (backup_directory / 'manifest.json').read_bytes()
                        digest = sha256(manifest).hexdigest()
                        destination = restore_backup(backup_directory, restored_root,
                                                     expected_manifest_sha256=digest)
                        return destination, digest, manifest

                    destination, manifest_sha, manifest_raw = worker('Synthetic language evidence library backup', full_backup_restore)
                    restored, _ = EvidenceStore(destination).load()
                    check('actual full library backup restores the checked manifest and complete passive history',
                          Path(destination).resolve(strict=True) == restored_root.resolve(strict=True)
                          and sha256((backup_directory / 'manifest.json').read_bytes()).hexdigest() == manifest_sha
                          and (backup_directory / 'manifest.json').read_bytes() == manifest_raw
                          and restored.observations == before.observations and restored.reviews == before.reviews
                          and restored.dormant_through == len(before.reviews)
                          and restored.revision == before.revision + 1
                          and LibraryApp(destination).edition_metadata(workspace, 'original').title == 'Retained original')
                    check('full backup restores candidates and sticky revocation without current-device approval',
                          capability(restored, first) == 'candidate'
                          and capability(restored, second) == 'revoked'
                          and build_capability_matrix(evidence=restored,
                              recorded_context=first.context.fingerprint).summary()['qualified_entries'] == 0)

                    def state_backup_restore():
                        state_backup_directory.mkdir()
                        entries = snapshot_state(original_root / 'state', state_backup_directory)
                        state_root.mkdir()
                        restore_state(state_backup_directory, state_root / 'state', entries)
                        return entries

                    state_entries = worker('Synthetic language evidence state backup', state_backup_restore)
                    state_saved, _ = EvidenceStore(state_root).load()
                    check('actual state backup preserves the same history with dormant positive reviews',
                          state_saved == restored and any('language-qualification' in row['filename']
                                                         for row in state_entries)
                          and capability(state_saved, first) == 'candidate'
                          and capability(state_saved, second) == 'revoked')
                    check('backup operations leave original evidence and imported source bytes unchanged',
                          EvidenceStore(original_root).load() == (before, token)
                          and EvidenceStore(original_root).path.read_bytes() == original_raw
                          and all(path.read_bytes() == raw for path, raw in imported_bytes.items()))
                    old_library = frame.library
                    picker['directory'] = restored_root
                    menu('Open existing library folder')
                    wait_host()
                    check('actual library-folder action replaces the host scope with the restored library',
                          frame.library is not old_library
                          and Path(frame.library.root).resolve(strict=True) == restored_root.resolve(strict=True))
                    with_native_dialog(restored_evidence_flow)
                    check('all evidence mutations use the original shared worker away from the GUI thread',
                          admissions and all(row['admitted'] and row['same_executor'] for row in admissions)
                          and len(worker_threads) == 1 and initial_thread not in worker_threads)
                    check('synthetic language evidence workflow creates no audio or runtime payload',
                          not list(root.rglob('*.wav')) and not list(root.rglob('*.pcm')))
                except BaseException:
                    remember_failure()
                finally:
                    begin_shutdown()

            wx.CallAfter(scenario)
            gui.MainLoop()
        except BaseException:
            detail = traceback.format_exc()
            if detail not in errors:
                errors.append(detail)
                print('FAIL: native evidence setup or loop\n' + detail, file=sys.stderr, flush=True)
        finally:
            if frame is not None and not closing['complete']:
                begin_shutdown()
                gui.MainLoop()
            if host_executor is not None:
                try:
                    host_executor.shutdown(wait=True, cancel_futures=True)
                    check('original evidence host executor drains before fixture retirement',
                          closing['complete'] and not host_executor.cleanup_failures)
                except BaseException:
                    errors.append(traceback.format_exc())
            try:
                from carr_library.operations import ResourceBroker
                check('native evidence work retains no library resource lease after host Close',
                      all(not ResourceBroker.for_app(path).snapshot()['leases']
                          for path in library_roots if path.exists()))
            except BaseException:
                errors.append(traceback.format_exc())
            ui._native_dialog_type = original_factory
            wx.FileDialog, wx.DirDialog = original_file_picker, original_directory_picker
            if destroy_observer is not None:
                wx.EvtHandler.RemoveFilter(destroy_observer)
            if native_handle is not None:
                native_handle.close()
        check('original passive observation files survive native worker and owner cleanup',
              all(path.read_bytes() == raw for path, raw in imported_bytes.items()))

    for module in modules:
        actual = Path(module.__file__).resolve(strict=True)
        expected = Path(source_locations[module.__name__])
        check('unchanged current module source after native evidence work: ' + module.__name__,
              str(actual) == module_origins[module.__name__]
              and sha256(actual.read_bytes()).hexdigest() == source_digests[module.__name__]
              and sha256(expected.read_bytes()).hexdigest() == source_digests[module.__name__])
    check('synthetic fixture source remains unchanged after native work', fixture_path.read_bytes() == fixture_raw)
    report = {
        'result': 'FAIL' if errors else 'PASS', 'mode': mode, 'checks': checks,
        'module_origins': module_origins, 'native_keyboard': 'PASS' if not errors else 'FAIL',
        'native_registered_menu_dispatch': 'PASS' if not errors else 'FAIL',
        'native_default_no': 'PASS' if not errors else 'FAIL',
        'native_review_and_history': 'PASS' if not errors else 'FAIL',
        'native_backup_restore': 'PASS' if not errors else 'FAIL',
        'native_owner_cleanup': 'PASS' if closing['complete'] and not errors else 'FAIL',
        'fixture': 'synthetic passive observations and synthetic reviewer assertions',
        'authenticated_reviewer_identity': 'NOT_RUN', 'language_quality': 'NOT_RUN',
        'acoustic_inference': 'NOT_RUN', 'physical_devices': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'native_file_pickers': 'NOT_RUN', 'all_135_languages': 'NOT_RUN',
        'worker_admissions': admissions, 'errors': [item[-4096:] for item in errors[-8:]],
    }
    with arguments.report.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
    _cleanup_complete.set()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if errors else 0


if __name__ == '__main__':
    import faulthandler
    faulthandler.enable(all_threads=True)

    def watchdog():
        if not _cleanup_complete.wait(300):
            os.write(2, b'FAIL: native language evidence or owned cleanup exceeded 300 seconds.\n')
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(86)

    threading.Thread(target=watchdog, daemon=True, name='language-native-watchdog').start()
    raise SystemExit(main())
