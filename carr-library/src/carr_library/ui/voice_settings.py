"""Keyboard-accessible registration of already installed local speech engines."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from ..audio.voice_settings import VoiceSettingsStore
from ..audio.voices import VoiceRegistry


FIELDS = (
    ('ffmpeg', 'FFmpeg executable (blank: discover locally)'),
    ('espeak_ng', 'eSpeak NG executable (blank: discover locally)'),
    ('piper_executable', 'Piper executable'),
    ('piper_manifest', 'Reviewed Piper manifest JSON'),
    ('dordiai_manifest', 'Reviewed dordiai-voices manifest JSON'),
)


class VoiceSettingsModel:
    def __init__(self, root):
        self.root = Path(root)
        self.store = VoiceSettingsStore(root)

    def save(self, fields, *, sapi, flite, espeak, expected_revision, cancelled=None):
        if set(fields) != {key for key, _ in FIELDS}:
            raise ValueError('Unsupported local voice form fields.')
        fields = {key: value.strip() for key, value in fields.items()}
        config = {'include_sapi': bool(sapi),
                  'ffmpeg': (fields['ffmpeg'] or None) if flite else False,
                  'espeak_ng': (fields['espeak_ng'] or None) if espeak else False}
        if fields['piper_executable'] or fields['piper_manifest']:
            if not fields['piper_executable'] or not fields['piper_manifest']:
                raise ValueError('Select both the Piper executable and its reviewed manifest.')
            config['piper'] = {'executable': fields['piper_executable'], 'manifest': fields['piper_manifest']}
        if fields['dordiai_manifest']:
            config['dordiai_manifest'] = fields['dordiai_manifest']
        return self.store.register(config, expected_revision=expected_revision, cancelled=cancelled)

    def describe(self):
        registry = VoiceRegistry.for_library(self.root)
        lines = ['The saved registration is active. Open a new Audio Studio window to use these voices.',
                 'Enumeration validates installation metadata; assess pronunciation with an actual production.']
        for voice in registry.list_voices():
            lines.append(f'{voice.name}; {voice.engine}; supported: {", ".join(voice.languages)}; ID {voice.id}.')
            if voice.candidate_languages:
                lines.append('Candidates, not production capabilities: ' + ', '.join(voice.candidate_languages) + '.')
        for row in registry.engine_status():
            for profile in row.get('profiles', ()):
                if not profile['available']:
                    lines.append(f'{profile["name"]}: unavailable. {profile["reason"]} Candidates: '
                                 + (', '.join(profile['candidate_languages']) or 'none declared') + '.')
        return '\n'.join(lines)


class VoiceSettingsDialog:
    def __init__(self, parent, root):
        import wx
        self.wx, self.model = wx, VoiceSettingsModel(root)
        self.closed, self.busy = False, False
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-voice-settings')
        self.dialog = wx.Dialog(parent, title='Local voice engines', size=(850, 700),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        instructions = wx.TextCtrl(self.dialog, value=(
            'Select already installed local programs and reviewed model manifests. Saving verifies their files and '
            'records their hashes for this library. No models are downloaded or copied. A dordiai manifest must '
            'declare supported and candidate languages separately for each profile; candidates cannot be used '
            'for production. Registration does not certify pronunciation quality.'),
            style=wx.TE_MULTILINE | wx.TE_READONLY)
        instructions.SetName('Local speech setup and language capability instructions')
        instructions.SetMinSize((-1, 95))
        layout.Add(instructions, 0, wx.EXPAND | wx.ALL, 8)
        self.sapi = wx.CheckBox(self.dialog, label='Use installed Windows SAPI 5 voices')
        self.flite = wx.CheckBox(self.dialog, label='Use local FFmpeg Flite English voices')
        self.espeak = wx.CheckBox(self.dialog, label='Use locally installed eSpeak NG voices')
        for control in (self.sapi, self.flite, self.espeak):
            control.SetName(control.GetLabel())
            layout.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        form = wx.FlexGridSizer(cols=3, vgap=8, hgap=8)
        form.AddGrowableCol(1)
        self.fields, self.browse_buttons = {}, []
        for key, label in FIELDS:
            form.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            field = wx.TextCtrl(self.dialog)
            field.SetName(label)
            self.fields[key] = field
            form.Add(field, 1, wx.EXPAND)
            button = wx.Button(self.dialog, label='Browse...')
            button.SetName('Browse for ' + label)
            button.Bind(wx.EVT_BUTTON, lambda event, key=key, label=label: self.browse(key, label))
            self.browse_buttons.append(button)
            form.Add(button)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        commands = wx.WrapSizer(wx.HORIZONTAL)
        self.save_button = wx.Button(self.dialog, label='&Verify files and save')
        self.import_button = wx.Button(self.dialog, label='&Import configuration JSON...')
        self.revalidate_button = wx.Button(self.dialog, label='&Revalidate saved paths')
        self.reset_button = wx.Button(self.dialog, label='&Reset to installed defaults')
        self.stop_button = wx.Button(self.dialog, label='&Cancel verification')
        self.packages_button = wx.Button(self.dialog, label='Reference voice &packages...')
        for button, action in ((self.save_button, self.save), (self.import_button, self.import_config),
                               (self.revalidate_button, self.revalidate), (self.reset_button, self.reset),
                               (self.stop_button, self.stop), (self.packages_button, self.packages)):
            button.Bind(wx.EVT_BUTTON, action)
            commands.Add(button, 0, wx.ALL, 4)
        layout.Add(commands, 0, wx.EXPAND | wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Voice registration status, supported languages and errors')
        self.status.SetMinSize((-1, 160))
        layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
        close = wx.Button(self.dialog, wx.ID_CLOSE, label='Close')
        close.Bind(wx.EVT_BUTTON, self.close)
        layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.dialog.SetSizer(layout)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.load()
        self.sapi.SetFocus()

    def load(self):
        value = self.model.store.read()
        self.revision = value['revision']
        config = value['config']
        self.sapi.SetValue(config.get('include_sapi', True))
        self.flite.SetValue(config.get('ffmpeg') is not False)
        self.espeak.SetValue(config.get('espeak_ng') is not False)
        fields = {key: config.get(key) or '' for key in ('ffmpeg', 'espeak_ng', 'dordiai_manifest')}
        fields.update(piper_executable=config.get('piper', {}).get('executable', ''),
                      piper_manifest=config.get('piper', {}).get('manifest', ''))
        for key, control in self.fields.items():
            control.ChangeValue(fields[key])
        self.say('Restored registration is inactive. Review these local paths, then explicitly revalidate.'
                 if value['restored'] else 'Choose local paths and verify to save. Installed defaults need no model registration.')
        self.enable()

    def say(self, text, *, focus=False):
        if not self.closed:
            self.status.ChangeValue(text)
            if focus:
                self.status.SetInsertionPoint(0)
                self.status.SetFocus()

    def enable(self):
        for control in (self.sapi, self.flite, self.espeak, *self.fields.values(), *self.browse_buttons,
                        self.save_button, self.import_button, self.revalidate_button, self.reset_button, self.packages_button):
            control.Enable(not self.busy)
        self.stop_button.Enable(self.busy)

    def browse(self, key, label):
        picker = self.wx.FileDialog(self.dialog, label, style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == self.wx.ID_OK:
                self.fields[key].SetValue(picker.GetPath())
        finally:
            picker.Destroy()

    def start(self, action):
        if self.busy:
            return
        self.busy = True
        self.cancelled.clear()
        self.enable()
        self.say('Verifying selected local files and enumeration. No downloads. Cancellation prevents publication at the next verification checkpoint.')
        def run():
            value = action()
            return value, self.model.describe()
        future = self.executor.submit(run)
        def done(result):
            if self.closed:
                return
            def finish():
                if self.closed:
                    return
                self.busy = False
                try:
                    value, description = result.result()
                    self.load()
                    self.say(description, focus=True)
                except Exception as error:
                    self.say('Local voice operation failed: ' + str(error), focus=True)
                self.enable()
            self.wx.CallAfter(finish)
        future.add_done_callback(done)

    def save(self, event=None):
        fields = {key: control.GetValue() for key, control in self.fields.items()}
        sapi, flite, espeak, revision = self.sapi.GetValue(), self.flite.GetValue(), self.espeak.GetValue(), self.revision
        self.start(lambda: self.model.save(fields, sapi=sapi, flite=flite, espeak=espeak,
            expected_revision=revision, cancelled=self.cancelled.is_set))

    def import_config(self, event=None):
        wx = self.wx
        picker = wx.FileDialog(self.dialog, 'Select an explicitly reviewed local voice configuration',
            wildcard='Voice configuration (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            path = Path(picker.GetPath())
        finally:
            picker.Destroy()
        revision = self.revision
        self.start(lambda: self.model.store.import_config(path, expected_revision=revision, cancelled=self.cancelled.is_set))

    def revalidate(self, event=None):
        revision = self.revision
        self.start(lambda: self.model.store.revalidate(expected_revision=revision, cancelled=self.cancelled.is_set))

    def reset(self, event=None):
        try:
            self.model.store.reset(expected_revision=self.revision)
            self.load()
            self.say('Optional registrations detached. Installed assets remain in their original folders. New Studio windows use installed defaults.', focus=True)
        except Exception as error:
            self.say('Voice settings could not be reset: ' + str(error), focus=True)

    def packages(self, event=None):
        if self.busy:
            return
        from .voice_packages import show_voice_packages
        show_voice_packages(self.dialog, self.model.root)
        self.load()

    def stop(self, event=None):
        self.cancelled.set()
        self.say('Cancellation requested. The current bounded discovery may finish before the next publication checkpoint.')

    def close(self, event=None):
        self.closed = True
        self.cancelled.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)
        else:
            self.dialog.Hide()


def show_voice_settings(parent, root):
    controller = VoiceSettingsDialog(parent, root)
    try:
        return controller.dialog.ShowModal()
    finally:
        controller.close()
        controller.dialog.Destroy()
