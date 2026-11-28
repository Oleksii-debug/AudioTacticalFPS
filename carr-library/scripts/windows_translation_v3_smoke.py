"""Native installed translation controls with synthetic REST and a temporary OS-vault entry.

No Google call, billing or linguistic-quality claim. Actual native Space/focus,
shared gateway/ledger/job state, OAuth-vault reads and derivative bytes are checked.
Run: python -I -B scripts/windows_translation_v3_smoke.py --expect-installed --output result.json
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import faulthandler
from hashlib import sha256
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep, time
import traceback


def run_native(source, report, checkpoint):
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import profiles, translation, providers
    from carr_library.assistant.credentials import WindowsCredentialVault
    from carr_library.speech.reading import ReadingDocument
    from carr_library.ui import translation_dialog as ui

    checks, errors, calls = report['checks'], [], []
    check_lock = threading.RLock()
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        with check_lock:
            checks.append(label)
            checkpoint(label)

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

    modules = (profiles, translation, providers, ui)
    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in modules}
    if source is None:
        from importlib.metadata import distribution
        package = Path(distribution('universal-research-library').locate_file('carr_library')).resolve(strict=True)
        for name, path in origins.items():
            check('installed module: ' + name, Path(path).is_relative_to(package))
    check('native Windows wx', wx.Platform == '__WXMSW__')
    parent = 'projects/123456789/locations/us-central1'
    address = 'https://translation.googleapis.com/v3/' + parent
    glossary = parent + '/glossaries/reviewed-terms'
    original = 'Unchanged synthetic source — Україна &amp; original.'

    class SyntheticHTTPS:
        def __init__(self, host, port, **options):
            check('fixed official authority', host == 'translation.googleapis.com' and port in (None, 443))
            self.position = 0
        def request(self, method, path, body, headers):
            calls.append((method, path))
            check('OAuth bearer, never API-key header', headers.get('Authorization', '').startswith('Bearer SYNTHETIC_') and 'X-goog-api-key' not in headers)
            if method == 'GET':
                result = {'languages': [{'languageCode': language, 'displayName': language, 'supportSource': True, 'supportTarget': True}
                                       for language in ('en', 'uk')]}
            else:
                request = json.loads(body)
                result = {'translations': [{'translatedText': 'Unused ordinary result', 'model': request['model']}],
                    'glossaryTranslations': [{'translatedText': 'Похідний переклад &amp; ' + request['contents'][0],
                        'model': request['model'], 'glossaryConfig': request['glossaryConfig']}]}
            self.raw = json.dumps(result, ensure_ascii=False).encode('utf-8')
        def getresponse(self):
            self.status = 200
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, size):
            block = self.raw[self.position:self.position + size]; self.position += len(block)
            return block
        def close(self): pass

    def synthetic_transport(self, profile, route, body, headers, *, method='POST', **kwargs):
        # Test-only network replacement. The real gateway, vault, request
        # hashing, decoded ledger binding and response checks remain active.
        from urllib.parse import urlsplit
        parsed = urlsplit(profile.endpoint)
        connection = SyntheticHTTPS(parsed.hostname, parsed.port)
        try:
            connection.request(method, route, body, headers)
            response = connection.getresponse()
            return providers.HTTPResult(response.getheader('Content-Type'),
                response.read(kwargs.get('maximum_bytes', 1024 * 1024) + 1), response.status)
        finally:
            connection.close()

    actual_transport, actual_message, actual_directory = providers.HTTPTransport.request, wx.MessageBox, wx.DirDialog
    vault, reference, frame = WindowsCredentialVault(), None, None
    executor = ThreadPoolExecutor(max_workers=1)
    with TemporaryDirectory(prefix='resonance-translation-native-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'private library')
            profile = profiles.ProviderProfile('native_translation', 'Synthetic v3 account', 'google-translation-v3',
                address, 'general/nmt', capabilities=('translation.text', 'translation.languages'), enabled=True)
            store = profiles.ProfileStore(library.root / 'state' / 'providers')
            profile = profiles.save_google_token(store, vault, profile, 'SYNTHETIC_NATIVE_TOKEN', time() + 1800)
            reference = profile.credential_ref
            providers.HTTPTransport.request = synthetic_transport
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
            frame = wx.Frame(None, title='Native Advanced v3 translation acceptance')
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
                    find('Source language code; auto detects').ChangeValue('en')
                    find('Target language code').ChangeValue('uk')
                    glossary_input = find('Advanced v3 glossary resource; blank for none')
                    glossary_input.ChangeValue(glossary)
                    selected = find('Selected text; originals remain unchanged'); selected.ChangeValue(original)
                    find('Maximum approved budget in USD; no default price').ChangeValue('1')
                    press(find('Fetch languages'))
                    pump(lambda: not frame.working and 'source: True' in status.GetValue(), 'Language discovery completion')
                    check('language completion retains readable native status focus', wx.Window.FindFocus() is status)
                    press(find('Preview'))
                    pump(lambda: not frame.working and 'No request sent' in status.GetValue(), 'Asynchronous preview completion')
                    check('preview contains exact endpoint and glossary', address in status.GetValue() and glossary in status.GetValue())
                    press(find('Translate / resume'))
                    pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(), 'Translation completion')
                    check('translation completion status focus', wx.Window.FindFocus() is status)
                    check('confirmation binds exact glossary', glossary in approvals[-1] and address in approvals[-1])
                    press(find('Export'))
                    pump(lambda: not frame.working and status.GetValue().startswith('Saved new files in:'), 'Derivative export')
                    output, = root.glob('Translation-*')
                    reading = ReadingDocument.load(output / 'translation.reading.json')
                    receipt = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
                    check('retained original and non-authoritative derivative', reading.source_text == original and not reading.source_span(0, len(reading.text)).exact)
                    check('actual exported hashes', all(sha256((output / name).read_bytes()).hexdigest() == checksum for name, checksum in receipt['files'].items()))
                    check('verified glossary provenance', receipt['chunks'][0]['glossary_config']['glossary'] == glossary)
                    check('independent decoded translation receipt is retained',
                          receipt['chunks'][0]['decoded_receipt_integrity'] == 'decoded-ledger-bound')
                    check('original selected text unchanged', selected.GetValue() == original)
                    before = len(calls)
                    press(find('Preview'))
                    pump(lambda: not frame.working and 'No request sent' in status.GetValue(), 'Second asynchronous preview')
                    glossary_input.ChangeValue(glossary + '-changed-after-review')
                    press(find('Translate / resume'))
                    pump(lambda: 'Preview this exact' in status.GetValue(), 'Changed glossary rejection')
                    check('changed glossary requires fresh review without transfer',
                          len(calls) == before and not frame.working)
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
            providers.HTTPTransport.request, wx.MessageBox, wx.DirDialog = actual_transport, actual_message, actual_directory
            if reference:
                vault.delete('native_translation', reference)
            if frame is not None:
                frame.Destroy()
    if errors:
        raise RuntimeError('\n'.join(errors))
    report.update(synthetic_rest_calls=len(calls), native_keyboard='PASS',
                  live_google_api='NOT_RUN', billing_and_quality='NOT_RUN',
                  native_approval_and_file_pickers='NOT_RUN', manual_nvda='NOT_RUN',
                  origins=origins, source_override=source is not None,
                  module_sha256={name: sha256(Path(path).read_bytes()).hexdigest()
                                 for name, path in origins.items()})


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    args = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'incomplete', 'checks': [], 'manual_nvda': 'NOT_RUN'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()
    def checkpoint(phase):
        with lock:
            report['phase'] = phase
            args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                                   encoding='utf-8')
            print('TRANSLATION_V3_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the native Advanced translation probe with Windows Python -I -B.')
    if args.expect_installed and args.source is not None:
        raise ValueError('Installed acceptance must not inject checkout source.')
    faulthandler.dump_traceback_later(180, repeat=False, exit=True)
    handle = None
    try:
        source = args.source.resolve(strict=True) if args.source is not None else None
        if source is not None:
            sys.path.insert(0, str(source))
            sys.path.insert(0, str(source / 'src'))
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        run_native(source, report, checkpoint)
        report['status'] = 'PASS'
        checkpoint('complete')
        return 0
    except BaseException:
        report['failure'] = traceback.format_exc()[-32000:]
        checkpoint('failed')
        raise
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
