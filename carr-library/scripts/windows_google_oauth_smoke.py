"""Actual Windows Google-renewal controls with synthetic vault and HTTPS.

The existing DesktopFrame, settings dialog, ProfileStore, Gateway, usage ledger
and shared worker execute. Credentials and both HTTPS endpoints are controlled
fixtures; no account, browser, real network, or Google service is exercised.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import replace
from hashlib import sha256
import http.client
import importlib
import json
import os
from pathlib import Path
import socket
import ssl
import sysconfig
from tempfile import TemporaryDirectory
import threading
import time
import traceback
from urllib.parse import parse_qs
from uuid import uuid4

ORIGINS = (
    'carr_library.assistant.google_oauth', 'carr_library.assistant.profiles',
    'carr_library.assistant.providers', 'carr_library.assistant.credentials',
    'carr_library.assistant.models', 'carr_library.assistant.translation',
    'carr_library.ui.google_oauth', 'carr_library.ui.assistant_dialog',
    'carr_library.ui.desktop_app', 'carr_library.ui.worker_lifetime',
    'carr_library.ui.progress_panel', 'carr_library.ui.command_registry',
    'carr_library.ui.keyboard_settings', 'carr_library.progress',
    'carr_library.application', 'carr_library.durable_state',
    'carr_library.native_runtime', 'carr_library.speech._io',
)
CLIENT = '123456789-native-fixture.apps.googleusercontent.com'
SECRET = 'SYNTHETIC_DESKTOP_SECRET_NATIVE_123'
REFRESH = 'SYNTHETIC_REFRESH_GRANT_NATIVE_123'
ACCESS = 'SYNTHETIC_ACCESS_TOKEN_NATIVE_123'
ENDPOINT = 'https://translation.googleapis.com/v3/projects/123456789/locations/global'
SELECTED_TEXT = 'Explicit synthetic excerpt Україна.'
FORM_TITLE = 'Automatic Google token renewal'
QUESTION_TITLE = 'Replace Google credential for this account'
SETTINGS_TITLE = 'AI and API connections — optional'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--synthetic-transport', action='store_true',
                        help='Required: acknowledge synthetic credentials and HTTPS responses.')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if os.name != 'nt' or not options.synthetic_transport:
        raise ValueError('Require actual Windows and --synthetic-transport; this is not a Google-account test.')

    import wx
    import carr_library.assistant as assistant
    from carr_library.application import LibraryApp
    from carr_library.assistant.google_oauth import parse_refresh_credential, refresh_credential
    from carr_library.assistant.profiles import ProfileStore, ProviderProfile, google_oauth_credential
    from carr_library.assistant.providers import Authorization, Gateway, ProviderError
    from carr_library.ui.assistant_dialog import show_provider_settings
    from carr_library.ui.desktop_app import DesktopFrame

    source_root = options.source_root.resolve(strict=True)
    installed = Path(os.environ.get('CARR_PROGRAM_DIR') or sysconfig.get_path('purelib')).resolve(strict=True)
    checks, errors, requests, connections, request_threads, network_attempts = [], [], [], [], [], []
    forms, futures, callbacks = [], [], []
    modal_loops = []
    unwind_pending = False
    gui_thread = threading.get_ident()
    token_status = 200

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        if name not in checks:
            checks.append(name)
            print('PASS: ' + name, flush=True)

    def origins():
        found = {}
        for name in ORIGINS:
            module = importlib.import_module(name)
            actual = Path(module.__file__).resolve(strict=True)
            expected = source_root.joinpath(*name.split('.')).with_suffix('.py')
            digest = sha256(actual.read_bytes()).hexdigest()
            if digest != sha256(expected.read_bytes()).hexdigest():
                raise AssertionError('Module bytes differ: ' + name)
            if options.require_installed and (not actual.is_relative_to(installed) or actual.is_relative_to(source_root)):
                raise AssertionError('The OAuth probe imported a source module: ' + name)
            found[name] = {'path': str(actual), 'sha256': digest, 'installed': actual.is_relative_to(installed)}
        return found

    before = origins()
    check('all eighteen OAuth settings and host module bytes match their requested origins', len(before) == 18)

    class Vault:
        """In-memory stand-in; the real Windows Credential Manager is never opened."""
        def __init__(self):
            self.values, self.reads, self.writes = {}, [], []
            self.after_get = lambda: None

        def get(self, identity, reference):
            self.reads.append((identity, reference))
            value = self.values[reference]
            self.after_get()
            return value

        def put(self, identity, value, *, reference=None):
            if reference is None or reference not in self.values:
                raise AssertionError('This probe only replaces its exact existing synthetic reference.')
            self.writes.append((identity, reference))
            self.values[reference] = value
            return reference

        def delete(self, identity, reference):
            raise AssertionError('Renewal must not delete or transfer this existing account.')

    vault = Vault()

    class Connection:
        """Same HTTPSConnection boundary as production, with no socket implementation."""
        def __init__(self, host, port, **kwargs):
            if (host not in ('oauth2.googleapis.com', 'translation.googleapis.com')
                    or port not in (None, 443)
                    or not isinstance(kwargs.get('context'), ssl.SSLContext)
                    or not kwargs['context'].check_hostname
                    or kwargs['context'].verify_mode != ssl.CERT_REQUIRED
                    or not 0 < kwargs.get('timeout', 0) <= 30):
                raise AssertionError('The actual Gateway changed the reviewed TLS authority or budget.')
            self.host, self.position, self.closed = host, 0, False
            connections.append(self)

        def request(self, method, route, body=None, headers=None):
            request_threads.append(threading.get_ident())
            requests.append({'host': self.host, 'method': method, 'route': route,
                             'body': body, 'headers': dict(headers)})
            if self.host == 'oauth2.googleapis.com':
                if method != 'POST' or route != '/token':
                    raise AssertionError('Renewal left the fixed token route.')
                self.status = token_status
                data = {'access_token': ACCESS, 'expires_in': 3600, 'token_type': 'Bearer',
                        'scope': 'https://www.googleapis.com/auth/cloud-platform'}
                if self.status != 200:
                    data = {'error': 'invalid_grant', 'error_description': SECRET + REFRESH}
            else:
                if method != 'POST' or route != '/v3/projects/123456789/locations/global:translateText':
                    raise AssertionError('Translation left the approved project route.')
                self.status = 200
                payload = json.loads(body)
                if payload['contents'] != [SELECTED_TEXT]:
                    raise AssertionError('An unselected payload reached the synthetic API.')
                data = {'translations': [{'translatedText': 'Синтетичний переклад.',
                                           'model': payload['model']}]}
            self.raw = json.dumps(data, ensure_ascii=True).encode('ascii')

        def getresponse(self):
            return self

        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)

        def read(self, size):
            block = self.raw[self.position:self.position + min(size, 97)]
            self.position += len(block)
            return block

        def close(self):
            self.closed = True

    def deny_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError('A real socket or name-resolution request is forbidden in this synthetic probe.')

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            unwind()
            return True  # Preserve the failure and unwind the active owned loop first.

    app = App(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    app.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes, user.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user.SetForegroundWindow.argtypes, user.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user.GetForegroundWindow.argtypes, user.GetForegroundWindow.restype = [], wintypes.HWND
    user.FindWindowW.argtypes, user.FindWindowW.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND
    user.GetFocus.argtypes, user.GetFocus.restype = [], wintypes.HWND
    user.GetDlgCtrlID.argtypes, user.GetDlgCtrlID.restype = [wintypes.HWND], ctypes.c_int
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetWindow.argtypes, user.GetWindow.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    user.GetDlgItem.argtypes, user.GetDlgItem.restype = [wintypes.HWND, ctypes.c_int], wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL

    def own_modal(window):
        loop = wx.EventLoopBase.GetActive()
        if loop is None or not loop.IsRunning() or loop.IsMain():
            raise AssertionError('The owned OAuth modal has no running native event loop.')
        modal_loops.append((window, window.GetHandle(), loop))

    def dismiss_owned_question():
        # Native MessageBox is not a wx.Dialog. Only this process's exact title
        # and an owner chain rooted in one of our live modal windows may be closed.
        handle = user.FindWindowW(None, QUESTION_TITLE)
        if not handle:
            return False
        process = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(process))
        owners = {native for window, native, _ in modal_loops if window and user.IsWindow(native)}
        parent = user.GetWindow(handle, 4)  # GW_OWNER
        for _ in range(16):
            if not parent or parent in owners:
                break
            parent = user.GetWindow(parent, 4)
        no_button = user.GetDlgItem(handle, 7)  # IDNO, never affirmative cleanup.
        if process.value != os.getpid() or parent not in owners or not no_button:
            raise AssertionError('Refusing to dismiss a confirmation not owned by this probe.')
        if not user.PostMessageW(handle, 0x0111, 7, no_button):  # WM_COMMAND
            raise OSError(ctypes.get_last_error(), 'Could not queue owned native No cleanup.')
        return True

    def unwind():
        nonlocal unwind_pending
        if unwind_pending:
            return
        unwind_pending = True

        def close_active():
            nonlocal unwind_pending
            unwind_pending = False
            try:
                if dismiss_owned_question():
                    return
                for window, handle, loop in reversed(modal_loops):
                    if window and user.IsWindow(handle) and window.IsModal() and loop.IsRunning():
                        window.EndModal(wx.ID_CANCEL)
                        return
                active = wx.EventLoopBase.GetActive()
                if errors and active is not None and active.IsMain():
                    active.ScheduleExit(1)
            except Exception:
                errors.append(traceback.format_exc())
                # Do not close an unowned native question or destroy its parent.
                # The finite watchdog remains active until cleanup is confirmed.
        wx.CallAfter(close_active)

    def pump(predicate, message, timeout=20, *, cleanup=False):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            app.Yield()
            time.sleep(.01)

    def settle():
        until = time.monotonic() + .04
        pump(lambda: time.monotonic() >= until, 'Native OAuth events did not settle.')

    def focus(control):
        if not control.IsEnabled():
            raise AssertionError('The selected OAuth native control is disabled.')
        top = wx.GetTopLevelParent(control)
        top.Show(); top.Raise()
        user.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        pump(lambda: user.GetForegroundWindow() == top.GetHandle() and wx.Window.FindFocus() is control,
             'The OAuth native control did not receive focus.')

    def key(control, value):
        focus(control)
        if not simulator.Char(value):
            raise AssertionError('Native keyboard input failed.')
        settle()

    def press(control):
        key(control, wx.WXK_SPACE)

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def named(window, name):
        found = [item for item in descendants(window) if item.GetName() == name]
        if len(found) != 1:
            raise AssertionError('Expected one accessible native control: ' + name)
        return found[0]

    def button(window, identity):
        found = [item for item in descendants(window) if isinstance(item, wx.Button) and item.GetId() == identity]
        if len(found) != 1:
            raise AssertionError('Expected one real native button with ID ' + str(identity))
        return found[0]

    def modal(title):
        found = [window for window in wx.GetTopLevelWindows()
                 if isinstance(window, wx.Dialog) and window.IsModal() and window.GetTitle() == title]
        return found[0] if len(found) == 1 else None

    def later(callback, *args):
        value = wx.CallLater(60, callback, *args)
        callbacks.append(value)
        return value

    questions = []

    def answer(yes, before_answer):
        try:
            pump(lambda: bool(user.FindWindowW(None, QUESTION_TITLE)), 'The real credential confirmation did not open.')
            handle = user.FindWindowW(None, QUESTION_TITLE)
            user.SetForegroundWindow(handle)
            pump(lambda: user.GetForegroundWindow() == handle and user.GetDlgCtrlID(user.GetFocus()) == 7,
                 'The actual credential confirmation did not default to No.')
            check('actual credential replacement confirmation defaults to native No', True)
            before_answer()
            questions.append(yes)
            if yes:
                if not simulator.Char(wx.WXK_LEFT):
                    raise AssertionError('Native Yes selection failed.')
                pump(lambda: user.GetDlgCtrlID(user.GetFocus()) == 6, 'Native Yes did not receive focus.')
            if not simulator.Char(wx.WXK_RETURN):
                raise AssertionError('Native confirmation Return failed.')
        except Exception:
            errors.append(traceback.format_exc())
            unwind()

    old_vault, old_https = assistant.WindowsCredentialVault, http.client.HTTPSConnection
    old_network = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo)
    frame = host_executor = None
    try:
        assistant.WindowsCredentialVault = lambda: vault
        http.client.HTTPSConnection = Connection
        socket.socket.connect = socket.socket.connect_ex = deny_network
        socket.create_connection = socket.getaddrinfo = deny_network
        with TemporaryDirectory(prefix='resonance-google-renewal-native-') as temporary:
            root = Path(temporary)
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Synthetic Google renewal qualification')
            store = ProfileStore(library.root / 'state' / 'providers')
            profile = ProviderProfile('native_google_refresh', 'Synthetic Google account', 'google-translation-v3',
                ENDPOINT, 'general/nmt', ('translation.text', 'translation.languages'),
                credential_ref='carr/provider/native_google_refresh/' + 'a' * 32, enabled=True)
            store.save(profile)
            profile_bytes = store.path(profile.id).read_bytes()
            legacy = google_oauth_credential('SYNTHETIC_LEGACY_ACCESS_NATIVE_123', time.time() + 3600, ENDPOINT)
            vault.values[profile.credential_ref] = legacy
            frame = DesktopFrame(library)
            frame.active = workspace, None
            host_executor, handle = frame.executor, frame.GetHandle()
            check('actual DesktopFrame owns this library and the existing shared worker',
                  frame.library is library and bool(user.IsWindow(handle)) and wx.Platform == '__WXMSW__')

            def drive_settings():
                try:
                    pump(lambda: modal(SETTINGS_TITLE) is not None, 'The actual provider settings did not open.')
                    settings = modal(SETTINGS_TITLE)
                    own_modal(settings)
                    listing = named(settings, 'Provider accounts')
                    status = named(settings, 'Connection configuration status')
                    renew = named(settings, 'Configure Google automatic token renewal')
                    key(listing, wx.WXK_HOME)
                    check('existing provider settings expose the selected account without a request',
                          listing.GetSelection() == 0 and listing.GetCount() == 1 and not vault.writes and not requests)

                    def renewal(action, before_answer=lambda: None):
                        captured = {}
                        def fill_form():
                            try:
                                pump(lambda: modal(FORM_TITLE) is not None, 'The real renewal form did not open.')
                                form = modal(FORM_TITLE)
                                own_modal(form)
                                captured['handle'] = form.GetHandle()
                                forms.append(captured['handle'])
                                fields = (
                                    named(form, 'Google Desktop app client ID'),
                                    named(form, 'Google Desktop app client secret; blank only if your client does not use one'),
                                    named(form, 'Authorized Google refresh token'))
                                check('renewal fields have distinct names and both credential fields are password controls',
                                      len({item.GetName() for item in fields}) == 3
                                      and bool(fields[1].GetWindowStyleFlag() & wx.TE_PASSWORD)
                                      and bool(fields[2].GetWindowStyleFlag() & wx.TE_PASSWORD))
                                for control, value in zip(fields, (CLIENT, SECRET, REFRESH)):
                                    focus(control)
                                    if not simulator.Text(value.encode('ascii')):
                                        raise AssertionError('Actual native credential text input failed.')
                                    pump(lambda control=control, value=value: control.GetValue() == value,
                                         'The native field did not receive its exact synthetic value.')
                                if action == 'cancel':
                                    press(button(form, wx.ID_CANCEL))
                                else:
                                    later(answer, action == 'yes', before_answer)
                                    press(button(form, wx.ID_OK))
                            except Exception:
                                errors.append(traceback.format_exc())
                                unwind()
                        later(fill_form)
                        press(renew)
                        pump(lambda: 'handle' in captured and not user.IsWindow(captured['handle']),
                             'The real renewal form did not finish and destroy its native controls.')
                        if errors:
                            raise RuntimeError('\n'.join(errors))

                    fixture = wx.Dialog(settings, title='Native OAuth failure cleanup fixture')
                    refused = []
                    def failure_cleanup_driver():
                        try:
                            own_modal(fixture)
                            later(unwind)
                            refused.append(wx.MessageBox('Synthetic failure cleanup; no credential is stored.',
                                QUESTION_TITLE, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, fixture))
                            if refused != [wx.NO] or not fixture.IsModal() or not settings.IsModal():
                                raise AssertionError('Native question cleanup touched its suspended parent.')
                            unwind()
                        except Exception:
                            errors.append(traceback.format_exc()); unwind()
                    try:
                        later(failure_cleanup_driver)
                        if fixture.ShowModal() != wx.ID_CANCEL:
                            raise AssertionError('The owned failure fixture did not unwind through Cancel.')
                        check('failure cleanup answers only the owned question No before unwinding its active modal',
                              refused == [wx.NO] and settings.IsModal() and not vault.writes and not requests)
                    finally:
                        fixture.Destroy()
                    if errors:
                        raise RuntimeError('\n'.join(errors))

                    renewal('cancel')
                    check('native form Cancel keeps the old credential and makes no confirmation or request',
                          not questions and not vault.writes and not requests and vault.values[profile.credential_ref] == legacy)
                    renewal('no')
                    check('native default No keeps the old credential and exact nonsecret profile',
                          questions == [False] and not vault.writes and not requests
                          and vault.values[profile.credential_ref] == legacy
                          and store.path(profile.id).read_bytes() == profile_bytes)
                    renewal('yes')
                    parsed = parse_refresh_credential(vault.values[profile.credential_ref], ENDPOINT)
                    check('native Yes stores only the explicitly reviewed grant in the synthetic vault',
                          questions == [False, True] and vault.writes == [(profile.id, profile.credential_ref)]
                          and parsed['client_id'] == CLIENT and parsed['client_secret'] == SECRET
                          and parsed['refresh_token'] == REFRESH and not requests)
                    check('credential replacement preserves exact profile bytes and account identity',
                          store.path(profile.id).read_bytes() == profile_bytes and store.get(profile.id) == profile)
                    check('successful configuration reports no provider contact and restores accessible status focus',
                          'No provider was contacted' in status.GetValue() and wx.Window.FindFocus() is status)
                    local_checks = [item for item in descendants(settings) if isinstance(item, wx.Button)
                                    and item.GetLabel().replace('&', '') == 'Check local configuration']
                    if len(local_checks) != 1:
                        raise AssertionError('The existing local configuration button is missing.')
                    key(listing, wx.WXK_HOME)
                    press(local_checks[0])
                    check('actual local configuration check reads the vault without exchanging a token',
                          len(vault.reads) == 1 and not requests
                          and 'No token exchange or API access was tested' in status.GetValue()
                          and wx.Window.FindFocus() is status)
                    saved_record = vault.values[profile.credential_ref]
                    renewal('yes', lambda: store.save(replace(profile, name='Changed during native confirmation')))
                    check('changing the selected profile during real confirmation refuses the global credential write',
                          len(vault.writes) == 1 and vault.values[profile.credential_ref] == saved_record
                          and 'changed' in status.GetValue().lower() and not requests)
                    store.save(profile)
                    check('profile export remains inert and does not transfer the credential reference',
                          json.loads(store.export_json())['profiles'][0]['credential_ref'] is None
                          and json.loads(store.export_json())['profiles'][0]['enabled'] is False
                          and all(value not in store.export_json() for value in (SECRET, REFRESH, ACCESS)))
                    press(button(settings, wx.ID_CLOSE))
                except Exception:
                    errors.append(traceback.format_exc())
                    unwind()

            def perform(title, operation):
                received = []
                future = frame.run_job(title, operation,
                    done=lambda value: received.append((True, value)),
                    failed=lambda error: received.append((False, error)))
                if future is None:
                    raise AssertionError('The existing desktop worker refused an idle synthetic operation.')
                futures.append(future)
                pump(lambda: bool(received) and future.done() and not frame.working,
                     'The shared host worker did not deliver its actual Gateway result.')
                return received[0]

            def scenario():
                nonlocal token_status
                try:
                    later(drive_settings)
                    show_provider_settings(frame)
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    check('all four renewal forms close without shutting down the borrowed host worker',
                          len(forms) == 4 and all(not user.IsWindow(item) for item in forms)
                          and not host_executor._shutdown and not frame.working)
                    gateway = Gateway(library.root / 'state' / 'native-renewal-probe', vault=vault, mode='LOCAL_ONLY')
                    def approval():
                        identity = uuid4().hex
                        return Authorization(id=identity, task_id=identity, profiles=((profile.id, profile.endpoint),),
                            operations=('translation.text',), data_classes=('synthetic-test',),
                            budget='1', max_calls=2, expires_at=time.time() + 120)
                    def request(grant, operation_id):
                        return gateway.request(profile, 'translation.text',
                            {'q': SELECTED_TEXT, 'source': 'en', 'target': 'uk'},
                            task_id=grant.task_id, data_class='synthetic-test', authorization=grant,
                            max_cost='0.1', operation_id=operation_id, cancelled=frame.cancel_event.is_set,
                            timeout=20, max_response_bytes=4096)

                    reads = len(vault.reads)
                    grant = approval()
                    ok, failure = perform('Synthetic LOCAL ONLY refusal', lambda: request(grant, uuid4().hex))
                    check('LOCAL ONLY refuses renewal before any credential read or HTTPS boundary',
                          not ok and isinstance(failure, PermissionError) and len(vault.reads) == reads and not requests)
                    gateway.mode = 'APPROVED_REMOTE'
                    def without_approval():
                        return gateway.request(profile, 'translation.text',
                            {'q': SELECTED_TEXT, 'source': 'en', 'target': 'uk'}, task_id='unapproved',
                            data_class='synthetic-test', max_cost='0.1', cancelled=frame.cancel_event.is_set)
                    ok, failure = perform('Synthetic missing-approval refusal', without_approval)
                    check('explicit remote mode still refuses a missing job authorization before vault or transport',
                          not ok and isinstance(failure, PermissionError) and len(vault.reads) == reads and not requests)

                    grant, operation_id = approval(), uuid4().hex
                    ok, result = perform('Explicitly approved synthetic translation', lambda: request(grant, operation_id))
                    check('the actual Gateway returns the synthetic translation and a complete usage receipt',
                          ok and result['text'] == 'Синтетичний переклад.'
                          and gateway.ledger.get(grant.id)['operations'][operation_id]['status'] == 'complete')
                    tokens = [value for value in requests if value['host'] == 'oauth2.googleapis.com']
                    api = [value for value in requests if value['host'] == 'translation.googleapis.com']
                    check('renewal uses only the fixed official form route without selected document text',
                          len(tokens) == 1 and tokens[0]['method'] == 'POST' and tokens[0]['route'] == '/token'
                          and parse_qs(tokens[0]['body'].decode('ascii'), strict_parsing=True) == {
                              'grant_type': ['refresh_token'], 'client_id': [CLIENT],
                              'client_secret': [SECRET], 'refresh_token': [REFRESH]}
                          and 'Authorization' not in tokens[0]['headers']
                          and SELECTED_TEXT.encode('utf-8') not in tokens[0]['body'])
                    check('the approved API receives the access token without the desktop secret or refresh grant',
                          len(api) == 1 and api[0]['headers']['Authorization'] == 'Bearer ' + ACCESS
                          and all(value not in json.dumps(api[0], default=str) for value in (SECRET, REFRESH)))
                    reads, count = len(vault.reads), len(requests)
                    grant, operation_id = approval(), uuid4().hex
                    ok, result = perform('Second explicitly approved synthetic translation', lambda: request(grant, operation_id))
                    check('the same Gateway cache still rereads the vault and records a separate approved result',
                          ok and len(vault.reads) == reads + 1 and len(requests) == count + 1
                          and len([value for value in requests if value['host'] == 'oauth2.googleapis.com']) == 1
                          and gateway.ledger.get(grant.id)['operations'][operation_id]['status'] == 'complete')

                    saved = vault.values.pop(profile.credential_ref)
                    count = len(requests)
                    grant, operation_id = approval(), uuid4().hex
                    ok, failure = perform('Synthetic removed-credential refusal', lambda: request(grant, operation_id))
                    check('a cached access token cannot bypass a removed vault record',
                          not ok and isinstance(failure, ProviderError) and len(requests) == count
                          and gateway.ledger.get(grant.id)['operations'][operation_id]['status'] == 'not_sent')
                    vault.values[profile.credential_ref] = saved
                    vault.after_get = frame.cancel_event.set
                    grant, operation_id = approval(), uuid4().hex
                    try:
                        ok, failure = perform('Synthetic cancellation after vault read', lambda: request(grant, operation_id))
                    finally:
                        vault.after_get = lambda: None
                    check('cancellation after the actual vault boundary prevents even cached transmission',
                          not ok and isinstance(failure, InterruptedError) and len(requests) == count
                          and gateway.ledger.get(grant.id)['operations'][operation_id]['status'] == 'not_sent')

                    vault.values[profile.credential_ref] = refresh_credential(CLIENT, SECRET, REFRESH + '_replaced', ENDPOINT)
                    token_status = 400
                    grant, operation_id = approval(), uuid4().hex
                    ok, failure = perform('Synthetic rejected renewal', lambda: request(grant, operation_id))
                    check('a failed renewal is not sent to translation and never retried automatically',
                          not ok and isinstance(failure, ProviderError) and len(requests) == count + 1
                          and requests[-1]['host'] == 'oauth2.googleapis.com'
                          and gateway.ledger.get(grant.id)['operations'][operation_id]['status'] == 'not_sent')
                    check('HTTP credential error bodies and secret material stay out of user-visible status',
                          'HTTP 400' in str(failure)
                          and all(value not in str(failure) + frame.status.GetValue() for value in (SECRET, REFRESH, ACCESS)))
                    check('token exchange never rewrites or deletes the explicitly saved credential',
                          vault.writes == [(profile.id, profile.credential_ref)])
                    check('all synthetic HTTPS boundaries run on the one existing non-GUI host worker',
                          len(request_threads) == 4 and len(set(request_threads)) == 1
                          and gui_thread not in request_threads and len(futures) == 7)
                    check('every synthetic connection is closed and no real socket was attempted',
                          len(connections) == 4 and all(item.closed for item in connections) and not network_attempts)
                    check('persistent profile and usage files contain none of the synthetic credential material',
                          all(all(secret not in path.read_text(encoding='utf-8') for secret in (SECRET, REFRESH, ACCESS))
                              for path in library.root.rglob('*.json')))
                    check('the exact nonsecret account survives every worker result and refusal',
                          store.path(profile.id).read_bytes() == profile_bytes)
                    frame.Close()
                    pump(lambda: not user.IsWindow(handle), 'The actual DesktopFrame close path did not finish.')
                    host_executor.shutdown(wait=True, cancel_futures=True)
                    check('actual host closure drains all operations and finalizers before reporting success',
                          not user.IsWindow(handle) and all(future.done() for future in futures)
                          and not host_executor.cleanup_failures)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    app.ExitMainLoop()

            try:
                frame.Show(); app.SetTopWindow(frame)
                wx.CallAfter(scenario)
                app.MainLoop()
                if errors:
                    raise RuntimeError('\n'.join(errors))
            finally:
                for callback in callbacks:
                    callback.Stop()
                unwind()
                if user.IsWindow(handle):
                    frame.Close(force=True)
                    pump(lambda: not user.IsWindow(handle), 'Failure cleanup did not close the host.', cleanup=True)
                host_executor.shutdown(wait=True, cancel_futures=True)
    finally:
        assistant.WindowsCredentialVault, http.client.HTTPSConnection = old_vault, old_https
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo = old_network

    check('all requested module bytes remain unchanged after native and worker cleanup', origins() == before)
    report = {'status': 'PASS', 'checks': checks, 'installed_modules': before,
        'actual_windows_controls': 'PASS', 'actual_profile_store': 'PASS', 'actual_gateway_and_ledger': 'PASS',
        'synthetic_vault_and_https': 'PASS', 'shared_host_worker': 'PASS', 'native_cleanup': 'PASS',
        'native_renewal_forms': len(forms), 'synthetic_token_requests': 2, 'synthetic_translation_requests': 2,
        'real_google': 'NOT_RUN', 'real_credential_manager': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}
    if options.output is not None:
        from carr_library.speech._io import new_output
        with new_output(options.output) as output:
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    finished = threading.Event()
    def watchdog():
        if not finished.wait(180):
            os.write(2, b'Native Google-renewal probe exceeded its work and cleanup deadline.\n')
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True).start()
    native = None
    try:
        import faulthandler
        faulthandler.enable(all_threads=True)
        from carr_library.native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        raise SystemExit(main())
    finally:
        if native is not None:
            native.close()
        finished.set()
