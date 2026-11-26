"""Native, keyboard-first provider profiles and explicitly reviewed test calls.

Importing this module does not import wx or contact a service. Profile changes
and optional tests run in one worker; all widget updates stay on the GUI thread.
The key field is write-only to the UI. Existing keys are never displayed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import re
import threading
from uuid import uuid4

from ..assistant.credentials import WindowsCredentialVault
from ..assistant.providers import ConnectionStore, PlanApproval, ProviderGateway, ProviderProfile, endpoint
from ..audio.projects import canonical
from ..audio.voices import _destination


CHAT_TEST_TEXT = 'Reply with the single word: connected.'
SPEECH_TEST_TEXT = 'This is an explicitly approved voice connection test.'


def profile_label(profile):
    return f'{profile.name} — {profile.provider}; {profile.capability}; {"local" if profile.local else "remote"}; ID {profile.id}'


def reviewed_cost(value):
    """Exact USD microunits; no price guessing, float rounding or NaN budgets."""
    if not isinstance(value, str) or not value.strip() or len(value) > 32:
        raise ValueError('Enter a known, reviewed cost reservation in USD. An unknown cost cannot authorize a call.')
    if not re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,6})?', value.strip()):
        raise ValueError('Use a finite, non-negative USD amount with at most six decimal places.')
    try:
        amount = Decimal(value.strip())
        micro = amount * 1_000_000
        if not amount.is_finite() or not 0 <= amount <= 1_000_000 or micro != micro.to_integral_value():
            raise ValueError
        return int(micro)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError('Use a finite, non-negative USD amount with at most six decimal places.') from None


@dataclass(frozen=True)
class ConnectionTest:
    profile: ProviderProfile
    reserved_microunits: int
    output: Path | None = None
    plan_id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def text(self):
        return CHAT_TEST_TEXT if self.profile.capability == 'chat' else SPEECH_TEST_TEXT

    @property
    def fingerprint(self):
        return sha256(canonical({'profile': self.profile.fingerprint, 'text': self.text,
            'plan_id': self.plan_id,
            'reserved_microunits': self.reserved_microunits,
            'output': str(self.output) if self.output else None}).encode()).hexdigest()

    @property
    def review(self):
        cost = Decimal(self.reserved_microunits) / 1_000_000
        lines = [profile_label(self.profile), 'Destination: ' + self.profile.endpoint,
            'Model: ' + self.profile.model, 'Exactly one request. No retry.',
            'Data sent: only this synthetic test text:', self.text,
            'Maximum chat response: 64 tokens.' if self.profile.capability == 'chat'
                else 'Voice ID: ' + self.profile.voice_id,
            f'Reviewed request reservation and plan budget: USD {cost:f}.',
            'Actual billed cost is unknown; a reservation is not a provider invoice or an account spending limit.']
        if self.output:
            lines.append('New WAV destination: ' + str(self.output))
        return '\n\n'.join(lines)


def prepare_connection_test(profile, *, cost_text=None, output=None):
    if type(profile) is not ProviderProfile:
        raise ValueError('Select a saved, validated provider profile.')
    if not profile.local and not profile.credential_ref:
        raise ValueError('Save a native-vault API key before testing this remote profile.')
    reservation = 0 if profile.local else reviewed_cost(cost_text)
    if profile.capability == 'tts':
        if output is None:
            raise ValueError('Choose a new WAV destination before reviewing the speech request.')
        output = _destination(output)
    elif output is not None:
        raise ValueError('A chat connection test does not write an audio file.')
    return ConnectionTest(profile, reservation, output)


def run_reviewed_connection_test(store, test, *, approved_fingerprint, vault=None, cancelled=None, _transport=None):
    if type(test) is not ConnectionTest or approved_fingerprint != test.fingerprint:
        raise ValueError('Review the exact connection test before running it.')
    if store.get(test.profile.id).fingerprint != test.profile.fingerprint:
        raise ValueError('The provider profile changed after review. Review its current settings.')
    approval = PlanApproval.for_profiles((test.profile,), plan_id=test.plan_id,
        mode='LOCAL_ONLY' if test.profile.local else 'APPROVED_REMOTE',
        data_classes=('connection_test',), max_budget_microunits=test.reserved_microunits, max_calls=1)
    gateway = ProviderGateway(store, vault=vault, approval=approval, _transport=_transport)
    arguments = dict(data_class='connection_test', operation_id='reviewed-connection-test',
                     estimated_cost_microunits=test.reserved_microunits, cancelled=cancelled)
    if test.profile.capability == 'chat':
        return gateway.chat(test.profile.id, [{'role': 'user', 'content': test.text}],
                            max_output_tokens=64, **arguments)
    return gateway.tts(test.profile.id, test.text, test.output, **arguments)


class ProviderSettingsModel:
    """Testable profile editing logic; it never reads a key to populate a form."""
    def __init__(self, root, *, vault=None):
        self.store = ConnectionStore(root)
        self.vault = vault or WindowsCredentialVault()

    def save(self, *, current=None, name, mode, endpoint_text, model, voice_id='', secret=''):
        if mode not in ('local-chat', 'remote-chat', 'elevenlabs'):
            raise ValueError('Choose an implemented provider adapter.')
        local = mode == 'local-chat'
        speech = mode == 'elevenlabs'
        profile = ProviderProfile(id=current.id if current else uuid4().hex,
            name=name.strip(), provider='elevenlabs' if speech else 'openai-compatible',
            endpoint=endpoint(endpoint_text.strip(), local=local), model=model.strip(),
            capability='tts' if speech else 'chat', local=local,
            voice_id=voice_id.strip() if speech else None,
            credential_ref=current.credential_ref if current else None)
        return self.store.save_profile(profile, secret=secret if secret else None,
            vault=self.vault, expected_fingerprint=current.fingerprint if current else None)

    def remove(self, profile, *, delete_credential=True):
        self.store.remove_profile(profile.id, vault=self.vault, delete_credential=delete_credential,
                                  expected_fingerprint=profile.fingerprint)


class ProviderSettingsDialog:
    MODES = ('local-chat', 'remote-chat', 'elevenlabs')
    MODE_LABELS = ('Local OpenAI-compatible chat (loopback)',
                   'Remote OpenAI-compatible chat (HTTPS)', 'ElevenLabs speech (official API)')
    DEFAULT_ENDPOINTS = ('http://127.0.0.1:8080/v1', 'https://api.openai.com/v1',
                         'https://api.elevenlabs.io/v1')

    def __init__(self, frame, *, model=None):
        import wx
        self.wx, self.frame, self.library = wx, frame, frame.library
        self.model = model or ProviderSettingsModel(self.library.root)
        self.dialog = wx.Dialog(frame, title='API and local model connections', size=(920, 900),
                                 style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='provider-settings')
        self.cancelled = threading.Event()
        self.closed = self.busy = self.dirty = self.loading = False
        self.current = None
        self.profiles = ()
        layout = wx.BoxSizer(wx.VERTICAL)
        intro = wx.TextCtrl(self.dialog, value='Remote access is off by default. Saving a profile does not contact '
            'its provider or authorize book uploads. Each use requires its reviewed destination, data and budget. '
            'Local models must already be running at a literal loopback address. API keys are stored only in '
            'Windows Credential Manager; this form never reveals existing keys.', style=wx.TE_MULTILINE | wx.TE_READONLY)
        intro.SetName('Connection privacy, authorization and credential storage')
        intro.SetMinSize((-1, 90))
        layout.Add(intro, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self.dialog, label='Saved accounts and local connections'), 0, wx.LEFT, 8)
        self.list = wx.ListBox(self.dialog)
        self.list.SetName('Saved provider profiles; name, adapter and unique identity')
        self.list.SetMinSize((-1, 90))
        self.list.Bind(wx.EVT_LISTBOX, self.select)
        layout.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        self.fields = {}
        form = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        form.AddGrowableCol(1, 1)
        for key, label in (('name', 'Profile name'), ('adapter', 'Provider and capability'),
                           ('endpoint', 'Base URL'), ('model', 'Exact model ID'),
                           ('voice', 'Voice ID for ElevenLabs'), ('secret', 'New API key; blank keeps the saved key')):
            form.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            if key == 'adapter':
                control = wx.Choice(self.dialog, choices=self.MODE_LABELS)
                control.Bind(wx.EVT_CHOICE, self.adapter_changed)
            else:
                control = wx.TextCtrl(self.dialog, style=wx.TE_PASSWORD if key == 'secret' else 0)
                control.Bind(wx.EVT_TEXT, self.changed)
            control.SetName(label)
            self.fields[key] = control
            form.Add(control, 1, wx.EXPAND)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        self.credential_status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY)
        self.credential_status.SetName('Saved credential status; keys are never revealed')
        layout.Add(self.credential_status, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.new_button = wx.Button(self.dialog, label='&New profile')
        self.save_button = wx.Button(self.dialog, label='&Save profile')
        self.remove_button = wx.Button(self.dialog, label='&Remove selected profile')
        self.test_button = wx.Button(self.dialog, label='Review and &test saved connection...')
        self.cancel_button = wx.Button(self.dialog, label='&Cancel connection test')
        self.close_button = wx.Button(self.dialog, wx.ID_CLOSE, label='&Close')
        for button, callback in ((self.new_button, self.new), (self.save_button, self.save),
                (self.remove_button, self.remove), (self.test_button, self.test),
                (self.cancel_button, lambda event: self.cancelled.set()), (self.close_button, self.close)):
            button.Bind(wx.EVT_BUTTON, callback)
            buttons.Add(button, 0, wx.ALL, 4)
        self.cancel_button.Disable()
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Provider settings status and errors')
        self.status.SetMinSize((-1, 100))
        layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CLOSE)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.refresh()
        self._load(None)

    def say(self, message, focus=True):
        if not self.closed:
            self.status.ChangeValue(message)
            if focus:
                self.status.SetFocus()

    def _same_library(self):
        if self.frame.library is not self.library:
            raise ValueError('The library folder changed. Reopen connection settings.')

    def _confirm(self, title, message):
        dialog = self.wx.MessageDialog(self.dialog, message, title,
            self.wx.YES_NO | self.wx.NO_DEFAULT | self.wx.ICON_QUESTION)
        try:
            return dialog.ShowModal() == self.wx.ID_YES
        finally:
            dialog.Destroy()

    def _discard(self):
        return not self.dirty or self._confirm('Discard unsaved form changes?',
                                               'Discard the changes in this connection form?')

    def refresh(self, identity=None):
        self.profiles = self.model.store.list_profiles()
        self.list.Set([profile_label(profile) for profile in self.profiles])
        for index, profile in enumerate(self.profiles):
            if profile.id == identity:
                self.list.SetSelection(index)
                break

    def _mode(self):
        index = self.fields['adapter'].GetSelection()
        if index not in range(3):
            raise ValueError('Choose a provider and capability.')
        return self.MODES[index]

    def _load(self, profile):
        self.loading = True
        self.current = profile
        mode = 0 if profile is None or profile.local else (2 if profile.provider == 'elevenlabs' else 1)
        self.fields['adapter'].SetSelection(mode)
        for key, value in {'name': profile.name if profile else '',
                'endpoint': profile.endpoint if profile else self.DEFAULT_ENDPOINTS[0],
                'model': profile.model if profile else '',
                'voice': profile.voice_id or '' if profile else '', 'secret': ''}.items():
            self.fields[key].ChangeValue(value)
        self.credential_status.ChangeValue(
            ('A key reference is saved; existing key material is never displayed.' if profile and profile.credential_ref
             else 'No API key is connected to this profile.')
            + ('' if self.model.vault.available else ' Native Windows credential storage is unavailable here.'))
        self.dirty = self.loading = False
        self._enable()
        self.fields['name'].SetFocus()

    def _enable(self):
        for control in (self.list, *self.fields.values(), self.new_button, self.save_button,
                        self.remove_button, self.test_button):
            control.Enable(not self.busy)
        if not self.busy:
            mode = self._mode()
            self.fields['voice'].Enable(mode == 'elevenlabs')
            self.fields['endpoint'].Enable(mode != 'elevenlabs')
            self.fields['secret'].Enable(self.model.vault.available)
            self.remove_button.Enable(self.current is not None)
            self.test_button.Enable(self.current is not None and not self.dirty)

    def changed(self, event=None):
        if not self.loading:
            self.dirty = True
            self.test_button.Disable()
        if event:
            event.Skip()

    def adapter_changed(self, event=None):
        previous = self.fields['endpoint'].GetValue()
        index = self.fields['adapter'].GetSelection()
        if index in range(3) and (previous in self.DEFAULT_ENDPOINTS or index == 2):
            self.fields['endpoint'].ChangeValue(self.DEFAULT_ENDPOINTS[index])
        self.changed(event)
        self._enable()

    def select(self, event=None):
        if self.busy:
            return
        index = self.list.GetSelection()
        if not 0 <= index < len(self.profiles):
            return
        if not self._discard():
            self.refresh(self.current.id if self.current else None)
            return
        self._load(self.profiles[index])

    def new(self, event=None):
        if not self.busy and self._discard():
            self.list.SetSelection(self.wx.NOT_FOUND)
            self._load(None)

    def _start(self, label, operation, complete, *, cancellable=False):
        if self.busy:
            return
        self._same_library()
        self.busy = True
        self.cancelled.clear()
        self._enable()
        self.cancel_button.Enable(cancellable)
        self.say(label)
        try:
            future = self.executor.submit(operation)
        except BaseException:
            self.busy = False
            self.cancel_button.Disable()
            self._enable()
            raise
        def finish(future):
            if self.closed:
                return
            self.busy = False
            self.cancel_button.Disable()
            try:
                result = future.result()
                self._same_library()
                complete(result)
            except Exception as exc:
                self.say('Connection operation not completed: ' + str(exc))
            self._enable()
        future.add_done_callback(lambda done: self.wx.CallAfter(finish, done) if not self.closed else None)

    def save(self, event=None):
        if self.busy:
            return
        try:
            values = dict(current=self.current, name=self.fields['name'].GetValue(), mode=self._mode(),
                endpoint_text=self.fields['endpoint'].GetValue(), model=self.fields['model'].GetValue(),
                voice_id=self.fields['voice'].GetValue(), secret=self.fields['secret'].GetValue())
            def complete(profile):
                self.refresh(profile.id)
                self._load(profile)
                self.say('Profile saved. Saving did not contact a provider. A test remains a separate reviewed action.')
            self._start('Saving connection settings...', lambda: self.model.save(**values), complete)
        except Exception as exc:
            self.say('Profile not saved: ' + str(exc))

    def remove(self, event=None):
        if self.busy or self.current is None:
            return
        try:
            self._same_library()
            profile = self.current
            delete_key = bool(self.model.vault.available)
            detail = ('Its API key reference and key in this Windows account will also be removed.' if delete_key
                      else 'Only the profile will be removed. Credential storage is unavailable on this computer.')
            if not self._confirm('Remove selected connection?', profile_label(profile) + '\n\n' + detail):
                return
            def complete(result):
                self.refresh()
                self._load(None)
                self.say('Selected profile removed. No provider was contacted.')
            self._start('Removing selected profile...',
                        lambda: self.model.remove(profile, delete_credential=delete_key), complete)
        except Exception as exc:
            self.say('Profile not removed: ' + str(exc))

    def test(self, event=None):
        if self.busy or self.current is None or self.dirty:
            return
        try:
            self._same_library()
            profile = self.current
            cost = None
            if not profile.local:
                prompt = self.wx.TextEntryDialog(self.dialog,
                    'Enter the reviewed cost reservation in USD for this one synthetic request. '
                    'Check the chosen model and account pricing first. An unknown price cannot authorize a call. '
                    'The app cannot verify the final charge; no paid test starts until the following review is approved.',
                    'Known request budget required')
                try:
                    if prompt.ShowModal() != self.wx.ID_OK:
                        return
                    cost = prompt.GetValue()
                    reviewed_cost(cost)
                finally:
                    prompt.Destroy()
            output = None
            if profile.capability == 'tts':
                picker = self.wx.FileDialog(self.dialog, 'Choose a new WAV file for the voice test',
                    wildcard='PCM WAV (*.wav)|*.wav', style=self.wx.FD_SAVE)
                try:
                    if picker.ShowModal() != self.wx.ID_OK:
                        return
                    output = Path(picker.GetPath())
                finally:
                    picker.Destroy()
            test = prepare_connection_test(profile, cost_text=cost, output=output)
            if not self._confirm('Run this single reviewed connection test?', test.review):
                self.say('Test cancelled before sending. No provider was contacted.')
                return
            def complete(result):
                if profile.capability == 'chat':
                    self.say('Chat test returned a response. The following is untrusted model text:\n' + result['text'][:2000])
                else:
                    self.say(f'Voice test saved {result["duration_seconds"]:.2f} seconds of PCM WAV to {result["path"]}. '
                             'Playback was not started automatically.')
            self._start('Running the reviewed connection test...',
                lambda: run_reviewed_connection_test(self.model.store, test, approved_fingerprint=test.fingerprint,
                    vault=self.model.vault, cancelled=self.cancelled.is_set), complete, cancellable=True)
        except Exception as exc:
            self.say('Connection test not started: ' + str(exc))

    def close(self, event=None):
        if self.busy:
            self.cancelled.set()
            self.say('Waiting for the current operation to stop. Close again when it finishes. No automatic retry will run.')
            if event and hasattr(event, 'Veto'):
                event.Veto()
            return
        if not self._discard():
            if event and hasattr(event, 'Veto'):
                event.Veto()
            return
        self.closed = True
        self.fields['secret'].ChangeValue('')
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self):
        try:
            return self.dialog.ShowModal()
        finally:
            self.closed = True
            self.cancelled.set()
            self.fields['secret'].ChangeValue('')
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.dialog.Destroy()


def install_provider_settings(frame):
    import wx
    if getattr(frame, 'provider_settings_installed', False):
        return
    for index in range(frame.menu_bar.GetMenuCount()):
        if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Tools':
            menu = frame.menu_bar.GetMenu(index)
            break
    else:
        menu = wx.Menu()
        frame.menu_bar.Insert(max(0, frame.menu_bar.GetMenuCount() - 1), menu, '&Tools')
    def show(event=None):
        if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
            frame.report('Wait for the current operation or choose Cancel current operation.')
            return
        try:
            ProviderSettingsDialog(frame).show()
        except Exception as exc:
            frame.report('Connection settings could not open: ' + str(exc))
    item = menu.Append(wx.ID_ANY, 'API and local model &connections...')
    frame.Bind(wx.EVT_MENU, show, item)
    frame.provider_settings_installed = True
