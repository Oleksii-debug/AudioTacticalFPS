"""Actual native wx translation flow with synthetic HTTP/vault boundaries only.

Run from an installed wheel on Windows. This proves native controls, worker
completion, review/approval wiring and real derivative files; it does not call
Google, claim translation quality, test a real account, or claim NVDA coverage.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from types import SimpleNamespace


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise SystemExit('Native Windows execution is required; no simulated native PASS is produced.')
    import wx
    from carr_library import providers
    from carr_library.application import LibraryApp
    from carr_library.ui import translation_sources
    from carr_library.assistant.translation import TranslationService
    from carr_library.providers import (
        BudgetLedger, ProfileStore, ProviderGateway, ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT)
    from carr_library.reader.narration_text import NarrationDraft
    from carr_library.ui.translation_dialog import show_translation
    source_tree = Path(__file__).resolve().parents[1] / 'src'
    if Path(providers.__file__).resolve().is_relative_to(source_tree):
        raise RuntimeError('This native probe must import the installed wheel, not the checkout.')

    original = 'Public synthetic source. Український текст. No private book content.'
    requests, approvals, failures = [], [], []
    class Vault:
        def __init__(self):
            self.values = {}
        def put(self, reference, binding, secret):
            self.values[reference, binding] = secret
        def get(self, reference, binding):
            return self.values[reference, binding]
        def delete(self, reference, binding):
            self.values.pop((reference, binding), None)

    class Transport:
        def get(self, profile, route, headers, **kwargs):
            requests.append(('languages', route))
            return json.dumps({'data': {'languages': [
                {'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]}}).encode()
        def post(self, profile, route, body, headers, **kwargs):
            selected = json.loads(body)
            if selected['q'] != original or selected.get('source') != 'en' or selected['target'] != 'uk':
                raise AssertionError('Native review did not bind the exact synthetic text and pair.')
            requests.append(('text', route))
            return json.dumps({'data': {'translations': [{
                'translatedText': 'Синтетичний переклад для перевірки інтерфейсу.',
                'model': 'nmt'}]}}).encode()

    app = wx.App(False)
    with TemporaryDirectory(prefix='carr-native-translation-') as folder:
        root, exports = Path(folder).resolve() / 'library', Path(folder).resolve() / 'exports'
        root.mkdir()
        exports.mkdir()
        source_app = LibraryApp(root)
        workspace = source_app.create_workspace('Native retained source selection')
        book = source_app.add_text(workspace, 'Synthetic public source', original)
        source_file = source_app.assets.resolve(book['asset'])
        source_digest = sha256(source_file.read_bytes()).hexdigest()
        vault = Vault()
        profiles = ProfileStore(root / 'api-connections.json', vault)
        profiles.save(ProviderProfile('native-google', 'Synthetic NMT account',
            'google-translation-v2', GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',)),
            secret='synthetic-native-key-not-a-real-credential')
        service = TranslationService(root, profiles=profiles,
            gateway=ProviderGateway(BudgetLedger(root / 'api-operations.sqlite'), vault=vault, transport=Transport()))

        class Frame(wx.Frame):
            def __init__(self):
                super().__init__(None, title='Translation native probe host')
                self.library = source_app
                self.active = (workspace, None)
                self.busy = False
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
        deadline = monotonic() + 90
        phase, final = [0], {}
        timer = None
        source_timers, source_views = [], []
        native_source_selector = translation_sources.choose_translation_sources
        active, reported_phase = [True], [None]

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

        def selected_sources(frame, parent):
            def ready(view):
                source_views.append(view)
                submitted = [False]
                def choose():
                    if not view.alive:
                        return
                    try:
                        if monotonic() >= deadline:
                            raise TimeoutError('Native source selection exceeded its deadline.')
                        if view.busy or frame.working:
                            source_timers.append(wx.CallLater(25, choose))
                            return
                        if submitted[0]:
                            raise AssertionError('Native source capture returned without a completed selection.')
                        assert len(view.rows) == 1 and view.rows[0].edition_id == book['edition']
                        assert isinstance(view.books, wx.ListBox) and view.books.GetName()
                        view.books.SetSelection(0)
                        view.kind.SetSelection(0)
                        for key in ('add', 'prepare'):
                            action = view.buttons[key]
                            assert isinstance(action, wx.Button) and action.IsEnabled()
                            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, action.GetId())
                            event.SetEventObject(action)
                            assert action.GetEventHandler().ProcessEvent(event)
                        assert view.busy
                        submitted[0] = True
                        source_timers.append(wx.CallLater(25, choose))
                    except BaseException as exc:
                        failures.append(type(exc).__name__ + ': ' + str(exc))
                        view.close()
                source_timers.append(wx.CallLater(25, choose))
            return native_source_selector(frame, parent, on_ready=ready)

        translation_sources.choose_translation_sources = selected_sources

        def drive(event=None):
            nonlocal timer
            if not active[0]:
                return
            dialogs = [window for window in wx.GetTopLevelWindows()
                       if isinstance(window, wx.Dialog) and window.GetParent() is frame
                       and window.GetName() == 'Translation workspace'
                       and window.IsModal() and window.IsShown()]
            dialog = dialogs[0] if dialogs else None
            try:
                if failures:
                    raise AssertionError(failures[0])
                if monotonic() > deadline:
                    raise TimeoutError('Native translation probe exceeded its bounded wait.')
                if dialog is None:
                    return
                if phase[0] != reported_phase[0]:
                    print('Native translation phase:', phase[0], flush=True)
                    reported_phase[0] = phase[0]
                if phase[0] == 0:
                    assert not requests
                    assert child(dialog, 'Selected text; originals remain unchanged').GetValue() == original
                    button(dialog, 'Choose documents or chapters...')
                    assert len(source_views) == 1 and source_views[0].result is not None
                    assert source_views[0].result['documents'][0]['context']['source_anchor']['edition'] == book['edition']
                    assert not child(dialog, 'Selected text; originals remain unchanged').IsEditable()
                    button(dialog, 'Fetch languages')
                    phase[0] = 1
                elif phase[0] == 1 and not frame.working:
                    assert [row[0] for row in requests] == ['languages']
                    source = child(dialog, 'Source language')
                    source.SetSelection(next(i for i, label in enumerate(source.GetItems()) if label.startswith('en —')))
                    target = child(dialog, 'Target language')
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
                    assert report['schema'] == 'carr-translation-export/3'
                    assert (output / 'structured-bilingual.html').is_file()
                    assert (output / 'structured-translation.json').is_file()
                    assert sha256(source_file.read_bytes()).hexdigest() == source_digest
                    assert (output / 'original.txt').read_bytes() == original.encode()
                    draft = NarrationDraft.from_dict(json.loads(
                        (output / 'translation.narration.json').read_text(encoding='utf-8')), source_text=original)
                    assert draft.source_range(0, len(draft.text))[2] is False
                    for name, digest in report['files'].items():
                        assert sha256((output / name).read_bytes()).hexdigest() == digest
                    assert len(approvals) == 2
                    assert [row[0] for row in requests] == ['languages', 'text']
                    final.update(schema='carr-native-translation-smoke/1', status='PASS',
                        installed_module=str(Path(providers.__file__).resolve()), native_controls=True,
                        native_retained_document_selection=True, structured_export=True,
                        synthetic_provider_requests=2, exact_reviewed_pair='en->uk', source_immutable=True,
                        quotation_authority=False, derivative_hashes=report['files'],
                        approval_and_folder_picker='injected explicit synthetic decisions',
                        actual_google_api='NOT_RUN', actual_credential_vault='NOT_RUN', nvda='NOT_RUN')
                    phase[0] = 5
                    active[0] = False
                    timer.Stop()
                    button(dialog, 'Close')
            except BaseException as exc:
                failures.append(type(exc).__name__ + ': ' + str(exc))
                print('FAIL: native translation phase ' + str(phase[0]) + ': ' + failures[-1], flush=True)
                active[0] = False
                timer.Stop()
                frame.cancel_event.set()
                if dialog is not None:
                    close = wx.CloseEvent(wx.EVT_CLOSE.typeId, dialog.GetId())
                    dialog.GetEventHandler().ProcessEvent(close)
                else:
                    wx.CallAfter(app.ExitMainLoop)

            finally:
                if active[0]:
                    timer = wx.CallLater(50, drive)

        # A modal disables its parent frame. Use a parent-independent timer
        # so the driver and its deadline continue inside the modal loop.
        timer = wx.CallLater(50, drive)
        def open_workspace():
            try:
                show_translation(frame, initial_text=original, title='Synthetic public source', service=service)
            except BaseException as exc:
                failures.append(type(exc).__name__ + ': ' + str(exc))
            finally:
                # Exit the outer loop only after the modal has fully unwound.
                print('Native translation modal returned; phase:', phase[0], flush=True)
                wx.CallAfter(app.ExitMainLoop)
        wx.CallAfter(open_workspace)
        import faulthandler
        faulthandler.dump_traceback_later(100)
        try:
            app.MainLoop()
        finally:
            faulthandler.cancel_dump_traceback_later()
            active[0] = False
            timer.Stop()
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            wx.MessageBox, wx.DirDialog = native_message_box, native_dir_dialog
            translation_sources.choose_translation_sources = native_source_selector
            for source_timer in source_timers:
                source_timer.Stop()
            frame.Destroy()
        if failures or phase[0] != 5:
            raise RuntimeError('Native translation probe failed: ' + '; '.join(failures or ['incomplete flow']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(final, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(final, ensure_ascii=False))


if __name__ == '__main__':
    main()
