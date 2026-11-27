"""Actual installed Windows controls for one explicit scoped translation flow.

All source text and responses are synthetic. Native widgets, Windows vault,
catalog/reader verification, approved jobs, ledger and derivative files are real.
This probe does not contact Google or qualify linguistic quality or NVDA.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import importlib
import json
from pathlib import Path
import runpy
import sys
import traceback
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, time


def run_native(args, progress, runtime_owner):
    if sys.platform != 'win32' or not sys.flags.isolated:
        raise SystemExit('Actual isolated Windows execution is required.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_runtime = activate_bundled_runtime()
    runtime_owner.append(native_runtime)
    progress('native-runtime-active')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant.translation import TranslationService
    from carr_library.assistant.translation_selection import blocks
    from carr_library.assistant.translation_scope import TranslationScopeService
    from carr_library.assistant.translation_state import split_text
    from carr_library.providers import (
        BudgetLedger, ProfileStore, ProviderGateway, ProviderProfile, WindowsCredentialVault)
    from carr_library.ui.translation_scope_dialog import show_translation_scope

    origins = {}
    for name in ('carr_library.application', 'carr_library.providers',
                 'carr_library.assistant.translation', 'carr_library.assistant.translation_state',
                 'carr_library.assistant.translation_selection', 'carr_library.assistant.translation_scope_state',
                 'carr_library.assistant.translation_scope', 'carr_library.ui.translation_scope_dialog',
                 'carr_library.library_backup'):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        if not path.is_relative_to(Path(sys.prefix).resolve(strict=True)) or 'site-packages' not in path.parts:
            raise RuntimeError('The native probe imported foreign product source.')
        origins[name] = dict(path=str(path), sha256=sha256(path.read_bytes()).hexdigest())

    parent = 'projects/123456789/locations/us-central1'
    endpoint = 'https://translation.googleapis.com/v3/' + parent
    glossary = parent + '/glossaries/native-scopes'
    token = 'SYNTHETIC_NATIVE_SCOPE_TOKEN_NEVER_REAL'
    requests, approvals, failures = [], [], []

    class Transport:
        def check(self, headers, options):
            if headers != {'Authorization': 'Bearer ' + token, 'X-goog-user-project': '123456789'}:
                raise AssertionError('The native job used a different reviewed credential scope.')
            if options.get('credential_expires_at', 0) <= time():
                raise AssertionError('The native OAuth credential expired.')
        def get(self, profile, route, headers, **options):
            self.check(headers, options)
            requests.append(('languages', route))
            return json.dumps({'languages': [
                {'languageCode': 'en', 'displayName': 'English', 'supportSource': True},
                {'languageCode': 'uk', 'displayName': 'Ukrainian', 'supportTarget': True}]}).encode()
        def post(self, profile, route, raw, headers, **options):
            self.check(headers, options)
            body = json.loads(raw)
            if (body.get('sourceLanguageCode') != 'en' or body['targetLanguageCode'] != 'uk'
                    or body['mimeType'] != 'text/plain' or body['model'] != parent + '/models/general/nmt'
                    or body['glossaryConfig'] != {'glossary': glossary, 'ignoreCase': False}):
                raise AssertionError('The scoped native request differs from the explicit approval.')
            requests.append(('text', body['contents'][0]))
            return json.dumps({'glossaryTranslations': [{
                'translatedText': 'СИНТЕТИЧНИЙ ПЕРЕКЛАД ' + body['contents'][0],
                'model': body['model'], 'glossaryConfig': body['glossaryConfig']}]},
                ensure_ascii=False).encode()

    wx_app = wx.App(False)
    with TemporaryDirectory(prefix='carr-native-scope-') as folder:
        root = Path(folder)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Native scoped translation')
        text_book = library.add_text(workspace, 'Native plain document',
            'Public synthetic first section.\fSecond synthetic section. Україна &amp; <script>inert</script>.',
            language='en')
        fixture_path = Path(__file__).resolve().parents[1] / 'tests/reader/test_ebook_reader.py'
        make_book = runpy.run_path(str(fixture_path))['book']
        epub_path = root / 'synthetic.epub'
        make_book(epub_path)
        epub_book = library.import_file(workspace, epub_path, title='Native EPUB document')
        if text_book['state'] != 'complete' or epub_book['state'] != 'complete':
            raise AssertionError('The real synthetic source import failed.')
        original = {book['asset']: library.assets.resolve(book['asset']).read_bytes()
                    for book in (text_book, epub_book)}
        exports = root / 'exports'
        exports.mkdir()
        vault = WindowsCredentialVault()
        profiles = ProfileStore(library.root / 'api-connections.json', vault)
        selected_profile = profiles.save_google_token(ProviderProfile(
            'native-scoped', 'Synthetic scoped IAM account', 'google-translation-v3',
            endpoint, 'general/nmt', ('translation',)), token, time() + 1800)
        try:
            translation = TranslationService(library.root, profiles=profiles,
                gateway=ProviderGateway(BudgetLedger(library.root / 'api-operations.sqlite'),
                                        vault=vault, transport=Transport()))
            scoped = TranslationScopeService(library, service=translation)

            class Frame(wx.Frame):
                def __init__(self):
                    super().__init__(None, title='Structured translation native probe')
                    self.library, self.active = library, (workspace, None)
                    self.working, self._closing = False, False
                    self.cancel_event = Event()
                    self.executor = ThreadPoolExecutor(max_workers=1)
                def report(self, message):
                    failures.append(str(message))
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
            def approve(message, title, *a, **k):
                approvals.append(dict(title=title, message=message))
                return wx.YES
            class ExportSelection:
                def __init__(self, *a, **k):
                    pass
                def ShowModal(self):
                    return wx.ID_OK
                def GetPath(self):
                    return str(exports)
                def Destroy(self):
                    pass
            wx.MessageBox, wx.DirDialog = approve, ExportSelection
            phase, final = [0], {}
            deadline = monotonic() + 120
            timer = wx.Timer(frame)

            def child(dialog, name):
                found = wx.FindWindowByName(name, dialog)
                if found is None:
                    raise AssertionError('Missing native named control: ' + name)
                return found
            def button(dialog, label):
                found = next((control for control in dialog.GetChildren() if isinstance(control, wx.Button)
                    and control.GetLabel().replace('&', '') == label), None)
                if found is None or not found.IsEnabled():
                    raise AssertionError('Native button is unavailable: ' + label)
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, found.GetId())
                event.SetEventObject(found)
                found.GetEventHandler().ProcessEvent(event)
            def choice(control, index):
                control.SetSelection(index)
                event = wx.CommandEvent(wx.EVT_CHOICE.typeId, control.GetId())
                event.SetEventObject(control)
                control.GetEventHandler().ProcessEvent(event)

            def drive(event):
                dialogs = [window for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)
                           and window.GetTitle() == 'Translation — documents, chapters and batches']
                dialog = dialogs[0] if dialogs else None
                try:
                    if failures:
                        raise AssertionError(failures[0])
                    if monotonic() > deadline:
                        raise TimeoutError('Native scoped translation exceeded its bounded wait.')
                    progress('drive-' + str(phase[0]), working=bool(frame.working),
                             synthetic_requests=len(requests))
                    if dialog is None or not dialog.IsShown() or not dialog.IsModal():
                        return
                    if phase[0] == 0:
                        assert not requests
                        scope_control = child(dialog, 'Translation scope')
                        scope_control.SetFocus()
                        phase[0] = 'focus'
                    elif phase[0] == 'focus':
                        scope_control = child(dialog, 'Translation scope')
                        if wx.Window.FindFocus() is not scope_control:
                            return
                        choice(scope_control, ('document', 'chapter', 'batch').index(args.mode))
                        button(dialog, 'Load books')
                        phase[0] = 1
                    elif phase[0] == 1 and not frame.working:
                        listing = child(dialog, 'Documents to translate; Space checks each item')
                        assert listing.GetCount() == 2
                        wanted = {text_book['edition']} if args.mode == 'document' else {epub_book['edition']}
                        if args.mode == 'batch':
                            wanted.add(text_book['edition'])
                        for index, label in enumerate(listing.GetItems()):
                            listing.Check(index, any(identity in label for identity in wanted))
                        if args.mode == 'chapter':
                            button(dialog, 'Load chapters')
                            phase[0] = 2
                        else:
                            button(dialog, 'Fetch languages')
                            phase[0] = 3
                    elif phase[0] == 2 and not frame.working:
                        chapters = child(dialog, 'Exact EPUB chapter; load contents after choosing one book')
                        assert chapters.GetCount() == 2
                        choice(chapters, 1)
                        button(dialog, 'Fetch languages')
                        phase[0] = 3
                    elif phase[0] == 3 and not frame.working:
                        assert [row[0] for row in requests] == ['languages']
                        sources = child(dialog, 'Source language')
                        targets = child(dialog, 'Target language')
                        choice(sources, next(i for i, name in enumerate(sources.GetItems()) if name.startswith('en —')))
                        choice(targets, next(i for i, name in enumerate(targets.GetItems()) if name.startswith('uk —')))
                        child(dialog, 'Advanced v3 glossary resource; blank for none').ChangeValue(glossary)
                        child(dialog, 'Optional owner contract price per million characters in USD').ChangeValue('20')
                        child(dialog, 'Total approved reservation in USD; preview allocates each item').ChangeValue('2.000003')
                        button(dialog, 'Preview transfers')
                        phase[0] = 4
                    elif phase[0] == 4 and not frame.working:
                        status = child(dialog, 'Per-item preview, approval and receipt status').GetValue()
                        assert 'review_sha256' in status and 'estimated_micros' in status, status
                        assert [row[0] for row in requests] == ['languages']
                        source_view = child(dialog, 'Exact selected source and original headings').GetValue()
                        if args.mode == 'chapter':
                            assert 'Archive context' in source_view and 'Railway research' not in source_view
                        button(dialog, 'Translate / resume')
                        phase[0] = 5
                    elif phase[0] == 5 and not frame.working:
                        saved = scoped.list()
                        assert len(saved) == 1 and saved[0]['status'] == 'complete'
                        assert not child(dialog, 'Translation scope').IsEnabled()
                        button(dialog, 'Export bilingual')
                        phase[0] = 6
                    elif phase[0] == 6 and not frame.working:
                        outputs = list(exports.glob('Structured-translation-*'))
                        assert len(outputs) == 1
                        output = outputs[0]
                        report = json.loads((output / 'translation-provenance.json').read_text('utf-8'))
                        structure = json.loads((output / 'translation-structure.json').read_text('utf-8'))
                        plan = scoped.plans.load(scoped.list()[0]['id'])
                        expected = [part for unit in blocks(plan['selection']) if unit['text'].strip()
                                    for part in split_text(unit['text']) if part.strip()]
                        assert [row[1] for row in requests if row[0] == 'text'] == expected
                        assert structure['mode'] == args.mode and report['quotation_authority'] is False
                        assert report['source_is_immutable'] and report['glossary'] == glossary
                        assert len(approvals) == 2 and approvals[-1]['title'] == 'Approve exact structured source transfer'
                        assert all(str(item['edition']) in child(dialog, 'Selected documents in transfer order').GetValue()
                                   for item in plan['selection']['items'])
                        if args.mode == 'chapter':
                            assert [section['ordinal'] for section in structure['documents'][0]['sections']] == [2]
                        if args.mode == 'batch':
                            assert len(structure['documents']) == 2
                        for asset, raw in original.items():
                            assert library.assets.resolve(asset).read_bytes() == raw
                        for name, digest in report['files'].items():
                            assert sha256((output / name).read_bytes()).hexdigest() == digest
                        assert '<script>' not in (output / 'bilingual.html').read_text('utf-8')
                        assert token not in scoped.plans.path(plan['id']).read_text('utf-8')
                        assert all(row['decoded_receipt_integrity'] == 'decoded-ledger-bound'
                                   for block in report['blocks'] for row in block['receipts'])
                        final.update(schema='carr-native-scoped-translation/1', status='PASS',
                            mode=args.mode, windows=True, isolated=bool(sys.flags.isolated),
                            native_controls=True, keyboard_focus=True, actual_credential_vault='PASS',
                            synthetic_provider_requests=len(requests), translated_blocks=len(expected),
                            documents=len(structure['documents']), per_item_approval=True,
                            source_immutable=True, quotation_authority=False, structure_preserved=True,
                            derivative_hashes=report['files'], module_origins=origins,
                            fixture_helper_sha256=sha256(fixture_path.read_bytes()).hexdigest(),
                            actual_google_api='NOT_RUN', translation_quality='NOT_QUALIFIED', nvda='NOT_RUN',
                            approval_and_folder_picker='injected explicit synthetic decisions')
                        phase[0] = 7
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
                    show_translation_scope(frame, service=scoped)
                    progress('owned-modal-returned')
                except BaseException as error:
                    failures.append(type(error).__name__ + ': ' + str(error))
                    progress('modal-open-failed', failures=failures[-4:], traceback=traceback.format_exc()[-12000:])
                finally:
                    # Exit the application loop only after ShowModal has
                    # unwound; an exit posted inside it can target its nested loop.
                    wx.CallAfter(wx_app.ExitMainLoop)
            wx.CallAfter(show_owned_modal)
            try:
                wx_app.MainLoop()
            finally:
                timer.Stop()
                frame._closing = True
                frame.cancel_event.set()
                progress('settling-native-worker', failures=failures[-4:])
                frame.executor.shutdown(wait=True, cancel_futures=True)
                wx.MessageBox, wx.DirDialog = native_message_box, native_dir_dialog
                frame.Destroy()
                progress('native-frame-cleaned', failures=failures[-4:])
            if failures or phase[0] != 7:
                raise RuntimeError('Native scoped translation failed: ' + '; '.join(failures or ['incomplete flow']))
        finally:
            vault.delete(selected_profile.credential_ref, selected_profile.credential_binding)
            progress('synthetic-vault-cleaned', vault_cleanup='PASS')
    return final


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('document', 'chapter', 'batch'), required=True)
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
