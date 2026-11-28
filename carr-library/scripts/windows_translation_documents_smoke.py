"""Installed native local/document translation controls with synthetic transport.

The actual wx controls, host background queue, source reader, approved job store,
ledger and derivative bytes execute. Picker/consent answers and HTTP response
bytes are controlled. No local engine/model inference, acoustics, NVDA or
135-language qualification is implied.
"""
import argparse
from hashlib import sha256
import http.client
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows wx is required.'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1] / 'src')
    args = parser.parse_args()
    import ctypes
    from ctypes import wintypes
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library import progress as progress_module
    from carr_library.assistant import profiles, providers, translation, translation_local, translation_documents
    from carr_library.ui import translation_dialog, translation_documents as document_ui
    from carr_library.ui import desktop_app, worker_lifetime, progress_panel
    from carr_library.speech.reading import ReadingDocument

    checks, errors, calls, approvals, worker_contexts = [], [], [], [], []
    origins = {}
    ui_thread = threading.get_ident()
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)
    for module in (profiles, providers, translation, translation_local, translation_documents,
                   translation_dialog, document_ui, desktop_app, worker_lifetime,
                   progress_module, progress_panel):
        actual = Path(module.__file__).resolve()
        expected = args.source_root / (module.__name__.replace('.', '/') + '.py')
        check('installed exact source: ' + module.__name__,
            actual.is_relative_to(Path(sys.prefix).resolve())
            and not actual.is_relative_to(args.source_root.resolve())
            and 'site-packages' in actual.parts and actual.read_bytes() == expected.read_bytes())
        origins[module.__name__] = {'path': str(actual), 'sha256': sha256(actual.read_bytes()).hexdigest()}

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    app = App(False)
    check('native Windows wx', wx.Platform == '__WXMSW__')
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    def pump(predicate, label, timeout=20):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError(errors[-1])
            if monotonic() > deadline:
                raise TimeoutError(label)
            app.Yield()
            sleep(.01)
    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(),
             'Native foreground focus')
        check('native Space down: ' + control.GetName(), simulator.KeyDown(wx.WXK_SPACE))
        app.Yield()
        sleep(.04)
        check('native Space up: ' + control.GetName(), simulator.KeyUp(wx.WXK_SPACE))
        app.Yield()

    class SyntheticHTTP:
        def __init__(self, host, port, **options):
            if host != '127.0.0.1' or port != 55123:
                raise AssertionError('Only the explicitly selected synthetic loopback authority may be used.')
            self.position = 0
        def request(self, method, route, body=None, headers=None):
            task = progress_module.current_progress()
            if threading.get_ident() == ui_thread or task is None:
                raise AssertionError('Provider work must use the actual host worker and bound ProgressHub task.')
            worker_contexts.append((threading.get_ident(), task.operation_id))
            if any(key.lower() in ('authorization', 'x-goog-api-key') for key in headers):
                raise AssertionError('Local translation cannot transmit credentials.')
            calls.append((method, route))
            if method == 'GET':
                result = [{'code': 'en', 'name': 'English', 'targets': ['uk']},
                          {'code': 'uk', 'name': 'Ukrainian', 'targets': []}]
            else:
                payload = json.loads(body)
                if route != '/translate' or payload['format'] != 'text':
                    raise AssertionError('The selected local protocol changed.')
                result = {'translatedText': 'Похідний контроль: ' + payload['q']}
                if payload['source'] == 'auto':
                    result['detectedLanguage'] = {'language': 'en', 'confidence': 95}
            self.raw = json.dumps(result, ensure_ascii=False).encode()
        def getresponse(self):
            self.status = 200
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, size):
            raw = self.raw[self.position:self.position + size]
            self.position += len(raw)
            return raw
        def close(self):
            pass

    finished = threading.Event()
    def watchdog():
        if not finished.wait(180):
            print('FAIL: native local/document translation watchdog', flush=True)
            os._exit(2)
    threading.Thread(target=watchdog, daemon=True, name='translation-probe-watchdog').start()
    actual = (http.client.HTTPConnection, wx.MessageBox, wx.SingleChoiceDialog, wx.TextEntryDialog, wx.DirDialog)
    frame = None
    with TemporaryDirectory(prefix='resonance-translation-documents-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'private-library')
            workspace = library.create_workspace('Native document translation')
            entries = [library.add_text(workspace, 'Selected book ' + str(i),
                'Synthetic source ' + str(i) + '\n\nUnchanged second paragraph 🙂.') for i in range(2)]
            original_bytes = [(library.assets.resolve(row['asset']), library.assets.resolve(row['asset']).read_bytes())
                              for row in entries]
            p = profiles.ProviderProfile('native_local_translation', 'Synthetic local API', 'libretranslate-local',
                'http://127.0.0.1:55123', 'synthetic-protocol-only',
                ('translation.text', 'translation.languages'), local=True, enabled=True)
            profiles.ProfileStore(library.root / 'state/providers').save(p)
            http.client.HTTPConnection = SyntheticHTTP
            def approve(message, *args, **kwargs):
                approvals.append(message)
                return wx.YES
            wx.MessageBox = approve
            choices = iter((0, 1))
            class SelectedBook:
                def __init__(self, *args, **kwargs):
                    self.index = next(choices)
                def ShowModal(self):
                    return wx.ID_OK
                def GetSelection(self):
                    return self.index
                def Destroy(self):
                    pass
            class SourceRange:
                def __init__(self, *args, **kwargs):
                    pass
                def ShowModal(self):
                    return wx.ID_OK
                def GetValue(self):
                    return '*'
                def Destroy(self):
                    pass
            class Directory(SourceRange):
                def GetPath(self):
                    return str(root)
            wx.SingleChoiceDialog, wx.TextEntryDialog, wx.DirDialog = SelectedBook, SourceRange, Directory
            frame = desktop_app.DesktopFrame(library)
            frame.Show()
            app.SetTopWindow(frame)
            check('actual shared desktop executor', isinstance(frame.executor, worker_lifetime.DesktopExecutor))
            check('actual unmodified host run_job', frame.run_job.__func__ is desktop_app.DesktopFrame.run_job)
            check('actual shared ProgressHub', isinstance(frame.progress_panel.hub, progress_module.ProgressHub))
            panel = frame.book.GetPage(1)
            check('actual retained catalog and workspace', frame.active == (workspace, None)
                  and panel.ready and panel.workspace == workspace
                  and set(row['edition'] for row in entries).issubset(panel.rows))
            commands = {command.command_id for command in frame.keyboard_commands.registry.commands('main')}
            check('document translation uses the shared keyboard registry', 'tools.translation_documents' in commands)
            def find(dialog, name):
                control = wx.FindWindowByName(name, dialog)
                if control is None:
                    raise AssertionError('Missing native control: ' + name)
                return control

            def local_phase():
                dialog = None
                try:
                    dialog = next(w for w in wx.GetTopLevelWindows() if w.GetName() == 'Translation workspace')
                    check('local translation native modal', dialog.IsModal())
                    status = find(dialog, 'Preview, supported languages and job status')
                    find(dialog, 'Selected text; originals remain unchanged').ChangeValue('Explicit pasted local source.')
                    find(dialog, 'Source language code; auto detects').ChangeValue('en')
                    check('local external budget is zero and readonly',
                        find(dialog, 'Maximum approved budget in USD; no default price').GetValue() == '0'
                        and not find(dialog, 'Maximum approved budget in USD; no default price').IsEnabled())
                    press(find(dialog, 'Fetch languages'))
                    pump(lambda: not frame.working and 'Provider-declared' in status.GetValue(), 'Local language completion')
                    press(find(dialog, 'Preview'))
                    check('local preview includes unqualified runtime distinction',
                          'model identity' in status.GetValue() and wx.Window.FindFocus() is status)
                    press(find(dialog, 'Translate / resume'))
                    pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(), 'Local selected-text completion')
                    check('local completion retains native status focus', wx.Window.FindFocus() is status)
                    press(find(dialog, 'Close'))
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def document_phase():
                dialog = None
                try:
                    dialog = next(w for w in wx.GetTopLevelWindows() if w.GetName() == 'Document translation workstation')
                    check('document workstation native modal', dialog.IsModal())
                    status = find(dialog, 'Document translation review and saved job IDs')
                    listing = find(dialog, 'Explicitly selected retained books or ranges')
                    find(dialog, 'Total approved batch budget in USD; local service requires 0').ChangeValue('0')
                    find(dialog, 'Source language code; auto detects').ChangeValue('en')
                    before = len(calls)
                    for count in (1, 2):
                        press(find(dialog, 'Add book or range'))
                        pump(lambda: not frame.working and listing.GetCount() == count, 'Retained document selection')
                    check('source capture makes no provider calls', len(calls) == before)
                    press(find(dialog, 'Preview document batch'))
                    pump(lambda: not frame.working and 'review_sha256' in status.GetValue(), 'Batch preview')
                    check('batch preview has exact four paragraph requests', '"calls": 4' in status.GetValue())
                    press(find(dialog, 'Approve and save jobs'))
                    pump(lambda: not frame.working and '"status": "prepared"' in status.GetValue(), 'Batch preparation')
                    check('preparation has no provider calls', len(calls) == before)
                    press(find(dialog, 'Run / resume saved jobs'))
                    pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(), 'Actual saved batch completion')
                    check('exact approved paragraph calls', len(calls) == before + 4)
                    check('actual host worker and progress bind every protocol call',
                          len(worker_contexts) == len(calls) == 6
                          and all(thread != ui_thread and identity for thread, identity in worker_contexts))
                    check('batch completion retains native status focus', wx.Window.FindFocus() is status)
                    press(find(dialog, 'Export saved documents'))
                    pump(lambda: not frame.working and '"exports"' in status.GetValue(), 'Batch publication')
                    receipt = json.loads(status.GetValue())
                    check('two actual published source derivatives', len(receipt['exports']) == 2)
                    for row in receipt['exports']:
                        output = Path(row['path'])
                        reading = ReadingDocument.load(output / 'translation.reading.json')
                        manifest = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
                        mapping = json.loads((output / 'translation-structure.json').read_text(encoding='utf-8'))
                        check('paragraph inventory and original lineage: ' + row['job'],
                            len(mapping['paragraphs']) == 2 and reading.source_anchor == manifest['source_anchor']
                            and manifest['quotation_authority'] is False)
                        check('actual structured export hashes: ' + row['job'],
                            all(sha256((output / name).read_bytes()).hexdigest() == digest
                                for name, digest in manifest['files'].items()))
                    check('selected original bytes remain unchanged', all(path.read_bytes() == raw for path, raw in original_bytes))
                    check('batch export retains native status focus', wx.Window.FindFocus() is status)
                    press(find(dialog, 'Close'))
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def opened():
                try:
                    wx.CallLater(200, local_phase)
                    translation_dialog.show_translation(frame, local=True)
                    if not errors:
                        wx.CallLater(200, document_phase)
                        document_ui.show_translation_documents(frame)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    app.ExitMainLoop()
            wx.CallAfter(opened)
            app.MainLoop()
        finally:
            try:
                if frame is not None:
                    frame.cancel_event.set()
                    frame._closing = True
                    try:
                        frame.executor.shutdown(wait=True, cancel_futures=True)
                        check('actual host shutdown has no retained cleanup failure', not frame.executor.cleanup_failures)
                    finally:
                        try:
                            frame.progress_panel.close()
                        finally:
                            frame.Destroy()
                app.Yield()
            finally:
                http.client.HTTPConnection, wx.MessageBox, wx.SingleChoiceDialog, wx.TextEntryDialog, wx.DirDialog = actual
                if native_handle is not None:
                    native_handle.close()
    # The hard deadline remains live through worker/native/temporary cleanup.
    finished.set()
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'check_count': len(checks),
        'source_origins': origins, 'host_jobs': 'actual DesktopFrame.run_job / DesktopExecutor / ProgressHub',
        'native_keyboard': 'PASS',
        'local_and_document_controls': 'PASS', 'synthetic_protocol_calls': len(calls),
        'controlled_consent_and_pickers': 'NOT_RUN', 'actual_local_model_inference': 'NOT_RUN',
        'offline_server_egress': 'NOT_RUN', 'translation_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
