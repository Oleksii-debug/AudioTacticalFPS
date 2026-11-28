"""Actual existing-host conversion/verified Read/Import controls on Windows.

Only native file/folder picker selections are supplied by this fixture. Real
Pandoc, the existing conversion service, real reader, native default-No/Yes,
the shared host worker and canonical TXT import all execute. No native source
citation, visual fidelity, audio-device or screen-reader qualification follows.
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
UI_MODULES = (
    'carr_library.ui.document_conversion', 'carr_library.ui.desktop_app',
    'carr_library.ui.worker_lifetime', 'carr_library.ui.worker_cleanup',
    'carr_library.ui.speech_dialog', 'carr_library.speech.reading',
    'carr_library.native_runtime', 'carr_library.speech.session',
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'src')
    parser.add_argument('--mode', choices=('source', 'installed'), required=True)
    parser.add_argument('--pandoc', type=Path, required=True)
    parser.add_argument('--calibre', type=Path, required=True)
    parser.add_argument('--office', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, required=True)
    parser.add_argument('--runtime-lock', type=Path, required=True)
    parser.add_argument('--firewall-receipt', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        raise RuntimeError('Actual Windows is required for native selected-format control acceptance.')
    source_root = args.source_root.resolve(strict=True)
    if args.mode == 'source':
        sys.path.insert(0, str(source_root))
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.dont_write_bytecode = True
    import importlib.util
    helper_path = source_root.parent / 'scripts/selected_conversion_fixture_support.py'
    spec = importlib.util.spec_from_file_location('_native_conversion_fixture', helper_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    helper_hash = sha256(helper_path.read_bytes()).hexdigest()
    _, selected_inputs = fixture.load_lock(args.runtime_lock, args.downloads)
    boundary = fixture.firewall(args.firewall_receipt, source_root,
        fixture.native_programs(args.calibre, args.office, args.pandoc))
    MODULES = fixture.MODULES + UI_MODULES
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.conversion import ConversionService
    from carr_library.conversion.selected import read_result
    from carr_library.conversion import selected as selected_module
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.operations import ResourceBroker
    from carr_library.audio import windows_job
    from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
    from carr_library.ui import document_conversion as view
    from carr_library.ui import desktop_app, worker_lifetime, worker_cleanup, speech_dialog

    checks, errors, origins, installed_paths, pressed = [], [], {}, {}, set()
    modal_loops, failure_control = [], {}
    frame = host_handle = host_executor = host_cancel = None
    unwind_pending = False
    ui_thread = threading.get_ident()
    command_epoch = 0
    conversion_calls, import_calls, reader_finalizers, native_children, worker_calls = [], [], [], [], []
    reader_handoffs = []
    timers = []
    hold_read = [False]
    read_entered, read_release = threading.Event(), threading.Event()
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
    user32.GetFocus.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL

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
            raise AssertionError('The selected conversion modal has no running owned event loop.')
        handle = window.GetHandle()
        if not any(old is window and native == handle for old, native, _ in modal_loops):
            modal_loops.append((window, handle, loop))

    def owned_question():
        handle = user32.FindWindowW('#32770', 'Import verified reading copy')
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
            raise AssertionError('Refusing a conversion question outside this probe process and owner chain.')
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
                    if no_button is None:
                        wx.CallLater(25, unwind)
                        return
                    user32.SetActiveWindow(question)
                    if not user32.PostMessageW(no_button, 0x00F5, 0, 0):  # BM_CLICK admission only
                        raise OSError(ctypes.get_last_error(), 'Could not queue the owned conversion No answer.')
                    if errors:
                        wx.CallLater(25, unwind)
                    return
                if errors and host_handle and user32.IsWindow(host_handle):
                    # A failed reading-copy confirmation can legitimately return No
                    # and then open its read-only result. Capture only this
                    # workflow's exact direct-child dialogs in their live loop.
                    for candidate in wx.GetTopLevelWindows():
                        if (isinstance(candidate, wx.Dialog) and candidate.IsShown()
                                and candidate.IsModal() and candidate.GetParent() is frame
                                and candidate.GetTitle() in ('Voice reader and reading copy',)):
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
                print('FAIL: native conversion cleanup\n' + detail, file=sys.stderr, flush=True)
        wx.CallAfter(close_active)

    def remember_failure():
        exception = sys.exc_info()[1]
        window, handle = failure_control.get('window'), failure_control.get('handle')
        if (failure_control.get('injecting') and type(exception) is RuntimeError
                and str(exception) == 'controlled native conversion modal failure'
                and window and user32.IsWindow(handle) and window.IsModal()):
            failure_control['handled'] = True
        else:
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native conversion callback\n' + detail, file=sys.stderr, flush=True)
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
        outer = wx.Dialog(frame, title='Conversion owned outer cleanup fixture')
        inner = wx.Dialog(outer, title='Conversion owned inner cleanup fixture')
        failure_control.update(window=inner, handle=inner.GetHandle(),
                               injecting=False, handled=False)
        answered = []
        until = monotonic() + 5
        def inject():
            try:
                if not owned_question():
                    if monotonic() >= until:
                        raise TimeoutError('The controlled conversion native question did not appear.')
                    wx.CallLater(25, inject)
                    return
                failure_control['injecting'] = True
                try:
                    raise RuntimeError('controlled native conversion modal failure')
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
                answered.append(wx.MessageBox('Controlled cleanup only; no import is authorized.',
                    'Import verified reading copy', wx.YES_NO | wx.NO_DEFAULT, inner))
                if answered != [wx.NO] or not inner.IsModal() or not outer.IsModal():
                    raise AssertionError('Failure cleanup terminated a suspended conversion parent.')
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
                      and not frame.working and not conversion_calls and not import_calls)
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


    def later(delay, callback):
        epoch = command_epoch
        def invoke():
            if epoch != command_epoch:
                return
            try:
                callback()
            except Exception:
                remember_failure()
        timer = wx.CallLater(delay, invoke)
        timers.append(timer)
        return timer

    def release(key):
        try:
            if key in pressed:
                simulator.KeyUp(key)
        finally:
            pressed.discard(key)

    def press(control, key=wx.WXK_SPACE):
        top = wx.GetTopLevelParent(control)
        handle = top.GetHandle()
        until = monotonic() + 5
        def dispatch():
            if not user32.IsWindow(handle):
                raise AssertionError('The selected conversion control is gone.')
            top.Raise()
            user32.SetForegroundWindow(handle)
            control.SetFocus()
            if wx.Window.FindFocus() is not control or user32.GetForegroundWindow() != handle:
                if monotonic() >= until:
                    raise TimeoutError('Native conversion focus did not reach the exact control.')
                later(25, dispatch)
                return
            if not simulator.KeyDown(key):
                raise AssertionError('Native conversion key down failed.')
            pressed.add(key)
            later(40, lambda: release(key))
        later(1, dispatch)

    def control(window, name):
        found = [item for item in descendants(window) if item.GetName() == name]
        if len(found) != 1:
            raise AssertionError('Native conversion control is missing or ambiguous: ' + name)
        return found[0]

    def dispatch_picker(source, output, *, cancel_first=False, cancel_runtime=False):
        picked = []
        class FileChoice:
            def __init__(self, parent, title, **options):
                if parent is not frame or title not in (
                        'Choose documents to convert for reading',
                        'Choose the installed Pandoc executable for RTF'):
                    raise AssertionError('Unexpected conversion file selection.')
                self.title = title
            def ShowModal(self):
                picked.append(self.title)
                if cancel_first and self.title.startswith('Choose documents'):
                    return wx.ID_CANCEL
                if cancel_runtime and self.title.startswith('Choose the installed'):
                    return wx.ID_CANCEL
                return wx.ID_OK
            def GetPaths(self):
                if self.title != 'Choose documents to convert for reading':
                    raise AssertionError('Unexpected conversion source selection.')
                return [str(source)]
            def GetPath(self):
                if self.title != 'Choose the installed Pandoc executable for RTF':
                    raise AssertionError('Unexpected selected runtime path.')
                return str(args.pandoc.resolve(strict=True))
            def Destroy(self):
                pass
        class FolderChoice:
            def __init__(self, parent, title):
                if parent is not frame or title != 'Choose the folder for new reading copies':
                    raise AssertionError('Unexpected conversion destination.')
            def ShowModal(self):
                picked.append('destination')
                return wx.ID_OK
            def GetPath(self):
                return str(output)
            def Destroy(self):
                pass
        with patch.object(wx, 'FileDialog', FileChoice), patch.object(wx, 'DirDialog', FolderChoice):
            if not frame.keyboard_commands.invoke('conversion.run'):
                raise AssertionError('The actual registered conversion menu command did not dispatch.')
        return picked

    def visit_reader(read_button, expected):
        state = {'seen': False, 'save': False, 'close': False}
        until = monotonic() + 30
        def visit():
            if monotonic() >= until:
                raise TimeoutError('The real source reader did not complete its verified-copy handoff.')
            readers = [window for window in wx.GetTopLevelWindows()
                       if isinstance(window, wx.Dialog) and window.GetParent() is frame
                       and window.GetTitle() == 'Voice reader and reading copy'
                       and window.IsShown() and window.IsModal()]
            if not readers:
                if state['close']:
                    state['complete'] = True
                    return
                later(25, visit)
                return
            if len(readers) != 1:
                raise AssertionError('Verified reading copy opened multiple source readers.')
            reader = readers[0]
            own_modal(reader)
            editor = control(reader, 'Reading copy (editable; Ctrl+S saves)')
            status = control(reader, 'Original and reading-copy status')
            if not state['seen']:
                actual_text = editor.GetValue()
                if actual_text != expected:
                    try:
                        observe_native(event='reader-value-mismatch', expected=text_shape(expected),
                            actual=text_shape(actual_text), handoffs=reader_handoffs[-2:])
                    except Exception:
                        pass  # Preserve the exact comparison if diagnostics fail.
                check('real existing reader receives the exact verified UTF-8 copy',
                      actual_text == expected)
                state['seen'] = True
            if not state['save']:
                # Real Ctrl+S is needed before the existing reader's close guard.
                editor.SetFocus()
                reader.Raise()
                user32.SetForegroundWindow(reader.GetHandle())
                if wx.Window.FindFocus() is not editor or user32.GetForegroundWindow() != reader.GetHandle():
                    later(25, visit)
                    return
                if not simulator.KeyDown(wx.WXK_CONTROL):
                    raise AssertionError('Native reader Control key did not press.')
                pressed.add(wx.WXK_CONTROL)
                if not simulator.KeyDown(ord('S')):
                    raise AssertionError('Native reader Save key did not press.')
                pressed.add(ord('S'))
                later(40, lambda: (release(ord('S')), release(wx.WXK_CONTROL)))
                state['save'] = True
                later(80, visit)
                return
            resume = ReaderDraftStore(frame.library.root, slot='converted-reading-copy').load()
            if resume is None or resume.document.text != expected or frame.working:
                later(25, visit)
                return
            if not state['close']:
                check('verified copy remains unbound and gives no native quotation authority',
                      resume.document.source_anchor is None
                      and 'No retained-library source locator.' in status.GetValue())
                check('real reader Save preserves the verified copy and its original',
                      resume.document.text == resume.document.source_text == expected)
                state['close'] = True
                press(editor, wx.WXK_ESCAPE)
            later(25, visit)
        later(25, visit)
        press(read_button)
        pump(lambda: state.get('complete', False), 'Native reading-copy modal did not close.', timeout=35)
        pump(lambda: not frame.working, 'Verified Read callback did not return to the host.')
        check('native reader closes and returns to the same conversion result window',
              state['seen'] and state['save'] and state['close']
              and frame.conversion_results_dialog is not None)

    def confirm_import(button, *, yes):
        state = {'observed': False, 'answered': False, 'returned': False}
        until = monotonic() + 15
        def visit():
            if monotonic() >= until:
                raise TimeoutError('Native reading-copy confirmation did not finish.')
            question = owned_question()
            if question and not state['answered']:
                no_button = native_question_button(question, 'No')
                yes_button = native_question_button(question, 'Yes')
                if (not no_button or not yes_button or user32.GetForegroundWindow() != question
                        or user32.GetFocus() != no_button):
                    later(25, visit)
                    return
                if not state['observed']:
                    check('import confirmation actually starts with default No' if not yes
                          else 'explicit import still starts with default No',
                          user32.GetFocus() == no_button)
                    state['observed'] = True
                if yes:
                    # An explicit Tab from native No selects native Yes.
                    if not simulator.KeyDown(wx.WXK_TAB):
                        raise AssertionError('Native explicit Yes selection failed.')
                    pressed.add(wx.WXK_TAB)
                    later(40, lambda: release(wx.WXK_TAB))
                    def accept():
                        if (owned_question() != question
                                or native_question_button(question, 'Yes') != yes_button
                                or user32.GetFocus() != yes_button):
                            raise AssertionError('Native confirmation did not move to Yes.')
                        if not simulator.KeyDown(wx.WXK_SPACE):
                            raise AssertionError('Native Yes key failed.')
                        pressed.add(wx.WXK_SPACE)
                        later(40, lambda: release(wx.WXK_SPACE))
                    later(80, accept)
                else:
                    if not simulator.KeyDown(wx.WXK_SPACE):
                        raise AssertionError('Native default No key failed.')
                    pressed.add(wx.WXK_SPACE)
                    later(40, lambda: release(wx.WXK_SPACE))
                state['answered'] = True
            elif state['answered'] and not question:
                state['returned'] = True
                return
            later(25, visit)
        later(25, visit)
        press(button)
        pump(lambda: state['returned'], 'The owned reading-copy question did not return.', timeout=20)
        pump(lambda: not frame.working, 'The actual canonical reading-copy import did not return.', timeout=40)
        return state

    outcomes = {}
    extraction_state = {'started': 0, 'returned': 0, 'error': None}
    actual_host_job = None

    def observe_native(**values):
        try:
            print('DIAG: actual native conversion ' + json.dumps(values, ensure_ascii=True),
                  file=sys.stderr, flush=True)
        except Exception:
            pass  # Optional diagnostics cannot change an actual call or result.

    def text_shape(value):
        if type(value) is not str:
            return {'type': type(value).__name__[:64]}
        return {'characters': len(value),
                'utf8_sha256': sha256(value.encode('utf-8')).hexdigest(),
                'cr': value.count('\r'), 'lf': value.count('\n'),
                'crlf': value.count('\r\n'),
                'leading_codepoints': [ord(char) for char in value[:16]],
                'trailing_codepoints': [ord(char) for char in value[-16:]]}

    def observed_reader(*values, **options):
        try:
            record = {'parent_is_host': bool(values and values[0] is frame),
                      'text': text_shape(options.get('text'))}
            reader_handoffs.append(record)
            observe_native(event='reader-handoff-entered', **record)
        except Exception:
            pass  # Diagnostics must not change reader arguments or its outcome.
        return actual_reader(*values, **options)

    def error_text(error):
        try:
            return type(error).__name__ + ': ' + str(error)[:4096]
        except Exception:
            return type(error).__name__

    def conversion_state():
        try:
            return {
                'working': bool(frame.working),
                'status': frame.status.GetValue()[:4096],
                'extract': dict(extraction_state),
                'worker_admissions': [
                    {'title': row['title'], 'admitted': row.get('future') is not None,
                     'done': row['future'].done() if row.get('future') is not None else None,
                     'error': row.get('error')}
                    for row in worker_calls[-16:]],
                'completed_conversions': len(conversion_calls),
                'native_children': len(native_children),
            }
        except Exception as error:
            return {'diagnostic_error_type': type(error).__name__}

    def observed_host_job(*values, **options):
        title = values[0] if values else options.get('title')
        row = {'title': title[:128] if type(title) is str else '<unknown>', 'future': None}
        worker_calls.append(row)
        try:
            result = actual_host_job(*values, **options)
        except BaseException as error:
            row['error'] = error_text(error)
            observe_native(event='host-admission-error', error=row['error'])
            raise
        row['future'] = result
        observe_native(event='host-admission-returned', title=row['title'], admitted=result is not None)
        return result

    def cleanup_check(name, condition):
        try:
            check(name, condition())
        except Exception:
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native conversion cleanup assertion\n' + detail,
                  file=sys.stderr, flush=True)
            return False
        return True

    actual_job, actual_extract = windows_job.JobProcess, ConversionService.extract
    actual_close_after = worker_cleanup.close_after_worker
    actual_reader = speech_dialog.show_speech_reader
    actual_verified_read = selected_module.read_result
    actual_import = None

    def observed_job(*values, **options):
        child = actual_job(*values, **options)
        native_children.append(child)
        return child

    def observed_extract(service, *values, **options):
        extraction_state['started'] += 1
        observe_native(event='extract-entered')
        try:
            if threading.get_ident() == ui_thread:
                raise AssertionError('Native document conversion ran on the UI thread.')
            result = actual_extract(service, *values, **options)
        except BaseException as error:
            extraction_state['error'] = error_text(error)
            observe_native(event='extract-error', error=extraction_state['error'],
                           traceback=traceback.format_exc(limit=8)[:8192])
            raise
        conversion_calls.append(result)
        extraction_state['returned'] += 1
        observe_native(event='extract-returned', completed=len(conversion_calls))
        return result

    def observed_verified_read(*values, **options):
        if hold_read[0]:
            if threading.get_ident() == ui_thread:
                raise AssertionError('Queued verified Read did not use the existing host worker.')
            read_entered.set()
            if not read_release.wait(10):
                raise TimeoutError('The native queued-Read control was not released.')
        return actual_verified_read(*values, **options)

    def observed_reader_cleanup(executor, resources):
        future = actual_close_after(executor, resources)
        reader_finalizers.append((executor, future))
        return future

    with TemporaryDirectory(prefix='carr-native-selected-conversion-') as temporary:
        root = Path(temporary)
        try:
            for name in MODULES:
                module = importlib.import_module(name)
                actual = Path(module.__file__).resolve(strict=True)
                expected = source_root / Path(*name.split('.')).with_suffix('.py')
                digest = sha256(actual.read_bytes()).hexdigest()
                if digest != sha256(expected.read_bytes()).hexdigest():
                    raise AssertionError('The native conversion imported different package bytes: ' + name)
                if args.mode == 'source':
                    if actual != expected.resolve(strict=True):
                        raise AssertionError('The source native control gate imported another package.')
                elif (not actual.is_relative_to(Path(sys.prefix).resolve(strict=True))
                      or actual.is_relative_to(source_root.parent) or 'site-packages' not in actual.parts):
                    raise AssertionError('The installed native control gate imported a checkout.')
                origins[name], installed_paths[name] = digest, actual
            check('all selected source or installed module identities are exact', len(origins) == 30)
            check('real native Windows controls', wx.Platform == '__WXMSW__')
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Native verified reading copies')
            original_text = 'Unrelated retained catalog original remains exact.'
            library.add_text(workspace, 'Existing original', original_text)
            ProgressPreferenceStore(library.root / 'progress-preferences.json').save(ProgressPreferences(muted=True))
            frame = desktop_app.DesktopFrame(library)
            actual_host_job = frame.run_job
            host_handle, host_executor, host_cancel = frame.GetHandle(), frame.executor, frame.cancel_event
            frame.Show()
            gui.SetTopWindow(frame)
            check('existing host worker and workspace own the conversion path',
                  isinstance(host_executor, worker_lifetime.DesktopExecutor) and frame._workspace() == workspace)
            check('conversion reuses its actual registered command identifier',
                  any(command.command_id == 'conversion.run' for command in frame.keyboard_commands.registry.commands()))
            inputs, outputs = root / 'inputs', root / 'reading'
            inputs.mkdir()
            outputs.mkdir()
            source = inputs / 'Оригінал.rtf'
            raw = ('{\\rtf1\\ansi ' + fixture.TEXT + '\\par Second paragraph.}').encode('ascii')
            source.write_bytes(raw)
            actual_import = library.import_files
            def observed_import(*values, **options):
                if threading.get_ident() == ui_thread:
                    raise AssertionError('Actual reading-copy import ran on the UI thread.')
                result = actual_import(*values, **options)
                import_calls.append(result)
                return result

            def run_steps():
                try:
                    failure_cleanup_control()
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    selected = dispatch_picker(source, outputs, cancel_first=True)
                    check('cancelling the source picker admits no conversion or output',
                          len(selected) == 1 and not conversion_calls and not list(outputs.iterdir()))
                    selected = dispatch_picker(source, outputs, cancel_runtime=True)
                    check('cancelling the selected runtime picker admits no conversion or output',
                          len(selected) == 2 and not conversion_calls and not list(outputs.iterdir()))
                    picked = dispatch_picker(source, outputs)
                    observe_native(event='explicit-conversion-dispatched', pickers=picked[:4],
                                   state=conversion_state())
                    try:
                        pump(lambda: getattr(frame, 'conversion_results_dialog', None) is not None and not frame.working,
                             'Actual Pandoc conversion did not return its native result window.', timeout=50)
                    except BaseException:
                        observe_native(event='result-window-wait-failed', state=conversion_state())
                        raise
                    result_dialog = frame.conversion_results_dialog
                    check('actual existing menu converts RTF with the selected native Pandoc',
                          len(conversion_calls) == 1
                          and conversion_calls[0].report['engine'] == 'selected-pandoc-rtf-sandbox'
                          and len(native_children) == 1)
                    result = conversion_calls[0]
                    reading = read_result(result)
                    check('native conversion commits exact original reading text and fidelity report',
                          fixture.TEXT in reading and source.read_bytes() == result.original_path.read_bytes() == raw
                          and set(path.name for path in result.path.parent.iterdir()) ==
                              {result.path.name, 'original.rtf', 'provenance.json'})
                    detail = control(result_dialog, 'Reading copy original and fidelity report')
                    check('result names the external original and report without catalog authority',
                          str(result.original_path) in detail.GetValue() and str(result.report_path) in detail.GetValue()
                          and result.report['native_catalog_source'] is False)
                    check('conversion alone neither imports nor automatically starts reading',
                          len(library.browse(workspace)) == 1 and not import_calls
                          and not any(window.GetTitle() == 'Voice reader and reading copy'
                              for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)))
                    read_button = control(result_dialog, 'Read verified reading copy')
                    import_button = control(result_dialog, 'Import verified reading copy into selected library')
                    visit_reader(read_button, reading)
                    state = confirm_import(import_button, yes=False)
                    check('native default No preserves the library and external result',
                          state['observed'] and not import_calls and len(library.browse(workspace)) == 1
                          and source.read_bytes() == result.original_path.read_bytes() == raw)
                    state = confirm_import(import_button, yes=True)
                    check('explicit native Yes runs the canonical TXT importer on the existing worker',
                          state['observed'] and len(import_calls) == 1
                          and len(import_calls[0]) == 1 and import_calls[0][0]['state'] == 'complete')
                    status = control(result_dialog, 'Reading copy action status')
                    check('import completion explicitly preserves the external evidence boundary',
                          'The retained native original and report remain in the external folder.' in status.GetValue())
                    check('actual imported text is searchable and native original is not substituted',
                          len(library.search(workspace, 'Synthetic selected-format')) == 1
                          and raw not in [path.read_bytes() for path in library.assets_dir.rglob('*') if path.is_file()])
                    check('unrelated original and all selected native bytes remain exact',
                          library.assets.resolve(sha256(original_text.encode('utf-8')).hexdigest()).read_bytes()
                              == original_text.encode('utf-8')
                          and source.read_bytes() == result.original_path.read_bytes() == raw)
                    before_jobs = len(native_children)
                    original_reading = result.path.read_bytes()
                    result.path.write_bytes(original_reading + b' changed')
                    press(read_button)
                    pump(lambda: 'changed' in status.GetValue().casefold() and not frame.working,
                         'A changed reading copy was not refused before use.')
                    check('changed reading bytes cannot reopen the reader or re-run a converter',
                          len(native_children) == before_jobs
                          and not any(window.GetTitle() == 'Voice reader and reading copy'
                              for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)))
                    result.path.write_bytes(original_reading)
                    hold_read[0] = True
                    press(read_button)
                    pump(read_entered.is_set, 'The queued verified Read did not reach the real worker.')
                    result_dialog.Close()
                    pump(lambda: frame.conversion_results_dialog is None,
                         'The modeless conversion result did not release its owner.')
                    read_release.set()
                    pump(lambda: not frame.working, 'Closed-window verified Read did not settle.')
                    check('native result close revokes its actual queued Read callback',
                          frame.conversion_results_dialog is None and len(import_calls) == 1
                          and not any(window.GetTitle() == 'Voice reader and reading copy'
                              for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)))
                    outcomes.update(native_conversion='PASS', native_verified_read='PASS',
                                    native_canonical_import='PASS', native_source_guards='PASS')
                except Exception:
                    remember_failure()
                finally:
                    active = wx.EventLoopBase.GetActive()
                    if active is not None and active.IsMain():
                        active.ScheduleExit(1 if errors else 0)
                    elif errors:
                        unwind()

            with patch.object(windows_job, 'JobProcess', side_effect=observed_job), \
                 patch.object(frame, 'run_job', new=observed_host_job), \
                 patch.object(ConversionService, 'extract', new=observed_extract), \
                 patch.object(library, 'import_files', side_effect=observed_import), \
                 patch.object(worker_cleanup, 'close_after_worker', side_effect=observed_reader_cleanup), \
                 patch.object(speech_dialog, 'show_speech_reader', new=observed_reader), \
                 patch.object(selected_module, 'read_result', side_effect=observed_verified_read):
                try:
                    wx.CallAfter(run_steps)
                    gui.MainLoop()
                finally:
                    command_epoch += 1
                    for timer in timers:
                        timer.Stop()
                    for key in tuple(pressed):
                        release(key)
                    host_cancel.set()
                    read_release.set()
                    try:
                        result_dialog = getattr(frame, 'conversion_results_dialog', None)
                        if result_dialog:
                            result_dialog.Close()
                        if any(window and user32.IsWindow(handle) and window.IsModal()
                               for window, handle, _ in modal_loops):
                            if not errors:
                                errors.append('A modal remained open at native conversion cleanup.')
                            unwind()
                            pump(lambda: not any(window and user32.IsWindow(handle) and window.IsModal()
                                for window, handle, _ in modal_loops), 'Owned native reader did not unwind.',
                                timeout=20, cleanup=True)
                        if user32.IsWindow(host_handle):
                            frame.Close(force=True)
                        pump(lambda: not user32.IsWindow(host_handle), 'Actual conversion host Close did not drain.',
                             timeout=20, cleanup=True)
                    finally:
                        try:
                            host_executor.shutdown(wait=True, cancel_futures=True)
                        finally:
                            for executor, future in reader_finalizers:
                                try:
                                    # This is the reader's queued cleanup task:
                                    # never cancel it before it gets its turn.
                                    future.result(timeout=20)
                                finally:
                                    executor.shutdown(wait=True, cancel_futures=False)
                    # Evaluate every original cleanup obligation, retaining an
                    # earlier operation failure before any secondary assertion.
                    cleanup = [
                        cleanup_check('native host Close drains the shared worker and retained finalizers',
                          lambda: not user32.IsWindow(host_handle) and not host_executor.cleanup_failures),
                        cleanup_check('actual reader finalizers release their existing worker owners',
                          lambda: len(reader_finalizers) == 1 and all(future.done() for _, future in reader_finalizers)
                          and not getattr(frame, '_reader_playback_retained', [])),
                        cleanup_check('actual converter private Job and child are closed before any fallback',
                          lambda: len(native_children) == 1
                          and all(child.poll() is not None and child._job is None for child in native_children)),
                        cleanup_check('current conversion resource leases and staging are empty after cleanup',
                          lambda: not ResourceBroker.for_app(library.root).snapshot()['leases']
                          and not any(path.name.startswith('.pending-conversion-') for path in outputs.rglob('*'))),
                    ]
                    if all(cleanup):
                        outcomes['native_host_cleanup'] = 'PASS'
            for name, actual in installed_paths.items():
                if (sha256(actual.read_bytes()).hexdigest() != origins[name]
                        or origins[name] != sha256((source_root / Path(*name.split('.')).with_suffix('.py')).read_bytes()).hexdigest()):
                    raise AssertionError('Imported conversion source changed during native work: ' + name)
            check('all source or installed origins remain unchanged after actual cleanup', len(origins) == 30)
            if sha256(helper_path.read_bytes()).hexdigest() != helper_hash:
                raise AssertionError('The native qualification companion changed.')
        finally:
            try:
                # Also drain an initialized host if setup failed before entering
                # the observed conversion context. No work is reported as PASS.
                if host_executor is not None:
                    host_cancel.set()
                    try:
                        if user32.IsWindow(host_handle):
                            frame.Close(force=True)
                            pump(lambda: not user32.IsWindow(host_handle),
                                 'Failed native conversion setup did not close its host.',
                                 timeout=20, cleanup=True)
                    finally:
                        host_executor.shutdown(wait=True, cancel_futures=True)
            finally:
                if native_handle is not None:
                    native_handle.close()
        if errors:
            raise RuntimeError('\n'.join(errors))
    check('native owned temporary files were removed after every worker settled', not root.exists())
    report = {'status': 'PASS', 'mode': args.mode, 'checks': checks, 'origins': origins, **outcomes,
        'native_failure_cleanup': 'PASS', 'source_originals_unchanged': 'PASS',
        'fixture_companion_sha256': helper_hash, 'external_inputs': selected_inputs, 'firewall': boundary,
        'file_picker': 'explicit fixture selections; system file/folder picker not automated',
        'actual_format_quality': 'NOT_RUN', 'native_catalog_fidelity': 'NOT_RUN',
        'physical_audio_device': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    _cleanup_complete.set()
    return 0


if __name__ == '__main__':
    import faulthandler
    faulthandler.enable(all_threads=True)
    def watchdog():
        if not _cleanup_complete.wait(180):
            os.write(2, b'FAIL: selected conversion native controls or cleanup exceeded 180 seconds.\n')
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True, name='selected-conversion-native-watchdog').start()
    raise SystemExit(main())
