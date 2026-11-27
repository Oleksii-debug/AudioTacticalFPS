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

    for module in (profiles, translation, providers, ui):
        check('installed module: ' + module.__name__, 'site-packages' in Path(module.__file__).resolve().parts)
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

    actual_connection, actual_message, actual_directory = http.client.HTTPSConnection, wx.MessageBox, wx.DirDialog
    actual_saved_chooser = wx.SingleChoiceDialog
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
            approvals, export_pickers, saved_choices = [], [], []
            def approve(message, *args, **kwargs):
                approvals.append(message)
                return wx.YES
            wx.MessageBox = approve
            class Directory:
                def __init__(self, *args, **kwargs): export_pickers.append(1)
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(root)
                def Destroy(self): pass
            wx.DirDialog = Directory
            class SavedChooser:
                # Deterministic single saved-job selection; native outer controls are real.
                def __init__(self, parent, message, title, choices):
                    saved_choices.append(tuple(choices))
                def ShowModal(self): return wx.ID_OK
                def GetSelection(self): return 0
                def Destroy(self): pass
            wx.SingleChoiceDialog = SavedChooser
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
                    # Exercise real outer native buttons against the existing saved job,
                    # without another provider call, approval or destination picker.
                    saved, = translation.TranslationService(library.root / 'state' / 'translation').list()
                    fields = ((selected, original, 'Unrelated synthetic text'),
                              (find('Source language code; auto detects'), 'en', 'auto'),
                              (find('Target language code'), 'uk', 'sk'),
                              (glossary_input, glossary, ''),
                              (find('Maximum approved budget in USD; no default price'), saved['authorization']['budget'], '9'))
                    price = find('Optional price per million characters in USD; your current contract rate')
                    for control, expected, changed in fields:
                        control.ChangeValue(changed)
                    price.ChangeValue('99')
                    before, approval_count, picker_count = len(calls), len(approvals), len(export_pickers)
                    press(find('Saved jobs'))
                    check('saved job label identifies exact account and job', len(saved_choices) == 1 and
                          len(saved_choices[0]) == 1 and saved['id'] in saved_choices[0][0] and
                          profile.id in saved_choices[0][0])
                    for control, expected, changed in fields:
                        check('restored native field: ' + control.GetName(), control.GetValue() == expected)
                    check('restored job clears stale price', price.GetValue() == '')
                    check('saved job status has focus and identity', wx.Window.FindFocus() is status and saved['id'] in status.GetValue())
                    for control, expected, changed in fields:
                        control.ChangeValue(changed)
                        press(find('Translate / resume'))
                        check('changed saved scope rejected: ' + control.GetName(),
                              not frame.working and 'Visible settings differ' in status.GetValue() and
                              wx.Window.FindFocus() is status and len(calls) == before and len(approvals) == approval_count)
                        control.ChangeValue(expected)
                    selected.ChangeValue('Unrelated synthetic text')
                    press(find('Export'))
                    check('mismatched saved export rejected before picker', not frame.working and
                          'Visible settings differ' in status.GetValue() and wx.Window.FindFocus() is status and
                          len(export_pickers) == picker_count and len(calls) == before)
                    selected.ChangeValue(original)
                    press(find('Preview'))
                    glossary_input.ChangeValue(glossary + '-changed-after-review')
                    press(find('Translate / resume'))
                    check('changed glossary requires fresh review without transfer', 'Preview this exact' in status.GetValue() and len(calls) == before)
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
            http.client.HTTPSConnection, wx.MessageBox, wx.DirDialog = actual_connection, actual_message, actual_directory
            wx.SingleChoiceDialog = actual_saved_chooser
            if reference:
                vault.delete('native_translation', reference)
            if frame is not None:
                frame.Destroy()
            if native_handle is not None:
                native_handle.close()
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'synthetic_rest_calls': len(calls),
        'native_keyboard': 'PASS', 'live_google_api': 'WAITING_INPUT', 'billing_and_quality': 'WAITING_INPUT',
        'native_approval_and_file_pickers': 'NOT_RUN', 'native_saved_job_picker': 'NOT_RUN',
        'saved_job_outer_buttons': 'native; selection and field text supplied by probe', 'nvda': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
