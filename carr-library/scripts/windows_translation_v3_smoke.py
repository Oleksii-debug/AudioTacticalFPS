"""Native installed translation controls with synthetic REST and a temporary OS-vault entry.

No Google call, billing or linguistic-quality claim. Actual native Space/focus,
shared gateway/ledger/job state, OAuth-vault reads and derivative bytes are checked.
Run: python -I -B scripts/windows_translation_v3_smoke.py
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path
import runpy
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
    from carr_library.assistant import profiles, translation, providers
    from carr_library.assistant.credentials import WindowsCredentialVault
    from carr_library.speech.reading import ReadingDocument
    from carr_library.ui import translation_dialog as ui
    contract = runpy.run_path(str(Path(__file__).with_name('verify_operations_acceptance.py')))
    tested_source = contract['source_identity']()
    tested_origins = contract['installed_origins']()

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
    app.SetExitOnFrameDelete(False)
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

    for module in (profiles, translation, providers, ui):
        check('installed module: ' + module.__name__, 'site-packages' in Path(module.__file__).resolve().parts)
    check('native Windows wx', wx.Platform == '__WXMSW__')
    parent = 'projects/123456789/locations/us-central1'
    address = 'https://translation.googleapis.com/v3/' + parent
    glossary = parent + '/glossaries/reviewed-terms'
    original = 'Unchanged synthetic source — Україна &amp; original.'
    pending_response = threading.Event()
    response_entered = threading.Event()
    response_release = threading.Event()
    callback_finished = threading.Event()
    parent_destroy_requested = False

    class SyntheticHTTPS:
        def __init__(self, host, port, **options):
            check('fixed official authority', host == 'translation.googleapis.com' and port in (None, 443))
            self.position = 0
        def request(self, method, path, body, headers):
            self.method = method
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
            if pending_response.is_set() and self.method == 'POST':
                response_entered.set()
                if not response_release.wait(10):
                    raise TimeoutError('Synthetic response was not released by the native lifetime probe.')
            self.status = 200
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, size):
            block = self.raw[self.position:self.position + size]; self.position += len(block)
            return block
        def close(self): pass

    actual_connection, actual_message, actual_directory = http.client.HTTPSConnection, wx.MessageBox, wx.DirDialog
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
            http.client.HTTPSConnection = SyntheticHTTPS
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
                callback_finished.clear()
                future = executor.submit(callback)
                def finish():
                    frame.working = False
                    try:
                        try:
                            result = future.result()
                        except Exception as error:
                            failed(error)
                        else:
                            done(result)
                    finally:
                        callback_finished.set()
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
                    account = find('Google API account and edition')
                    check('explicit Advanced edition is selected', 'google-translation-v3' in account.GetStringSelection())
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
                    check('preview contains exact endpoint and glossary', address in status.GetValue() and glossary in status.GetValue())
                    press(find('Translate / resume'))
                    pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(), 'Translation completion')
                    check('translation completion status focus', wx.Window.FindFocus() is status)
                    check('confirmation binds exact glossary', glossary in approvals[-1] and address in approvals[-1])
                    press(find('Export'))
                    pump(lambda: not frame.working and status.GetValue().startswith('Saved new files in:'), 'Derivative export')
                    output, = root.glob('Translation-*')
                    reading = ReadingDocument.load(output / 'translation.reading.json')
                    report = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
                    check('retained original and non-authoritative derivative', reading.source_text == original and not reading.source_span(0, len(reading.text)).exact)
                    check('actual exported hashes', all(sha256((output / name).read_bytes()).hexdigest() == checksum for name, checksum in report['files'].items()))
                    check('verified glossary provenance', report['chunks'][0]['glossary_config']['glossary'] == glossary)
                    check('original selected text unchanged', selected.GetValue() == original)
                    before = len(calls)
                    check('saved glossary control remains read-only', not glossary_input.IsEnabled())
                    glossary_input.ChangeValue(glossary + '-changed-after-review')
                    press(find('Translate / resume'))
                    pump(lambda: not frame.working and 'Visible settings differ' in status.GetValue(), 'Saved glossary scope rejection')
                    check('changed saved glossary never transmits', len(calls) == before)
                    check('saved scope rejection has readable status focus', wx.Window.FindFocus() is status)
                    press(find('Close'))
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def exercise_destroy():
                nonlocal parent_destroy_requested
                dialog = None
                try:
                    dialog = next(window for window in wx.GetTopLevelWindows()
                                  if window.GetName() == 'Translation workspace')
                    owner_id = dialog.GetId()
                    destroy_seen = threading.Event()
                    def observed_destroy(event):
                        if event.GetId() == owner_id:
                            destroy_seen.set()
                        event.Skip()
                    dialog.Bind(wx.EVT_WINDOW_DESTROY, observed_destroy)
                    def find(name):
                        result = wx.FindWindowByName(name, dialog)
                        check('named pending control: ' + name, result is not None)
                        return result
                    find('Source language code; auto detects').ChangeValue('en')
                    find('Target language code').ChangeValue('uk')
                    find('Advanced v3 glossary resource; blank for none').ChangeValue(glossary)
                    find('Selected text; originals remain unchanged').ChangeValue(original + ' Pending response.')
                    find('Maximum approved budget in USD; no default price').ChangeValue('1')
                    press(find('Preview'))
                    before = len(calls)
                    pending_response.set()
                    press(find('Translate / resume'))
                    pump(response_entered.is_set, 'Actual worker reached synthetic pending response')
                    parent_destroy_requested = True
                    frame.Destroy()  # No translation Close handler is invoked.
                    pump(destroy_seen.is_set, 'Actual native owner destruction')
                    response_release.set()
                    pump(callback_finished.is_set, 'Pending worker completion after native destruction')
                    service = translation.TranslationService(library.root / 'state' / 'translation', library=library)
                    jobs = service.list()
                    stopped = [job for job in jobs if job['status'] == 'paused']
                    complete = [job for job in jobs if job['status'] == 'complete']
                    check('destroyed pending owner preserves prior completed translation', len(complete) == 1)
                    check('destroyed pending owner publishes no late accepted result',
                          len(stopped) == 1 and stopped[0]['receipts'] == [] and len(calls) == before + 1)
                    job = stopped[0]
                    usage = service.gateway.ledger.get(job['authorization']['id'])['operations']
                    check('submitted cancellation remains an uncertain charge',
                          usage[job['id'] + '_0']['status'] == 'uncertain')
                    check('no callback reached a deleted native control', not errors)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    response_release.set()
                    if dialog is not None and not parent_destroy_requested and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def opened():
                try:
                    for exercise_case in (exercise, exercise_destroy):
                        wx.CallLater(200, exercise_case)
                        ui.show_translation(frame)
                        if errors:
                            break
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    app.ExitMainLoop()
            wx.CallAfter(opened)
            app.MainLoop()
        finally:
            response_release.set()
            executor.shutdown(wait=True, cancel_futures=True)
            http.client.HTTPSConnection, wx.MessageBox, wx.DirDialog = actual_connection, actual_message, actual_directory
            if reference:
                vault.delete('native_translation', reference)
            if frame is not None and not parent_destroy_requested:
                frame.Destroy()
            if native_handle is not None:
                native_handle.close()
    if errors:
        raise RuntimeError('\n'.join(errors))
    check('exact source and installed origins are unchanged',
          contract['source_identity']() == tested_source and contract['installed_origins']() == tested_origins)
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'synthetic_rest_calls': len(calls),
        'source': tested_source, 'installed_origins': tested_origins, 'source_and_origins_unchanged': True,
        'native_keyboard': 'PASS', 'native_parent_destroy_pending_translation': 'PASS',
        'live_google_api': 'WAITING_INPUT', 'billing_and_quality': 'WAITING_INPUT',
        'native_approval_and_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
