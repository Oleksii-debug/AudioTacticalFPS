"""Accessible local transcription on the desktop's existing job executor."""
from pathlib import Path
from threading import Lock

from ..audio.jobs import checked_directory
from ..audio.model_manifest import local_path


class TranscriptionBatchError(RuntimeError):
    def __init__(self, message, completed):
        self.completed = tuple(completed)
        super().__init__(f'{message}\n{len(self.completed)} completed file(s) remain saved in the selected output folder.')


class TranscriptionController:
    """Reuse one verified local model session across selected input files."""
    def __init__(self, private_root, *, session_factory=None):
        self.private_root = Path(private_root).resolve()
        self.session_factory = session_factory
        self.session = None
        self.manifest_path = None

    def transcribe(self, sources, output, manifest, *, settings, formats,
                   cancelled=lambda: False, on_status=lambda message: None):
        sources = tuple(sources)
        if not 1 <= len(sources) <= 1000:
            raise ValueError('Select between 1 and 1000 local audio or video files.')
        output = checked_directory(output)
        if output.is_relative_to(self.private_root):
            raise ValueError('Choose a transcription output folder outside the private library data.')
        manifest = local_path(manifest)
        inputs = tuple(local_path(source) for source in sources)
        if any(not source.is_file() for source in inputs):
            raise ValueError('Select regular local audio or video files.')
        if output.is_relative_to(manifest.parent):
            raise ValueError('Choose an output folder outside the selected model installation.')
        if cancelled():
            raise InterruptedError('Transcription cancelled before it started.')
        if self.session is None or self.manifest_path != manifest:
            factory = self.session_factory
            if factory is None:
                from ..audio.transcription import TranscriptionSession
                factory = TranscriptionSession
            self.session = factory(manifest)
            self.manifest_path = manifest
        completed = []
        try:
            for index, source in enumerate(inputs, 1):
                if cancelled():
                    raise InterruptedError('Transcription cancelled before the next file.')
                on_status(f'File {index} of {len(inputs)}: {source.name}')
                receipt = self.session.transcribe(source, output, settings=settings, formats=formats,
                    cancelled=cancelled, on_status=on_status)
                if not isinstance(receipt, dict) or receipt.get('status') != 'complete':
                    raise ValueError('The transcription service did not confirm a complete result.')
                completed.append(receipt)
        except (ValueError, RuntimeError, OSError, ImportError) as exc:
            raise TranscriptionBatchError(str(exc), completed) from exc
        return tuple(completed)


def show_transcription_dialog(frame, controller=None):
    import wx
    from ..audio.transcription import TranscriptionSettings
    from ..audio.transcription_models import register_local_model

    controller = controller or TranscriptionController(frame.library.root)

    class Dialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Resonance — local transcription', size=(800, 740),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.pending = False
            self.closed = False
            self.sources = ()
            self._progress_lock = Lock()
            self._latest_status = None
            self.mutable = []
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(self, label='Use an installed local Whisper model. No files are uploaded or downloaded.'),
                       0, wx.ALL, 8)
            self.files = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.files.SetName('Selected audio or video files')
            layout.Add(self.files, 1, wx.EXPAND | wx.ALL, 6)
            choose = wx.Button(self, label='Choose &audio or video files...')
            choose.Bind(wx.EVT_BUTTON, self.choose_inputs)
            layout.Add(choose, 0, wx.ALL, 6)
            self.mutable.append(choose)
            self.manifest = self.path_row(layout, 'Local &model manifest', self.choose_manifest)
            if controller.manifest_path is not None:
                self.manifest.SetValue(str(controller.manifest_path))
            register = wx.Button(self, label='&Register an existing local model...')
            register.Bind(wx.EVT_BUTTON, self.register_model)
            layout.Add(register, 0, wx.ALL, 6)
            self.mutable.append(register)
            self.output = self.path_row(layout, '&Output folder', self.choose_output)
            options = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            options.AddGrowableCol(1, 1)
            options.Add(wx.StaticText(self, label='Source &language code (auto, uk, en...)'), 0, wx.ALIGN_CENTER_VERTICAL)
            self.language = wx.TextCtrl(self, value='auto')
            self.language.SetName('Source language code or auto')
            options.Add(self.language, 1, wx.EXPAND)
            options.Add(wx.StaticText(self, label='&Task'), 0, wx.ALIGN_CENTER_VERTICAL)
            self.task = wx.Choice(self, choices=['Transcribe in the source language', 'Translate speech to English'])
            self.task.SetName('Transcription task')
            self.task.SetSelection(0)
            options.Add(self.task, 1, wx.EXPAND)
            options.Add(wx.StaticText(self, label='&Device'), 0, wx.ALIGN_CENTER_VERTICAL)
            self.device = wx.Choice(self, choices=['CPU — int8', 'CUDA GPU — float16'])
            self.device.SetName('Local inference device')
            self.device.SetSelection(0)
            options.Add(self.device, 1, wx.EXPAND)
            layout.Add(options, 0, wx.EXPAND | wx.ALL, 6)
            self.words = wx.CheckBox(self, label='Include &word timestamps in JSON')
            self.vad = wx.CheckBox(self, label='Use voice activity &detection')
            self.vad.SetValue(True)
            layout.Add(self.words, 0, wx.ALL, 6)
            layout.Add(self.vad, 0, wx.ALL, 6)
            self.mutable.extend((self.language, self.task, self.device, self.words, self.vad))
            layout.Add(wx.StaticText(self, label='Results: TXT, SRT, VTT and JSON. Speaker identification is not included.'),
                       0, wx.ALL, 6)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Transcription progress and result')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 6)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.start = wx.Button(self, label='&Start transcription')
            self.start.Bind(wx.EVT_BUTTON, self.run)
            self.mutable.append(self.start)
            self.cancel = wx.Button(self, label='&Cancel operation')
            self.cancel.Bind(wx.EVT_BUTTON, self.cancel_operation)
            self.cancel.Disable()
            close = wx.Button(self, wx.ID_CLOSE, '&Close')
            close.Bind(wx.EVT_BUTTON, self.close)
            for button in (self.start, self.cancel, close):
                buttons.Add(button, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALL, 2)
            self.SetSizer(layout)
            self.SetMinSize((620, 540))
            self.SetEscapeId(wx.ID_CLOSE)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.refresh_status, self.timer)
            self.timer.Start(250)
            choose.SetFocus()

        def path_row(self, layout, label, action):
            layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.TOP, 6)
            row = wx.BoxSizer(wx.HORIZONTAL)
            control = wx.TextCtrl(self)
            control.SetName(label.replace('&', ''))
            row.Add(control, 1, wx.EXPAND | wx.ALL, 6)
            button = wx.Button(self, label='Browse...')
            button.SetName('Browse ' + label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, action)
            row.Add(button, 0, wx.ALL, 6)
            layout.Add(row, 0, wx.EXPAND)
            self.mutable.extend((control, button))
            return control

        def choose_inputs(self, event):
            with wx.FileDialog(self, 'Select local audio or video files',
                    wildcard='Audio/video|*.wav;*.mp3;*.m4a;*.m4b;*.flac;*.ogg;*.opus;*.aac;*.mp4;*.mkv;*.webm|All files|*.*',
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as dialog:
                if dialog.ShowModal() == wx.ID_OK:
                    self.sources = tuple(Path(value) for value in dialog.GetPaths())
                    self.files.SetValue('\n'.join(str(path) for path in self.sources))

        def choose_manifest(self, event):
            with wx.FileDialog(self, 'Select the local model manifest', wildcard='JSON manifest|*.json',
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
                if dialog.ShowModal() == wx.ID_OK:
                    self.manifest.SetValue(dialog.GetPath())

        def choose_output(self, event):
            with wx.DirDialog(self, 'Choose a dedicated result folder', style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as dialog:
                if dialog.ShowModal() == wx.ID_OK:
                    self.output.SetValue(dialog.GetPath())

        def set_pending(self, value):
            self.pending = value
            for control in self.mutable:
                control.Enable(not value)
            self.cancel.Enable(value)

        def report(self, message, *, focus=True):
            if not self.closed:
                self.status.SetValue(str(message))
                if focus:
                    self.status.SetFocus()

        def progress(self, message):
            with self._progress_lock:
                self._latest_status = str(message)

        def refresh_status(self, event=None):
            with self._progress_lock:
                value, self._latest_status = self._latest_status, None
            if value is not None and self.pending:
                self.report(value, focus=False)

        def submit(self, title, operation, done):
            if self.pending or frame.working or getattr(frame, '_closing', False):
                self.report('Wait for the current operation or cancel it first.')
                return
            self.set_pending(True)
            self.report(title)
            def complete(result):
                self.set_pending(False)
                done(result)
            def failed(error):
                self.set_pending(False)
                self.report(str(error))
            frame.run_job(title, operation, complete, failed)

        def run(self, event):
            try:
                if not self.manifest.GetValue().strip() or not self.output.GetValue().strip():
                    raise ValueError('Select a local model manifest and output folder.')
                if self.task.GetSelection() not in (0, 1) or self.device.GetSelection() not in (0, 1):
                    raise ValueError('Choose a transcription task and local device.')
                settings = TranscriptionSettings(language=self.language.GetValue().strip(),
                    task=('transcribe', 'translate')[self.task.GetSelection()],
                    word_timestamps=self.words.GetValue(), vad_filter=self.vad.GetValue(),
                    device=('cpu', 'cuda')[self.device.GetSelection()],
                    compute_type=('int8', 'float16')[self.device.GetSelection()])
                sources, output, manifest = self.sources, self.output.GetValue(), self.manifest.GetValue()
                def operation():
                    return controller.transcribe(sources, output, manifest, settings=settings,
                        formats=('txt', 'srt', 'vtt', 'json'), cancelled=frame.cancel_event.is_set, on_status=self.progress)
                def done(results):
                    cached = sum(bool(row.get('cached')) for row in results)
                    warnings = [str(warning) for row in results for warning in row.get('warnings', ())]
                    self.report(f'Transcription complete: {len(results)} file(s); {cached} verified cached result(s).\n'
                                f'TXT, SRT, VTT and JSON are saved in {output}.'
                                + ('\n' + '\n'.join(warnings) if warnings else ''))
                self.submit('Transcribing local audio', operation, done)
            except Exception as exc:
                self.report(str(exc))

        def register_model(self, event):
            with wx.DirDialog(self, 'Choose the existing folder with model.bin, config.json and tokenizer.json',
                    style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                directory = Path(dialog.GetPath())
            with wx.TextEntryDialog(self, 'Name of this local model', 'Register existing model', directory.name) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                name = dialog.GetValue()
            with wx.TextEntryDialog(self, 'Licence of these model weights, as stated by their provider', 'Model licence') as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                license_name = dialog.GetValue()
            if wx.MessageBox('Register the files already in this folder?\nConfirm that you reviewed their licence and trust their source. '
                    'Registration writes only a small verification manifest; it does not download or copy the model.',
                    'Register local model', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                return
            def operation():
                return register_local_model(directory, model_id=name, license_name=license_name,
                    license_accepted=True, trusted_weights=True, private_root=frame.library.root,
                    cancelled=frame.cancel_event.is_set, on_status=self.progress)
            def done(receipt):
                self.manifest.SetValue(receipt['manifest'])
                if receipt.get('cleanup_pending'):
                    self.report('The model manifest was saved. Remove this leftover temporary file before using the model:\n'
                                + receipt['cleanup_pending'])
                else:
                    self.report('Local model registered. Select audio and an output folder to start.')
            self.submit('Verifying the existing local model', operation, done)

        def cancel_operation(self, event=None):
            if self.pending:
                frame.cancel_event.set()
                self.report('Cancellation requested. Waiting for the current inference segment to return; completed files remain saved.')

        def close(self, event=None):
            if self.pending:
                self.cancel_operation()
                if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                    event.Veto()
                return
            self.closed = True
            self.timer.Stop()
            self.EndModal(wx.ID_CLOSE)

    dialog = Dialog()
    try:
        dialog.ShowModal()
    finally:
        dialog.closed = True
        dialog.timer.Stop()
        dialog.Destroy()


def install_transcription_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command

    def open_dialog(event=None):
        if getattr(frame, '_closing', False):
            return
        if frame.working or getattr(frame, 'busy', False):
            frame.report('Wait for the current operation or cancel it before opening transcription.')
            return
        try:
            controller = getattr(frame, 'transcription_controller', None)
            if controller is None or controller.private_root != Path(frame.library.root).resolve():
                controller = TranscriptionController(frame.library.root)
                frame.transcription_controller = controller
            show_transcription_dialog(frame, controller)
        except Exception as exc:
            frame.report('Transcription could not open: ' + str(exc))

    bar = frame.menu_bar
    menu = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(index).replace('&', '') == 'Audio'), None)
    if menu is None:
        menu = wx.Menu()
        bar.Append(menu, 'A&udio')
    item = menu.Append(wx.ID_ANY, 'Local &transcription...')
    frame.Bind(wx.EVT_MENU, open_dialog, item)
    register_menu_command(frame, CommandSpec('audio.transcription.open', 'Local transcription'), item)
    return open_dialog
