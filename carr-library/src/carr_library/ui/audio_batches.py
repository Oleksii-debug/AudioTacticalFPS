"""Native queue planning inside the existing Audio Studio worker lifecycle."""
from dataclasses import replace
import json
from pathlib import Path

from ..audio.batch_models import AudioPreset, Selection
from ..audio.batches import AudioBatchService
from ..audio.managed import ManagedProjects
from ..audio.models import FORMATS, SAMPLE_RATES


class BatchDraft:
    """Explicit ordered selections; no filesystem or native work on edit."""
    def __init__(self):
        self.items = []

    def add(self, selection):
        if not isinstance(selection, Selection) or len(self.items) >= 128:
            raise ValueError('Choose a typed local item; one queue is limited to 128 items.')
        self.items.append(selection)

    def replace(self, index, *, title, preset, audio_stream):
        if type(index) is not int or not 0 <= index < len(self.items):
            raise ValueError('Choose the item whose delivery settings should change.')
        self.items[index] = replace(self.items[index], title=title, preset=preset, audio_stream=audio_stream)

    def remove(self, index):
        if type(index) is not int or not 0 <= index < len(self.items):
            raise ValueError('Choose an item to remove from this unstarted plan.')
        self.items.pop(index)

    def move(self, index, offset):
        if (type(index) is not int or not 0 <= index < len(self.items)
                or type(offset) is not int or offset not in (-1, 1) or not 0 <= index + offset < len(self.items)):
            raise ValueError('Choose an adjacent position inside this plan.')
        self.items[index], self.items[index + offset] = self.items[index + offset], self.items[index]

    def preview(self):
        return '\n\n'.join(
            f'{index}. {item.title}\nInput: {item.kind}; {item.source}\n'
            f'Preset: {item.preset.label}; {item.preset.output_format}; '
            f'{item.preset.sample_rate} Hz; {item.preset.channels} channel(s); '
            f'{item.preset.bitrate_kbps} kbps for lossy formats\n'
            f'Split: {item.preset.split}; selected audio stream: {item.audio_stream}\n'
            f'Maximum duration: {item.preset.max_seconds:g} seconds; timeout: {item.preset.timeout_seconds:g} seconds\n'
            'Final names include this item position, title and immutable queue item ID.'
            for index, item in enumerate(self.items, 1))


def preset_values(values):
    expected = {'label', 'output_format', 'sample_rate', 'channels', 'bitrate_kbps',
                'split', 'max_seconds', 'timeout_seconds'}
    if type(values) is not dict or set(values) != expected:
        raise ValueError('All visible delivery preset fields are required.')
    if any(type(value) is not str for value in values.values()):
        raise ValueError('Visible delivery controls require explicit text values.')
    try:
        return AudioPreset(label=values['label'], output_format=values['output_format'],
                           sample_rate=int(values['sample_rate']), channels=int(values['channels']),
                           bitrate_kbps=int(values['bitrate_kbps']), split=values['split'],
                           max_seconds=float(values['max_seconds']), timeout_seconds=float(values['timeout_seconds']))
    except (TypeError, ValueError) as error:
        raise ValueError('Review the selected format and bounded numeric preset: ' + str(error)) from error


class AudioBatchDialog:
    """The host owns the executor, cancellation, observer and late callback guard."""
    def __init__(self, host):
        import wx
        self.wx, self.host = wx, host
        self.draft = BatchDraft()
        self.batch_id = None
        self.batch = None
        self.busy = self.closed = self.close_pending = False
        self.token = None
        self.dialog = wx.Dialog(host.dialog, title='Local audio batches and audiobooks', size=(940, 800),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Local audio batches and audiobooks')
        self.controls, self.buttons = [], {}
        self.service = AudioBatchService(host.controller.private_root, ffmpeg=host.controller.ffmpeg,
                                        registry=host.controller.registry,
                                        trust_controller=host.controller.trust_controller)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.name = wx.TextCtrl(self.dialog, value='Local audio queue')
        self.name.SetName('Saved audio queue title')
        layout.Add(wx.StaticText(self.dialog, label='Queue title'), 0, wx.ALL, 4)
        layout.Add(self.name, 0, wx.EXPAND | wx.ALL, 4)
        self.controls.append(self.name)
        self.runtime = wx.TextCtrl(self.dialog, value=str(self.service.ffmpeg or ''), style=wx.TE_READONLY)
        self.runtime.SetName('Explicit selected local FFmpeg executable')
        runtime_row = wx.BoxSizer(wx.HORIZONTAL)
        runtime_row.Add(self.runtime, 1, wx.ALL, 4)
        self._button(runtime_row, 'runtime', 'Choose local FFmpeg', self.choose_runtime)
        layout.Add(runtime_row, 0, wx.EXPAND)
        additions = wx.WrapSizer(wx.HORIZONTAL)
        self._button(additions, 'audio', 'Add local audio files', lambda event: self.add_media('audio'))
        self._button(additions, 'video', 'Add local video audio streams', lambda event: self.add_media('video'))
        self._button(additions, 'books', 'Add saved book or narration projects', self.add_books)
        self._button(additions, 'open', 'Open saved queue', self.open_queue)
        self._button(additions, 'new', 'New plan', self.new_plan)
        layout.Add(additions, 0, wx.ALL, 4)
        self.items = wx.ListBox(self.dialog, style=wx.LB_SINGLE)
        self.items.SetName('Ordered local audio batch items')
        self.items.Bind(wx.EVT_LISTBOX, self.selected)
        layout.Add(self.items, 1, wx.EXPAND | wx.ALL, 4)
        self.controls.append(self.items)
        self.fields = {}
        grid = wx.FlexGridSizer(cols=4, hgap=6, vgap=4)
        specs = [
            ('title', 'Delivery filename title', 'Audio item'),
            ('label', 'Per-item preset name', 'Speech delivery'),
            ('output_format', 'Output audio format', list(FORMATS)),
            ('sample_rate', 'Output sample rate in Hz', [str(rate) for rate in SAMPLE_RATES]),
            ('channels', 'Output channel count', ['1', '2']),
            ('bitrate_kbps', 'Lossy audio bitrate in kbps', '192'),
            ('split', 'Audiobook splitting', ['whole', 'chapters']),
            ('audio_stream', 'Explicit audio stream index', '0'),
            ('max_seconds', 'Maximum accepted input seconds', '3600'),
            ('timeout_seconds', 'Native worker timeout seconds', '3600'),
        ]
        for key, label, values in specs:
            grid.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            if isinstance(values, list):
                control = wx.Choice(self.dialog, choices=values)
                initial = 'mp3' if key == 'output_format' else '48000' if key == 'sample_rate' else '2' if key == 'channels' else 'whole'
                control.SetStringSelection(initial)
            else:
                control = wx.TextCtrl(self.dialog, value=values)
            control.SetName(label)
            grid.Add(control, 1, wx.EXPAND)
            self.fields[key] = control
            self.controls.append(control)
        grid.AddGrowableCol(1)
        grid.AddGrowableCol(3)
        layout.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
        edits = wx.WrapSizer(wx.HORIZONTAL)
        self._button(edits, 'apply', 'Apply settings to selected item', self.apply_item)
        self._button(edits, 'up', 'Move item earlier', lambda event: self.move(-1))
        self._button(edits, 'down', 'Move item later', lambda event: self.move(1))
        self._button(edits, 'remove', 'Remove unstarted item', self.remove)
        layout.Add(edits, 0, wx.ALL, 4)
        self.preview = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.preview.SetName('Exact audio queue preview, source identities and output names')
        layout.Add(self.preview, 1, wx.EXPAND | wx.ALL, 4)
        actions = wx.WrapSizer(wx.HORIZONTAL)
        self._button(actions, 'create', 'Save retained plan and preview names', self.create)
        self._button(actions, 'run', 'Run saved queue', lambda event: self.run('run'))
        self._button(actions, 'resume', 'Resume saved queue', lambda event: self.run('resume'))
        self._button(actions, 'retry', 'Retry selected failed item', lambda event: self.run('retry'))
        self._button(actions, 'pause', 'Pause queue', lambda event: self.control('pause'), busy_safe=True)
        self._button(actions, 'cancel', 'Cancel queue', lambda event: self.control('cancel'), busy_safe=True)
        self._button(actions, 'export', 'Save verified output as', self.export)
        layout.Add(actions, 0, wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Audio queue status and acknowledged checkpoint')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
        close = wx.Button(self.dialog, id=wx.ID_CANCEL, label='Close audio queue')
        close.SetName('Close audio queue after worker settlement')
        close.Bind(wx.EVT_BUTTON, self.close)
        layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.SetSizer(layout)
        self.refresh()

    def _button(self, row, key, label, action, *, busy_safe=False):
        button = self.wx.Button(self.dialog, label=label)
        button.SetName(label)
        button.Bind(self.wx.EVT_BUTTON, action)
        row.Add(button, 0, self.wx.ALL, 3)
        self.buttons[key] = button
        if not busy_safe:
            self.controls.append(button)
        return button

    def message(self, value, *, focus=False):
        if self.closed:
            return
        self.status.ChangeValue(str(value))
        if focus:
            self.status.SetFocus()

    def _guard(self):
        if self.closed or self.busy or self.host.closed or self.host.busy:
            raise ValueError('Wait until the current Studio worker has settled.')

    def _edit_guard(self):
        self._guard()
        if self.batch_id is not None:
            raise ValueError('Saved queue settings are immutable. Choose New plan to preserve both versions.')

    def refresh(self, selection=None):
        if self.closed:
            return
        previous = self.items.GetSelection() if selection is None else selection
        if self.batch_id is None:
            rows = [f'{n}. {row.title} — {row.kind}; {row.preset.output_format}; {row.preset.split}'
                    for n, row in enumerate(self.draft.items, 1)]
            self.preview.ChangeValue(self.draft.preview())
        else:
            self.batch = self.service.load(self.batch_id)
            rows = [f'{item.ordinal}. {item.title} — {item.state}; attempt {item.attempt}' for item in self.batch.items]
            self.preview.ChangeValue(json.dumps(self.service.describe(self.batch_id), ensure_ascii=False, indent=2))
        self.items.SetItems(rows)
        if rows:
            self.items.SetSelection(max(0, min(previous, len(rows) - 1)))
        self.selected()
        for key in ('audio', 'video', 'books', 'apply', 'up', 'down', 'remove'):
            self.buttons[key].Enable(not self.busy and self.batch_id is None)
        for control in self.fields.values():
            control.Enable(not self.busy and self.batch_id is None)
        self.name.Enable(not self.busy and self.batch_id is None)
        self.buttons['create'].Enable(not self.busy and self.batch_id is None and bool(self.draft.items))
        for key in ('run', 'resume', 'retry', 'export'):
            self.buttons[key].Enable(not self.busy and self.batch_id is not None)
        self.buttons['pause'].Enable(self.batch_id is not None)
        self.buttons['cancel'].Enable(self.batch_id is not None or self.busy)

    def selected(self, event=None):
        index = self.items.GetSelection()
        rows = self.draft.items if self.batch_id is None else self.batch.items if self.batch else ()
        if not 0 <= index < len(rows):
            return
        row = rows[index]
        values = {**row.preset.to_dict(), 'title': row.title, 'audio_stream': row.audio_stream}
        for key, control in self.fields.items():
            if isinstance(control, self.wx.Choice):
                control.SetStringSelection(str(values[key]))
            else:
                control.ChangeValue(str(values[key]))

    def apply_item(self, event=None):
        try:
            self._edit_guard()
            values = {key: control.GetStringSelection() if isinstance(control, self.wx.Choice) else control.GetValue()
                      for key, control in self.fields.items()}
            title, stream = values.pop('title'), int(values.pop('audio_stream'))
            self.draft.replace(self.items.GetSelection(), title=title,
                               preset=preset_values(values), audio_stream=stream)
            self.refresh()
            self.message('Selected item settings updated. Source files remain unchanged.')
        except Exception as error:
            self.message(error, focus=True)

    def choose_runtime(self, event=None):
        try:
            self._guard()
            picker = self.wx.FileDialog(self.dialog, 'Choose the explicit installed FFmpeg executable',
                                        style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST)
            try:
                if picker.ShowModal() != self.wx.ID_OK:
                    return
                selected = Path(picker.GetPath()).absolute()
            finally:
                picker.Destroy()
            self.service.ffmpeg = selected
            self.runtime.ChangeValue(str(selected))
            self.message('Local runtime selected. Its exact bytes will be checked before new work.')
        except Exception as error:
            self.message(error, focus=True)

    def add_media(self, kind):
        try:
            self._edit_guard()
            picker = self.wx.FileDialog(self.dialog, 'Choose explicit local ' + kind + ' files',
                                        style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST | self.wx.FD_MULTIPLE)
            try:
                if picker.ShowModal() != self.wx.ID_OK:
                    return
                selected = tuple(Path(path).absolute() for path in picker.GetPaths())
            finally:
                picker.Destroy()
            from ..audio.media_conversion import selected_file
            additions = []
            for path in selected:
                selected_file(path, kind)
                additions.append(Selection(kind, str(path), path.stem, AudioPreset(), audio_stream=0))
            if len(self.draft.items) + len(additions) > 128:
                raise ValueError('One queue can contain at most 128 selected items.')
            for item in additions:
                self.draft.add(item)
            self.refresh()
            self.message('Review each preset and the explicit audio stream index before saving the plan.')
        except Exception as error:
            self.message(error, focus=True)

    def add_books(self, event=None):
        def choose(projects):
            if not projects:
                self.message('Save book or narration projects in this library from Audio Studio first.')
                return
            picker = self.wx.MultiChoiceDialog(self.dialog, 'Choose saved projects; unsaved edits are not included.',
                                               'Saved book and narration projects', [title for title, _ in projects])
            try:
                if picker.ShowModal() != self.wx.ID_OK:
                    return
                selected = tuple(picker.GetSelections())
            finally:
                picker.Destroy()
            if len(self.draft.items) + len(selected) > 128:
                raise ValueError('One queue can contain at most 128 saved books.')
            for index in selected:
                title, path = projects[index]
                self.draft.add(Selection('book', path.parent.parent.name, title, AudioPreset(split='chapters')))
            self.refresh()
        try:
            self._edit_guard()
            self._start('Read saved book projects', lambda report: ManagedProjects(self.service.library).projects(), choose)
        except Exception as error:
            self.message(error, focus=True)

    def new_plan(self, event=None):
        try:
            self._guard()
            self.draft, self.batch_id, self.batch = BatchDraft(), None, None
            self.refresh()
            self.message('New plan. Existing saved queues and completed outputs are retained.')
        except Exception as error:
            self.message(error, focus=True)

    def move(self, offset):
        try:
            self._edit_guard()
            index = self.items.GetSelection()
            self.draft.move(index, offset)
            self.refresh(index + offset)
        except Exception as error:
            self.message(error, focus=True)

    def remove(self, event=None):
        try:
            self._edit_guard()
            self.draft.remove(self.items.GetSelection())
            self.refresh()
        except Exception as error:
            self.message(error, focus=True)

    def _start(self, label, action, done):
        self._guard()
        token = self.token = object()
        self.busy = True
        for control in self.controls:
            control.Disable()
        self.message(label)
        def report(message):
            self._report(message, token=token)
        def work():
            try:
                value = action(report)
                status = getattr(value, 'status', None)
                if isinstance(value, dict):
                    status = value.get('status')
                status = status if status in ('complete', 'paused', 'cancelled', 'failed') else 'complete'
                return {'status': status, 'value': value, 'error': None}
            except Exception as error:
                from ..audio.jobs import RenderCancelled
                from ..operations import ResourceCancelled
                status = 'cancelled' if isinstance(error, (RenderCancelled, ResourceCancelled, InterruptedError)) else 'failed'
                return {'status': status, 'value': None, 'error': str(error) or type(error).__name__}
        def settled(result):
            if self.closed or self.token is not token:
                return
            self.busy = False
            for control in self.controls:
                control.Enable()
            if result['error'] is not None:
                self.message(result['error'], focus=True)
            else:
                try:
                    done(result['value'])
                except Exception as error:
                    self.message(error, focus=True)
            try:
                self.refresh()
            except Exception as error:
                self.message(error, focus=True)
            if self.close_pending:
                self.close_pending = False
                self.close()
        self.host._run(label, work, settled)
        if not self.host.busy and self.busy:
            # The host can decline startup if playback or executor setup failed.
            self.busy = False
            for control in self.controls:
                control.Enable()
            self.refresh()
            self.message('Studio could not start this operation; inspect its status and retry.', focus=True)

    def _report(self, message, *, token):
        def apply():
            if not self.closed and self.token is token:
                self.message(message)
        self.wx.CallAfter(apply)

    def create(self, event=None):
        try:
            self._edit_guard()
            selections, title, service = tuple(self.draft.items), self.name.GetValue(), self.service
            def accepted(batch):
                self.batch_id, self.batch = batch.id, batch
                self.message('Retained plan saved. Review the exact source identities and output names, then choose Run.')
            self._start('Retain the selected originals and save the audio plan',
                        lambda report: service.create(selections, title=title, cancelled=self.host.cancelled.is_set), accepted)
        except Exception as error:
            self.message(error, focus=True)

    def open_queue(self, event=None):
        def choose(queues):
            if not queues:
                self.message('This library has no saved audio queues.')
                return
            picker = self.wx.SingleChoiceDialog(self.dialog, 'Choose a saved queue without starting it.',
                                                'Saved audio queues', [f'{value.title} — {value.status} — {value.id}' for value in queues])
            try:
                if picker.ShowModal() != self.wx.ID_OK:
                    return
                self.batch_id = queues[picker.GetSelection()].id
            finally:
                picker.Destroy()
            self.message('Saved queue opened. Select its exact local FFmpeg runtime before resuming.')
        try:
            self._start('Read saved audio queues', lambda report: self.service.list(), choose)
        except Exception as error:
            self.message(error, focus=True)

    def run(self, action):
        try:
            self._guard()
            if self.batch_id is None:
                raise ValueError('Save or open a queue before starting it.')
            selected = ()
            if action == 'retry':
                index = self.items.GetSelection()
                if not 0 <= index < len(self.batch.items) or self.batch.items[index].state != 'failed':
                    raise ValueError('Select one failed item to retry.')
                selected = (self.batch.items[index].id,)
            service, identity = self.service, self.batch_id
            self._start('Execute the existing audio queue: ' + action,
                lambda report: service.run(identity, action=action, retry_items=selected,
                                    cancelled=self.host.cancelled.is_set, on_status=report),
                lambda value: self.message('Audio queue checkpoint: ' + value.status))
        except Exception as error:
            self.message(error, focus=True)

    def control(self, action):
        try:
            if self.closed:
                return
            if self.batch_id is not None:
                self.service.control(self.batch_id, action)
                self.message(action.capitalize() + ' requested. Wait for the owned worker and its checkpoint to settle.')
            elif self.busy and action == 'cancel':
                self.host.cancelled.set()
                self.message('Plan capture cancellation requested. No incomplete queue will be acknowledged.')
            else:
                raise ValueError('Choose an existing queue first.')
        except Exception as error:
            self.message(error, focus=True)

    def export(self, event=None):
        try:
            self._guard()
            index = self.items.GetSelection()
            if self.batch is None or not 0 <= index < len(self.batch.items):
                raise ValueError('Choose one completed queue item.')
            item = self.batch.items[index]
            if item.state != 'complete':
                raise ValueError('Only verified completed output can be exported.')
            chooser = self.wx.SingleChoiceDialog(self.dialog, 'Choose the exact completed output.',
                                                  'Verified audio outputs', list(item.outputs))
            try:
                if chooser.ShowModal() != self.wx.ID_OK:
                    return
                filename = item.outputs[chooser.GetSelection()]
            finally:
                chooser.Destroy()
            picker = self.wx.FileDialog(self.dialog, 'Save an independent verified audio copy',
                                        defaultFile=filename, style=self.wx.FD_SAVE)
            try:
                if picker.ShowModal() != self.wx.ID_OK:
                    return
                destination = Path(picker.GetPath()).absolute()
            finally:
                picker.Destroy()
            from ..research.export_file import capture_export_destination
            approval = capture_export_destination(destination, private_root=self.service.library)
            if not approval.require_absent:
                raise FileExistsError('Choose a new filename; this workflow never replaces an existing file.')
            service, identity = self.service, self.batch_id
            self._start('Export a verified independent audio copy',
                lambda report: service.export_file(identity, item.id, filename, destination,
                    approval=approval, cancelled=self.host.cancelled.is_set),
                lambda result: self.message('Verified copy saved: ' + result['output']))
        except Exception as error:
            self.message(error, focus=True)

    def close(self, event=None):
        if self.closed:
            return
        if self.busy:
            self.close_pending = True
            self.control('pause' if self.batch_id is not None else 'cancel')
            self.host.cancelled.set()
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        self.closed = True
        self.token = None
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CANCEL)
        else:
            self.dialog.Hide()

    def show(self):
        try:
            self.items.SetFocus()
            self.dialog.ShowModal()
        finally:
            if self.busy:
                # Ordinary close is deferred by close(); this path refuses to
                # destroy controls behind an unexpectedly active host owner.
                raise RuntimeError('The audio queue cannot be destroyed before its Studio worker settles.')
            self.closed = True
            self.token = None
            self.dialog.Destroy()


def show_audio_batches(host):
    if host.busy or host.closed:
        raise ValueError('Wait for the current Studio operation to settle.')
    return AudioBatchDialog(host).show()
