"""Native Hunspell review through the existing DesktopFrame and reading worker.

The DLL is an explicit external input. Tiny original dictionaries are fixtures.
One real C-API review precedes controlled owned-child cancellation/close cases;
the latter delay the unchanged worker rather than asserting dictionary speed.
No speech, audio-device, language-quality or manual NVDA qualification.
"""
import argparse
import ctypes
from ctypes import wintypes
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
import threading
import time
import traceback


ORIGINS = (
    'carr_library.speech.spelling', 'carr_library.speech.spelling_worker',
    'carr_library.speech.reading', 'carr_library.speech.source',
    'carr_library.speech.session', 'carr_library.speech._io',
    'carr_library.ui.spelling_dialog', 'carr_library.ui.pronunciation_dialog',
    'carr_library.ui.speech_dialog', 'carr_library.ui.reading_shortcuts',
    'carr_library.ui.builtin_keyboard_commands', 'carr_library.ui.desktop_app',
    'carr_library.ui.worker_lifetime', 'carr_library.ui.worker_cleanup',
    'carr_library.audio.process', 'carr_library.audio.windows_job',
    'carr_library.audio.cleanup', 'carr_library.operations.broker',
    'carr_library.operations.models', 'carr_library.native_runtime',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('Actual Windows and an interactive native desktop are required.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import VoiceRegistry
    from carr_library.speech import spelling
    from carr_library.speech.source import read_source_range
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.audio import windows_job
    from carr_library.operations import ResourceBroker
    from carr_library.ui import desktop_app, speech_dialog, spelling_dialog, worker_cleanup
    from carr_library.ui.builtin_keyboard_commands import VOICE_READER_COMMANDS
    from carr_library import runtime_settings

    checks, errors, futures, children, stages, worker_threads = [], [], [], [], [], []
    host_thread = threading.get_ident()
    modal_loops = []
    unwind_pending = False
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        if label not in checks:
            checks.append(label)
            print('PASS: ' + label, flush=True)

    source_root = options.source_root.resolve(strict=True)
    installed = (Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR')
                 else Path(sysconfig.get_path('purelib')).resolve())
    def checked_origins():
        result = {}
        for name in ORIGINS:
            module = importlib.import_module(name)
            actual = Path(module.__file__).resolve(strict=True)
            expected = source_root.joinpath(*name.split('.')).with_suffix('.py')
            digest = sha256(actual.read_bytes()).hexdigest()
            if digest != sha256(expected.read_bytes()).hexdigest():
                raise AssertionError('Changed module bytes: ' + name)
            if options.require_installed and (not actual.is_relative_to(installed)
                                              or actual.is_relative_to(source_root)):
                raise AssertionError('Not an installed module: ' + name)
            result[name] = {'path': str(actual), 'sha256': digest,
                            'installed': actual.is_relative_to(installed)}
        return result
    origins = checked_origins()
    check('all twenty application module bytes and requested installed origins match', len(origins) == 20)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            unwind()
            return True  # Recorded failure; let the active owned modal unwind first.
    app = App(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    app.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL

    def own_modal(window):
        # Capture the actual running loop while inside this dialog's driver.
        # IsModal alone also describes suspended outer loops.
        loop = wx.EventLoopBase.GetActive()
        if loop is None or not loop.IsRunning() or loop.IsMain():
            raise AssertionError('The owned modal has no active native event loop.')
        modal_loops.append((window, window.GetHandle(), loop))

    def unwind():
        nonlocal unwind_pending
        if unwind_pending:
            return
        unwind_pending = True

        def close_active():
            nonlocal unwind_pending
            unwind_pending = False
            try:
                for window, handle, loop in reversed(modal_loops):
                    if window and user32.IsWindow(handle) and window.IsModal() and loop.IsRunning():
                        window.EndModal(wx.ID_CANCEL)
                        return
                active = wx.EventLoopBase.GetActive()
                if errors and active is not None and active.IsMain():
                    active.ScheduleExit(1)
            except Exception:
                errors.append(traceback.format_exc())
                active = wx.EventLoopBase.GetActive()
                if active is not None:
                    active.ScheduleExit(1)
        wx.CallAfter(close_active)

    def pump(predicate, message, timeout=25, *, allow_errors=False):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors and not allow_errors:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            app.Yield()
            time.sleep(.01)

    def settle(seconds=.05):
        deadline = time.monotonic() + seconds
        pump(lambda: time.monotonic() >= deadline, 'Native event delivery did not settle.')

    def named(parent, name):
        control = wx.FindWindowByName(name, parent)
        if control is None:
            raise AssertionError('Missing native spelling control: ' + name)
        return control

    def key(control, code, modifiers=wx.MOD_NONE):
        top = wx.GetTopLevelParent(control)
        top.Show(); top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: user32.GetForegroundWindow() == top.GetHandle()
                      and wx.Window.FindFocus() is control, 'Native spelling focus/foreground')
        if not simulator.Char(code, modifiers):
            raise AssertionError('Native spelling key input failed.')
        settle()

    def press(control):
        if not control.IsEnabled():
            raise AssertionError('Disabled native spelling action: ' + control.GetName())
        key(control, wx.WXK_SPACE)

    def modal(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.IsModal()
                     and window.GetTitle() == title), None)

    close_count, renders = [], []
    class Provider:
        name = 'Empty synthetic voice inventory; spelling only'
        def voices(self):
            return ()
        def render(self, *args, **kwargs):
            renders.append(True)
            raise AssertionError('Spelling must not synthesize audio.')
        def close(self):
            close_count.append(True)

    original_registry = runtime_settings.voice_registry
    original_cleanup = worker_cleanup.close_after_worker
    original_child = windows_job.JobProcess
    original_run = spelling.run_process
    original_review, original_check = spelling_dialog.review_hunspell, spelling_dialog.check_spelling
    delayed = [False]
    def registry(*args, **kwargs):
        return VoiceRegistry([Provider()])
    def owned_cleanup(executor, resources):
        future = original_cleanup(executor, resources)
        futures.append(future)
        return future
    def create_child(*args, **kwargs):
        child = original_child(*args, **kwargs)
        children.append(child)
        return child
    def observed_run(command, **kwargs):
        stages.append(Path(kwargs['cwd']))
        if delayed[0]:
            command = [command[0], '-I', '-B', '-S', '-c',
                'import runpy,sys,time; sys.argv=sys.argv[1:]; time.sleep(30); '
                'runpy.run_path(sys.argv[0],run_name="__main__")', *command[4:]]
        return original_run(command, **kwargs)
    def observed_review(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return original_review(*args, **kwargs)
    def observed_check(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return original_check(*args, **kwargs)

    runtime_settings.voice_registry = registry
    worker_cleanup.close_after_worker = owned_cleanup
    windows_job.JobProcess = create_child
    spelling.run_process = observed_run
    spelling_dialog.review_hunspell, spelling_dialog.check_spelling = observed_review, observed_check
    native_library = Path(options.library).resolve(strict=True)
    native_hash = sha256(native_library.read_bytes()).hexdigest()
    try:
        with TemporaryDirectory(prefix='native-hunspell-reader-') as temporary:
            root = Path(temporary)
            library = LibraryApp(root / 'Бібліотека 😀')
            workspace = library.create_workspace('Native spelling acceptance')
            original_text = '😀 hello wrld worlds Україна слово'
            imported = library.add_text(workspace, 'Unchanged spelling source', original_text)
            asset = library.assets.resolve(imported['asset'])
            original_asset = asset.read_bytes()
            original_hash = sha256(original_asset).hexdigest()
            affix, dictionary = root / 'original.aff', root / 'original.dic'
            affix.write_text('SET UTF-8\nFLAG UTF-8\nTRY abcdefghijklmnopqrstuvwxyz\n'
                             'SFX S Y 1\nSFX S 0 s .\n', encoding='utf-8')
            dictionary.write_text('5\nhello\nworld/S\nretained\nУкраїна\nслово\n', encoding='utf-8')
            dictionary_bytes = dictionary.read_bytes()
            affix_bytes = affix.read_bytes()
            frame = desktop_app.DesktopFrame(library)
            frame.active = workspace, None
            host_executor, frame_handle = frame.executor, frame.GetHandle()
            root_info = library.root.stat()
            root_identity = root_info.st_dev, root_info.st_ino
            slot = 'hunspell-acceptance'
            drafts = ReaderDraftStore(library.root, slot=slot)
            readers, spellings = [], []
            check('actual native DesktopFrame and Windows controls', bool(user32.IsWindow(frame_handle))
                  and wx.Platform == '__WXMSW__')
            check('spelling uses the current semantic command identity',
                  any(command.command_id == 'voice_reader.spelling' for command in VOICE_READER_COMMANDS))

            def scope():
                if frame.library is not library or frame._closing or frame.active != (workspace, None):
                    raise ValueError('Native spelling owner scope changed.')
                current = library.root.stat()
                if (current.st_dev, current.st_ino) != root_identity:
                    raise ValueError('Native spelling library was replaced.')

            def source_loader(cancelled):
                worker_threads.append(threading.get_ident())
                scope()
                desk = ReadingDesk(library, workspace, imported['edition'], cancelled=cancelled)
                return read_source_range(desk, 1, 1, guard=scope, cancelled=cancelled)

            def idle(dialog):
                return not dialog.busy

            def all_reaped():
                return bool(children) and all(child.poll() is not None and child._job is None
                                               for child in children)

            def spelling_driver():
                try:
                    pump(lambda: modal('Spelling review — Hunspell') is not None, 'Spelling modal did not open.')
                    dialog = modal('Spelling review — Hunspell')
                    spellings.append(dialog)
                    own_modal(dialog)
                    editor = named(readers[-1], 'Reading copy (editable; Ctrl+S saves)')
                    check('profile starts empty without loading a native library',
                          dialog.profile is None and dialog.review is None and not children)
                    dialog.library.ChangeValue(str(native_library))
                    dialog.affix.ChangeValue(str(affix))
                    dialog.dictionary.ChangeValue(str(dictionary))
                    dialog.language.ChangeValue('synthetic en+uk')
                    dialog.provenance.ChangeValue('Original tiny acceptance dictionaries; externally built Hunspell 1.7.2.')
                    press(dialog.buttons['review'])
                    pump(lambda: idle(dialog), 'Native profile hashing did not finish.')
                    check('native Review hashes the exact three selected files without execution',
                          dialog.profile is not None and not children
                          and dialog.profile.library.sha256 == native_hash
                          and dialog.profile.dictionary.sha256 == sha256(dictionary_bytes).hexdigest())
                    check('profile identity is displayed in a read-only native control',
                          dialog.profile.identity in dialog.identity.GetValue() and not dialog.identity.IsEditable())
                    press(dialog.buttons['check'])
                    pump(lambda: idle(dialog), 'Actual native spelling check did not finish.')
                    check('actual Hunspell C API returns UTF8 affix and Unicode results',
                          dialog.review is not None and dialog.review.result.encoding == 'UTF-8'
                          and dialog.review.result.words == 5
                          and len(dialog.review.result.issues) == 1
                          and dialog.review.result.issues[0].word == 'wrld'
                          and 'world' in dialog.review.result.issues[0].suggestions
                          and len(children) == 1 and all_reaped())
                    check('checking alone never edits the retained reading copy', editor.GetValue() == original_text)
                    key(dialog.issues, wx.WXK_HOME)
                    pump(lambda: dialog.issues.GetSelection() == 0
                         and editor.GetStringSelection() == 'wrld', 'Native issue selection did not select the exact word.')
                    issue = dialog.review.result.issues[0]
                    check('native selection preserves the Unicode source character range',
                          (issue.start, issue.end) == (original_text.index('wrld'), original_text.index('wrld') + 4))
                    target = tuple(issue.suggestions).index('world')
                    key(dialog.suggestions, wx.WXK_HOME)
                    for _ in range(target):
                        key(dialog.suggestions, wx.WXK_DOWN)
                    pump(lambda: dialog.replacement.GetValue() == 'world', 'Native suggestion selection did not update preview input.')
                    press(dialog.buttons['preview'])
                    check('native Preview remains a non-mutating read-only review',
                          dialog.preview is not None and editor.GetValue() == original_text
                          and not dialog.preview_text.IsEditable() and 'world' in dialog.preview_text.GetValue())
                    press(dialog.buttons['apply'])
                    changed = original_text.replace('wrld', 'world')
                    check('explicit native Apply changes exactly the reviewed word',
                          editor.GetValue() == changed and dialog.applied is not None)
                    saved = drafts.load()
                    check('reading recovery preserves original source and non-authorizing lineage',
                          saved.document.text == changed and saved.document.source_text == original_text
                          and saved.document.source_anchor['quotation_authority'] is False)
                    check('spelling never overwrites the original library asset', asset.read_bytes() == original_asset)
                    press(dialog.buttons['undo'])
                    check('one native Undo restores the exact reading copy',
                          editor.GetValue() == original_text and dialog.applied is None
                          and drafts.load().document.text == original_text)
                    dictionary.write_bytes(dictionary_bytes.replace(b'hello', b'other'))
                    press(dialog.buttons['check'])
                    pump(lambda: idle(dialog), 'Changed dictionary refusal did not finish.')
                    check('changed dictionary cannot reuse its earlier native profile',
                          dialog.review is None and 'changed' in dialog.status.GetValue().lower()
                          and editor.GetValue() == original_text and len(children) == 2 and all_reaped())
                    dictionary.write_bytes(dictionary_bytes)
                    delayed[0] = True
                    press(dialog.buttons['check'])
                    pump(lambda: len(children) == 3 and children[-1].poll() is None,
                         'Controlled cancellation child never became live.')
                    check('native check retains its private request and shared reservation',
                          dialog.busy and (stages[-1] / 'request.json').is_file()
                          and bool(ResourceBroker.for_app(library.root).snapshot()['leases']))
                    press(dialog.buttons['cancel'])
                    pump(lambda: idle(dialog), 'Native Cancel did not settle the existing worker.', timeout=40)
                    check('actual Cancel reaps the native Job before releasing staging and admission',
                          all_reaped() and not stages[-1].exists()
                          and ResourceBroker.for_app(library.root).snapshot()['leases'] == []
                          and editor.GetValue() == original_text)
                    press(dialog.buttons['check'])
                    pump(lambda: len(children) == 4 and children[-1].poll() is None,
                         'Controlled close child never became live.')
                    check('Close is available while the existing spelling worker is active',
                          dialog.busy and dialog.buttons['close'].IsEnabled())
                    press(dialog.buttons['close'])
                except Exception:
                    errors.append(traceback.format_exc())
                    unwind()

            def reader_driver():
                try:
                    pump(lambda: modal('Voice reader and reading copy') is not None, 'Reader modal did not open.')
                    reader = modal('Voice reader and reading copy')
                    readers.append(reader)
                    own_modal(reader)
                    editor = named(reader, 'Reading copy (editable; Ctrl+S saves)')
                    spelling_button = named(reader, 'Spelling review with Hunspell')
                    pump(lambda: spelling_button.IsEnabled() and editor.GetValue() == original_text,
                         'The actual reader worker did not load its retained source.', timeout=40)
                    check('retained source loads through the existing reader worker', editor.GetValue() == original_text)
                    check('spelling needs no installed voice or audio endpoint',
                          named(reader, 'Voice (installed locally)').GetCount() == 0 and not renders)
                    wx.CallLater(60, spelling_driver)
                    press(spelling_button)
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    pump(lambda: spelling_button.IsEnabled() and all_reaped(),
                         'Closing the spelling modal did not settle its owned worker.', timeout=40)
                    check('late native completion after modal destruction is safely discarded',
                          len(children) == 4 and not spellings[-1].alive
                          and all(not path.exists() for path in stages)
                          and ResourceBroker.for_app(library.root).snapshot()['leases'] == []
                          and editor.GetValue() == original_text)
                    key(editor, ord('S'), wx.MOD_CONTROL)
                    pump(lambda: drafts.path.is_file()
                         and 'saved for recovery' in named(reader, 'Speech and save status').GetValue(),
                         'Explicit native reader save did not complete.')
                    check('native CtrlS saves only the unchanged reading derivative',
                          drafts.load().document.text == original_text
                          and drafts.load().document.source_text == original_text
                          and asset.read_bytes() == original_asset)
                    press(wx.FindWindowById(wx.ID_CANCEL, reader))
                except Exception:
                    errors.append(traceback.format_exc())
                    unwind()

            def verify_nested_failure_cleanup():
                outer = wx.Dialog(frame, title='Native outer cleanup fixture')
                inner = wx.Dialog(outer, title='Native inner cleanup fixture')
                closed = []

                def inner_driver():
                    try:
                        own_modal(inner)
                        unwind()
                    except Exception:
                        errors.append(traceback.format_exc()); unwind()

                def outer_driver():
                    try:
                        own_modal(outer)
                        wx.CallLater(60, inner_driver)
                        assert inner.ShowModal() == wx.ID_CANCEL
                        closed.append('inner')
                        check('failure cleanup unwinds only the active owned modal before its parent',
                              outer.IsModal() and wx.EventLoopBase.GetActive().IsRunning()
                              and not wx.EventLoopBase.GetActive().IsMain())
                        unwind()
                    except Exception:
                        errors.append(traceback.format_exc()); unwind()

                try:
                    wx.CallLater(60, outer_driver)
                    assert outer.ShowModal() == wx.ID_CANCEL
                    closed.append('outer')
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    if closed != ['inner', 'outer'] or not wx.EventLoopBase.GetActive().IsMain():
                        raise AssertionError('Nested native failure cleanup did not restore the host loop.')
                finally:
                    inner.Destroy()
                    outer.Destroy()

            def scenario():
                try:
                    verify_nested_failure_cleanup()
                    wx.CallLater(60, reader_driver)
                    speech_dialog.show_speech_reader(frame, title='Retained spelling source',
                        source_loader=source_loader, source_guard=scope, recovery_slot=slot)
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    pump(lambda: bool(futures) and all(future.done() for future in futures),
                         'Existing reader owner cleanup did not finish.', timeout=40)
                    for future in futures:
                        future.result()
                    check('all spelling and source work uses the one existing reader worker',
                          worker_threads and len(set(worker_threads)) == 1 and host_thread not in worker_threads)
                    check('reader cleanup closes the metadata-only voice owner exactly once',
                          close_count == [True] and not renders and not getattr(frame, '_reader_playback_retained', []))
                    check('native spell check leaves selected runtime and dictionaries unchanged',
                          sha256(native_library.read_bytes()).hexdigest() == native_hash
                          and affix.read_bytes() == affix_bytes and dictionary.read_bytes() == dictionary_bytes)
                    check('retained source remains byte-exact after every native action',
                          sha256(asset.read_bytes()).hexdigest() == original_hash)
                    frame.Close()
                    pump(lambda: not user32.IsWindow(frame_handle), 'DesktopFrame did not complete its real close path.')
                    host_executor.shutdown(wait=True, cancel_futures=True)
                    check('actual host and existing owner finalizers close cleanly',
                          not user32.IsWindow(frame_handle) and not host_executor.cleanup_failures)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    app.ExitMainLoop()

            try:
                frame.Show()
                app.SetTopWindow(frame)
                wx.CallAfter(scenario)
                app.MainLoop()
                if errors:
                    raise RuntimeError('\n'.join(errors))
            finally:
                # Fallback is failure cleanup only. The successful assertions
                # above establish native death before any kill/reap here.
                unwind()
                if user32.IsWindow(frame_handle):
                    frame.Close(force=True)
                    pump(lambda: not user32.IsWindow(frame_handle),
                         'Failure-path host cleanup did not finish.', allow_errors=True)
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    child.close()
                for future in futures:
                    future.result(timeout=40)
                host_executor.shutdown(wait=True, cancel_futures=True)
                if host_executor.cleanup_failures:
                    raise RuntimeError('The native host still owns an unresolved resource.')
    finally:
        runtime_settings.voice_registry = original_registry
        worker_cleanup.close_after_worker = original_cleanup
        windows_job.JobProcess = original_child
        spelling.run_process = original_run
        spelling_dialog.review_hunspell, spelling_dialog.check_spelling = original_review, original_check
    check('application bytes remain exact after native work and cleanup', checked_origins() == origins)
    report = {'status': 'PASS', 'checks': checks, 'installed_modules': origins,
              'actual_hunspell_c_api': 'PASS', 'actual_reader_worker': 'PASS',
              'native_review_apply_undo': 'PASS', 'native_cancel_and_close': 'PASS',
              'native_library_sha256': native_hash, 'owned_child_invocations': len(children),
              'lifetime_fixture': 'Last two actual owned children delay the unchanged worker before native loading',
              'dictionaries': 'Original tiny synthetic fixtures; no language-quality qualification',
              'speech_audio': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
              'dictionary_quality': 'NOT_RUN', 'distribution_clearance': 'NOT_GRANTED'}
    if options.output is not None:
        from carr_library.speech._io import new_output
        with new_output(options.output) as staged:
            staged.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    finished = threading.Event()
    def watchdog():
        if not finished.wait(180):
            os.write(2, b'Native Hunspell probe exceeded its work/cleanup deadline.\n')
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True).start()
    native = None
    try:
        import faulthandler
        faulthandler.enable(all_threads=True)
        from carr_library.native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        raise SystemExit(main())
    finally:
        if native is not None:
            native.close()
        finished.set()
