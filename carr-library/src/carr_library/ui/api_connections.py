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
                         ProviderProfile, ProviderRequest, ProviderError)
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
        translation = None
        try:
            with os.fdopen(fd, 'wb') as stream:
                result = gateway.perform(profile, request, approval=approval, operation_id=operation,
                                         estimated_micros=estimate_micros, cancelled=cancelled)
                completed = True
                if request.capability == 'llm':
                    result = json.loads(result)['choices'][0]['message']['content'].encode('utf-8')
                elif request.capability == 'translation':
                    from ..translation import translation_document
                    document = translation_document(profile, request, result,
                        operation_id=operation, estimated_micros=estimate_micros)
                    translation = document['translation']['text']
                    result = json.dumps(document, ensure_ascii=False, sort_keys=True,
                                        allow_nan=False, indent=2).encode('utf-8')
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
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        else:
            # Publication is the commit point. Failure to remove the staging
            # alias must not turn a saved paid result into a retry suggestion.
            cleanup_pending = None
            try:
                os.unlink(temporary)
            except OSError:
                cleanup_pending = str(temporary)
        result = {'output': str(output), 'operation': operation,
                  'estimated_micros': estimate_micros, 'billed_cost': 'unknown'}
        if cleanup_pending:
            result['cleanup_pending'] = cleanup_pending
        if translation is not None:
            result['translation_text'] = translation
        return result


def show_api_connections(frame):
    import wx
    controller = ConnectionsController(frame.library.root)

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
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            for label, action in [('&Add profile', self.add), ('&Edit profile', self.edit),
                                  ('&Remove profile and key', self.remove), ('&Export profiles', self.export),
                                  ('Run a re&viewed task', self.run)]:
                button = wx.Button(self, label=label)
                button.Bind(wx.EVT_BUTTON, action)
                buttons.Add(button, 0, wx.ALL, 4)
            self.layout.Add(buttons)
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
            provider_index = self.choose('Provider adapter', ['Local or remote OpenAI-compatible text model',
                'ElevenLabs speech, music and sound effects', 'Google Cloud Translation Basic v2'])
            if provider_index is None:
                return
            provider = ('openai-compatible', 'elevenlabs', 'google-translate-v2')[provider_index]
            fields = {}
            same_provider = previous if previous and previous.provider == provider else None
            for key, label, default in [('name', 'Friendly account name', ''),
                    ('endpoint', 'API base URL (loopback IP for local service)',
                     ('http://127.0.0.1:8080/v1', 'https://api.elevenlabs.io/v1',
                      'https://translation.googleapis.com')[provider_index]),
                    ('model', 'Installed model ID or provider model ID', 'nmt' if provider_index == 2 else '')]:
                value = self.prompt('Connection profile', label, getattr(same_provider, key, default))
                if value is None:
                    return
                fields[key] = value.strip()
            if provider_index == 0:
                kind = self.choose('Network location', ['User-installed loopback service', 'Remote HTTPS provider; per-task approval required'])
                if kind is None:
                    return
                local, capabilities = kind == 0, ('llm',)
            elif provider_index == 1:
                cap = self.choose('Capability for this profile and model', ['Speech synthesis', 'Music generation', 'Sound effects'])
                if cap is None:
                    return
                local, capabilities = False, (('tts', 'music', 'sound_effects')[cap],)
            else:
                local, capabilities = False, ('translation',)
            key = self.prompt('API key', 'API key (masked). Leave blank for local access or to keep an unchanged profile key.', password=True)
            if key is None:
                return
            profile = ProviderProfile(previous.id if previous else uuid.uuid4().hex,
                provider=provider, capabilities=capabilities, local=local,
                credential_ref=previous.credential_ref if previous and not key else None, **fields)
            controller.store.save(profile, secret=key or None)
            key = ''
            self.refresh()
            self.status.SetValue('Profile saved. No credential test or generation request was sent.')
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
            except Exception as exc:
                self.status.SetValue(str(exc))

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
                source, target = None, None
                if capability == 'translation':
                    source = self.prompt('Source language', 'Google language code; leave blank for automatic detection.', '')
                    if source is None:
                        return
                    target = self.prompt('Target language', 'Google language code, for example uk, en, pl or zh-TW.', 'uk')
                    if target is None:
                        return
                    source, target = source.strip() or None, target.strip()
                request = ProviderRequest(capability, text, voice, duration,
                                          source_language=source, target_language=target)
                value = self.prompt('Approved estimated cost', 'Enter your reviewed maximum estimate in USD. Provider billing can differ; unknown cost requires review.', '0' if profile.local else '')
                if value is None:
                    return
                estimate = parse_budget(value)
                default_file = ('translation.json' if capability == 'translation'
                                else 'result.txt' if capability == 'llm' else 'result.mp3')
                dialog = wx.FileDialog(self, 'Choose a new result file', defaultFile=default_file, style=wx.FD_SAVE)
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    output = Path(dialog.GetPath())
                finally:
                    dialog.Destroy()
                notice = (f'Provider: {profile.name}\nEndpoint: {profile.endpoint}\nModel: {profile.model}\n'
                    f'Task: {capability}\nText length: {len(text)} characters.\nEstimated cost approved: USD {value}.\n'
                    + (f'Languages: {source or "automatic detection"} to {target}.\n' if capability == 'translation' else '') +
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


def install_api_connection_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu = wx.Menu()
    item = menu.Append(wx.ID_ANY, 'AI and API &connections...')
    frame.Bind(wx.EVT_MENU, lambda event: show_api_connections(frame), item)
    frame.menu_bar.Append(menu, '&Connections')
    register_menu_command(frame, CommandSpec('api.connections.open', 'AI and API connections'), item)
