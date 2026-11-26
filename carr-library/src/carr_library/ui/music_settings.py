"""Native keyboard-accessible setup and actual sample generation for local music."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading

from ..audio.music import MusicGenConfig, MusicSpec, generate_music
from ..audio.music_settings import MusicSettingsStore, music_configuration_status
from ..audio.render import file_sha256, local_file, tool_path


ENGINE_LABELS = ('Procedural instrumental (included)', 'FluidSynth and local SoundFont',
                 'MusicGen local neural model')
ENGINES = ('procedural', 'fluidsynth', 'musicgen')
FIELDS = {
    'musicgen': (
        ('model_dir', 'Installed model directory', 'directory'),
        ('manifest', 'Reviewed model manifest JSON', 'file'),
        ('manifest_sha256', 'Approved model manifest SHA256', 'text'),
        ('python_executable', 'Python executable in the installed model environment', 'file'),
        ('device', 'Device: cpu, cuda or cuda:0', 'text'),
        ('timeout_seconds', 'Sample or chunk time limit in seconds; blank means no deadline', 'text')),
    'fluidsynth': (
        ('soundfont', 'Installed SF2 SoundFont', 'file'),
        ('soundfont_sha256', 'Reviewed SoundFont SHA256', 'text'),
        ('soundfont_license', 'SoundFont license and attribution', 'text'),
        ('fluidsynth_library', 'Installed FluidSynth shared library (.dll, .so or .dylib)', 'file')),
    'procedural': (),
}


class MusicSettingsModel:
    def __init__(self, root):
        self.store = MusicSettingsStore(root)

    def values(self, engine):
        saved = self.store.read().get(engine) or {}
        defaults = {'python_executable': sys.executable, 'device': 'cpu', 'timeout_seconds': ''}
        return {key: str(saved.get(key, defaults.get(key, '')) or '') for key, _, _ in FIELDS[engine]}

    def registration(self, engine, values, *, private_noncommercial=True, cancelled=None):
        if engine not in ('musicgen', 'fluidsynth'):
            raise ValueError('The included procedural engine requires no registration.')
        wanted = {key for key, _, _ in FIELDS[engine]}
        if set(values) != wanted or any(not isinstance(value, str) for value in values.values()):
            raise ValueError('Invalid local music registration form.')
        record = {key: value.strip() for key, value in values.items()}
        if engine == 'musicgen':
            python = Path(tool_path('Python', record['python_executable']))
            record['python_executable'] = str(python)
            record['python_sha256'] = file_sha256(python, cancelled)
            record['private_noncommercial'] = private_noncommercial
            timeout = record['timeout_seconds']
            record['timeout_seconds'] = float(timeout) if timeout else None
        else:
            library = Path(tool_path('FluidSynth', record['fluidsynth_library']))
            record['fluidsynth_library'] = str(library)
            record['fluidsynth_sha256'] = file_sha256(library, cancelled)
        return record

    def save(self, engine, values, *, expected_revision, private_noncommercial=True, cancelled=None):
        value = self.store.read()
        value[engine] = self.registration(engine, values, private_noncommercial=private_noncommercial, cancelled=cancelled)
        return self.store.save(value, expected_revision=expected_revision, cancelled=cancelled)

    def sample(self, engine, output, *, cancelled=None):
        from ..audio.music_settings import load_music_options
        options = {} if engine == 'procedural' else load_music_options(self.store.projects.root)
        return generate_music(MusicSpec(3, engine=engine, seed=20261126), output, cancelled=cancelled, **options)


class MusicSettingsDialog:
    def __init__(self, parent, root, *, model=None):
        import wx
        self.wx, self.model = wx, model or MusicSettingsModel(root)
        self.dialog = wx.Dialog(parent, title='Local music engines and samples', size=(900, 790),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-music-setup')
        self.cancelled = threading.Event()
        self.closed = self.busy = False
        self.player = None
        self.revision = self.model.store.read()['revision']
        self.fields, self.browse_buttons = {}, []
        layout = wx.BoxSizer(wx.VERTICAL)
        instructions = wx.TextCtrl(self.dialog, value=(
            'Choose an already installed engine. Saving verifies local files; it does not download models or claim tested inference. '
            'MusicGen weights use the private noncommercial profile. To hear this installation, generate a three-second sample. '
            'In a saved production use @music musicgen calm 80 or @music fluidsynth calm 80; ordinary @music calm 80 uses the included procedural engine.'),
            style=wx.TE_MULTILINE | wx.TE_READONLY)
        instructions.SetName('Local music setup, license and production instructions')
        instructions.SetMinSize((-1, 90))
        layout.Add(instructions, 0, wx.EXPAND | wx.ALL, 8)
        self.engine = wx.Choice(self.dialog, choices=ENGINE_LABELS)
        self.engine.SetName('Local music engine to configure or test')
        self.engine.SetSelection(0)
        self.engine.Bind(wx.EVT_CHOICE, self.choose_engine)
        layout.Add(self.engine, 0, wx.EXPAND | wx.ALL, 8)
        self.form_panel = wx.Panel(self.dialog)
        self.form = wx.FlexGridSizer(cols=3, vgap=8, hgap=8)
        self.form.AddGrowableCol(1)
        self.form_panel.SetSizer(self.form)
        layout.Add(self.form_panel, 1, wx.EXPAND | wx.ALL, 8)
        self.private = wx.CheckBox(self.dialog, label='Use reviewed MusicGen weights only for private noncommercial work')
        self.private.SetName('MusicGen private noncommercial use profile')
        self.private.SetValue(True)
        layout.Add(self.private, 0, wx.EXPAND | wx.ALL, 8)
        commands = wx.WrapSizer(wx.HORIZONTAL)
        self.save_button = wx.Button(self.dialog, label='&Verify files and save registration')
        self.sample_button = wx.Button(self.dialog, label='Generate and &play a three-second sample...')
        self.remove_button = wx.Button(self.dialog, label='&Remove selected registration')
        self.stop_button = wx.Button(self.dialog, label='&Stop verification or sample')
        for button, callback in ((self.save_button, self.save), (self.sample_button, self.sample),
                                 (self.remove_button, self.remove), (self.stop_button, self.stop)):
            button.Bind(wx.EVT_BUTTON, callback)
            commands.Add(button, 0, wx.ALL, 4)
        layout.Add(commands, 0, wx.EXPAND | wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Music registration, verification, sample result and errors')
        self.status.SetMinSize((-1, 130))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        close = wx.Button(self.dialog, wx.ID_CLOSE, label='Close')
        close.Bind(wx.EVT_BUTTON, self.close)
        layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.dialog.SetSizer(layout)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.choose_engine()
        self.engine.SetFocus()

    def selected(self):
        index = self.engine.GetSelection()
        if not 0 <= index < len(ENGINES):
            raise ValueError('Choose a local music engine.')
        return ENGINES[index]

    def say(self, message, *, focus=False):
        if not self.closed:
            self.status.ChangeValue(message)
            if focus:
                self.status.SetInsertionPoint(0)
                self.status.SetFocus()

    def enabled(self):
        engine = self.selected()
        self.engine.Enable(not self.busy)
        for control in (*self.fields.values(), *self.browse_buttons):
            control.Enable(not self.busy)
        self.private.Enable(not self.busy and engine == 'musicgen')
        self.save_button.Enable(not self.busy and engine != 'procedural')
        self.remove_button.Enable(not self.busy and engine != 'procedural')
        self.sample_button.Enable(not self.busy)
        self.stop_button.Enable(self.busy or self.player is not None)

    def choose_engine(self, event=None):
        wx, engine = self.wx, self.selected()
        self.form.Clear(delete_windows=True)
        self.fields, self.browse_buttons = {}, []
        values = self.model.values(engine)
        for key, label, kind in FIELDS[engine]:
            self.form.Add(wx.StaticText(self.form_panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control = wx.TextCtrl(self.form_panel, value=values[key])
            control.SetName(label)
            self.fields[key] = control
            self.form.Add(control, 1, wx.EXPAND)
            if kind in ('file', 'directory'):
                button = wx.Button(self.form_panel, label='Browse...')
                button.SetName('Browse for ' + label)
                button.Bind(wx.EVT_BUTTON, lambda event, key=key, kind=kind, label=label: self.browse(key, kind, label))
                self.browse_buttons.append(button)
                self.form.Add(button)
            else:
                self.form.Add((1, 1))
        configured = self.model.store.read().get(engine) is not None if engine != 'procedural' else True
        self.say(('Included algorithmic engine; no model registration required. Its motif recurs.' if engine == 'procedural' else
                  ('A local registration is saved. Full model inference is not certified by registration.' if configured else
                   'This engine has no saved installation. Select local paths and reviewed hashes, then verify and save.')))
        self.form_panel.Layout()
        self.dialog.Layout()
        self.enabled()

    def browse(self, key, kind, label):
        wx = self.wx
        picker = wx.DirDialog(self.dialog, label, style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) if kind == 'directory' else \
            wx.FileDialog(self.dialog, label, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                self.fields[key].SetValue(picker.GetPath())
        finally:
            picker.Destroy()

    def start(self, action, complete, message):
        if self.busy:
            return
        self.busy = True
        self.cancelled.clear()
        self.enabled()
        self.say(message)
        future = self.executor.submit(action)
        def done(result):
            if self.closed:
                return
            def finish():
                if self.closed:
                    return
                self.busy = False
                try:
                    complete(result.result())
                except Exception as error:
                    self.say('Local music operation failed: ' + str(error), focus=True)
                self.enabled()
            self.wx.CallAfter(finish)
        future.add_done_callback(done)

    def save(self, event=None):
        engine = self.selected()
        values = {key: control.GetValue() for key, control in self.fields.items()}
        private, revision = self.private.GetValue(), self.revision
        def saved(value):
            self.revision = value['revision']
            self.choose_engine()
            self.say('Registration saved after local file verification. Neural inference has not been run. Generate a sample to test this installation.', focus=True)
        self.start(lambda: self.model.save(engine, values, expected_revision=revision,
                   private_noncommercial=private, cancelled=self.cancelled.is_set), saved,
                   'Verifying selected local files and exact model inventory. Large weights can take time; Stop cancels safely.')

    def sample(self, event=None):
        wx, engine = self.wx, self.selected()
        if engine != 'procedural' and {key: control.GetValue().strip() for key, control in self.fields.items()} != self.model.values(engine):
            self.say('Verify and save the edited installation before generating its sample.', focus=True)
            return
        picker = wx.FileDialog(self.dialog, 'Choose a new WAV file for the three-second sample',
                               wildcard='WAV audio (*.wav)|*.wav', style=wx.FD_SAVE)
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            output = Path(picker.GetPath())
        finally:
            picker.Destroy()
        def generated(result):
            message = f'Actual {engine} sample generated: {result.path}. {result.frames} frames per channel, {result.sample_rate} Hz, {result.channels} channels. SHA256 {result.sha256}.'
            try:
                from ..audio.playback import LocalPlayer
                if self.player:
                    self.player.close()
                self.player = LocalPlayer()
                self.player.open(result.path)
                self.player.play()
                message += ' Playing the generated sample.'
            except Exception as error:
                message += ' Playback unavailable: ' + str(error)
            self.say(message, focus=True)
        self.start(lambda: self.model.sample(engine, output, cancelled=self.cancelled.is_set), generated,
                   'Generating an actual three-second sample with the selected local engine. No automatic fallback is used.')

    def remove(self, event=None):
        try:
            value = self.model.store.remove(self.selected(), expected_revision=self.revision)
            self.revision = value['revision']
            self.choose_engine()
            self.say('Registration removed. Installed assets and completed samples remain on disk.', focus=True)
        except Exception as error:
            self.say('Registration could not be removed: ' + str(error), focus=True)

    def stop(self, event=None):
        self.cancelled.set()
        if self.player:
            self.player.close()
            self.player = None
        self.enabled()

    def close(self, event=None):
        if self.closed:
            return
        self.closed = True
        self.cancelled.set()
        if self.player:
            self.player.close()
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self):
        try:
            return self.dialog.ShowModal()
        finally:
            self.close()
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.dialog.Destroy()


def show_music_settings(parent, data_directory):
    return MusicSettingsDialog(parent, data_directory).show()
