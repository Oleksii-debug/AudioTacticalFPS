"""Installed native Advanced v3 controls and a temporary Windows vault credential.

Only synthetic REST replies and public fixture text are used. This probe checks
actual native controls, the current gateway/ledger, durable jobs and derivative
files. It does not call Google, qualify translation quality or claim NVDA proof.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sys
import traceback
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, time
from types import SimpleNamespace


def run_native(args, progress, runtime_owner):
    if sys.platform != 'win32':
        raise SystemExit('Native Windows execution is required; no simulated native PASS is produced.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_runtime = activate_bundled_runtime()
    runtime_owner.append(native_runtime)
    progress('native-runtime-active')
    import wx
    from carr_library import providers
    from carr_library.assistant.translation import TranslationService
    from carr_library.providers import (
        BudgetLedger, ProfileStore, ProviderGateway, ProviderProfile, WindowsCredentialVault)
    from carr_library.reader.narration_text import NarrationDraft
    from carr_library.ui.translation_dialog import show_translation
    source_tree = Path(__file__).resolve().parents[1] / 'src'
    if Path(providers.__file__).resolve().is_relative_to(source_tree):
        raise RuntimeError('This native probe must import the installed wheel, not the checkout.')

    parent = 'projects/123456789/locations/us-central1'
    endpoint = 'https://translation.googleapis.com/v3/' + parent
    glossary = parent + '/glossaries/native-terms'
    token = 'SYNTHETIC_NATIVE_OAUTH_NEVER_REAL'
    original = 'Public synthetic source. Український текст &amp; No private book content.'
    requests, approvals, failures = [], [], []
    class Transport:
        def check(self, headers, options):
            if headers != {'Authorization': 'Bearer ' + token, 'X-goog-user-project': '123456789'}:
                raise AssertionError('Native v3 did not use the reviewed OAuth scope.')
            if options.get('credential_expires_at', 0) <= time():
                raise AssertionError('Native v3 did not retain the actual token expiry.')
        def get(self, profile, route, headers, **options):
            self.check(headers, options)
            requests.append(('languages', route))
            return json.dumps({'languages': [
                {'languageCode': 'en', 'displayName': 'English', 'supportSource': True},
                {'languageCode': 'uk', 'displayName': 'Українська', 'supportTarget': True}]}).encode()
        def post(self, profile, route, body, headers, **options):
            self.check(headers, options)
            selected = json.loads(body)
            if (selected['contents'] != [original] or selected.get('sourceLanguageCode') != 'en'
                    or selected['targetLanguageCode'] != 'uk' or selected['mimeType'] != 'text/plain'
                    or selected['model'] != parent + '/models/general/nmt'
                    or selected['glossaryConfig'] != {'glossary': glossary, 'ignoreCase': False}):
                raise AssertionError('Native review did not bind the exact text, pair, model and glossary.')
            requests.append(('text', route))
            return json.dumps({'glossaryTranslations': [{
                'translatedText': 'Синтетичний переклад &amp; для перевірки інтерфейсу.',
                'model': selected['model'], 'glossaryConfig': selected['glossaryConfig']}]}).encode()

    app = wx.App(False)
    with TemporaryDirectory(prefix='carr-native-translation-') as folder:
        root, exports = Path(folder) / 'library', Path(folder) / 'exports'
        root.mkdir()
        # Match LibraryApp's canonical root, including Windows short/long aliases.
        root = root.resolve(strict=True)
        exports.mkdir()
        vault = WindowsCredentialVault()
        profiles = ProfileStore(root / 'api-connections.json', vault)
        selected_profile = profiles.save_google_token(ProviderProfile(
            'native-google-v3', 'Synthetic IAM account', 'google-translation-v3',
            endpoint, 'general/nmt', ('translation',)), token, time() + 1800)
        try:
            service = TranslationService(root, profiles=profiles,
                gateway=ProviderGateway(BudgetLedger(root / 'api-operations.sqlite'), vault=vault, transport=Transport()))

            class Frame(wx.Frame):
                def __init__(self):
                    super().__init__(None, title='Translation native probe host')
                    self.library = SimpleNamespace(root=root)
                    self.working, self._closing = False, False
                    self.cancel_event = Event()
                    self.executor = ThreadPoolExecutor(max_workers=1)
                def run_job(self, label, operation, done=None, failed=None):
                    if self.working or self._closing:
                        return False
                    self.working = True
                    self.cancel_event.clear()
                    future = self.executor.submit(operation)
                    def completed(task):
                        def apply():
                            self.working = False
                            if self._closing:
                                return
                            try:
                                result = task.result()
                                if done:
                                    done(result)
                            except BaseException as exc:
                                if failed:
                                    failed(exc)
                                failures.append(type(exc).__name__ + ': ' + str(exc))
                        wx.CallAfter(apply)
                    future.add_done_callback(completed)
                    return True

            frame = Frame()
            frame.Show()
            native_message_box, native_dir_dialog = wx.MessageBox, wx.DirDialog
            def approve(message, title, *args, **kwargs):
                approvals.append(title)
                return wx.YES
            class ExportSelection:
                def __init__(self, *args, **kwargs):
                    pass
                def ShowModal(self):
                    return wx.ID_OK
                def GetPath(self):
                    return str(exports)
                def Destroy(self):
                    pass
            wx.MessageBox, wx.DirDialog = approve, ExportSelection
            deadline = monotonic() + 45
            phase, final = [0], {}
            timer = wx.Timer(frame)

            def child(dialog, name):
                found = wx.FindWindowByName(name, dialog)
                if found is None:
                    raise AssertionError('Missing native named control: ' + name)
                return found

            def button(dialog, label):
                found = next((control for control in dialog.GetChildren() if isinstance(control, wx.Button)
                              and control.GetLabel().replace('&', '') == label), None)
                if found is None:
                    raise AssertionError('Missing native translation button: ' + label)
                if not found.IsEnabled():
                    raise AssertionError('Native translation button is not available: ' + label)
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, found.GetId())
                event.SetEventObject(found)
                found.GetEventHandler().ProcessEvent(event)

            def drive(event):
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.GetTitle() == 'Translation — selected text derivatives']
                dialog = dialogs[0] if dialogs else None
                try:
                    if failures:
                        raise AssertionError(failures[0])
                    if monotonic() > deadline:
                        raise TimeoutError('Native translation probe exceeded its bounded wait.')
                    progress('drive-' + str(phase[0]), working=bool(frame.working),
                             synthetic_requests=len(requests))
                    if dialog is None or not dialog.IsShown() or not dialog.IsModal():
                        return
                    if phase[0] == 0:
                        assert not requests
                        assert child(dialog, 'Selected text; originals remain unchanged').GetValue() == original
                        glossary_control = child(dialog, 'Advanced v3 glossary resource; blank for none')
                        assert glossary_control.IsEnabled()
                        glossary_control.SetFocus()
                        glossary_control.ChangeValue(glossary)
                        phase[0] = 'focus'
                    elif phase[0] == 'focus':
                        if wx.Window.FindFocus() is not child(dialog, 'Advanced v3 glossary resource; blank for none'):
                            return
                        button(dialog, 'Fetch languages')
                        phase[0] = 1
                    elif phase[0] == 1 and not frame.working:
                        assert [row[0] for row in requests] == ['languages']
                        source = child(dialog, 'Source language')
                        assert not any(label.startswith('uk —') for label in source.GetItems())
                        source.SetSelection(next(i for i, label in enumerate(source.GetItems()) if label.startswith('en —')))
                        target = child(dialog, 'Target language')
                        assert not any(label.startswith('en —') for label in target.GetItems())
                        target.SetSelection(next(i for i, label in enumerate(target.GetItems()) if label.startswith('uk —')))
                        child(dialog, 'Maximum approved reservation in USD; no default price').ChangeValue('1')
                        button(dialog, 'Preview')
                        assert [row[0] for row in requests] == ['languages']
                        phase[0] = 2
                    elif phase[0] == 2:
                        button(dialog, 'Translate / resume')
                        phase[0] = 3
                    elif phase[0] == 3 and not frame.working:
                        rows = service.list()
                        assert len(rows) == 1 and rows[0]['status'] == 'complete'
                        assert child(dialog, 'Accepted translation derivative; not a source quotation').GetValue().startswith('Синтетичний')
                        assert not child(dialog, 'Selected text; originals remain unchanged').IsEnabled()
                        button(dialog, 'Export derivative')
                        phase[0] = 4
                    elif phase[0] == 4 and not frame.working:
                        outputs = list(exports.glob('Translation-*'))
                        assert len(outputs) == 1
                        output = outputs[0]
                        report = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
                        assert report['quotation_authority'] is False
                        assert report['schema'] == 'carr-translation-export/2' and report['glossary'] == glossary
                        assert '&amp;' in (output / 'translated.txt').read_text(encoding='utf-8')
                        job = service.jobs.load(service.list()[0]['id'])
                        assert job['schema'] == 'carr-translation-job/2'
                        assert job['receipts'][0]['reported_model'] == parent + '/models/general/nmt'
                        assert token not in profiles.path.read_text(encoding='utf-8')
                        assert token not in service.jobs.path(job['id']).read_text(encoding='utf-8')
                        assert (output / 'original.txt').read_bytes() == original.encode()
                        draft = NarrationDraft.from_dict(json.loads(
                            (output / 'translation.narration.json').read_text(encoding='utf-8')), source_text=original)
                        assert draft.source_range(0, len(draft.text))[2] is False
                        for name, digest in report['files'].items():
                            assert sha256((output / name).read_bytes()).hexdigest() == digest
                        assert len(approvals) == 2
                        assert [row[0] for row in requests] == ['languages', 'text']
                        final.update(schema='carr-native-translation-v3-smoke/1', status='PASS',
                            installed_module=str(Path(providers.__file__).resolve()), native_controls=True,
                            synthetic_provider_requests=2, exact_reviewed_pair='en->uk', source_immutable=True,
                            quotation_authority=False, derivative_hashes=report['files'],
                            approval_and_folder_picker='injected explicit synthetic decisions',
                            actual_google_api='NOT_RUN', actual_credential_vault='PASS',
                            explicit_advanced_model=True, glossary_verified=True, directional_languages=True, nvda='NOT_RUN')
                        phase[0] = 5
                        timer.Stop()
                        button(dialog, 'Close')
                        progress('native-controls-complete', synthetic_requests=len(requests))
                except BaseException as exc:
                    failures.append(type(exc).__name__ + ': ' + str(exc))
                    timer.Stop()
                    frame.cancel_event.set()
                    if dialog is not None:
                        close = wx.CloseEvent(wx.EVT_CLOSE.typeId, dialog.GetId())
                        dialog.GetEventHandler().ProcessEvent(close)
                    progress('native-flow-failed', failures=failures[-4:])

            frame.Bind(wx.EVT_TIMER, drive, timer)
            timer.Start(50)
            def show_owned_modal():
                try:
                    progress('opening-owned-modal')
                    show_translation(frame, initial_text=original, title='Synthetic public source', service=service)
                    progress('owned-modal-returned')
                except BaseException as error:
                    failures.append(type(error).__name__ + ': ' + str(error))
                    progress('modal-open-failed', failures=failures[-4:], traceback=traceback.format_exc()[-12000:])
                finally:
                    # Exit the application loop only after ShowModal has
                    # unwound; an exit posted inside it can target its nested loop.
                    wx.CallAfter(app.ExitMainLoop)
            wx.CallAfter(show_owned_modal)
            try:
                app.MainLoop()
            finally:
                timer.Stop()
                frame._closing = True
                frame.cancel_event.set()
                progress('settling-native-worker', failures=failures[-4:])
                frame.executor.shutdown(wait=True, cancel_futures=True)
                wx.MessageBox, wx.DirDialog = native_message_box, native_dir_dialog
                frame.Destroy()
                progress('native-frame-cleaned', failures=failures[-4:])
            if failures or phase[0] != 5:
                raise RuntimeError('Native translation probe failed: ' + '; '.join(failures or ['incomplete flow']))
        finally:
            # This exact synthetic native vault entry is removed on every exit.
            vault.delete(selected_profile.credential_ref, selected_profile.credential_binding)
            progress('synthetic-vault-cleaned', vault_cleanup='PASS')
    return final


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    state = {'schema': 'carr-native-translation-progress/1', 'status': 'RUNNING',
             'flow': getattr(args, 'mode', 'selected-advanced'), 'phase': 'starting',
             'windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
             'actual_google_api': 'NOT_RUN', 'nvda': 'NOT_RUN', 'vault_cleanup': 'NOT_RUN',
             'runtime_cleanup': 'NOT_RUN'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    runtime_owner = []
    def progress(phase, **values):
        changed = (not args.output.is_file() or state.get('phase') != phase
                   or any(state.get(key) != value for key, value in values.items()))
        state.update(phase=phase, **values)
        if not changed:
            return
        args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if changed:
            print('PROGRESS ' + json.dumps(state, ensure_ascii=False), flush=True)
    progress('starting')
    result, failure = None, None
    try:
        if not sys.flags.isolated:
            raise RuntimeError('The native probe requires the isolated installed interpreter.')
        result = run_native(args, progress, runtime_owner)
    except BaseException as error:
        failure = type(error).__name__ + ': ' + str(error)
        state.update(status='FAIL', error=failure, traceback=traceback.format_exc()[-12000:])
    finally:
        try:
            for owner in runtime_owner:
                if owner is not None:
                    owner.close()
            state['runtime_cleanup'] = 'PASS'
        except BaseException as error:
            failure = type(error).__name__ + ': ' + str(error)
            state.update(status='FAIL', cleanup_error=failure, traceback=traceback.format_exc()[-12000:])
        if failure is None and result is not None and result.get('status') == 'PASS':
            result.update(vault_cleanup=state['vault_cleanup'], runtime_cleanup=state['runtime_cleanup'],
                          native_event_loop='owned-modal-unwound')
            if result['vault_cleanup'] != 'PASS':
                failure = 'The synthetic vault entry did not confirm cleanup.'
        if failure is not None or result is None:
            result = {**state, 'status': 'FAIL', 'error': failure or 'Native flow returned no completed receipt.'}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
