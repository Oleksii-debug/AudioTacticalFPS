"""Installed Windows document batches: real keys, converter children and receipts.

File/folder pickers supply explicit synthetic paths. Controls, the registered
menu action, existing desktop executor, conversion and persistence are real.
This does not qualify native file pickers, speech, human keyboard use or NVDA.
"""
from hashlib import sha256
import faulthandler
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def _document_batch_command(commands):
    # Display labels retain wx access-key markers and may gain an accelerator.
    # Use the actual registry identity captured from the installed Convert menu.
    action = 'convert/document batches — split, name and produce'
    matches = [command for command in commands if command.action == action]
    if len(matches) != 1:
        raise AssertionError('Expected exactly one registered document-batch action; found '
                             + str(len(matches)) + '. Convert actions: '
                             + repr([command.action for command in commands if command.action.startswith('convert/')]))
    return matches[0]


def _abort(wx, app, frame, loop, timer, release, errors, detail):
    errors.append(detail); print('FAIL:', detail, flush=True)
    try:
        for callback in (frame.cancel_event.set if frame is not None else lambda: None,
                         timer.Stop if timer is not None else lambda: None, release):
            try:
                callback()
            except Exception:
                errors.append(traceback.format_exc())
        for window in tuple(wx.GetTopLevelWindows()):
            try:
                if (window and isinstance(window, wx.Dialog) and window.GetParent() is frame and window.IsModal()):
                    window.EndModal(wx.ID_CANCEL)
            except Exception:
                errors.append(traceback.format_exc())
    finally:
        owner = loop if loop is not None else app.GetMainLoop()
        if owner is None:
            raise RuntimeError('The native document-batch owner loop is unavailable.')
        owner.ScheduleExit(1)


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(100, repeat=True, file=sys.stderr)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        return run_native()
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.conversion import batches, formats, planning, service as conversion
    from carr_library.ui import document_batch, document_conversion
    from carr_library.durable_state import read_json

    checks, errors, identities, evidence = [], [], {}, {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label); print('PASS:', label, flush=True)
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc()); return False
    app = ProbeApp(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    simulator = wx.UIActionSimulator()
    frame = timer = None
    held, loop, in_tick = [None], [None], [False]
    stage = ['starting', monotonic()]
    deadline = monotonic() + 160
    original_files, original_directory = wx.FileDialog, wx.DirDialog
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def advance(name):
        stage[:] = [name, monotonic()]; print('STAGE:', name, flush=True)
    def abort(detail):
        _abort(wx, app, frame, loop[0], timer, release, errors, detail)
    def release():
        value, held[0] = held[0], None
        if value is not None and not simulator.KeyUp(*value):
            raise AssertionError('Native document-batch key release failed.')
    def release_safely():
        try:
            release()
        except Exception:
            abort(traceback.format_exc())
    def key(control, code, next_stage, modifiers=wx.MOD_NONE):
        if held[0] is not None:
            return False
        top = wx.GetTopLevelParent(control)
        foreground, owner = user32.GetForegroundWindow(), wintypes.DWORD()
        if foreground:
            user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
        if (owner.value != os.getpid() or int(foreground or 0) != int(top.GetHandle())
                or wx.Window.FindFocus() is not control):
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
            return False
        advance(next_stage)
        held[0] = (code, modifiers)
        if not simulator.KeyDown(code, modifiers):
            raise AssertionError('Native document-batch key press failed.')
        wx.CallLater(40, release_safely)
        return True
    def window():
        return next((item for item in wx.GetTopLevelWindows()
                     if item and isinstance(item, wx.Dialog) and item.GetParent() is frame
                     and item.GetTitle() == 'Document batches — split, name and produce' and item.IsShown()), None)

    bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
    installed = bundle or Path(sysconfig.get_path('purelib')).resolve()
    checkout = Path(__file__).resolve().parents[1]
    for module in (batches, formats, planning, conversion, document_batch, document_conversion):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, actual.is_relative_to(installed)
              and not actual.is_relative_to(checkout) and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest
    check('actual native Windows wx controls', wx.Platform == '__WXMSW__')

    with TemporaryDirectory(prefix='resonance-document-batch-native-') as temporary:
        root = Path(temporary)
        output = root / 'Outputs — Україна'; output.mkdir()
        inputs = (root / 'Перший документ.txt', root / 'Second document.txt')
        texts = ('😀 Exact first.\r\n\r\nДругий абзац.\r\n',
                 '[Scene: literal source]\r\n\r\nNarrator: still literal prose.\n')
        for path, text in zip(inputs, texts):
            path.write_bytes(text.encode('utf-8'))
        source_hashes = [sha256(text.encode('utf-8')).hexdigest() for text in texts]
        pickers = []
        class Files:
            def __init__(self, parent, title, **kwargs):
                check('source picker scope is explicit and permits multiple existing files',
                      title == 'Choose explicit documents' and kwargs['style'] == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
                pickers.append('files')
            def ShowModal(self): return wx.ID_OK
            def GetPaths(self): return list(map(str, inputs))
            def Destroy(self): pass
        class Directory:
            def __init__(self, parent, title, **kwargs):
                check('destination picker requests an existing output directory', title == 'Choose an existing directory for new outputs')
                pickers.append('directory')
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(output)
            def Destroy(self): pass
        wx.FileDialog, wx.DirDialog = Files, Directory
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic document batch')
        observer = batches.DocumentBatches(library.root)

        def verify_completed(dialog):
            state = observer.get(dialog.identity)
            check('shared service persisted complete with every reviewed receipt',
                  state['status'] == 'complete' and len(state['outputs']) == 4)
            chunks = []
            for receipt in state['outputs']:
                raw = (output / receipt['name']).read_bytes()
                sidecar = (output / receipt['provenance']['name']).read_bytes()
                check('native output bytes match the retained completion receipt',
                      len(raw) == receipt['bytes'] and sha256(raw).hexdigest() == receipt['sha256']
                      and sha256(sidecar).hexdigest() == receipt['provenance']['sha256'])
                report = json.loads(sidecar)
                check('output provenance binds the reviewed source without quotation authority',
                      report['plan_sha256'] == state['review_sha256'] and report['quotation_authority'] is False)
                chunks.append(raw)
            check('all source characters, Unicode and CRLF survive the real split',
                  b''.join(chunks[:2]).decode() == texts[0] and b''.join(chunks[2:]).decode() == texts[1])
            evidence['outputs'] = [dict(name=row['name'], sha256=row['sha256'], bytes=row['bytes']) for row in state['outputs']]
            evidence['job_id'], evidence['review_sha256'] = state['id'], state['review_sha256']

        def step():
            if loop[0] is None:
                loop[0] = app.GetMainLoop()
            if monotonic() > deadline or monotonic() - stage[1] > 35:
                dialog = window()
                detail = dialog.status.GetValue() if dialog is not None else frame.status.GetValue()
                raise TimeoutError('Native document batch timed out at ' + stage[0] + ': ' + detail)
            if held[0] is not None:
                return
            dialog = window()
            name = stage[0]
            if name == 'starting':
                if frame.working:
                    return
                binding = _document_batch_command(frame.keyboard_settings.commands)
                check('document batch action belongs to the existing command registry',
                      binding.action.startswith('convert/') and binding.item.IsEnabled())
                frame.keyboard_settings.apply({**frame.keyboard_settings.overrides, binding.action: 'Ctrl+Shift+F10'})
                check('synthetic local keymap binds the original menu action',
                      binding.item.GetItemLabel().endswith('\tCtrl+Shift+F10'))
                check('opening the application did not create a document job', observer.jobs() == ())
                advance('open')
            elif name == 'open':
                key(frame.choice, wx.WXK_F10, 'opened', wx.MOD_CONTROL | wx.MOD_SHIFT)
            elif name == 'opened' and dialog is not None:
                check('actual registered keyboard command opens the owned modal dialog', dialog.IsModal())
                check('review and status are named native read-only controls',
                      isinstance(dialog.review, wx.TextCtrl) and isinstance(dialog.status, wx.TextCtrl)
                      and dialog.review.GetName() == 'Exact split and filename preview'
                      and bool(dialog.review.GetWindowStyleFlag() & wx.TE_READONLY))
                check('approval and execution start disabled',
                      not dialog.buttons['approve'].IsEnabled() and not dialog.buttons['run'].IsEnabled())
                key(dialog.buttons['files'], wx.WXK_SPACE, 'captured')
            elif name == 'captured' and dialog is not None and not dialog.pending and dialog.identity:
                state = observer.get(dialog.identity)
                check('capture retained both exact sources before preparation',
                      [row['sha256'] for row in state['sources']] == source_hashes
                      and all(row['prepared'] is None for row in state['sources']))
                for path in inputs:
                    path.unlink(missing_ok=True)
                key(dialog.buttons['prepare'], wx.WXK_SPACE, 'prepared')
            elif name == 'prepared' and dialog is not None and not dialog.pending:
                state = observer.get(dialog.identity)
                check('real isolated conversion succeeds after selected originals are removed',
                      state['status'] == 'prepared' and all(row['prepared'] for row in state['sources'])
                      and not any(path.exists() for path in inputs))
                for row in state['sources']:
                    report = next(item for item in row['prepared']['artifacts'] if item['path'].endswith('.provenance.json'))
                    value = read_json(observer.store.directory / report['path'])
                    check('converter receipt binds actual retained source bytes', value['source_sha256'] == row['sha256'])
                key(dialog.buttons['destination'], wx.WXK_SPACE, 'destination')
            elif name == 'destination' and dialog is not None:
                check('native picker selection populates the exact new destination', dialog.destination.GetValue() == str(output))
                key(dialog.method, wx.WXK_HOME, 'method_home')
            elif name == 'method_home' and dialog is not None:
                key(dialog.method, wx.WXK_DOWN, 'method_one')
            elif name == 'method_one' and dialog is not None:
                key(dialog.method, wx.WXK_DOWN, 'method_two')
            elif name == 'method_two' and dialog is not None:
                check('real native choice keys select blank-line splitting', dialog.method.GetStringSelection() == 'Blank lines')
                key(dialog.buttons['preview'], wx.WXK_SPACE, 'previewed')
            elif name == 'previewed' and dialog is not None and not dialog.pending:
                check('exact boundary and name preview precedes all output writes',
                      dialog.preview is not None and len(dialog.preview.outputs) == 4
                      and 'SHA256' in dialog.review.GetValue() and '[0, ' in dialog.review.GetValue()
                      and not list(output.iterdir()) and observer.get(dialog.identity)['review'] is None)
                key(dialog.buttons['approve'], wx.WXK_SPACE, 'approved')
            elif name == 'approved' and dialog is not None and not dialog.pending:
                state = observer.get(dialog.identity)
                check('explicit keyboard approval saves the exact reviewed plan only',
                      state['status'] == 'reviewed' and state['review_sha256'] == dialog.preview.fingerprint
                      and not list(output.iterdir()))
                key(dialog.buttons['run'], wx.WXK_SPACE, 'completed')
            elif name == 'completed' and dialog is not None and not dialog.pending:
                verify_completed(dialog)
                evidence['before'] = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in output.iterdir()}
                key(dialog.buttons['run'], wx.WXK_SPACE, 'reverified')
            elif name == 'reverified' and dialog is not None and not dialog.pending:
                check('explicit same-job run verifies completed outputs without rewriting them',
                      observer.get(dialog.identity)['status'] == 'complete'
                      and {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in output.iterdir()} == evidence['before'])
                key(dialog.buttons['preview'], wx.WXK_SPACE, 'conflicts')
            elif name == 'conflicts' and dialog is not None and not dialog.pending:
                check('occupied filenames disable native approval without overwriting outputs',
                      dialog.preview is not None and len(dialog.preview.conflicts) == 4
                      and not dialog.buttons['approve'].IsEnabled() and not dialog.buttons['run'].IsEnabled()
                      and {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in output.iterdir()} == evidence['before'])
                key(dialog.buttons['close'], wx.WXK_SPACE, 'closed')
            elif name == 'closed' and dialog is None:
                check('owned modal dialog closed through a real native key', not frame.working)
                check('only explicitly selected picker paths were used', pickers == ['files', 'directory'])
                advance('complete'); timer.Stop(); loop[0].ScheduleExit(0)

        def tick(event=None):
            if errors or in_tick[0]:
                return
            in_tick[0] = True
            try:
                step()
            except Exception:
                abort(traceback.format_exc())
            finally:
                in_tick[0] = False

        try:
            frame = DesktopFrame(library)
            app.SetTopWindow(frame); frame.Show()
            timer = wx.Timer(frame); frame.Bind(wx.EVT_TIMER, tick, timer); timer.Start(40)
            app.MainLoop()
            check('normal native MainLoop completed every document-batch stage', stage[0] == 'complete' and not errors)
        finally:
            try:
                try:
                    if timer is not None:
                        timer.Stop()
                finally:
                    release()
            finally:
                wx.FileDialog, wx.DirDialog = original_files, original_directory
                if frame is not None:
                    frame._closing = True; frame.cancel_event.set()
                    try:
                        frame.executor.shutdown(wait=True, cancel_futures=True)
                    finally:
                        try:
                            frame.operation_progress.close()
                        finally:
                            frame.Destroy(); app.Yield()
        evidence.pop('before', None)
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'source_identity': identities,
            'evidence': evidence, 'source_sha256': source_hashes, 'native_keyboard': 'PASS',
            'native_registered_menu_dispatch': 'PASS', 'actual_isolated_conversion': 'PASS',
            'verified_same_job_rerun': 'PASS', 'native_file_picker': 'NOT_RUN',
            'native_interrupted_resume': 'NOT_RUN', 'speech_quality': 'NOT_RUN',
            'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
