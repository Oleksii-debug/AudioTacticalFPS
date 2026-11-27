"""Installed native book split/prepare/render acceptance on the existing host.

Actual Windows controls, selected SAPI, pinned FFmpeg and source receipts are
required. This is not an NVDA, listening-quality or private-model evaluation.
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
from zipfile import ZipFile, ZIP_STORED


_cleanup_complete = threading.Event()


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio-directory', required=True, type=Path)
    parser.add_argument('--audio-lock', type=Path,
        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    arguments = parser.parse_args()
    selected_audio = arguments.audio_directory.resolve(strict=True)
    audio_pin = json.loads(arguments.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library import runtime_settings
    from carr_library.audio import dsp, process
    from carr_library.operations import broker
    from carr_library.speech import NativeSapiProvider
    from carr_library.speech import source as source_module
    from carr_library.reader import reading_desk
    from carr_library.studio import book_audio, project, render
    from carr_library.ui import book_audio_dialog, worker_lifetime, desktop_app

    checks, errors, pressed, audio_proof, spoken = [], [], set(), {}, []
    frame = dialog = service = production = registry = None
    host_handle = host_executor = host_cancel = None
    close_dialog = None
    modal_loops, question_waits, failure_control = [], {}, {}
    unwind_pending = False
    question_titles = ('Prepare reviewed book items', 'Run reviewed book audio')
    ui_thread = threading.get_ident()
    stage, key_pending, finished = -1, False, False
    deadline = monotonic() + 240

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
                and str(exception) == 'controlled native Book confirmation failure'
                and fixture and user32.IsWindow(fixture_handle) and fixture.IsModal()):
            failure_control['handled'] = True
        else:
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native Book probe callback\n' + detail, file=sys.stderr, flush=True)
        unwind()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            remember_failure()
            return True

    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
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

    def own_modal(window):
        loop = wx.EventLoopBase.GetActive()
        if loop is None or not loop.IsRunning() or loop.IsMain():
            raise AssertionError('The owned Book modal has no running native event loop.')
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
            raise AssertionError('Refusing a native confirmation not owned by this Book probe.')
        return handle

    def dismiss_owned_question():
        # MessageBox has its own native modal loop. Answer only its exact
        # process/owner-bound question No before touching any suspended wx loop.
        for title in question_titles:
            handle = owned_question(title)
            if not handle:
                continue
            no_button = user32.GetDlgItem(handle, 7)  # IDNO
            if not no_button:
                raise AssertionError('The owned Book confirmation has no native No button.')
            if not user32.PostMessageW(handle, 0x0111, 7, no_button):  # WM_COMMAND
                raise OSError(ctypes.get_last_error(), 'Could not queue owned Book No cleanup.')
            return True
        return False

    def unwind():
        nonlocal unwind_pending
        if unwind_pending:
            return
        unwind_pending = True

        def close_active():
            nonlocal unwind_pending
            unwind_pending = False
            try:
                if dismiss_owned_question():
                    # Product handlers return after No without calling the
                    # probe again. Continue real-failure cleanup only after the
                    # native question has yielded back to its owning wx loop.
                    if errors:
                        wx.CallLater(25, unwind)
                    return
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
                print('FAIL: native Book cleanup\n' + detail, file=sys.stderr, flush=True)
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

    def named(name):
        def descendants(window):
            for child in window.GetChildren():
                yield child
                yield from descendants(child)
        values = [control for control in descendants(dialog) if control.GetName() == name]
        if len(values) != 1:
            raise AssertionError('Unique accessible control missing: ' + name)
        return values[0]

    def confirm(title, *, accepted):
        """Drive the product's actual default-No Windows MessageBox."""
        nonlocal key_pending
        handle = owned_question(title)
        if not handle:
            return False
        expected_parent = dialog.GetHandle()
        if user32.GetWindow(handle, 4) != expected_parent:
            raise AssertionError('The actual Book confirmation has a different direct parent.')
        current_focus = user32.GetDlgCtrlID(user32.GetFocus())
        key_identity = (stage, title, handle)
        if key_identity not in question_waits:
            question_waits[key_identity] = min(deadline, monotonic() + 5)
            print('DIAG: Book confirmation focus ' + json.dumps({
                'stage': stage, 'foreground': user32.GetForegroundWindow() == handle,
                'control_id': current_focus}), flush=True)
        # Appearance precedes focus settlement on some Windows builds. Observe
        # the actual default; never move focus to No to manufacture this proof.
        if user32.GetForegroundWindow() != handle or current_focus != 7:
            if monotonic() >= question_waits[key_identity]:
                raise AssertionError('Native Book confirmation did not settle on its default No; '
                                     'observed control ID ' + str(current_focus))
            return False
        check('confirmation belongs to the book dialog: ' + title,
              user32.GetWindow(handle, 4) == dialog.GetHandle())
        check('confirmation defaults to No: ' + title, current_focus == 7)
        key = wx.WXK_ESCAPE
        if accepted:
            yes = user32.GetDlgItem(handle, 6)
            check('native Yes button exists: ' + title, bool(yes))
            user32.SetFocus(yes)
            check('native explicit Yes focus: ' + title, user32.GetFocus() == yes)
            key = wx.WXK_SPACE
        key_pending = True
        check('native confirmation key: ' + title, simulator.KeyDown(key))
        pressed.add(key)
        wx.CallLater(40, guarded(lambda: release(key, title)))
        return True

    def native_failure_control():
        fixture = wx.Dialog(dialog, title='Native Book failure cleanup fixture')
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
                    raise RuntimeError('controlled native Book confirmation failure')
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
                if (refused != [wx.NO] or not fixture.IsModal() or not dialog.IsModal()
                        or not failure_control.get('handled')):
                    raise AssertionError('Native Book question cleanup touched a suspended parent.')
                unwind()
            except Exception:
                remember_failure()

        try:
            wx.CallLater(25, fixture_opened)
            if fixture.ShowModal() != wx.ID_CANCEL:
                raise AssertionError('The controlled Book modal did not unwind through Cancel.')
            check('native failure answers only owned question No before unwinding active modal',
                  refused == [wx.NO] and failure_control.get('handled')
                  and dialog.IsModal() and not frame.working and not spoken
                  and production.jobs() == () and not list(output.iterdir()))
            failure_control['passed'] = True
        finally:
            failure_control['injecting'] = False
            if user32.IsWindow(failure_control['handle']):
                fixture.Destroy()

    def release_owned():
        try:
            if service is not None:
                service.close()
        finally:
            if production is not None:
                production.close()

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

    with TemporaryDirectory(prefix='resonance-native-book-audio-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        try:
            checkout = Path(__file__).resolve().parents[1]
            modules = (book_audio, book_audio_dialog, worker_lifetime, render, project,
                       reading_desk, source_module, runtime_settings, broker, dsp, process, desktop_app)
            for module in modules:
                actual = Path(module.__file__).resolve()
                original = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
                check('exact installed source: ' + module.__name__,
                      actual.is_relative_to(Path(sys.prefix).resolve()) and not actual.is_relative_to(checkout)
                      and sha256(actual.read_bytes()).digest() == sha256(original.read_bytes()).digest())
            check('real native Windows controls', wx.Platform == '__WXMSW__')
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Native source-bound book audio')
            epub = root / 'selected-source.epub'
            with ZipFile(epub, 'w') as archive:
                archive.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
                archive.writestr('META-INF/container.xml',
                    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                    '<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/>'
                    '</rootfiles></container>')
                archive.writestr('book.opf',
                    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:carr:native-book</dc:identifier>'
                    '<dc:title>Native source book</dc:title><dc:language>en</dc:language>'
                    '<meta property="dcterms:modified">2026-11-27T00:00:00Z</meta></metadata>'
                    '<manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>'
                    '<spine><itemref idref="one"/><itemref idref="two"/></spine></package>')
                for number, title, text in [('one', 'First chapter', '[Music: calm] is literal source text.'),
                                             ('two', 'Second chapter', 'The selected original remains unchanged.')]:
                    archive.writestr(number + '.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml">'
                        '<head><title>' + title + '</title></head><body><h1>' + title +
                        '</h1><p>' + text + '</p></body></html>')
                archive.writestr('nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body>'
                    '<nav epub:type="toc"><ol><li><a href="one.xhtml">First chapter</a></li>'
                    '<li><a href="two.xhtml">Second chapter</a></li></ol></nav></body></html>')
            original = epub.read_bytes()
            imported = library.import_file(workspace, epub)
            check('actual retained EPUB import completes', imported['state'] == 'complete')
            output = root / 'explicit-output'
            output.mkdir()
            settings = runtime_settings.load_settings(library.root / 'state')
            settings.update(ffmpeg=str(selected_audio / 'bin' / 'ffmpeg.exe'),
                            ffprobe=str(selected_audio / 'bin' / 'ffprobe.exe'))
            runtime_settings.save_settings(library.root / 'state', settings)
            production = render.ProductionService(library.root / 'state' / 'studio')
            verify_audio(production._runtime())
            service = book_audio.BookAudioService(library, workspace, production=production)
            frame = desktop_app.DesktopFrame(library)
            host_handle, host_executor, host_cancel = frame.GetHandle(), frame.executor, frame.cancel_event
            frame.register_worker_cleanup(release_owned)
            frame.Show()
            gui.SetTopWindow(frame)
            check('existing shared worker owns finalizers', isinstance(frame.executor, worker_lifetime.DesktopExecutor))
            commands = {command.command_id for command in frame.keyboard_commands.registry.commands('main')}
            check('Book audio command is registered in the existing keyboard registry', 'studio.book_audio' in commands)
            catalog = frame.book.GetPage(1)
            check('current Books and Collections exposes the selected retained edition',
                  catalog.ready and imported['edition'] in catalog.rows)
            catalog.books.SetSelection(catalog.book_ids.index(imported['edition']))
            dialog, close_dialog = book_audio_dialog.create_book_audio_dialog(frame, service=service)
            accepted = {}

            def drive():
                nonlocal stage, registry
                if monotonic() >= deadline:
                    raise TimeoutError('Native book acceptance exceeded its deadline at stage ' + str(stage))
                try:
                    if key_pending:
                        return
                    status = named('Book split preview and saved job status')
                    if stage == -1:
                        own_modal(dialog)
                        native_failure_control()
                        stage = 0
                    elif stage == 0:
                        stage = 1
                        press(named('Load selected book'))
                    elif stage == 1 and not frame.working and 'reading positions; revision' in named('Selected library source').GetValue():
                        check('local source inventory starts no speech or jobs', spoken == [] and production.jobs() == ())
                        stage = 2
                        press(named('Discover local voices'))
                    elif stage == 2 and not frame.working and named('Installed local voice').GetCount():
                        registry = production.voices
                        voices = tuple(registry.voices())
                        selected = next((index for index, voice in enumerate(voices)
                            if isinstance(registry._owners[voice.id], NativeSapiProvider)), None)
                        check('installed native SAPI voice is actually enumerated', selected is not None)
                        named('Installed local voice').SetSelection(selected)
                        synthesize = registry.synthesize
                        def observed(text, voice_id, destination, **options):
                            spoken.append((text, voice_id, threading.get_ident()))
                            return synthesize(text, voice_id, destination, **options)
                        registry.synthesize = observed
                        named('Split mode').SetSelection(book_audio.MODES.index('chapters'))
                        named('Reviewed output folder').ChangeValue(str(output))
                        stage = 3
                        press(named('Preview split and names'))
                    elif stage == 3 and not frame.working and 'PREVIEW ONLY' in status.GetValue():
                        check('native preview names both real EPUB chapters and original revision',
                              'First chapter' in status.GetValue() and 'Second chapter' in status.GetValue()
                              and 'Retained original SHA-256:' in status.GetValue())
                        check('preview and voice discovery do not synthesize', spoken == [] and not production.jobs())
                        check('native preview retains readable status focus', wx.Window.FindFocus() == status)
                        stage = 4
                        press(named('Prepare saved items'))
                    elif stage == 4 and confirm('Prepare reviewed book items', accepted=False):
                        stage = 5
                    elif stage == 5 and not frame.working:
                        check('default-No cancellation prepares no jobs or output', not production.jobs() and not list(output.iterdir()))
                        stage = 6
                        press(named('Prepare saved items'))
                    elif stage == 6 and confirm('Prepare reviewed book items', accepted=True):
                        stage = 7
                    elif stage == 7 and not frame.working and status.GetValue().startswith('PREPARED'):
                        plans = service.saved()
                        check('Prepare stores one immutable plan and two ordinary schema2 jobs',
                              len(plans) == 1 and len(production.jobs()) == 2
                              and all(state['project']['schema'] == 2 for state in production.jobs()))
                        check('Prepare still starts no speech or output', spoken == [] and not list(output.iterdir()))
                        accepted['plan'] = plans[0]
                        stage = 8
                        press(named('Run or resume saved items'))
                    elif stage == 8 and confirm('Run reviewed book audio', accepted=True):
                        stage = 9
                    elif stage == 9 and not frame.working and status.GetValue().startswith('Verified saved items'):
                        plan = accepted['plan']
                        states = [production.get(plan.job_id(index)) for index in (1, 2)]
                        check('native explicit Run completes both source-bound Studio jobs',
                              all(state['status'] == 'complete' for state in states))
                        check('actual selected SAPI renders the literal source without cues',
                              len(spoken) == 2 and all(voice_id == plan.role.voice_id and thread != ui_thread
                                                     for _, voice_id, thread in spoken)
                              and '[Music: calm]' in spoken[0][0]
                              and all(not scene.cues and scene.music is None
                                      for index in (1, 2) for scene in plan.project(index).scenes))
                        for index, state in enumerate(states, 1):
                            path = Path(state['destination'])
                            with wave.open(str(path / 'master.wav'), 'rb') as audio:
                                check('measured complete chapter PCM: ' + str(index),
                                      audio.getframerate() == 24000 and audio.getnchannels() == 2 and audio.getnframes() > 0)
                            check('published exact non-authorizing source receipt: ' + str(index),
                                  json.loads((path / 'book-source.json').read_bytes()) == plan.provenance(index))
                        accepted['artifacts'] = {item['path']: sha256(Path(item['path']).read_bytes()).hexdigest()
                                                 for state in states for item in state['artifacts']}
                        check('completed book rendering returns native status focus', wx.Window.FindFocus() == status)
                        library.archive_workspace(workspace)
                        stage = 10
                        press(named('Run or resume saved items'))
                    elif stage == 10 and confirm('Run reviewed book audio', accepted=True):
                        stage = 11
                    elif stage == 11 and not frame.working and 'archived' in status.GetValue():
                        check('archived source blocks completed-job reuse visibly', len(spoken) == 2
                              and wx.Window.FindFocus() == status)
                        check('source denial preserves every previously committed artifact',
                              accepted['artifacts'] == {path: sha256(Path(path).read_bytes()).hexdigest()
                                                       for path in accepted['artifacts']})
                        check('original EPUB and retained asset remain byte-identical',
                              epub.read_bytes() == original
                              and library.assets.resolve(sha256(original).hexdigest()).read_bytes() == original)
                        check('book narration never creates quotation approval', library._selections == {})
                        stage = 12
                        press(named('Close'), wx.WXK_ESCAPE)
                finally:
                    if not errors and not finished:
                        wx.CallLater(25, guarded(drive))

            def open_dialog():
                nonlocal finished
                try:
                    wx.CallLater(25, guarded(drive))
                    dialog.ShowModal()
                    close_dialog()
                    check('native Escape closes the book dialog', stage == 12)
                    finished = True
                except Exception:
                    remember_failure()
                finally:
                    active = wx.EventLoopBase.GetActive()
                    if active is not None and active.IsMain():
                        active.ScheduleExit(1 if errors else 0)
                    elif errors:
                        unwind()
            wx.CallAfter(guarded(open_dialog))
            gui.MainLoop()
        finally:
            owners_released = False
            try:
                for key in tuple(pressed):
                    simulator.KeyUp(key)
                    pressed.discard(key)
                if close_dialog is not None:
                    close_dialog()
                if host_executor is not None:
                    host_cancel.set()
                    try:
                        # Use the real host shutdown path, including schedules
                        # and native desktop owners. Retained references survive
                        # an already-destroyed frame.
                        if user32.IsWindow(host_handle):
                            frame.Close(force=True)
                        pump(lambda: not user32.IsWindow(host_handle),
                             'Actual Book host Close did not release its native owners.', cleanup=True)
                    finally:
                        host_executor.shutdown(wait=True, cancel_futures=True)
                    check('native host shutdown has no retained cleanup failure',
                          not user32.IsWindow(host_handle) and not host_executor.cleanup_failures)
                else:
                    release_owned()
                gui.Yield()
                owners_released = True
            finally:
                if native_handle is not None:
                    native_handle.close()
        if errors:
            raise RuntimeError('\n'.join(errors))
    if not owners_released:
        raise RuntimeError('Native Book cleanup did not confirm release of its owners.')
    # TemporaryDirectory disposal is part of cleanup too; disarm only after
    # leaving that context, never before its final filesystem operations.
    _cleanup_complete.set()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'audio_runtime': audio_proof,
        'native_keyboard': 'PASS', 'native_sapi_render': 'PASS',
        'native_failure_cleanup': 'PASS' if failure_control.get('passed') else 'FAIL',
        'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN', 'listening_quality': 'NOT_RUN',
        'private_model_inference': 'NOT_RUN'}, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    import faulthandler
    faulthandler.enable(all_threads=True)

    def watchdog():
        if not _cleanup_complete.wait(300):
            os.write(2, b'FAIL: native Book acceptance or owned cleanup exceeded 300 seconds.\n')
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(86)

    threading.Thread(target=watchdog, daemon=True, name='book-native-watchdog').start()
    raise SystemExit(main())
