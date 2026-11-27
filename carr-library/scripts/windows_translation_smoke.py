"""Actual native wx translation flow with synthetic HTTP/vault boundaries only.

Run from an installed wheel on Windows. This proves native controls, worker
completion, review/approval wiring and real derivative files; it does not call
Google, claim translation quality, test a real account, or claim NVDA coverage.
"""
from hashlib import sha256
import json
import socket
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Timer
from time import monotonic


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'windows_execution': 'NOT_RUN'}))
        return 77
    bundle = args.bundle.resolve()
    import carr_library
    if not Path(carr_library.__file__).resolve().is_relative_to(bundle / '.venv'):
        raise ValueError('The probe must use this bundle\'s actual installed application.')
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime(bundle_root=bundle)
    assert native_handle is not None
    import wx
    from carr_library import providers
    from carr_library.assistant.translation import TranslationService
    from carr_library.providers import (
        BudgetLedger, ProfileStore, ProviderGateway, ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT)
    from carr_library.reader.narration_text import NarrationDraft
    from carr_library.ui.translation_dialog import show_translation
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.application import LibraryApp
    source_root = Path(__file__).resolve().parents[1] / 'src/carr_library'
    installed_root = Path(carr_library.__file__).resolve().parent
    modules = ('providers.py', 'library_backup.py', 'assistant/state.py',
               'assistant/translation.py', 'assistant/translation_state.py',
               'ui/api_connections.py', 'ui/translation_dialog.py', 'ui/desktop_app.py')
    hashes = {}
    for name in modules:
        raw = (source_root / name).read_bytes()
        if (installed_root / name).read_bytes() != raw:
            raise ValueError('Installed translation source differs from the acceptance checkout.')
        hashes[name] = sha256(raw).hexdigest()

    original = 'Public synthetic source. Український текст. No private book content.'
    requests, approvals, failures, network_attempts = [], [], [], []
    def offline(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError('Network is disabled for synthetic translation acceptance.')
    socket.getaddrinfo = offline
    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    def force_failure():
        import os
        try:
            print(json.dumps({'status': 'FAIL', 'windows_execution': 'FAIL',
                              'forced_process_exit': True,
                              'error': 'Native translation acceptance exceeded its process deadline.',
                              'failures': failures}, ensure_ascii=False), flush=True)
        finally:
            os._exit(1)

    watchdog = Timer(60, force_failure)
    watchdog.daemon = True
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
        root, exports = Path(folder) / 'library', Path(folder) / 'exports'
        root.mkdir()
        exports.mkdir()
        vault = Vault()
        profiles = ProfileStore(root / 'api-connections.json', vault)
        profiles.save(ProviderProfile('native-google', 'Synthetic NMT account',
            'google-translation-v2', GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',)),
            secret='synthetic-native-key-not-a-real-credential')
        service = TranslationService(root, profiles=profiles,
            gateway=ProviderGateway(BudgetLedger(root / 'api-operations.sqlite'), vault=vault, transport=Transport()))

        frame = DesktopFrame(LibraryApp(root))
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
                if dialog is None:
                    return
                if phase[0] == 0:
                    assert not requests
                    assert child(dialog, 'Selected text; originals remain unchanged').GetValue() == original
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
                    measured = frame.progress_view.tracker.snapshot()
                    assert measured.state == 'completed' and measured.percent == 100
                    assert not child(dialog, 'Selected text; originals remain unchanged').IsEnabled()
                    button(dialog, 'Export derivative')
                    phase[0] = 4
                elif phase[0] == 4 and not frame.working:
                    outputs = list(exports.glob('Translation-*'))
                    assert len(outputs) == 1
                    output = outputs[0]
                    report = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
                    assert report['quotation_authority'] is False
                    assert (output / 'original.txt').read_bytes() == original.encode()
                    draft = NarrationDraft.from_dict(json.loads(
                        (output / 'translation.narration.json').read_text(encoding='utf-8')), source_text=original)
                    assert draft.source_range(0, len(draft.text))[2] is False
                    for name, digest in report['files'].items():
                        assert sha256((output / name).read_bytes()).hexdigest() == digest
                    assert len(approvals) == 2
                    assert [row[0] for row in requests] == ['languages', 'text']
                    assert not network_attempts
                    final.update(schema='carr-native-translation-smoke/1', status='PASS',
                        installed_module=str(Path(providers.__file__).resolve()), native_controls=True,
                        windows_execution='PASS', source_commit=manifest['source_commit'],
                        source_hashes=hashes, desktop_worker='actual DesktopFrame.run_job',
                        synthetic_provider_requests=2, exact_reviewed_pair='en->uk', source_immutable=True,
                        quotation_authority=False, derivative_hashes=report['files'],
                        approval_and_folder_picker='injected explicit synthetic decisions',
                        real_network='blocked', actual_google_api='NOT_RUN',
                        actual_credential_vault='NOT_RUN', nvda='NOT_RUN')
                    phase[0] = 5
                    timer.Stop()
                    button(dialog, 'Close')
                    wx.CallAfter(app.ExitMainLoop)
            except BaseException as exc:
                failures.append(type(exc).__name__ + ': ' + str(exc))
                timer.Stop()
                frame.cancel_event.set()
                if dialog is not None:
                    close = wx.CloseEvent(wx.EVT_CLOSE.typeId, dialog.GetId())
                    dialog.GetEventHandler().ProcessEvent(close)
                wx.CallAfter(app.ExitMainLoop)

        frame.Bind(wx.EVT_TIMER, drive, timer)
        timer.Start(50)
        watchdog.start()
        wx.CallAfter(show_translation, frame, initial_text=original, title='Synthetic public source', service=service)
        try:
            app.MainLoop()
        finally:
            timer.Stop()
            frame._closing = True
            frame.cancel_event.set()
            try:
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.progress_view.close()
                wx.MessageBox, wx.DirDialog = native_message_box, native_dir_dialog
                frame.Destroy()
            finally:
                watchdog.cancel()
        if failures or phase[0] != 5:
            raise RuntimeError('Native translation probe failed: ' + '; '.join(failures or ['incomplete flow']))
    for name, digest in hashes.items():
        if sha256((source_root / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Acceptance source changed during native execution.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(final, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(final, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
