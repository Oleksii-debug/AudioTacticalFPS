"""Accessible profile management and individually reviewed provider tasks.

Opening settings never sends a request. The default remains LOCAL ONLY. A
native confirmation binds one task's exact text, profile and estimated budget.
"""
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import tempfile
import uuid

from ..providers import (BudgetLedger, JobApproval, ProfileStore, ProviderGateway,
                         ProviderProfile, ProviderRequest, ProviderError,
                         GOOGLE_TRANSLATION_ENDPOINT, GOOGLE_TRANSLATION_PROVIDERS,
                         TRANSLATION_PROVIDERS, LOCAL_TRANSLATION_PROVIDER,
                         google_v3_parent, decode_profile_import, MAX_REQUEST)
from ..research.export_file import _filename, _destination


def parse_budget(text):
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError('Enter a nonnegative decimal amount in USD.') from None
    if not amount.is_finite() or amount < 0 or amount > 10000 or amount * 1000000 != (amount * 1000000).to_integral_value():
        raise ValueError('Enter a USD amount from 0 to 10000 with at most six decimal places.')
    return int(amount * 1000000)


class ConnectionsController:
    def __init__(self, root, *, vault=None, transport=None):
        self.root = Path(root)
        self.store = ProfileStore(self.root / 'api-connections.json', vault)
        self.transport = transport
        info = self.root.lstat()
        self.import_root_identity = info.st_dev, info.st_ino

    def review_profiles(self, path):
        """Capture one bounded, immutable local settings file for explicit review."""
        from ..audio.jobs import file_identity, regular_file, verified_reader
        path = Path(path).absolute()
        before = regular_file(path)
        if before.st_nlink != 1 or not 0 < before.st_size <= MAX_REQUEST:
            raise ProviderError('Choose a regular unlinked profile export within the byte budget.')
        with verified_reader(path, expected_identity=file_identity(before)) as stream:
            raw = stream.read(MAX_REQUEST + 1)
        if len(raw) != before.st_size:
            raise ProviderError('The selected profile export changed during review.')
        return raw, decode_profile_import(raw)

    def import_profiles(self, raw, *, approved=False):
        if approved is not True:
            raise ProviderError('Review the selected account settings before importing.')
        from ..audio.jobs import checked_directory
        checked = checked_directory(self.root)
        info = checked.stat()
        if (info.st_dev, info.st_ino) != self.import_root_identity:
            raise ProviderError('The selected library changed. Reopen connection settings before importing.')
        return self.store.import_profiles(raw)

    def review_recovery(self, path, *, cancelled=lambda: False):
        from ..provider_recovery import read_record
        return read_record(Path(path).absolute(), cancelled=cancelled)

    def import_recovery(self, raw, *, approved=False):
        from ..provider_recovery import decode, profile_export
        return self.import_profiles(profile_export(decode(raw)), approved=approved)

    def export_recovery(self, raw, path, *, approval=None, cancelled=lambda: False):
        from ..provider_recovery import decode
        from ..research.export_file import write_export
        decode(raw)
        return write_export(path, raw.decode('utf-8'), private_root=self.root,
                            approval=approval, require_absent=approval is None, cancelled=cancelled)

    def run(self, profile, request, output, *, approved=False, estimate_micros=None,
            cancelled=lambda: False):
        if approved is not True:
            raise ProviderError('Review and approve the exact task before running it.')
        requested = Path(output).absolute()
        _filename(requested.name)
        output, parent_identity, previous = _destination(requested, self.root)
        if previous is not None:
            raise FileExistsError('Choose a new output file in a local folder.')
        approval = JobApproval(uuid.uuid4().hex, profile.fingerprint,
            (request.fingerprint(profile),), budget_micros=estimate_micros,
            mode='LOCAL_ONLY' if profile.local else 'APPROVED_REMOTE', allow_loopback=profile.local)
        ledger = BudgetLedger(self.root / 'api-operations.sqlite')
        gateway = ProviderGateway(ledger, vault=self.store.vault, transport=self.transport)
        operation = uuid.uuid4().hex
        # Prove basic output access before incurring a provider request.
        fd, temporary = tempfile.mkstemp(prefix='.provider-result-', dir=output.parent)
        completed, durable = False, False
        try:
            with os.fdopen(fd, 'wb') as stream:
                result = gateway.perform(profile, request, approval=approval, operation_id=operation,
                                         estimated_micros=estimate_micros, cancelled=cancelled)
                completed = True
                if request.capability == 'llm':
                    result = json.loads(result)['choices'][0]['message']['content'].encode('utf-8')
                stream.write(result)
                stream.flush()
                os.fsync(stream.fileno())
                durable = True
            checked, parent_now, current = _destination(requested, self.root)
            if checked != output or parent_now != parent_identity or current is not None:
                raise ValueError('The selected output destination changed while the request was running.')
            os.link(temporary, output)
        except Exception:
            if completed:
                state = f'Recover the complete result from {temporary}' if durable else f'A partial result may remain at {temporary}'
                raise ProviderError(f'The provider completed but its output could not be saved. '
                    f'{state}. Operation {operation}; do not repeat the paid request.') from None
            os.unlink(temporary)
            raise
        else:
            os.unlink(temporary)
        return {'output': str(output), 'operation': operation,
                'estimated_micros': estimate_micros, 'billed_cost': 'unknown'}


def show_api_connections(frame):
    import wx
    library = frame.library
    controller = ConnectionsController(library.root)

    class Dialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='AI and API connections', size=(800, 650))
            self.rows = []
            self.layout = wx.BoxSizer(wx.VERTICAL)
            self.layout.Add(wx.StaticText(self, label='Default: LOCAL ONLY. Profiles do not enable automatic external requests.'), 0, wx.ALL, 8)
            self.profiles = wx.ListBox(self)
            self.profiles.SetName('Saved provider accounts')
            self.layout.Add(self.profiles, 1, wx.EXPAND | wx.ALL, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Connection status')
            self.layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            for actions in (
                    (('&Add profile', self.add), ('&Edit profile', self.edit),
                     ('&Remove profile and key', self.remove)),
                    (('&Export profiles', self.export), ('&Import profiles', self.import_profiles),
                     ('Run a re&viewed task', self.run)),
                    (('Reco&ver saved provider settings', self.recover_profiles),)):
                buttons = wx.BoxSizer(wx.HORIZONTAL)
                for label, action in actions:
                    button = wx.Button(self, label=label)
                    button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, action)
                    buttons.Add(button, 0, wx.ALL, 4)
                self.layout.Add(buttons, 0, wx.EXPAND)
            self.layout.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 8)
            self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
            self.SetSizer(self.layout)
            self.profiles.Bind(wx.EVT_LISTBOX, self.describe)
            self.refresh()

        def refresh(self):
            self.rows = list(controller.store.profiles())
            self.profiles.SetItems([f'{p.name}: {p.provider}, {p.model}, '
                + ('local service' if p.local else 'remote, approval required') for p in self.rows])
            if self.rows:
                self.profiles.SetSelection(0)
            self.describe()

        def selected(self):
            choice = self.profiles.GetSelection()
            if not 0 <= choice < len(self.rows):
                raise ValueError('Select a saved provider profile.')
            return self.rows[choice]

        def describe(self, event=None):
            if not self.rows:
                self.status.ChangeValue('No profiles. Local voices and local music do not require API keys.')
                return
            p = self.selected()
            self.status.ChangeValue(f'{p.name}\nEndpoint: {p.endpoint}\nModel: {p.model}\n'
                f'Capabilities: {", ".join(p.capabilities)}\n'
                f'Credential: {"stored in Windows vault" if p.credential_ref else "not connected"}\n'
                'No requests have been sent by opening this dialog.')

        def prompt(self, title, label, value='', *, password=False, multiline=False):
            style = wx.OK | wx.CANCEL | (wx.TE_PASSWORD if password else 0) | (wx.TE_MULTILINE if multiline else 0)
            dialog = wx.TextEntryDialog(self, label, title, value, style=style)
            try:
                return dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()

        def choose(self, title, items):
            dialog = wx.SingleChoiceDialog(self, title, title, items)
            try:
                return dialog.GetSelection() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()

        def configure(self, previous=None):
            provider_index = self.choose('Provider adapter', [
                'Local or remote OpenAI-compatible text model',
                'ElevenLabs speech, music and sound effects',
                'Google Cloud Translation Basic v2 (NMT)',
                'Google Cloud Translation Advanced v3 (NMT; IAM/ADC OAuth)',
                'Local LibreTranslate service (explicit loopback; no credentials)'])
            if provider_index is None:
                return
            provider = ('openai-compatible', 'elevenlabs', *TRANSLATION_PROVIDERS)[provider_index]
            fields = {}
            prompts = [('name', 'Friendly account name', '')]
            if provider == 'google-translation-v2':
                # This adapter never accepts an arbitrary host, model or API key URL.
                fields.update(endpoint=GOOGLE_TRANSLATION_ENDPOINT, model='nmt')
            elif provider == 'google-translation-v3':
                parent = (google_v3_parent(previous.endpoint).split('/')
                          if previous is not None and previous.provider == provider else None)
                project = self.prompt('Advanced v3 project', 'Google Cloud numeric project number', parent[1] if parent else '')
                if project is None:
                    return
                location = self.prompt('Advanced v3 location',
                    'Model location: global, or the same region as your existing glossary (for example us-central1)',
                    parent[3] if parent else 'global')
                if location is None:
                    return
                fields.update(endpoint='https://translation.googleapis.com/v3/projects/' + project.strip()
                              + '/locations/' + location.strip(), model='general/nmt')
                google_v3_parent(fields['endpoint'])
            elif provider == LOCAL_TRANSLATION_PROVIDER:
                prompts.extend([
                    ('endpoint', 'Selected local translator origin; loopback IP and port', 'http://127.0.0.1:5000'),
                    ('model', 'Your reviewed local runtime label; the API does not attest model files', '')])
            else:
                prompts.extend([
                    ('endpoint', 'API base URL (loopback IP for local service)',
                     'http://127.0.0.1:8080/v1' if provider_index == 0 else 'https://api.elevenlabs.io/v1'),
                    ('model', 'Installed model ID or provider model ID', '')])
            for key, label, default in prompts:
                unchanged_adapter = previous is not None and previous.provider == provider
                value = self.prompt('Connection profile', label,
                    getattr(previous, key, default) if unchanged_adapter or key == 'name' else default)
                if value is None:
                    return
                fields[key] = value.strip()
            if provider_index == 0:
                kind = self.choose('Network location', ['User-installed loopback service', 'Remote HTTPS provider; per-task approval required'])
                if kind is None:
                    return
                local, capabilities = kind == 0, ('llm',)
            elif provider in TRANSLATION_PROVIDERS:
                local, capabilities = provider == LOCAL_TRANSLATION_PROVIDER, ('translation',)
            else:
                cap = self.choose('Capability for this profile and model', ['Speech synthesis', 'Music generation', 'Sound effects'])
                if cap is None:
                    return
                local, capabilities = False, (('tts', 'music', 'sound_effects')[cap],)
            advanced = provider == 'google-translation-v3'
            key = '' if provider == LOCAL_TRANSLATION_PROVIDER else self.prompt('OAuth access token' if advanced else 'API key',
                ('IAM/ADC OAuth access token (masked), not an API key or JSON file. '
                 'Leave blank only to keep an unchanged profile credential.' if advanced else
                 'API key (masked). Leave blank for local access or to keep an unchanged profile key.'),
                password=True)
            if key is None:
                return
            try:
                profile = ProviderProfile(previous.id if previous else uuid.uuid4().hex,
                    provider=provider, capabilities=capabilities, local=local,
                    credential_ref=(previous.credential_ref if previous and (not key or advanced)
                                    and provider != LOCAL_TRANSLATION_PROVIDER else None), **fields)
                if advanced and key:
                    expires = self.prompt('OAuth token expiry',
                        'Actual expiry with UTC offset, for example 2026-11-27T15:30:00Z. '
                        'This app does not discover ADC files, run gcloud or refresh tokens.')
                    if expires is None:
                        return
                    renewal = False
                    if previous is not None and previous.credential_ref and previous.fingerprint == profile.fingerprint:
                        decision = self.choose('OAuth credential update', [
                            'Replace credential; require fresh approval for saved jobs',
                            'I confirm this renews the same account; preserve exact existing approvals'])
                        if decision is None:
                            return
                        renewal = decision == 1
                    controller.store.save_google_token(profile, key, expires, renew_same_account=renewal)
                else:
                    controller.store.save(profile, secret=key or None)
            finally:
                key = ''
            self.refresh()
            self.status.SetValue('Profile saved. No credential test or generation request was sent.'
                                 + (' ' + controller.store.warning if controller.store.warning else ''))
            self.status.SetFocus()

        def add(self, event):
            try:
                self.configure()
            except Exception as exc:
                self.status.SetValue(str(exc))

        def edit(self, event):
            try:
                self.configure(self.selected())
            except Exception as exc:
                self.status.SetValue(str(exc))

        def remove(self, event):
            try:
                profile = self.selected()
                if wx.MessageBox(f'Remove {profile.name} and its saved API key?', 'Remove connection',
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    return
                controller.store.remove(profile.id)
                self.refresh()
                self.status.SetValue('Profile removed.'
                                     + (' ' + controller.store.warning if controller.store.warning else ''))
                self.status.SetFocus()
            except Exception as exc:
                self.status.SetValue(str(exc))

        def import_profiles(self, event):
            dialog = wx.FileDialog(self, 'Import profiles without API keys',
                wildcard='JSON files (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = Path(dialog.GetPath())
            finally:
                dialog.Destroy()
            try:
                raw, selected = controller.review_profiles(path)
                if not selected:
                    self.status.SetValue('The selected export contains no accounts.')
                    return
                names = '\n'.join(profile.name + ' — ' + profile.provider for profile in selected[:20])
                if len(selected) > 20:
                    names += '\n... and ' + str(len(selected) - 20) + ' more accounts.'
                notice = ('Import these ' + str(len(selected)) + ' captured account settings?\n\n' + names
                    + '\n\nExisting accounts are never replaced. Credential references are cleared. '
                    'Reconnect remote accounts explicitly; every task still needs its own review and approval. '
                    'Importing sends no requests.')
                if wx.MessageBox(notice, 'Review provider profile import',
                        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    return
                if frame.library is not library:
                    raise ProviderError('The selected library changed. Reopen connection settings before importing.')
                imported = controller.import_profiles(raw, approved=True)
                self.refresh()
                self.status.SetValue(str(len(imported)) + ' account settings imported without keys or approvals. '
                    'No provider request was sent.')
            except Exception as exc:
                self.status.SetValue(str(exc))
                self.status.SetFocus()

        def recover_profiles(self, event):
            from ..provider_recovery import DIRECTORY
            dialog = wx.FileDialog(self, 'Select passive provider recovery record',
                defaultDir=str(controller.root / DIRECTORY),
                wildcard='JSON files (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = Path(dialog.GetPath())
            finally:
                dialog.Destroy()
            self.EndModal(wx.ID_OK)
            frame.run_job('Reviewing saved provider settings and usage history',
                lambda: controller.review_recovery(path, cancelled=frame.cancel_event.is_set),
                done=lambda result: show_provider_recovery(frame, controller, result, library=library))

        def export(self, event):
            dialog = wx.FileDialog(self, 'Export profiles without API keys', wildcard='JSON files (*.json)|*.json', style=wx.FD_SAVE)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = Path(dialog.GetPath())
            finally:
                dialog.Destroy()
            try:
                with path.open('x', encoding='utf-8') as output:
                    json.dump(controller.store.export_profiles(), output, ensure_ascii=False, indent=2)
                self.status.SetValue('Profiles exported without keys or credential references. Reconnect on the destination computer.')
            except Exception as exc:
                self.status.SetValue(str(exc))

        def run(self, event):
            try:
                profile = self.selected()
                if profile.provider in TRANSLATION_PROVIDERS:
                    from .translation_dialog import show_translation
                    self.EndModal(wx.ID_OK)
                    wx.CallAfter(show_translation, frame)
                    return
                index = self.choose('Task', list(profile.capabilities))
                if index is None:
                    return
                capability = profile.capabilities[index]
                text = self.prompt('Task data', 'Only this exact text will be sent to the selected service.', multiline=True)
                if text is None:
                    return
                voice = self.prompt('Voice', 'Provider voice ID') if capability == 'tts' else None
                if capability == 'tts' and voice is None:
                    return
                duration = None
                if capability in ('music', 'sound_effects'):
                    value = self.prompt('Duration', 'Duration in seconds for this request', '10')
                    if value is None:
                        return
                    duration = int(value)
                request = ProviderRequest(capability, text, voice, duration)
                value = self.prompt('Approved estimated cost', 'Enter your reviewed maximum estimate in USD. Provider billing can differ; unknown cost requires review.', '0' if profile.local else '')
                if value is None:
                    return
                estimate = parse_budget(value)
                dialog = wx.FileDialog(self, 'Choose a new result file', defaultFile='result.txt' if capability == 'llm' else 'result.mp3', style=wx.FD_SAVE)
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    output = Path(dialog.GetPath())
                finally:
                    dialog.Destroy()
                notice = (f'Provider: {profile.name}\nEndpoint: {profile.endpoint}\nModel: {profile.model}\n'
                    f'Task: {capability}\nText length: {len(text)} characters.\nEstimated cost approved: USD {value}.\n'
                    f'Output: {output}\nOne request only. No automatic retry or fallback. '
                    'The provider may retain submitted text according to its terms. Send this exact task now?')
                if wx.MessageBox(notice, 'Approve task data and estimated cost',
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    return
                self.EndModal(wx.ID_OK)
                frame.run_job('Running approved provider task', lambda: controller.run(profile, request, output,
                    approved=True, estimate_micros=estimate, cancelled=frame.cancel_event.is_set),
                    done=lambda result: frame.report(f'Provider output saved: {result["output"]}. '
                        'Recorded cost is an estimate; actual billing was not supplied by the provider.'))
            except Exception as exc:
                self.status.SetValue(str(exc))
                self.status.SetFocus()

    dialog = Dialog()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def show_provider_recovery(frame, controller, captured, *, library):
    """Inspect inert recovery data; importing still needs a new explicit review."""
    if getattr(frame, '_closing', False) or frame.library is not library:
        frame.report('The selected library changed. Reopen connection settings to review recovered data.')
        return
    import wx
    from ..provider_recovery import describe, profile_export
    from ..research.export_file import export_completion
    from .export_destination import approve_export_destination
    raw, value = captured
    settings = profile_export(value)
    selected = decode_profile_import(settings)

    class RecoveryDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Recovered provider settings and usage history', size=(760, 540))
            layout = wx.BoxSizer(wx.VERTICAL)
            text = describe(value)
            if selected:
                text += '\n\nAccounts:\n' + '\n'.join(
                    row.name + ' — ' + row.provider for row in selected[:20])
                if len(selected) > 20:
                    text += '\n... and ' + str(len(selected) - 20) + ' more.'
            self.status = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Passive provider settings and usage history; no restored authority')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            actions = wx.BoxSizer(wx.HORIZONTAL)
            for label, action in (('&Import recovered settings', self.import_settings),
                                  ('&Export recovery history', self.export_history)):
                button = wx.Button(self, label=label)
                button.SetName(label.replace('&', ''))
                button.Bind(wx.EVT_BUTTON, action)
                button.Enable(bool(selected) if action == self.import_settings else True)
                actions.Add(button, 0, wx.ALL, 4)
            layout.Add(actions, 0, wx.EXPAND)
            layout.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 8)
            self.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
            self.SetSizer(layout)

        def current(self):
            if getattr(frame, '_closing', False) or frame.library is not library:
                raise ProviderError('The selected library changed. Review recovery again in the current library.')

        def import_settings(self, event):
            try:
                self.current()
                if wx.MessageBox('Import ' + str(len(selected)) + ' disconnected account settings? '
                        'Existing accounts are never replaced. Keys and prior approvals are not restored. '
                        'No request will be sent.',
                        'Review recovered provider settings', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    return
                self.current()
                imported = controller.import_recovery(raw, approved=True)
                self.status.AppendText('\n\nImported ' + str(len(imported)) +
                    ' disconnected account settings. Reconnect remote accounts and review new tasks separately.')
            except Exception as exc:
                self.status.AppendText('\n\n' + str(exc))
                self.status.SetFocus()

        def export_history(self, event):
            try:
                self.current()
                dialog = wx.FileDialog(self, 'Export passive provider recovery history',
                    defaultFile='provider-recovery.json', wildcard='JSON files (*.json)|*.json',
                    style=wx.FD_SAVE)
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    target = Path(dialog.GetPath())
                finally:
                    dialog.Destroy()
                approval = approve_export_destination(self, target, private_root=controller.root)
                if approval is None:
                    return
                self.current()
                self.EndModal(wx.ID_OK)
                frame.run_job('Exporting passive provider recovery history',
                    lambda: controller.export_recovery(raw, target, approval=approval,
                        cancelled=frame.cancel_event.is_set),
                    done=lambda receipt: frame.report(export_completion(
                        'Passive provider recovery history exported.', receipt)))
            except Exception as exc:
                self.status.AppendText('\n\n' + str(exc))
                self.status.SetFocus()

    dialog = RecoveryDialog()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def install_api_connection_actions(frame):
    import wx
    menu = wx.Menu()
    item = menu.Append(wx.ID_ANY, 'AI and API &connections...')
    frame.Bind(wx.EVT_MENU, lambda event: show_api_connections(frame), item)
    frame.menu_bar.Append(menu, 'S&ettings')
