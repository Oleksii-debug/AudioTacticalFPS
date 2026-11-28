"""Actual installed Windows controls/shared workers with synthetic provider replies.

This is not a live Google, translation-quality, acoustic or manual NVDA test.
"""
import json
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time

def main():
    if sys.platform != 'win32':
        raise RuntimeError('This native acceptance probe requires actual Windows.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import canonical_gateway
    from carr_library.assistant.profiles import ProfileStore, ProviderProfile
    from carr_library.assistant.providers import Authorization, Gateway, HTTPResult, HTTPTransport
    from carr_library.assistant.translation import TranslationService, translation_approval, ENDPOINT
    from carr_library.assistant.library_translation import capture_library_sources, LibrarySelection
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.translation_dialog import create_translation_dialog

    app = wx.App(False)
    failures, calls, checks = [], [], []
    old_transport, old_vault = HTTPTransport.request, canonical_gateway.WindowsCredentialVault
    class Vault:
        def get(self, identity, reference):
            return 'SYNTHETIC_NATIVE_TRANSLATION_KEY'
    def response(self, profile, route, body, headers, *, method='POST', **kwargs):
        calls.append(method)
        data = ({'languages': [{'language': 'en', 'name': 'English'},
                               {'language': 'uk', 'name': 'Ukrainian'}]} if method == 'GET' else
                {'translations': [{'translatedText': 'Переклад: ' + json.loads(body)['q'],
                                   'detectedSourceLanguage': 'en'}]})
        return HTTPResult('application/json', json.dumps({'data': data}, ensure_ascii=False).encode(), 200)
    HTTPTransport.request, canonical_gateway.WindowsCredentialVault = response, Vault
    frame = dialog = None
    def pump(predicate, label, seconds=30):
        deadline = monotonic() + seconds
        while not predicate():
            app.Yield()
            if failures:
                raise AssertionError(failures[0])
            if monotonic() >= deadline:
                raise AssertionError('Native timeout: ' + label)
            sleep(.01)
        app.Yield()
    def click(button):
        button.GetEventHandler().ProcessEvent(wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId()))
    def choose(title):
        deadline = monotonic() + 20
        def poll():
            try:
                for window in wx.GetTopLevelWindows():
                    if window.GetTitle() == title and window.IsShown():
                        window.SetSelection(0)
                        window.EndModal(wx.ID_OK)
                        return
                if monotonic() >= deadline:
                    raise AssertionError('Native chooser unavailable: ' + title)
                wx.CallLater(20, poll)
            except Exception as exc:
                failures.append(str(exc))
        wx.CallLater(20, poll)
    try:
        with TemporaryDirectory(prefix='carr-native-translation-') as temporary:
            root = Path(temporary)
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Native linked originals')
            original = 'Original exact CRLF.\r\nUkrainian: Україна.\fSecond chapter.'
            imported = library.add_text(workspace, 'Native original', original)
            p = ProviderProfile('native_translation', 'Synthetic native account', 'google-translation-v2',
                ENDPOINT, 'nmt', capabilities=('translation.text', 'translation.languages'), enabled=True,
                credential_ref='carr/provider/native_translation/' + 'd' * 32)
            profiles = ProfileStore(library.root / 'state/providers')
            profiles.save(p)
            service = TranslationService(library.root / 'state/translation', library=library, workspace=workspace,
                gateway=Gateway(library.root / 'state/translation/provider', mode='APPROVED_REMOTE'))
            approval = Authorization('native_languages', 'native_languages', ((p.id, p.endpoint),),
                ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
            service.refresh_languages(p, authorization=approval)
            frame = DesktopFrame(library)
            frame.Show()
            app.Yield()
            assert frame.active == (workspace, None)
            dialog = create_translation_dialog(frame)
            dialog.Show()
            controls = dialog.translation_controls
            for key in ('input_scope', 'selected', 'budget', 'status', 'sources', 'read'):
                assert controls[key].GetName().strip()
            controls['input_scope'].SetSelection(2)
            dialog.on_scope_changed()
            choose('Choose translation originals')
            click(controls['sources'])
            pump(lambda: not frame.working and 'captured' in controls['status'].GetValue(),
                 'source capture through the shared background job')
            assert not controls['selected'].IsEditable()
            selected = capture_library_sources(library, workspace, (LibrarySelection(imported['edition']),))
            assert controls['selected'].GetValue().replace('\r\n', '\n') == selected.text.replace('\r\n', '\n')
            controls['budget'].ChangeValue('1')
            controls['source'].ChangeValue('auto')
            click(controls['preview'])
            pump(lambda: not frame.working and 'No request sent' in controls['status'].GetValue(),
                 'exact CRLF preview')
            assert selected.binding['sha256'] in controls['status'].GetValue()
            assert calls == ['GET']
            checks += ['named native controls and read-only captured source',
                       'actual shared-worker source capture and exact CRLF preview without transmission']

            envelope, cost = translation_approval(p, selected.text, 'auto', 'uk', '1',
                                                  source_binding=selected.binding)
            job = service.submit(p, selected.text, 'auto', 'uk',
                authorization=envelope, per_call_cost=cost, source_binding=selected.binding)
            assert service.resume(job)['status'] == 'complete'
            choose('Translation jobs')
            click(controls['jobs'])
            pump(lambda: not frame.working and 'Saved job ' + job in controls['status'].GetValue(), 'saved selection')
            count = len(calls)
            controls['budget'].ChangeValue('2')
            click(controls['run'])
            pump(lambda: not frame.working and 'Visible settings differ' in controls['status'].GetValue(),
                 'changed visible budget refusal')
            assert len(calls) == count and wx.Window.FindFocus() is controls['status']
            choose('Translation jobs')
            click(controls['jobs'])
            pump(lambda: not frame.working and 'Saved job ' + job in controls['status'].GetValue(), 'restore saved fields')
            controls['selected'].ChangeValue('Changed visible selection')
            click(controls['export'])
            pump(lambda: not frame.working and 'Visible settings differ' in controls['status'].GetValue(),
                 'changed visible export refusal')
            assert len(calls) == count
            checks += ['saved exact account/languages/budget/source restoration',
                       'native resume/export handlers reject changed visible scope and focus the error']

            dialog.on_close()
            dialog.Destroy()
            dialog = None
            profiles.path(p.id).unlink()
            dialog = create_translation_dialog(frame)
            dialog.Show()
            controls = dialog.translation_controls
            choose('Translation jobs')
            click(controls['jobs'])
            pump(lambda: not frame.working and 'Account unavailable' in controls['status'].GetValue(),
                 'offline saved-account selection')
            assert controls['provider'].GetSelection() == wx.NOT_FOUND
            count = len(calls)
            click(controls['run'])
            assert 'account' in controls['status'].GetValue() and len(calls) == count
            source = dialog.translation_service.reading_source(job, view='bilingual')
            reading = source.reading_document()
            assert reading.source_text == selected.text
            assert reading.source_anchor == selected.binding['sources'][0]['anchor']
            assert 'ORIGINAL\n' in reading.text and 'TRANSLATION\n' in reading.text
            assert controls['read'].IsEnabled()
            inspected = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-m', 'carr_library', 'translate',
                '--data-dir', str(library.root), '--workspace', workspace, 'inspect', '--job', job],
                capture_output=True, text=True, encoding='utf-8', timeout=30)
            assert inspected.returncode == 0, inspected.stderr
            assert json.loads(inspected.stdout)['source']['source_binding_sha256'] == selected.binding['sha256']
            checks.append('actual installed CLI dispatch reads the same immutable source/job/receipt contract')
            assert library.assets.resolve(imported['asset']).read_bytes() == original.encode('utf-8')
            checks += ['unavailable saved account blocks remote resume while keeping offline linked reading',
                       'bilingual shared-reader document retains original source anchor',
                       'retained original bytes unchanged']
            print(json.dumps({'status': 'PASS', 'platform': sys.platform, 'checks': checks,
                'provider_transport': 'synthetic protocol only', 'live_google': 'NOT_RUN',
                'translation_quality': 'NOT_RUN', 'acoustic_inference': 'NOT_RUN',
                'manual_nvda': 'NOT_RUN'}, ensure_ascii=False, indent=2))
    finally:
        HTTPTransport.request, canonical_gateway.WindowsCredentialVault = old_transport, old_vault
        if dialog is not None:
            dialog.on_close()
            dialog.Destroy()
        if frame is not None:
            frame.Close()
            app.Yield()
        if native_handle is not None:
            native_handle.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
