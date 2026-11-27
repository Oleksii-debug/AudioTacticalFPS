"""Native installed local-translation controls with synthetic protocol responses.

Actual Windows Space/focus, shared jobs/ledger, exact approval and derivative
bytes are checked. No LibreTranslate service/model, external network, account,
linguistic quality or offline-egress qualification is claimed.
Run: python -I -B scripts/windows_translation_local_smoke.py
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep, time
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows wx and Credential Manager are required.'}))
        return 77
    import ctypes
    from ctypes import wintypes
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import profiles, translation, providers, translation_batch, translation_local
    from carr_library.speech.reading import ReadingDocument
    from carr_library.ui import translation_dialog as ui
    from carr_library.ui import translation_batch_dialog as batch_ui

    checks, errors, calls = [], [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    app = App(False)
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
        name = control.GetName()
        top = wx.GetTopLevelParent(control)
        top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(), 'Native foreground focus')
        check('foreground focus: ' + name, wx.Window.FindFocus() is control)
        check('native Space down: ' + name, simulator.KeyDown(wx.WXK_SPACE))
        app.Yield(); sleep(.04)
        check('native Space up: ' + name, simulator.KeyUp(wx.WXK_SPACE))
        app.Yield()

    module_hashes = {}
    for module in (profiles, translation, providers, translation_batch, translation_local, ui, batch_ui):
        source = Path(module.__file__).resolve(strict=True)
        check('installed module: ' + module.__name__, 'site-packages' in source.parts)
        module_hashes[module.__name__] = sha256(source.read_bytes()).hexdigest()
    check('native Windows wx', wx.Platform == '__WXMSW__')
    address = 'http://127.0.0.1:55123'
    original = 'Unchanged synthetic source — Україна &amp; original.'

    class SyntheticLocal:
        def __init__(self, target, *, local, deadline, cancelled):
            check('fixed explicit loopback authority', target.hostname == '127.0.0.1'
                  and target.port == 55123 and local is True)
            self.position = 0
            self.deadline, self.cancelled = deadline, cancelled
        def check(self):
            providers._check_request(self.deadline, self.cancelled)
        def request(self, method, path, body=None, headers=None):
            check('no credential header', not any(key.lower() in (
                'authorization', 'x-goog-api-key', 'xi-api-key') for key in headers))
            calls.append((method, path))
            if method == 'GET':
                check('explicit local language route', path == '/languages' and body is None)
                result = [{'code': 'en', 'name': 'English', 'targets': ['uk']},
                          {'code': 'uk', 'name': 'Ukrainian', 'targets': []}]
            else:
                check('explicit local text route', method == 'POST' and path == '/translate')
                request = json.loads(body)
                check('only reviewed local protocol fields', set(request) == {'q', 'source', 'target', 'format'}
                      and request['format'] == 'text' and request['target'] == 'uk')
                result = {'translatedText': 'Похідний переклад &amp; ' + request['q']}
                if request['source'] == 'auto':
                    result['detectedLanguage'] = {'language': 'en', 'confidence': 99}
            self.raw = json.dumps(result, ensure_ascii=False).encode('utf-8')
        def getresponse(self):
            self.status = 200
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, size):
            block = self.raw[self.position:self.position + size]
            self.position += len(block)
            return block
        def close(self):
            pass

    class NoVault:
        def get(self, *args, **kwargs):
            raise AssertionError('Local translation cannot read credentials.')

    actual_connection, actual_message, actual_directory = providers._Connection, wx.MessageBox, wx.DirDialog
    actual_dns, actual_vault = providers.socket.getaddrinfo, providers.WindowsCredentialVault
    def no_dns(*args, **kwargs):
        raise AssertionError('The native local translation smoke must not resolve or contact any service.')
    frame = None
    executor = ThreadPoolExecutor(max_workers=1)
    with TemporaryDirectory(prefix='resonance-local-translation-native-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'private library')
            profile = profiles.ProviderProfile('native_local_translation', 'Explicit synthetic local translator',
                'libretranslate-local', address, 'synthetic-protocol-only',
                capabilities=('translation.text', 'translation.languages'), local=True, enabled=True)
            store = profiles.ProfileStore(library.root / 'state' / 'providers')
            store.save(profile)
            providers._Connection = SyntheticLocal
            providers.WindowsCredentialVault = NoVault
            providers.socket.getaddrinfo = no_dns
            approvals = []
            def approve(message, *args, **kwargs):
                approvals.append(message)
                return wx.YES
            wx.MessageBox = approve
            class Directory:
                def __init__(self, *args, **kwargs): pass
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(root)
                def Destroy(self): pass
            wx.DirDialog = Directory
            frame = wx.Frame(None, title='Native local translation acceptance')
            frame.library = library
            frame.working = frame._closing = False
            frame.cancel_event = threading.Event()
            def run_job(title, callback, done, failed):
                frame.working = True; frame.cancel_event.clear()
                future = executor.submit(callback)
                def finish():
                    frame.working = False
                    try:
                        result = future.result()
                    except Exception as error:
                        failed(error)
                    else:
                        done(result)
                future.add_done_callback(lambda unused: wx.CallAfter(finish))
                return True
            frame.run_job = run_job
            frame.Show(); app.SetTopWindow(frame)

            def exercise():
                dialog = None
                try:
                    dialog = next(window for window in wx.GetTopLevelWindows() if window.GetName() == 'Translation workspace')
                    check('real modal translation workspace', dialog.IsModal())
                    def find(name):
                        result = wx.FindWindowByName(name, dialog)
                        check('named native control: ' + name, result is not None)
                        return result
                    status = find('Preview, supported languages and job status')
                    find('Source language code; auto detects').ChangeValue('auto')
                    check('local profile is visible', find('Translation provider profile').GetStringSelection().startswith(profile.name))
                    find('Target language code').ChangeValue('uk')
                    glossary_input = find('Advanced v3 glossary resource; blank for none')
                    glossary_input.ChangeValue('')
                    selected = find('Selected text; originals remain unchanged'); selected.ChangeValue(original)
                    find('Maximum approved budget in USD; no default price').ChangeValue('0')
                    press(find('Fetch languages'))
                    pump(lambda: not frame.working and 'targets: uk' in status.GetValue(), 'Language discovery completion')
                    check('language completion retains readable native status focus', wx.Window.FindFocus() is status)
                    press(find('Preview'))
                    check('preview contains exact endpoint and zero external cost', address in status.GetValue()
                          and '"estimated_cost": "0"' in status.GetValue())
                    press(find('Translate / resume'))
                    pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(), 'Translation completion')
                    check('translation completion status focus', wx.Window.FindFocus() is status)
                    check('confirmation binds explicit local transfer and endpoint', 'explicitly selected local' in approvals[-1]
                          and address in approvals[-1] and 'No external charge' in approvals[-1])
                    press(find('Export'))
                    pump(lambda: not frame.working and status.GetValue().startswith('Saved new files in:'), 'Derivative export')
                    output, = root.glob('Translation-*')
                    reading = ReadingDocument.load(output / 'translation.reading.json')
                    report = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
                    check('retained original and non-authoritative derivative', reading.source_text == original and not reading.source_span(0, len(reading.text)).exact)
                    check('actual exported hashes', all(sha256((output / name).read_bytes()).hexdigest() == checksum for name, checksum in report['files'].items()))
                    check('verified local decoded receipt', report['chunks'][0]['detected_source'] == 'en'
                          and len(report['chunks'][0]['decoded_translation_sha256']) == 64
                          and report['local_runtime_identity'] == 'not attested by the local API')
                    check('original selected text unchanged', selected.GetValue() == original)
                    before = len(calls)
                    press(find('Preview'))
                    glossary_input.ChangeValue('unsupported-local-glossary')
                    press(find('Translate / resume'))
                    check('changed local options require fresh review without transfer', 'Preview this exact' in status.GetValue() and len(calls) == before)
                    glossary_input.ChangeValue('')
                    selected.ChangeValue(original)
                    press(find('Add current source to batch'))
                    requested_second = 'Second explicitly reviewed document.\r\nУкраїнський оригінал.'
                    selected.ChangeValue(requested_second)
                    second_original = selected.GetValue()
                    check('native multiline batch selection retains visible Unicode lines',
                          second_original.splitlines() == requested_second.splitlines())
                    press(find('Add current source to batch'))
                    before_batch = len(calls)
                    batch_checks = []
                    def exercise_batch():
                        child = None
                        try:
                            child = next(window for window in wx.GetTopLevelWindows()
                                         if window.GetName() == 'Translation document batch')
                            check('real modal document batch', child.IsModal())
                            def batch_control(name):
                                control = wx.FindWindowByName(name, child)
                                check('named native batch control: ' + name, control is not None)
                                return control
                            batch_status = batch_control('Translation batch review and existing checkpoints')
                            check('two explicit immutable document rows',
                                  batch_control('Ordered translation documents').GetCount() == 2)
                            press(batch_control('Review entire translation batch'))
                            pump(lambda: not frame.working and 'REVIEW ONLY' in batch_status.GetValue(), 'Batch review')
                            check('batch review makes no provider call', len(calls) == before_batch)
                            check('batch review retains native status focus', wx.Window.FindFocus() is batch_status)
                            check('batch review contains exact provider and both sources',
                                  address in batch_status.GetValue() and '"documents": 2' in batch_status.GetValue())
                            press(batch_control('Prepare approved batch'))
                            pump(lambda: not frame.working and 'PREPARED' in batch_status.GetValue(), 'Batch preparation')
                            check('preparation starts no translation', len(calls) == before_batch)
                            check('exact native batch confirmation includes transfer and total reservation',
                                  'these 2 documents' in approvals[-1] and '0 USD' in approvals[-1]
                                  and 'Explicit local profiles' in approvals[-1] and address in approvals[-1])
                            check('prepared batch order is immutable', not batch_control('Move document up').IsEnabled())
                            batch_service = translation_batch.TranslationBatchService(
                                translation.TranslationService(library.root / 'state' / 'translation', library=library))
                            saved, = batch_service.list()
                            check('ordinary child jobs are queued', saved['status'] == 'queued' and saved['total'] == 2)
                            press(batch_control('Run or resume batch'))
                            pump(lambda: not frame.working and '"status": "complete"' in batch_status.GetValue(), 'Batch translation')
                            check('batch made exactly one bounded request per short document', len(calls) == before_batch + 2)
                            accepted = len(calls)
                            press(batch_control('Run or resume batch'))
                            pump(lambda: not frame.working, 'Verified batch reuse')
                            check('completed batch makes no repeat local request', len(calls) == accepted)
                            press(batch_control('Export completed batch'))
                            pump(lambda: not frame.working and batch_status.GetValue().startswith('Saved new batch files in:'),
                                 'Batch export')
                            exported, = root.glob('TranslationBatch-*')
                            for index, expected in enumerate((original, second_original), 1):
                                child_output = exported / f'{index:04d}-translation'
                                check(f'batch original bytes {index}', (child_output / 'original.txt').read_bytes()
                                      == expected.encode('utf-8'))
                                copy = ReadingDocument.load(child_output / 'translation.reading.json')
                                check(f'batch derivative has no quotation authority {index}',
                                      copy.source_text == expected and not copy.source_span(0, len(copy.text)).exact)
                            batch_report = json.loads((exported / 'batch-provenance.json').read_bytes())
                            check('batch provenance binds original child jobs', batch_report['batch_id'] == saved['id']
                                  and [item['job_id'] for item in batch_report['items']]
                                      == [item['id'] for item in saved['items']])
                            check('batch export completion focus', wx.Window.FindFocus() is batch_status)
                            batch_checks.append(True)
                            press(batch_control('Close batch'))
                        except Exception:
                            errors.append(traceback.format_exc())
                            if child is not None and child.IsModal():
                                child.EndModal(wx.ID_CANCEL)
                    wx.CallLater(200, exercise_batch)
                    press(find('Document batches...'))
                    check('native batch scenario completed', batch_checks == [True])
                    check('batch collection leaves current original unchanged', selected.GetValue() == second_original)
                    press(find('Close'))
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def opened():
                wx.CallLater(200, exercise)
                try:
                    ui.show_translation(frame)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    app.ExitMainLoop()
            wx.CallAfter(opened)
            app.MainLoop()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            providers._Connection, wx.MessageBox, wx.DirDialog = actual_connection, actual_message, actual_directory
            providers.socket.getaddrinfo = actual_dns
            providers.WindowsCredentialVault = actual_vault
            if frame is not None:
                frame.Destroy()
            if native_handle is not None:
                native_handle.close()
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'synthetic_rest_calls': len(calls),
        'native_keyboard': 'PASS', 'live_libretranslate_models': 'NOT_RUN', 'linguistic_quality_and_egress': 'NOT_RUN',
        'module_sha256': module_hashes,
        'native_approval_and_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
