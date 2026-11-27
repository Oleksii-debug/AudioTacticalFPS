"""Native prepared audio conversion and explicit video-soundtrack selection."""
import os
from pathlib import Path

from ..audio.media_conversion import MediaConversionService
from ..audio.media_types import (ConversionAllowance, ConversionPreset, MediaChoice,
                                MediaInspection, MAX_ITEMS, text)
from ..audio.models import FORMATS, SAMPLE_RATES
from ..backup_files import local_path, regular
from .transcription_dialog import TranscriptionSession, _error_text

_CHOICES = {'format': FORMATS, 'sample_rate': SAMPLE_RATES, 'channels': (1, 2),
            'pcm_bits': (16, 24)}
_LABELS = {'format': 'Output audio format', 'sample_rate': 'Output sample rate, Hz',
           'channels': 'Output channels', 'pcm_bits': 'Lossless PCM bit depth',
           'bitrate_kbps': 'Compressed bitrate, kbit/s', 'title': 'Output title tag',
           'artist': 'Output artist tag', 'album': 'Output album tag'}
_SUFFIXES = frozenset(('.wav', '.wave', '.flac', '.mp3', '.ogg', '.opus', '.m4a',
    '.m4b', '.mp4', '.mov', '.mkv', '.webm', '.aac', '.aif', '.aiff', '.wma',
    '.wmv', '.asf', '.avi', '.mpg', '.mpeg', '.ts', '.m2ts'))


def preset_from_controls(fields):
    value = {}
    for name in ConversionPreset.__dataclass_fields__:
        if name in _CHOICES:
            index = fields[name].GetSelection()
            if type(index) is not int or not 0 <= index < len(_CHOICES[name]):
                raise ValueError('Choose ' + _LABELS[name].lower() + '.')
            value[name] = _CHOICES[name][index]
        else:
            value[name] = fields[name].GetValue()
    return ConversionPreset(**value)


def apply_preset(fields, preset):
    if type(preset) is not ConversionPreset:
        raise ValueError('Review a typed media preset.')
    for name, value in preset.to_dict().items():
        if name in _CHOICES:
            fields[name].SetSelection(_CHOICES[name].index(value))
        else:
            fields[name].SetValue(value)


def folder_inputs(path, *, cancelled=lambda: False):
    """One explicit folder; no recursive crawl, playlist expansion or links."""
    directory = local_path(path, exists=True)
    before = regular(directory, directory=True)
    values = []
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries, 1):
            if cancelled():
                raise InterruptedError('Media folder selection stopped.')
            if count > 4096:
                raise ValueError('Choose files explicitly from a folder with more than 4096 entries.')
            selected = Path(entry.path)
            if selected.suffix.casefold() not in _SUFFIXES:
                continue
            regular(local_path(selected, root=directory, exists=True))
            values.append(str(selected))
            if len(values) > MAX_ITEMS:
                raise ValueError('Choose at most 1000 explicit media files per job.')
    after = regular(directory, directory=True)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError('The selected media folder changed; choose it again.')
    if not values:
        raise ValueError('No supported media filenames were found directly in that folder.')
    return tuple(sorted(values, key=lambda value: (Path(value).name.casefold(), value)))


class MediaSetup:
    """A reviewed in-memory selection; preparation freezes a separate job."""
    def __init__(self):
        self.inspections, self.choices = (), {}

    def inspected(self, values):
        values = tuple(values)
        if not 1 <= len(values) <= MAX_ITEMS or any(type(row) is not MediaInspection for row in values):
            raise ValueError('Inspect the explicitly chosen media before selecting streams.')
        self.inspections, self.choices = values, {}

    def choose(self, index, stream, preset):
        if type(index) is not int or not 0 <= index < len(self.inspections):
            raise ValueError('Select one inspected media input.')
        choice = MediaChoice(self.inspections[index], stream, preset)
        self.choices[index] = choice
        return choice

    def single_streams(self, preset):
        if type(preset) is not ConversionPreset:
            raise ValueError('Review a typed output preset.')
        selected = [index for index, row in enumerate(self.inspections) if len(row.streams) == 1]
        if not selected:
            raise ValueError('Select each audio track explicitly; these inputs have multiple tracks.')
        for index in selected:
            self.choose(index, self.inspections[index].streams[0].index, preset)
        return len(selected)

    def frozen_choices(self):
        if not self.inspections or set(self.choices) != set(range(len(self.inspections))):
            raise ValueError('Explicitly apply an audio stream and output preset to every input.')
        return tuple(self.choices[index] for index in range(len(self.inspections)))


class MediaConversionDialog:
    def __init__(self, frame, *, service=None):
        import wx
        self.wx, self.frame = wx, frame
        self.root = Path(frame.library.root).absolute()
        self.service = service or MediaConversionService(self.root)
        if Path(self.service.library_root).absolute() != self.root:
            raise ValueError('Media conversion must use the selected library.')
        self.setup, self.sources = MediaSetup(), ()
        self.current_job = self.definition = self.summary = None
        self.rows, self.preset_names = (), ()
        self.fields, self.buttons = {}, {}
        self.dialog = wx.Dialog(frame, title='Audio and video conversion', size=(980, 900),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Prepared media conversion workspace')
        try:
            self._build()
            self.session = TranscriptionSession(frame, dispatch=wx.CallAfter,
                changed=self.controls, report=self.report, close=self._finish_close,
                request_stop=self._request_stop)
            self._bind()
            self.controls()
        except BaseException:
            self.dialog.Destroy()
            raise

    def _build(self):
        wx = self.wx
        layout = wx.BoxSizer(wx.VERTICAL)
        body = wx.ScrolledWindow(self.dialog, style=wx.VSCROLL)
        body.SetScrollRate(0, 12)
        content = wx.BoxSizer(wx.VERTICAL)
        def field(label, value='', readonly=False):
            content.Add(wx.StaticText(body, label=label), 0, wx.ALL, 3)
            control = wx.TextCtrl(body, value=value, style=wx.TE_READONLY if readonly else 0)
            control.SetName(label.replace('&', ''))
            content.Add(control, 0, wx.EXPAND | wx.ALL, 3)
            return control
        def buttons(names, parent, sizer):
            row = wx.WrapSizer(wx.HORIZONTAL)
            for name, label in names:
                button = wx.Button(parent, id=wx.ID_CLOSE if name == 'close' else wx.ID_ANY, label=label)
                button.SetName(label.replace('&', '').replace('...', ''))
                self.buttons[name] = button
                row.Add(button, 0, wx.ALL, 3)
            sizer.Add(row, 0, wx.EXPAND)
        self.title = field('Job title', 'Media conversion')
        self.title.SetMaxLength(200)
        self.runtime = field('Selected installed FFmpeg; empty uses the current local runtime', readonly=True)
        if self.service.ffmpeg is not None:
            self.runtime.ChangeValue(str(self.service.ffmpeg))
        self.identity = field('Prepared processor identity; Resume requires the same bytes', readonly=True)
        self.frozen_allowance = field('Exact prepared resource allowance', readonly=True)
        buttons((('files', 'Add &files...'), ('folder', 'Add folder...'),
                 ('remove', 'Remove input'), ('runtime', 'Choose installed FFmpeg...'),
                 ('inspect', '&Inspect streams')), body, content)
        self.source_list = wx.ListBox(body, style=wx.LB_SINGLE, size=(-1, 95))
        self.source_list.SetName('Selected media inputs and explicit stream choices')
        content.Add(self.source_list, 0, wx.EXPAND | wx.ALL, 3)
        content.Add(wx.StaticText(body, label='Audio stream of selected input'), 0, wx.ALL, 3)
        self.stream = wx.Choice(body)
        self.stream.SetName('Explicit audio stream; video streams cannot be selected')
        content.Add(self.stream, 0, wx.EXPAND | wx.ALL, 3)
        grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        grid.AddGrowableCol(1, 1)
        for name in ConversionPreset.__dataclass_fields__:
            grid.Add(wx.StaticText(body, label=_LABELS[name]), 0, wx.ALIGN_CENTER_VERTICAL)
            if name in _CHOICES:
                control = wx.Choice(body, choices=[str(value).upper() if name == 'format' else str(value)
                                                   for value in _CHOICES[name]])
            elif name == 'bitrate_kbps':
                control = wx.SpinCtrl(body, min=32, max=320, initial=192)
            else:
                control = wx.TextCtrl(body)
                control.SetMaxLength(500)
            control.SetName(_LABELS[name])
            self.fields[name] = control
            grid.Add(control, 1, wx.EXPAND)
        content.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
        content.Add(wx.StaticText(body, label='24-bit PCM applies to WAV, AIFF and FLAC. '
            'Opus and WMA require an explicit 48000 Hz choice. Compressed output may include codec padding.'),
            0, wx.ALL, 3)
        buttons((('apply', '&Apply to selected input'),
                 ('all_single', 'Apply to all single-track inputs')), body, content)
        self.preset_list = wx.Choice(body)
        self.preset_list.SetName('Saved named media conversion presets')
        content.Add(self.preset_list, 0, wx.EXPAND | wx.ALL, 3)
        buttons((('preset_load', 'Use saved preset'), ('preset_save', 'Save named preset...')), body, content)
        allowance = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        allowance.AddGrowableCol(1, 1)
        self.disk = wx.SpinCtrl(body, min=1, max=1048576, initial=4096)
        self.timeout = wx.SpinCtrlDouble(body, min=.05, max=31536000, initial=3600, inc=60)
        self.timeout.SetDigits(2)
        self.threads = wx.SpinCtrl(body, min=1, max=64, initial=2)
        for label, control in (('Job disk allowance, MiB', self.disk),
                               ('Per-item encode and verification time, seconds', self.timeout),
                               ('FFmpeg worker threads', self.threads)):
            control.SetName(label)
            allowance.Add(wx.StaticText(body, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            allowance.Add(control, 1, wx.EXPAND)
        content.Add(allowance, 0, wx.EXPAND | wx.ALL, 4)
        content.Add(wx.StaticText(body, label='The shared resource coordinator reserves declared capacity. '
            'RAM/VRAM usage is not measured. No source is changed or downloaded.'), 0, wx.ALL, 3)
        body.SetSizer(content)
        layout.Add(body, 1, wx.EXPAND | wx.ALL, 3)
        self.jobs_list = wx.ListBox(self.dialog, size=(-1, 65))
        self.jobs_list.SetName('Saved prepared media conversion jobs')
        layout.Add(self.jobs_list, 0, wx.EXPAND | wx.ALL, 3)
        self.outputs = wx.ListBox(self.dialog, size=(-1, 60))
        self.outputs.SetName('Verified converted audio output files')
        layout.Add(self.outputs, 0, wx.EXPAND | wx.ALL, 3)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 55))
        self.status.SetName('Media conversion status and file progress')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 3)
        buttons((('new', '&New job'), ('refresh', 'Refresh saved &jobs'),
                 ('load', 'Review saved job'), ('prepare', '&Prepare retained originals'),
                 ('start', '&Start or resume'), ('pause', 'Pa&use'),
                 ('stop', 'S&top'), ('verify', '&Verify outputs'),
                 ('export', '&Export new folder...'), ('close', '&Close')), self.dialog, layout)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CLOSE)
        apply_preset(self.fields, ConversionPreset())

    def _bind(self):
        methods = {'files': self.add_files, 'folder': self.add_folder,
            'remove': self.remove, 'runtime': self.choose_runtime, 'inspect': self.inspect,
            'apply': self.apply, 'all_single': self.apply_all_single,
            'preset_load': self.load_preset, 'preset_save': self.save_preset,
            'new': self.new, 'refresh': self.refresh, 'load': self.load,
            'prepare': self.prepare, 'start': self.start, 'pause': self.pause,
            'stop': lambda event: self.session.stop(), 'verify': self.verify,
            'export': self.export, 'close': self.close}
        for name, method in methods.items():
            self.buttons[name].Bind(self.wx.EVT_BUTTON, method)
        self.source_list.Bind(self.wx.EVT_LISTBOX, self.show_input)
        self.jobs_list.Bind(self.wx.EVT_LISTBOX, lambda event: self.controls())
        self.preset_list.Bind(self.wx.EVT_CHOICE, lambda event: self.controls())
        self.dialog.Bind(self.wx.EVT_CLOSE, self.close)
        native_id = self.dialog.GetId()
        def destroyed(event):
            if event.GetId() == native_id:
                self.session.closed = True
                self.session.cancel.set()
            event.Skip()
        self.dialog.Bind(self.wx.EVT_WINDOW_DESTROY, destroyed)

    def report(self, message):
        if not self.session.closed:
            self.status.ChangeValue(str(message))
            self.status.SetInsertionPoint(0)

    def controls(self):
        if self.session.closed:
            return
        idle = not self.session.busy and self.session.current()
        editing = idle and self.current_job is None
        for control in (self.title, self.disk, self.timeout, self.threads, self.stream, *self.fields.values()):
            control.Enable(editing)
        for name in ('files', 'folder', 'preset_save'):
            self.buttons[name].Enable(editing)
        self.buttons['remove'].Enable(editing and self.source_list.GetSelection() >= 0)
        self.buttons['inspect'].Enable(editing and bool(self.sources))
        self.buttons['apply'].Enable(editing and bool(self.setup.inspections) and self.source_list.GetSelection() >= 0)
        self.buttons['all_single'].Enable(editing and any(len(row.streams) == 1 for row in self.setup.inspections))
        self.buttons['preset_load'].Enable(editing and self.preset_list.GetSelection() >= 0)
        self.preset_list.Enable(editing)
        for name in ('runtime', 'new', 'refresh'):
            self.buttons[name].Enable(idle)
        self.source_list.Enable(idle)
        self.jobs_list.Enable(idle)
        self.buttons['load'].Enable(idle and 0 <= self.jobs_list.GetSelection() < len(self.rows))
        ready = bool(self.setup.inspections) and len(self.setup.choices) == len(self.setup.inspections)
        self.buttons['prepare'].Enable(editing and ready)
        self.buttons['start'].Enable(idle and self.current_job is not None)
        self.buttons['verify'].Enable(idle and self.current_job is not None)
        self.buttons['export'].Enable(idle and self.summary is not None
                                      and self.summary['completed'] == self.summary['total'])
        running = self.session.busy and self.session.kind == 'run' and not self.session.cancel.is_set()
        self.buttons['pause'].Enable(running)
        self.buttons['stop'].Enable(self.session.busy and not self.session.cancel.is_set())

    def add_paths(self, paths):
        self.session.guard()
        if self.session.busy or self.current_job is not None:
            raise ValueError('Choose New job before changing retained inputs.')
        chosen = list(self.sources)
        for path in paths:
            value = str(Path(path).absolute())
            if value not in chosen:
                chosen.append(value)
            if len(chosen) > MAX_ITEMS:
                raise ValueError('Choose at most 1000 explicit input files.')
        self.sources, self.setup = tuple(chosen), MediaSetup()
        self.source_list.SetItems(list(self.sources))
        self.stream.SetItems([])
        self.controls()

    def add_files(self, event=None):
        picker = self.wx.FileDialog(self.dialog, 'Choose local audio or video files',
            wildcard='Local media|*.wav;*.flac;*.mp3;*.ogg;*.opus;*.m4a;*.m4b;*.mp4;*.mov;*.mkv;*.webm;*.aac;*.aiff;*.aif;*.wma;*.wmv;*.avi;*.mpeg;*.mpg;*.ts|All files|*.*',
            style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST | self.wx.FD_MULTIPLE)
        try:
            if picker.ShowModal() == self.wx.ID_OK:
                self.add_paths(picker.GetPaths())
        except Exception as error:
            self.report(_error_text(error))
        finally:
            picker.Destroy()

    def add_folder(self, event=None):
        picker = self.wx.DirDialog(self.dialog, 'Choose one media folder; subfolders are not scanned',
                                   style=self.wx.DD_DEFAULT_STYLE | self.wx.DD_DIR_MUST_EXIST)
        try:
            if picker.ShowModal() != self.wx.ID_OK:
                return
            selected = picker.GetPath()
            self.session.submit('Reading selected media filenames',
                lambda: folder_inputs(selected, cancelled=self.session.stopped),
                self.add_paths, kind='folder')
        except Exception as error:
            self.report(_error_text(error))
        finally:
            picker.Destroy()

    def remove(self, event=None):
        if self.session.busy or self.current_job is not None:
            return
        index = self.source_list.GetSelection()
        if 0 <= index < len(self.sources):
            self.sources = self.sources[:index] + self.sources[index + 1:]
            self.setup = MediaSetup()
            self.source_list.SetItems(list(self.sources))
            self.stream.SetItems([])
            self.controls()

    def choose_runtime(self, event=None):
        if self.session.busy:
            return
        picker = self.wx.FileDialog(self.dialog, 'Choose an installed FFmpeg with a sibling FFprobe',
            wildcard='FFmpeg executable|ffmpeg.exe;ffmpeg|All files|*.*',
            style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == self.wx.ID_OK:
                self.session.guard()
                self.service.ffmpeg = picker.GetPath()
                self.runtime.ChangeValue(self.service.ffmpeg)
                if self.current_job is None:
                    self.setup = MediaSetup()
                    self.source_list.SetItems(list(self.sources))
                    self.stream.SetItems([])
                self.controls()
                self.report('Selected local executables are checked by exact bytes before any conversion.')
        except Exception as error:
            self.report(_error_text(error))
        finally:
            picker.Destroy()

    def _progress(self, generation, done, total, phase):
        if self.session.current() and self.session.busy and generation == self.session.generation:
            self.report(str(done) + '/' + str(total) + ': ' + str(phase))

    def progress(self):
        observer = getattr(self.frame, 'operation_progress', None)
        token = getattr(getattr(observer, 'event', None), 'token', None)
        generation = self.session.generation
        def advanced(done, total, phase):
            if observer is not None:
                observer.update(done, total, phase, token=token)
            self.wx.CallAfter(self._progress, generation, done, total, phase)
        return advanced

    def inspect(self, event=None):
        if self.current_job is not None:
            return
        paths = self.sources
        def ready(values):
            self.setup.inspected(values)
            self._input_labels()
            self.source_list.SetSelection(0)
            self.show_input()
            self.report('Select an audio track and apply a preset for every input. Video is not copied into audio outputs.')
        self.session.submit('Inspecting selected audio and video',
            lambda: self.service.inspect(paths, cancelled=self.session.stopped, progress=self.progress()),
            ready, kind='inspect', total=len(paths))

    def _input_labels(self):
        if self.definition is not None:
            labels = [row['name'] + ' — audio stream ' + str(row['stream_index']) +
                      ' → ' + row['preset']['format'].upper() for row in self.definition['inputs']]
        else:
            labels = []
            for index, row in enumerate(self.setup.inspections):
                choice = self.setup.choices.get(index)
                label = Path(row.path).name + ' — ' + str(len(row.streams)) + ' audio track(s)'
                if choice is not None:
                    label += ' — stream ' + str(choice.stream_index) + ' → ' + choice.preset.format.upper()
                labels.append(label)
        selected = self.source_list.GetSelection()
        self.source_list.SetItems(labels)
        if 0 <= selected < len(labels):
            self.source_list.SetSelection(selected)
        self.controls()

    def show_input(self, event=None):
        index = self.source_list.GetSelection()
        if self.definition is not None and 0 <= index < len(self.definition['inputs']):
            row = self.definition['inputs'][index]
            streams = row['streams']
            self.stream.SetItems([self._stream_label(stream) for stream in streams])
            self.stream.SetSelection(next(n for n, stream in enumerate(streams) if stream['index'] == row['stream_index']))
            apply_preset(self.fields, ConversionPreset.from_dict(row['preset']))
        elif 0 <= index < len(self.setup.inspections):
            row = self.setup.inspections[index]
            self.stream.SetItems([self._stream_label(stream.to_dict()) for stream in row.streams])
            choice = self.setup.choices.get(index)
            self.stream.SetSelection(next(n for n, stream in enumerate(row.streams) if stream.index == choice.stream_index)
                                     if choice is not None else 0 if len(row.streams) == 1 else -1)
            if choice is not None:
                apply_preset(self.fields, choice.preset)
        else:
            self.stream.SetItems([])
        self.controls()

    @staticmethod
    def _stream_label(row):
        return ('#' + str(row['index']) + ' — ' + row['codec'] + ' — ' +
                str(row['sample_rate']) + ' Hz — ' + str(row['channels']) +
                ' channel(s)' + (' — ' + row['language'] if row['language'] else '') +
                (' — ' + row['title'] if row['title'] else ''))

    def apply(self, event=None):
        try:
            self.session.guard()
            if self.session.busy or self.current_job is not None:
                return
            index, selected = self.source_list.GetSelection(), self.stream.GetSelection()
            if not 0 <= index < len(self.setup.inspections):
                raise ValueError('Select an inspected input.')
            streams = self.setup.inspections[index].streams
            if not 0 <= selected < len(streams):
                raise ValueError('Explicitly choose one audio track.')
            self.setup.choose(index, streams[selected].index, preset_from_controls(self.fields))
            self._input_labels()
        except Exception as error:
            self.report(_error_text(error))

    def apply_all_single(self, event=None):
        try:
            self.session.guard()
            if self.session.busy or self.current_job is not None:
                return
            count = self.setup.single_streams(preset_from_controls(self.fields))
            self._input_labels()
            self.report('Applied the reviewed preset to ' + str(count) +
                        ' single-track inputs. Multi-track inputs still require explicit selection.')
        except Exception as error:
            self.report(_error_text(error))

    def load_preset(self, event=None):
        if self.session.busy or self.current_job is not None:
            return
        index = self.preset_list.GetSelection()
        if not 0 <= index < len(self.preset_names):
            return
        name = self.preset_names[index]
        def loaded(values):
            if name not in values:
                raise ValueError('The selected preset changed; refresh it.')
            apply_preset(self.fields, ConversionPreset.from_dict(values[name]))
            self.report('Loaded ' + name + '. Apply it explicitly to the selected inputs.')
        self.session.submit('Reading saved media preset', self.service.presets, loaded, kind='preset')

    def save_preset(self, event=None):
        if self.session.busy or self.current_job is not None:
            return
        picker = self.wx.TextEntryDialog(self.dialog, 'Choose a new unique preset name', 'Save media preset')
        try:
            if picker.ShowModal() != self.wx.ID_OK:
                return
            name, preset = picker.GetValue(), preset_from_controls(self.fields)
            text(name, 'Preset name', 80)
            def saved(values):
                self._presets(values)
                self.report('Saved ' + name + '. Existing presets were not replaced.')
            def work():
                self.service.save_preset(name, preset)
                return self.service.presets()
            self.session.submit('Saving typed media preset', work, saved, kind='preset')
        except Exception as error:
            self.report(_error_text(error))
        finally:
            picker.Destroy()

    def _presets(self, values):
        selected = self.preset_list.GetStringSelection()
        self.preset_names = tuple(sorted(values, key=str.casefold))
        self.preset_list.SetItems(list(self.preset_names))
        self.preset_list.SetSelection(self.preset_names.index(selected) if selected in self.preset_names else -1)
        self.controls()

    def _selected(self, identity, definition):
        self.session.guard()
        self.current_job, self.definition, self.summary = identity, definition, None
        self.title.ChangeValue(definition['title'])
        self.identity.ChangeValue('FFmpeg ' + definition['processor']['ffmpeg_sha256'] +
                                  '; FFprobe ' + definition['processor']['ffprobe_sha256'])
        self.frozen_allowance.ChangeValue(str(definition['allowance']['disk_bytes']) + ' bytes; ' +
            str(definition['allowance']['timeout_seconds']) + ' seconds per item; ' +
            str(definition['allowance']['threads']) + ' worker threads')
        self.disk.SetValue(max(1, definition['allowance']['disk_bytes'] // 1024**2))
        self.timeout.SetValue(definition['allowance']['timeout_seconds'])
        self.threads.SetValue(definition['allowance']['threads'])
        self.outputs.SetItems([])
        self._input_labels()
        self.source_list.SetSelection(0)
        self.show_input()
        self.report('Prepared originals and settings are frozen. Start or resume checks the selected local processor.')

    def new(self, event=None):
        if self.session.busy:
            return
        self.session.guard()
        self.current_job = self.definition = self.summary = None
        self.setup, self.sources = MediaSetup(), ()
        self.title.ChangeValue('Media conversion')
        self.identity.ChangeValue('')
        self.frozen_allowance.ChangeValue('')
        self.source_list.SetItems([])
        self.stream.SetItems([])
        self.outputs.SetItems([])
        self.disk.SetValue(4096); self.timeout.SetValue(3600); self.threads.SetValue(2)
        apply_preset(self.fields, ConversionPreset())
        self.controls()
        self.report('Choose media, inspect streams, review output choices, then Prepare.')

    def refresh(self, event=None):
        selected = self.jobs_list.GetSelection()
        previous = self.rows[selected]['id'] if 0 <= selected < len(self.rows) else None
        def loaded(result):
            rows, presets = result
            self.rows = tuple(rows)
            self.jobs_list.SetItems([row['title'] + ' — ' + row['status'] + ' — ' +
                str(row['completed']) + '/' + str(row['inputs']) + ' — ' + row['id'][:8] for row in self.rows])
            index = next((index for index, row in enumerate(self.rows) if row['id'] == previous), -1)
            self.jobs_list.SetSelection(index)
            self._presets(presets)
            self.controls()
        self.session.submit('Reading saved media jobs',
            lambda: (self.service.jobs(cancelled=self.session.stopped), self.service.presets()),
            loaded, kind='list')

    def load(self, event=None):
        index = self.jobs_list.GetSelection()
        if not 0 <= index < len(self.rows):
            self.report('Select a saved media job first.')
            return
        identity = self.rows[index]['id']
        self.session.submit('Reviewing prepared media inputs',
            lambda: self.service.definition(identity),
            lambda definition: self._selected(identity, definition), kind='review')

    def prepare(self, event=None):
        try:
            self.session.guard()
            if self.current_job is not None:
                raise ValueError('Choose New job before changing prepared settings.')
            choices = self.setup.frozen_choices()
            allowance = ConversionAllowance(disk_bytes=self.disk.GetValue() * 1024**2,
                timeout_seconds=self.timeout.GetValue(), threads=self.threads.GetValue())
            title = text(self.title.GetValue(), 'Job title', 200)
            def work():
                identity = self.service.prepare(choices, allowance=allowance, title=title,
                    cancelled=self.session.stopped, progress=self.progress())
                return identity, self.service.definition(identity)
            self.session.submit('Retaining reviewed media originals', work,
                lambda result: self._selected(*result), kind='prepare', total=len(choices))
        except Exception as error:
            self.report(_error_text(error))

    def _verified(self, summary):
        self.summary = summary
        self.outputs.SetItems([row['path'] + ' — ' + str(row['bytes']) + ' bytes — ' +
            str(row['decoded_frames']) + ' decoded frames' for row in summary['outputs']])
        self.controls()
        self.report('Verified ' + str(summary['completed']) + '/' + str(summary['total']) +
                    ' outputs against retained originals, settings and actual audio bytes.')

    def start(self, event=None):
        if self.current_job is None:
            return
        identity = self.current_job
        self.summary = None
        self.outputs.SetItems([])
        def work():
            self.service.run(identity, cancelled=self.session.stopped, progress=self.progress())
            # Completion has been published. A late stop cannot replace that
            # truthful result; final read-only verification still checks bytes.
            return self.service.verify(identity, cancelled=lambda: not self.session.current())
        self.session.submit('Converting reviewed local media', work, self._verified,
            kind='run', total=len(self.definition['inputs']))

    def pause(self, event=None):
        if self.session.busy and self.session.kind == 'run' and not self.session.cancel.is_set():
            try:
                self.service.control(self.current_job, 'pause')
                self.report('Pause requested; completed outputs remain available for Resume.')
            except Exception as error:
                self.report(_error_text(error))

    def _request_stop(self):
        if self.session.kind == 'run' and self.current_job is not None:
            try:
                self.service.control(self.current_job, 'cancel')
            except ValueError:
                # The operation's own cancellation event is already set. It
                # may have completed its final publication before this request.
                pass
        self.controls()

    def verify(self, event=None):
        if self.current_job is None:
            return
        identity = self.current_job
        self.summary = None
        self.outputs.SetItems([])
        self.session.submit('Verifying saved media outputs',
            lambda: self.service.verify(identity, cancelled=self.session.stopped),
            self._verified, kind='verify')

    def export(self, event=None):
        if self.current_job is None or self.summary is None:
            return
        chooser = self.wx.DirDialog(self.dialog, 'Choose the parent of a new export folder',
            style=self.wx.DD_DEFAULT_STYLE | self.wx.DD_DIR_MUST_EXIST)
        try:
            if chooser.ShowModal() != self.wx.ID_OK:
                return
            self.session.guard()
            parent = local_path(chooser.GetPath(), exists=True)
            parent_info = regular(parent, directory=True)
            parent_identity = (parent_info.st_dev, parent_info.st_ino)
        except Exception as error:
            self.report(_error_text(error))
            return
        finally:
            chooser.Destroy()
        name_dialog = self.wx.TextEntryDialog(self.dialog, 'New output folder name; existing folders are refused',
                                              'Export verified audio', 'Converted audio')
        try:
            if name_dialog.ShowModal() != self.wx.ID_OK:
                return
            from ..audio.paths import filename_component
            name = filename_component(name_dialog.GetValue())
            destination = Path(parent) / name
            identity = self.current_job
            self.session.submit('Exporting independent verified audio files',
                lambda: self.service.export(identity, destination, cancelled=self.session.stopped,
                                            parent_identity=parent_identity),
                lambda path: self.report('Verified audio and provenance exported to the selected new folder.'),
                kind='export', total=self.summary['total'])
        except Exception as error:
            self.report(_error_text(error))
        finally:
            name_dialog.Destroy()

    def close(self, event=None):
        closed = self.session.request_close()
        if not closed and event is not None and callable(getattr(event, 'CanVeto', None)) and event.CanVeto():
            event.Veto()

    def _finish_close(self):
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self, *, on_ready=None):
        self.report('Local media only. Inspect and review explicit audio streams, then Prepare. Start is separate.')
        self.wx.CallAfter(self.refresh)
        if on_ready is not None:
            self.wx.CallAfter(on_ready, self)
        try:
            return self.dialog.ShowModal()
        finally:
            self.session.closed = True
            self.session.cancel.set()
            self.dialog.Destroy()


def show_media_conversion(frame, *, service=None, on_ready=None):
    return MediaConversionDialog(frame, service=service).show(on_ready=on_ready)


class MediaActions:
    def __init__(self, frame):
        self.frame = frame

    def open(self, event=None):
        try:
            return show_media_conversion(self.frame)
        except Exception as error:
            self.frame.report('Audio and video conversion could not open: ' + _error_text(error))


def install_media_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'media_conversion_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise ValueError('Install the shared Tools menu before media conversion.')
    actions = MediaActions(frame)
    item = menu.Append(wx.ID_ANY, 'Audio and video conversion...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    register_menu_command(frame, CommandSpec('conversion.audio', 'Audio and video conversion', 'main'), item)
    frame.media_conversion_actions = actions
    return actions
