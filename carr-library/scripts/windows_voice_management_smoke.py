"""Actual Windows voice-management controls and isolated synthetic owner API.

Uses the real DesktopFrame, shared worker, current engine-settings map, metadata
broker/Windows Jobs and native keyboard. Only local picker results and the inert
fixture interpreter are substituted. No private voice/model/inference is used.
"""
import argparse
import ast
import ctypes
from contextlib import ExitStack
from ctypes import wintypes
from hashlib import sha1, sha256
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep
import traceback
from unittest.mock import patch
import zipfile

_cleanup_complete = threading.Event()
FIXTURE_BLOB = 'ef5be0ea225a4222146edfb05ff03f9037dd0eb5'
MODULES = (
    'carr_library.application', 'carr_library.runtime_settings',
    'carr_library.durable_state', 'carr_library.native_runtime',
    'carr_library.audio.voice_settings', 'carr_library.audio.voice_packages',
    'carr_library.audio.voice_lifecycle', 'carr_library.audio.voice_reference',
    'carr_library.audio.package_publication', 'carr_library.audio.voice_manifest',
    'carr_library.audio.voice_ownership', 'carr_library.audio.voice_transport',
    'carr_library.audio.process', 'carr_library.audio.windows_job',
    'carr_library.operations.broker', 'carr_library.operations._locks',
    'carr_library.ui.desktop_app', 'carr_library.ui.engine_settings',
    'carr_library.ui.voice_packages', 'carr_library.ui.voice_reference',
    'carr_library.ui.worker_lifetime', 'carr_library.ui.recording_dialog',
)


def fixture_source(path):
    raw = path.read_bytes()
    if len(raw) > 65536 or sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != FIXTURE_BLOB:
        raise ValueError('Use the exact reviewed synthetic owner protocol companion.')
    tree = ast.parse(raw.decode('utf-8'), filename=str(path))
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {'POLICY', 'PACKAGES', 'ASSETS'}:
                    if target.id in constants:
                        raise ValueError('Duplicate synthetic protocol constant.')
                    constants[target.id] = ast.literal_eval(node.value)
    if set(constants) != {'POLICY', 'PACKAGES', 'ASSETS'} or any(type(v) is not str for v in constants.values()):
        raise ValueError('The synthetic owner protocol constants differ.')
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'package']
    if len(functions) != 1 or functions[0].decorator_list:
        raise ValueError('The reviewed fixture package builder is missing or ambiguous.')
    namespace = dict(sha256=sha256, json=json, zipfile=zipfile)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return constants, namespace['package'], sha256(raw).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', required=True, type=Path)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        raise RuntimeError('Actual Windows is required for native voice-management acceptance.')
    source_root = args.source_root.resolve(strict=True)
    fixture_path = source_root.parent / 'tests/audio/voice_management_fixtures.py'
    constants, make_package, fixture_digest = fixture_source(fixture_path)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library import runtime_settings
    from carr_library.audio import voice_transport, windows_job
    from carr_library.operations import ResourceBroker, ResourceLimits
    from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
    from carr_library.ui import voice_packages as view
    from carr_library.ui import desktop_app, worker_lifetime

    checks, errors, origins, installed_paths, pressed = [], [], {}, {}, set()
    modal_loops, failure_control, metadata_calls, native_children = [], {}, [], []
    frame = host_handle = host_executor = host_cancel = None
    unwind_pending = False
    ui_thread = threading.get_ident()
    deadline = monotonic() + 240
    timers, controllers = [], []
    queue_release = threading.Event()
    queue_started = threading.Event()
    queued_future = None
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
            raise AssertionError('The selected voice management modal has no running owned event loop.')
        handle = window.GetHandle()
        if not any(old is window and native == handle for old, native, _ in modal_loops):
            modal_loops.append((window, handle, loop))

    def owned_question():
        handle = user32.FindWindowW(None, 'Register local voice installation')
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
            raise AssertionError('Refusing a voice registration question outside this probe process and owner chain.')
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
                        raise AssertionError('The owned voice registration No control disappeared.')
                    user32.SetActiveWindow(question)
                    if not user32.PostMessageW(no_button, 0x00F5, 0, 0):  # BM_CLICK
                        raise OSError(ctypes.get_last_error(), 'Could not queue the owned voice registration No answer.')
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
                print('FAIL: native voice management cleanup\n' + detail, file=sys.stderr, flush=True)
        wx.CallAfter(close_active)

    def remember_failure():
        exception = sys.exc_info()[1]
        window, handle = failure_control.get('window'), failure_control.get('handle')
        if (failure_control.get('injecting') and type(exception) is RuntimeError
                and str(exception) == 'controlled native voice management modal failure'
                and window and user32.IsWindow(handle) and window.IsModal()):
            failure_control['handled'] = True
        else:
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native voice management callback\n' + detail, file=sys.stderr, flush=True)
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
        outer = wx.Dialog(frame, title='Voice management owned outer cleanup fixture')
        inner = wx.Dialog(outer, title='Voice management owned inner cleanup fixture')
        failure_control.update(window=inner, handle=inner.GetHandle(),
                               injecting=False, handled=False)
        answered = []
        until = monotonic() + 5
        def inject():
            try:
                if not owned_question():
                    if monotonic() >= until:
                        raise TimeoutError('The controlled voice management native question did not appear.')
                    wx.CallLater(25, inject)
                    return
                failure_control['injecting'] = True
                try:
                    raise RuntimeError('controlled native voice management modal failure')
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
                answered.append(wx.MessageBox('Controlled cleanup only; no voice management is authorized.',
                    'Register local voice installation', wx.YES_NO | wx.NO_DEFAULT, inner))
                if answered != [wx.NO] or not inner.IsModal() or not outer.IsModal():
                    raise AssertionError('Failure cleanup terminated a suspended voice management parent.')
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
                      and not frame.working and not metadata_calls)
                failure_control['passed'] = True
                outer.EndModal(wx.ID_CANCEL)
            except Exception:
                remember_failure()
        try:
            wx.CallLater(25, outer_opened)
            if outer.ShowModal() != wx.ID_CANCEL:
                raise AssertionError('Controlled voice management outer modal did not return Cancel.')
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
            raise AssertionError('Ambiguous owned voice modal: ' + title)
        return matches[0] if matches else None

    def focus(control):
        top = wx.GetTopLevelParent(control)
        handle = top.GetHandle()
        top.Raise()
        user32.SetForegroundWindow(handle)
        control.SetFocus()
        yield wait_for(lambda: user32.IsWindow(handle)
            and user32.GetForegroundWindow() == handle and wx.Window.FindFocus() is control,
            'Native voice focus did not reach the explicitly selected control.', 5)

    key_state = {'pending': False}

    def key(control, code=wx.WXK_SPACE):
        yield from focus(control)
        key_state['pending'] = True
        if not simulator.KeyDown(code):
            raise AssertionError('Native voice key press failed.')
        pressed.add(code)
        def release():
            try:
                if code in pressed and not simulator.KeyUp(code):
                    raise AssertionError('Native voice key release failed.')
            finally:
                pressed.discard(code)
                key_state['pending'] = False
        later(40, release)
        yield wait_for(lambda: not key_state['pending'], 'Native voice key release did not settle.', 5)

    def text_input(control, value):
        value.encode('ascii')
        yield from focus(control)
        for code in (wx.WXK_CONTROL, ord('A')):
            if not simulator.KeyDown(code):
                raise AssertionError('Native voice select-all press failed.')
            pressed.add(code)
        for code in (ord('A'), wx.WXK_CONTROL):
            if not simulator.KeyUp(code):
                raise AssertionError('Native voice select-all release failed.')
            pressed.discard(code)
        if not simulator.Text(value.encode('ascii')):
            raise AssertionError('Native voice text entry failed.')
        yield wait_for(lambda: control.GetValue() == value,
                       'Native voice text differs from the explicit input.', 5)

    def answer_registration(yes):
        yield wait_for(lambda: owned_question() is not None,
                       'The explicit native registration question did not appear.', 5)
        question = owned_question()
        no_button = native_question_button(question, 'No')
        yield wait_for(lambda: user32.GetForegroundWindow() == question
            and no_button and user32.GetFocus() == no_button,
            'Native voice registration did not settle on default No.', 5)
        check('registration confirmation defaults to the actual owned No button',
              user32.GetWindow(question, 4) == controllers[-1].dialog.GetHandle()
              and user32.GetFocus() == no_button)
        if yes:
            yes_button = native_question_button(question, 'Yes')
            if not yes_button:
                raise AssertionError('The explicit owned registration Yes button is unavailable.')
            user32.SetFocus(yes_button)
            yield wait_for(lambda: user32.GetFocus() == yes_button,
                           'Explicit registration Yes focus did not settle.', 5)
        if not simulator.KeyDown(wx.WXK_SPACE):
            raise AssertionError('Native registration answer press failed.')
        pressed.add(wx.WXK_SPACE)
        def release():
            if not simulator.KeyUp(wx.WXK_SPACE):
                raise AssertionError('Native registration answer release failed.')
            pressed.discard(wx.WXK_SPACE)
        later(40, release)
        yield wait_for(lambda: wx.WXK_SPACE not in pressed and not user32.IsWindow(question),
                       'Native registration answer did not return.', 5)

    def idle(controller):
        return wait_for(lambda: not controller.busy and not frame.working,
                        'The real voice-management worker did not settle.', 40)

    with TemporaryDirectory(prefix='carr-native-voice-management-') as temporary:
        root = Path(temporary).resolve(strict=True)
        runtime = root / 'reviewed-runtime'
        runtime.mkdir()
        assets = {
            'python/python.exe': b'INERT interpreter marker; only the fixture selects sys.executable',
            'nvda_worker.py': b'# Synthetic metadata-only fixture; never performs speech.\n',
            'voice_policy.py': constants['POLICY'].encode(),
            'voice_packages.py': constants['PACKAGES'].encode(),
            'voice_assets.py': constants['ASSETS'].encode(),
            'models/Holos/model.bin': b'SYNTHETIC model, never loaded',
            'models/OmniVoice/model.bin': b'SYNTHETIC model, never loaded',
            'models/OpenVoice/model.bin': b'SYNTHETIC model, never loaded',
            'models/voices/catalog.json': json.dumps({
                'legacy': 'Synthetic legacy', 'blocked-fixture': 'Excluded fixture'}).encode(),
        }
        for name, data in assets.items():
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        manifest = runtime / 'manifest.json'
        manifest.write_bytes(json.dumps({
            'schema_version': 1, 'engine': 'dordiai-voices', 'model_id': 'synthetic-owner-api',
            'revision': 'f' * 40, 'license': 'Synthetic protocol fixture, no voice assets',
            'files': {name: sha256(value).hexdigest() for name, value in assets.items()},
            'settings': {'protocol': 'nvda-worker-jsonl-pcm24k-v1',
                         'profile_languages': {'legacy': {'supported': ['uk'], 'candidates': ['sk']}}},
            'license_accepted': True, 'trusted_weights': True, 'personal_noncommercial': True,
        }, ensure_ascii=False).encode('utf-8'))
        package_path = root / 'fixture-1.dordiai-voice'
        expected_document = make_package(package_path)
        package_digest = sha256(package_path.read_bytes()).hexdigest()
        export_path = root / 'exported-reference.dordiai-voice'
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native synthetic voice metadata')
        library.add_text(workspace, 'Original', 'Untouched retained voice-management source.')
        ProgressPreferenceStore(library.root / 'progress-preferences.json').save(ProgressPreferences(muted=True))
        settings = runtime_settings.load_settings(library.root / 'state')
        settings['dordiai'] = {
            'enabled': True, 'runtime_directory': str(runtime),
            'python_executable': str(runtime / 'python/python.exe'),
            'expected_worker_sha256': sha256(assets['nvda_worker.py']).hexdigest(),
        }
        runtime_settings.save_settings(library.root / 'state', settings)
        preferences = {'favorites': ['legacy'], 'defaults': {'uk': 'legacy'}, 'profiles': {}}
        runtime_settings.save_voice_preferences(library.root / 'state', preferences,
            expected=runtime_settings.load_voice_preferences(library.root / 'state'))
        broker = ResourceBroker.for_app(library.root, limits=ResourceLimits(cpu_slots=2,
            memory_bytes=1024 * 1024**2, disk_bytes=512 * 1024**2, min_free_disk_bytes=0))
        actual_metadata = voice_transport.run_metadata
        actual_job = windows_job.JobProcess
        actual_init = view.VoicePackageDialog.__init__
        picker_calls = []

        def observed_job(*args, **kwargs):
            child = actual_job(*args, **kwargs)
            native_children.append(child)
            return child

        def synthetic_interpreter(command, **kwargs):
            if (not Path(command[0]).samefile(runtime / 'python/python.exe')
                    or command[1:4] != ['-I', '-B', '-u']):
                raise AssertionError('Only the reviewed inert fixture interpreter can be substituted.')
            if threading.get_ident() == ui_thread:
                raise AssertionError('Owner metadata must run on the borrowed host worker.')
            record = {'started': True, 'returned': False}
            metadata_calls.append(record)
            result = actual_metadata([sys.executable, *command[1:]], **kwargs)
            record['returned'] = True
            return result

        def capture_controller(controller, *args, **kwargs):
            actual_init(controller, *args, **kwargs)
            controllers.append(controller)

        class ExplicitPicker:
            def __init__(self, parent, message, **kwargs):
                if not controllers or parent is not controllers[-1].dialog:
                    raise AssertionError('Unexpected owner for the explicit voice fixture picker.')
                choices = {
                    'Choose the reviewed local voice installation manifest': manifest,
                    'Inspect a local owner voice data package': package_path,
                    'Export a private unencrypted reference data package': export_path,
                }
                if message not in choices:
                    raise AssertionError('Unexpected implicit native voice picker: ' + message)
                self.path, self.message = choices[message], message
            def ShowModal(self):
                picker_calls.append(self.message)
                return wx.ID_OK
            def GetPath(self):
                return str(self.path)
            def Destroy(self):
                pass

        def workflow():
            nonlocal queued_future
            yield wait_for(lambda: modal('Local audio, voice and music engines', frame) is not None,
                           'The existing registered engine settings command did not open.')
            engine = modal('Local audio, voice and music engines', frame)
            own_modal(engine)
            buttons = [child for child in descendants(engine) if isinstance(child, wx.Button)
                       and child.GetLabel() == 'Manage reviewed voice packages and references...']
            check('existing engine settings exposes one explicit package-management action', len(buttons) == 1)
            yield from key(buttons[0])
            yield wait_for(lambda: bool(controllers) and controllers[-1].dialog.IsModal(),
                           'The actual voice-package child did not open.')
            c = controllers[-1]
            own_modal(c.dialog)
            yield idle(c)
            check('package child borrows exact engine parent and existing host executor',
                  c.dialog.GetParent() is engine and c.frame is frame and c.library is library
                  and isinstance(host_executor, worker_lifetime.DesktopExecutor))
            check('unregistered package view has no implicit permission or metadata child',
                  not c.registered and c.revision == 0 and not c.rights.GetValue()
                  and not c.export_rights.GetValue() and not c.protected_export_rights.GetValue()
                  and not metadata_calls and not native_children)
            yield from key(c.register_button)
            yield from answer_registration(False)
            yield idle(c)
            check('default No leaves voice registration and owner files unchanged',
                  c.manager.store.read()['revision'] == 0 and not metadata_calls
                  and not (runtime / 'models/voices/packages').exists())
            yield from key(c.register_button)
            yield from answer_registration(True)
            yield wait_for(lambda: c.registered and not c.busy and not frame.working,
                           'Explicit registration and its current configuration did not settle.')
            check('explicit manifest registration binds the real current settings map',
                  c.manager.store.read()['config']['dordiai_manifest'] == str(manifest)
                  and c.revision == 1 and c.registered)
            check('Holos does not expose unreviewed reference creation',
                  c.backend.GetStringSelection() == 'holos'
                  and not c.create_reference_button.IsEnabled())
            yield from key(c.backend, wx.WXK_END)
            yield from text_input(c.reference_model, 'models/OmniVoice')
            yield from key(c.configure_button)
            yield idle(c)
            check('saved explicit OmniVoice configuration enables only its reference creator',
                  c.backend.GetStringSelection() == 'omnivoice'
                  and c._reference_configuration == ('omnivoice', 'models', 'models/OmniVoice', 'models/voices')
                  and c.create_reference_button.IsEnabled())
            yield from key(c.create_reference_button)
            yield wait_for(lambda: modal('Review local voice reference', c.dialog) is not None,
                           'The actual reference child did not open.')
            reference_window = modal('Review local voice reference', c.dialog)
            own_modal(reference_window)
            reference = reference_window.voice_reference
            before_reference = len(metadata_calls)
            check('reference child starts without recording permission or quality approval',
                  not reference.consent.GetValue() and not reference.quality.GetValue()
                  and not reference.create_button.IsEnabled()
                  and reference.fields['source'].GetValue() == ''
                  and reference.record_button is not None and not reference.busy)
            yield from key(reference.close_button)
            yield wait_for(lambda: not user32.IsWindow(reference_window.GetHandle())
                           if reference_window else True,
                           'Reference child close did not finish.')
            check('reference close opens no microphone and dispatches no owner operation',
                  len(metadata_calls) == before_reference and reference.closed and not reference.busy)
            yield from key(c.inspect_button)
            yield wait_for(lambda: c.review is not None and not c.busy and not frame.working,
                           'The actual isolated owner package inspection did not settle.')
            check('inspection returns exact source roles and clears permission and languages',
                  c.review.document == expected_document and not c.rights.GetValue()
                  and c.supported.GetValue() == '' and c.review.operation == 'install')
            before_refusal = len(metadata_calls)
            yield from key(c.apply_button)
            check('Apply without explicit rights cannot call the owner API',
                  len(metadata_calls) == before_refusal and c.review is not None
                  and not (runtime / 'models/voices/packages/fixture').exists())
            yield from text_input(c.supported, 'uk')
            yield from key(c.rights)
            check('native keyboard explicitly grants rights and the selected production language',
                  c.rights.GetValue() and c.supported.GetValue() == 'uk')
            yield from key(c.apply_button)
            yield idle(c)
            installed_profile = runtime / 'models/voices/packages/fixture/1.0.0'
            current_manifest = json.loads(manifest.read_bytes())
            check('real owner installation publishes only the reviewed data and language choice',
                  installed_profile.is_dir()
                  and json.loads((installed_profile / 'voice-package.json').read_bytes()) == expected_document
                  and current_manifest['settings']['profile_languages']['fixture']['supported'] == ['uk']
                  and current_manifest['settings']['profile_languages']['fixture']['candidates'] == ['sk']
                  and c.review is None and not c.rights.GetValue())
            yield from text_input(c.profile, 'fixture')
            yield from text_input(c.version, '1.0.0')
            yield from key(c.versions_button)
            yield idle(c)
            check('real installed version listing is visible in the readable status',
                  'fixture; 1.0.0;' in c.status.GetValue())
            yield from key(c.review_button)
            yield wait_for(lambda: c.review is not None and not c.busy and not frame.working,
                           'Fresh installed version review did not settle.')
            check('installed version review resets export and production permission',
                  c.review.operation == 'activate' and not c.export_rights.GetValue()
                  and not c.protected_export_rights.GetValue() and not c.rights.GetValue())
            before_export = len(metadata_calls)
            yield from key(c.export_button)
            check('unencrypted export requires separate explicit permission',
                  not export_path.exists() and len(metadata_calls) == before_export)
            yield from key(c.export_rights)
            yield from key(c.export_button)
            yield idle(c)
            with zipfile.ZipFile(export_path) as archive:
                exported = json.loads(archive.read('voice-package.json'))
                expected_names = {'voice-package.json', *expected_document['files']}
                check('actual owner export contains only the reviewed reference version',
                      exported == expected_document and set(archive.namelist()) == expected_names
                      and all(sha256(archive.read(name)).hexdigest() == row['sha256']
                              for name, row in expected_document['files'].items()))
            check('export result exposes its actual independently checked SHA256',
                  sha256(export_path.read_bytes()).hexdigest() in c.status.GetValue())
            yield from key(c.removal_review_button)
            yield wait_for(lambda: c.removal_review is not None and not c.busy and not frame.working,
                           'Explicit deactivation review did not settle.')
            deactivate_confirmation = c.removal_review.confirmation
            before_remove = len(metadata_calls)
            yield from key(c.removal_apply_button)
            check('empty removal confirmation cannot mutate the reviewed profile',
                  installed_profile.exists() and len(metadata_calls) == before_remove
                  and c.removal_confirmation.GetValue() == '')
            yield from text_input(c.removal_confirmation, deactivate_confirmation)
            yield from key(c.removal_apply_button)
            yield idle(c)
            index_path = runtime / 'models/voices/packages/voice-packages.json'
            index = json.loads(index_path.read_bytes())
            check('real deactivation retains files and revokes active production authority',
                  installed_profile.is_dir() and 'fixture' not in index['active']
                  and index.get('inactive', {}).get('fixture') == '1.0.0'
                  and 'fixture' not in json.loads(manifest.read_bytes())['settings'].get('profile_languages', {}))
            yield from key(c.removal_mode, wx.WXK_END)
            yield from key(c.removal_review_button)
            yield wait_for(lambda: c.removal_review is not None and not c.busy and not frame.working,
                           'Explicit full profile deletion review did not settle.')
            deletion_confirmation = c.removal_review.confirmation
            check('deletion requires a fresh distinct empty confirmation',
                  deletion_confirmation != deactivate_confirmation and c.removal_confirmation.GetValue() == '')
            yield from text_input(c.removal_confirmation, deletion_confirmation)
            yield from key(c.removal_apply_button)
            yield idle(c)
            index = json.loads(index_path.read_bytes())
            check('real deletion removes only the explicitly reviewed profile versions',
                  not (runtime / 'models/voices/packages/fixture').exists()
                  and 'fixture' not in index['active'] and 'fixture' not in index.get('inactive', {}))
            check('source package shared models and unrelated voice preferences remain exact',
                  sha256(package_path.read_bytes()).hexdigest() == package_digest
                  and all((runtime / name).read_bytes() == data for name, data in assets.items())
                  and runtime_settings.load_voice_preferences(library.root / 'state') == preferences)
            before_queued = len(metadata_calls)
            saved_revision = c.manager.store.read()['revision']
            def block():
                queue_started.set()
                if not queue_release.wait(20):
                    raise TimeoutError('The explicit shared-worker queue fixture was not released.')
            blocker = host_executor.submit(block)
            yield wait_for(queue_started.is_set, 'The real host-worker queue barrier did not start.', 5)
            yield from key(c.reload_button)
            queued_future = c._future
            check('queued management borrows the retained host executor',
                  queued_future is not None and not queued_future.running() and frame.working)
            yield from key(c.close_button)
            yield wait_for(lambda: c.closed and queued_future.cancelled(),
                           'Closing the package window did not cancel its queued Future.', 5)
            queue_release.set()
            yield wait_for(lambda: blocker.done() and not frame.working,
                           'Queued close did not settle the retained host work.', 10)
            blocker.result()
            check('closed queued child cannot call the owner API or deliver a late review',
                  len(metadata_calls) == before_queued and c.review is None and c._future is None)
            check('parent engine view reload preserves the current management binding',
                  runtime_settings.load_settings(library.root / 'state')['dordiai']['management']['revision']
                  == saved_revision and engine.IsModal())
            close_buttons = [child for child in descendants(engine)
                             if isinstance(child, wx.Button) and child.GetId() == wx.ID_CLOSE]
            check('existing engine parent retains one native Close control', len(close_buttons) == 1)
            yield from key(close_buttons[0])
            yield wait_for(lambda: not user32.IsWindow(engine_handle),
                           'Engine settings did not close after the package child.', 5)
            check('actual isolated owner metadata operations completed with no live Job handles',
                  len(metadata_calls) > 5 and len(native_children) == len(metadata_calls)
                  and all(row['returned'] for row in metadata_calls)
                  and all(child.poll() == 0 and child._job is None
                          and not child._process.hProcess and not child._process.hThread
                          for child in native_children))
            check('current broker has no active voice-management lease after native work',
                  not broker.snapshot()['leases'])
            outcomes.update(native_keyboard='PASS', native_metadata_lifecycle='PASS',
                            native_queued_close='PASS')

        workflow_state = {'pending': None, 'until': None, 'done': False, 'iterator': None}
        engine_handle = None

        def drive():
            nonlocal engine_handle
            if workflow_state['done']:
                return
            if errors:
                unwind()
                return
            later(25, drive)
            if monotonic() >= deadline:
                raise TimeoutError('Native voice-management workflow exceeded 240 seconds.')
            if controllers:
                parent = controllers[-1].dialog.GetParent() if not controllers[-1].closed else None
                if parent and engine_handle is None:
                    engine_handle = parent.GetHandle()
            pending = workflow_state['pending']
            if pending is not None:
                predicate, message, _ = pending
                if not predicate():
                    if monotonic() >= workflow_state['until']:
                        detail = {'message': message, 'metadata_calls': len(metadata_calls),
                                  'children': len(native_children), 'frame_working': frame.working}
                        if controllers and not controllers[-1].closed:
                            detail.update(status=controllers[-1].status.GetValue()[:1024],
                                          busy=controllers[-1].busy)
                        print('DIAG: ' + json.dumps(detail, ensure_ascii=True), flush=True)
                        raise TimeoutError(message)
                    return
            try:
                pending = next(workflow_state['iterator'])
            except StopIteration:
                workflow_state['done'] = True
                workflow_state['pending'] = None
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
            check('real native Windows voice controls', wx.Platform == '__WXMSW__')
            frame = desktop_app.DesktopFrame(library)
            host_handle, host_executor, host_cancel = frame.GetHandle(), frame.executor, frame.cancel_event
            frame.Show()
            gui.SetTopWindow(frame)
            check('registered engine action belongs to the existing host command registry',
                  'audio.engines' in {spec.command_id for spec in frame.keyboard_commands.registry.commands('main')})
            items = [item for index in range(frame.menu_bar.GetMenuCount())
                     for item in frame.menu_bar.GetMenu(index).GetMenuItems()]
            engine_items = [item for item in items
                            if item.GetItemLabel().replace('&', '') == 'Local voice and music engines...']
            check('actual host menu contains one existing local-engine command', len(engine_items) == 1)

            def run_steps():
                try:
                    failure_cleanup_control()
                    workflow_state['iterator'] = workflow()
                    later(25, drive)
                    event = wx.CommandEvent(wx.EVT_MENU.typeId, engine_items[0].GetId())
                    event.SetEventObject(frame)
                    if not frame.GetEventHandler().ProcessEvent(event):
                        raise AssertionError('The existing engine menu dispatch was not handled.')
                    pump(lambda: workflow_state['done'],
                         'The native voice-management sequence did not complete.', timeout=245)
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                except BaseException:
                    remember_failure()
                finally:
                    active = wx.EventLoopBase.GetActive()
                    if active is not None and active.IsMain():
                        active.ScheduleExit(1 if errors else 0)
                    elif errors:
                        unwind()

            patches.enter_context(patch.object(view.VoicePackageDialog, '__init__', capture_controller))
            patches.enter_context(patch.object(wx, 'FileDialog', ExplicitPicker))
            patches.enter_context(patch.object(voice_transport, 'run_metadata', synthetic_interpreter))
            patches.enter_context(patch.object(windows_job, 'JobProcess', observed_job))
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
                    detail = traceback.format_exc()[:8192]
                    errors.append(detail)
                    print('FAIL: native voice cleanup\n' + detail, file=sys.stderr, flush=True)
            if host_executor is not None:
                host_cancel.set()
                try:
                    if user32.IsWindow(host_handle):
                        frame.Close(force=True)
                    pump(lambda: not user32.IsWindow(host_handle),
                         'Actual voice host Close did not release native owners.', cleanup=True)
                except BaseException:
                    errors.append(traceback.format_exc()[:8192])
                finally:
                    host_executor.shutdown(wait=True, cancel_futures=True)
                cleanup_check('native host Close drains the original shared executor and finalizers',
                    lambda: not user32.IsWindow(host_handle) and not host_executor.cleanup_failures)
            cleanup_check('all actual metadata Job process and thread handles are settled before fallback',
                lambda: all(child.poll() is not None and child._job is None
                    and not child._process.hProcess and not child._process.hThread for child in native_children))
            cleanup_check('no active broker lease survives native host cleanup', lambda: not broker.snapshot()['leases'])
            # These exact owners are retained for a failure-only final cleanup.
            for child in native_children:
                if child._job or child._process.hProcess or child._process.hThread:
                    try:
                        child.terminate()
                        child.wait(timeout=5)
                        child.close()
                    except BaseException:
                        errors.append(traceback.format_exc()[:8192])
            # Keep every fixture route/observer until its actual workers and Jobs settle.
            try:
                patches.close()
            except BaseException:
                errors.append(traceback.format_exc()[:8192])
            cleanup_check('synthetic owner companion remains byte-identical after cleanup',
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
    check('native voice failure cleanup control completed', bool(failure_control.get('passed')))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'origins': origins, **outcomes,
        'native_failure_cleanup': 'PASS', 'synthetic_owner_protocol': True,
        'fixture_sha256': fixture_digest, 'real_owner_inference': 'NOT_RUN',
        'voice_similarity': 'NOT_RUN', 'physical_recording': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
        'picker': 'explicit fixture file selections; system picker not automated',
        'interpreter': 'only the inert synthetic owner marker is replaced with this installed Python'},
        ensure_ascii=True, indent=2))
    _cleanup_complete.set()
    return 0


if __name__ == '__main__':
    import faulthandler
    faulthandler.enable(all_threads=True)
    def watchdog():
        if not _cleanup_complete.wait(300):
            os.write(2, b'FAIL: native voice management or owned cleanup exceeded 300 seconds.\n')
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True, name='voice-management-native-watchdog').start()
    raise SystemExit(main())
