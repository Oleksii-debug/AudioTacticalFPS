"""Native local transcription through the existing supplied-worker service.

Inventory hashing, retained inputs, inference and verified transcript reads use
the desktop host's background executor. The view uses named standard wx controls
and the existing reader for derived edits; it imports no inference engine.
"""
from pathlib import Path
import threading

from ..audio.models import AudioCancelled
from ..audio.transcription import (AUDIO_EXTENSIONS, FORMATS, SuppliedWhisperRuntime,
                                   TranscriptionService, TranscriptionSettings)
from ..durable_state import local_path, regular
from .import_center import readable


def prepare_reading_copy(library_root, service, job_id, source_file, *, cancelled=lambda: False):
    """Seed one derived reader draft; preserve later edits and undo on reopening."""
    from ..speech.reading import ReadingDocument
    from ..speech.session import ReaderDraftStore, ReaderResume
    detail = service.details(job_id, cancelled=cancelled)
    if source_file not in {item['file'] for item in detail['record']['request']['inputs']}:
        raise ValueError('Select an input belonging to this transcription job.')
    slot = f'transcription:{job_id}:{source_file}'
    store = ReaderDraftStore(library_root, slot=slot)
    saved = store.load()
    if saved is not None:
        # A missing/corrupt generated receipt must not trap the user's later
        # edits. Reopening an existing draft does not certify ASR artifacts.
        return {'slot': slot, 'recovered': True}
    text, label = service.transcript_text(job_id, source_file, cancelled=cancelled)
    if cancelled():
        raise AudioCancelled('Opening the reading copy was cancelled.')
    title = ('Transcript reading copy: ' + readable(label))[:512]
    candidate = ReaderResume(ReadingDocument(text, title=title))
    saved = store.save_if_absent(candidate)
    return {'slot': slot, 'recovered': saved is not candidate}


class TranscriptionController:
    """UI-independent actions; expensive work goes through frame.run_job."""
    def __init__(self, frame, *, changed=lambda focus=False: None,
                 dispatch=lambda callback: callback(), service_factory=TranscriptionService,
                 runtime_factory=SuppliedWhisperRuntime):
        self.frame, self.library = frame, frame.library
        self.changed, self.dispatch = changed, dispatch
        self.service_factory, self.runtime_factory = service_factory, runtime_factory
        self.closed = self.busy = False
        self.mode = None
        self.manifest = ''
        self.dependency = ('Select a reviewed local runtime manifest. No model is installed or downloaded here. '
                           'Saved jobs and completed results remain available without a runtime.')
        self.paths, self.jobs, self.outputs = [], [], []
        self.record = self.job_id = None
        self.status = 'Choose audio files for a new job, or refresh and select a saved job.'
        self._token = None
        self._control_lock = threading.Lock()
        self._pending_command = self._active_job = self._active_service = None
        self._requested_command = None

    def guard(self):
        if self.closed or getattr(self.frame, '_closing', False) or self.frame.library is not self.library:
            raise ValueError('The library folder changed or this window closed. Reopen local transcription.')

    def report(self, message, *, focus=False):
        if not self.closed:
            self.status = str(message)
            self.changed(focus)

    def set_manifest(self, path):
        if self.busy:
            return False
        self.guard()
        self.manifest = str(path)
        self.dependency = 'Selected local manifest; worker, runtime and model inventories have not yet been verified.'
        self.changed(False)
        return True

    def set_files(self, paths):
        if self.busy:
            return False
        self.guard()
        self.paths = list(dict.fromkeys(str(path) for path in paths))
        self.record = self.job_id = None
        self.outputs = []
        self.report(f'{len(self.paths)} selected audio files. A new job retains immutable input copies.')
        return True

    def add_folder(self, folder):
        existing = tuple(self.paths) if self.job_id is None else ()
        def done(paths):
            self.set_files(paths)
            self.report(f'{len(paths)} selected audio files. Added direct folder contents only; subfolders were not scanned.',
                        focus=True)
        return self._job('Finding local audio files in the selected folder',
                         lambda progress: self._service().folder_inputs(folder, existing=existing,
                                                                         cancelled=self._cancelled), done)

    def _service(self, runtime=None):
        return self.service_factory(Path(self.library.root) / 'state', runtime)

    def _cancelled(self):
        with self._control_lock:
            command, service, job = self._pending_command, self._active_service, self._active_job
            if command and service is not None and job is not None:
                self._pending_command = None
        if command and service is not None and job is not None:
            service.control(job, command)
            command = None
        return (bool(command) or self.frame.cancel_event.is_set()
                or self.closed or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library)

    def _inspection_cancelled(self):
        with self._control_lock:
            requested = self._requested_command is not None
        return (requested or self.frame.cancel_event.is_set()
                or self.closed or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library)

    def _progress(self, token, text, progress_token):
        if self.closed or self._token is not token:
            return
        progress = getattr(self.frame, 'operation_progress', None)
        if progress is not None:
            progress.update(phase=readable(text)[:512], token=progress_token)
        def apply():
            if not self.closed and self._token is token and self.busy:
                self.report(text)
        self.dispatch(apply)

    def _job(self, title, operation, done, *, mode='inspect'):
        try:
            self.guard()
        except Exception as exc:
            self.report(exc, focus=True)
            return False
        if self.busy or self.frame.working:
            self.report('An operation is already running. Wait for it or cancel it before starting another.', focus=True)
            return False
        self.busy, self.mode = True, mode
        token = self._token = object()
        with self._control_lock:
            self._pending_command = self._active_job = self._active_service = None
            self._requested_command = None
        self.report(title + '…', focus=True)

        def work():
            self.guard()
            progress = getattr(self.frame, 'operation_progress', None)
            progress_token = progress.event.token if progress is not None else None
            try:
                return operation(lambda text: self._progress(token, text, progress_token))
            except AudioCancelled:
                return {'status': 'cancelled', 'preparation_cancelled': True}

        def finish(value=None, error=None):
            if self.closed or self._token is not token:
                return
            self.busy, self.mode = False, None
            with self._control_lock:
                self._pending_command = self._active_job = self._active_service = None
            try:
                self.guard()
                if error is not None:
                    raise error
                if isinstance(value, dict) and value.get('preparation_cancelled'):
                    self.report('Preparation cancelled. Refresh saved jobs to review previously retained work.', focus=True)
                else:
                    done(value)
            except Exception as exc:
                self.report(f'{title}: {exc}', focus=True)
            self.changed(False)

        try:
            accepted = self.frame.run_job(title, work, lambda value: finish(value),
                                          failed=lambda exc: finish(error=exc))
            if accepted is False:
                finish(error=RuntimeError('The desktop host could not start this operation.'))
                return False
        except Exception as exc:
            finish(error=exc)
            return False
        return True

    def verify_runtime(self):
        manifest = self.manifest
        if not manifest:
            self.report('Choose a reviewed local runtime manifest first.', focus=True)
            return False
        self.dependency = 'Local runtime verification is pending. Recognition verifies these files again.'
        def work(progress):
            runtime = self.runtime_factory(manifest)
            identity, *_ = runtime.verify(self._cancelled)
            return identity
        def done(identity):
            self.dependency = (f'Local worker, runtime and model inventories verified: {identity[:16]}. '
                               'This check does not run recognition or qualify model quality. No downloads.')
            self.report(self.dependency, focus=True)
        return self._job('Checking local transcription dependencies', work, done)

    def refresh(self):
        def done(value):
            rows = value['jobs']
            self.jobs = rows
            self.report(f'{len(rows)} saved jobs. Select one to inspect verified results and its original settings.', focus=True)
        # Listing an old failed job is a successful inspection, not a new failed
        # operation. Do not feed historical row statuses to the host reducer.
        return self._job('Checking saved transcription jobs',
                         lambda progress: {'jobs': self._service().jobs(cancelled=self._cancelled)}, done)

    def inspect(self, job_id):
        return self._job('Opening saved transcription job',
                         lambda progress: self._service().details(job_id, cancelled=self._cancelled), self._display)

    def _display(self, detail):
        self.record, self.outputs = detail['record'], detail['outputs']
        self.job_id, self.paths = self.record['id'], []
        request = self.record['request']
        settings = request['settings']
        summary = (f'Job {self.job_id[:16]}: {self.record["status"]}; '
                   f'{len(self.record["completed"])} of {len(request["inputs"])} files complete. '
                   f'Language: {settings["language"]}; task: {settings["task"]}; '
                   f'device: {settings["device"]}; compute: {settings["compute_type"]}; '
                   f'formats: {", ".join(request["formats"])}. ')
        if detail.get('validation_cancelled'):
            summary += 'Results were not rechecked after stop. Refresh and inspect this job to verify outputs. '
        if self.record.get('error'):
            summary += readable(self.record['error']) + ' '
        summary += ('Resume preserves original settings, runtime identity and input copies. '
                    'Pause/cancel stops the active file; resume restarts that file and reuses verified completed files.')
        self.report(summary, focus=True)

    def start(self, settings=None, formats=FORMATS):
        manifest, job_id, paths = self.manifest, self.job_id, tuple(self.paths)
        if not manifest:
            self.report('Choose a reviewed local runtime manifest before starting or resuming.', focus=True)
            return False
        if job_id is None and not paths:
            self.report('Choose at least one audio file, or select a saved job to resume.', focus=True)
            return False
        settings = settings or TranscriptionSettings()
        formats = tuple(formats)
        def work(progress):
            service = self._service(self.runtime_factory(manifest))
            active = job_id
            if active is None:
                progress('Verifying dependencies and retaining immutable audio input copies.')
                active = service.create(paths, settings=settings, formats=formats, cancelled=self._cancelled)
            with self._control_lock:
                self._active_job, self._active_service = active, service
            service.control(active, 'run')
            error, stopped = None, False
            try:
                service.run(active, cancelled=self._cancelled, on_status=progress)
            except AudioCancelled:
                stopped = True
            except Exception as exc:
                error = str(exc)
            try:
                if stopped or self._inspection_cancelled():
                    raise AudioCancelled('Result inspection stopped.')
                detail = service.details(active, cancelled=self._inspection_cancelled)
                detail['status'] = detail['record']['status']
            except AudioCancelled:
                detail = service.interrupted_details(active)
                with self._control_lock:
                    requested = self._requested_command
                detail['status'] = ('paused' if requested == 'pause' or
                                    detail['record']['status'] == 'paused' else 'cancelled')
            if error is not None:
                detail.update(status='failed', operation_error=error)
            return detail
        def done(detail):
            self._display(detail)
            if detail.get('operation_error'):
                self.report(self.status + ' Operation failed: ' + detail['operation_error'], focus=True)
        return self._job('Running local transcription', work, done, mode='run')

    def request_stop(self, command):
        if command not in ('pause', 'cancel'):
            raise ValueError('Choose pause or cancel.')
        if not self.busy:
            self.report('No active operation. Select a saved job and press F6 to resume.', focus=True)
            return False
        if command == 'pause' and self.mode != 'run':
            self.report('Preparation can be cancelled; pause is available during transcription.', focus=True)
            return False
        with self._control_lock:
            self._pending_command = command
            self._requested_command = command
        self.report(f'{command.capitalize()} requested. Waiting for the worker to stop and save its final state.', focus=True)
        return True

    def edit(self, source_file, open_reader):
        job_id = self.job_id
        if job_id is None:
            self.report('Select a completed TXT transcript first.', focus=True)
            return False
        def done(value):
            self.report('Opening the saved editable reading copy. Transcripts are derived text, not citation evidence.')
            open_reader(value['slot'])
        return self._job('Preparing transcript reading copy',
            lambda progress: prepare_reading_copy(self.library.root, self._service(), job_id,
                                                   source_file, cancelled=self._cancelled), done)

    def open_folder(self, source_file, launch):
        job_id = self.job_id
        def work(progress):
            detail = self._service().details(job_id, cancelled=self._cancelled)
            output = next((row for row in detail['outputs'] if row['file'] == source_file), None)
            if output is None:
                raise ValueError('Select a completed result folder first.')
            path = local_path(output['directory'], exists=True)
            regular(path, directory=True)
            return str(path)
        def done(path):
            # Recheck a directory immediately before the existing wx launcher.
            # No shell command or arbitrary transcript executable is launched.
            regular(local_path(path, exists=True), directory=True)
            if not launch(path):
                raise ValueError('The system could not open the result folder: ' + path)
            self.report('Opened the local result folder for review: ' + path, focus=True)
        return self._job('Checking local transcription result folder', work, done)

    def dispose(self):
        """Idempotently detach without I/O; pending work stops and drops UI callbacks."""
        self.closed = True
        self._token = None

    def close(self):
        if self.busy:
            self.report('Pause or cancel the operation and wait for its saved status before closing.', focus=True)
            return False
        self.dispose()
        return True


def create_transcription_dialog(frame, *, controller_factory=TranscriptionController):
    """Construct real wx controls separately so native CI can exercise the view."""
    import wx

    class TranscriptionDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Local transcription', size=(920, 820),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.controller = controller_factory(frame, changed=self.render,
                                                 dispatch=lambda callback: wx.CallAfter(callback))
            self._shown_record = None
            root_layout = wx.BoxSizer(wx.VERTICAL)
            self.pages = wx.Notebook(self)
            self.pages.SetName('Transcription pages')
            input_page, job_page = wx.Panel(self.pages), wx.Panel(self.pages)
            panel = input_page
            layout = wx.BoxSizer(wx.VERTICAL)

            def field(label, factory, *, proportion=0, **kwargs):
                layout.Add(wx.StaticText(panel, label=label), 0, wx.LEFT | wx.TOP, 6)
                value = factory(panel, **kwargs)
                value.SetName(label.replace('&', ''))
                layout.Add(value, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
                return value

            def buttons(entries):
                row = wx.WrapSizer(wx.HORIZONTAL)
                controls = []
                for label, handler in entries:
                    button = wx.Button(panel, label=label)
                    button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler)
                    row.Add(button, 0, wx.ALL, 3)
                    controls.append(button)
                layout.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 3)
                return controls

            self.manifest = field('Local runtime &manifest', wx.TextCtrl, style=wx.TE_READONLY)
            self.choose_runtime, self.verify = buttons([
                ('&Browse runtime manifest…', self.choose_manifest),
                ('&Verify local dependencies', lambda event: self.controller.verify_runtime())])
            self.dependencies = field('Local dependency status', wx.TextCtrl,
                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 50))
            self.files = field('Audio &inputs', wx.ListBox, size=(-1, 65))
            self.choose_files_button, self.folder_inputs_button, self.new_button = buttons([
                ('Choose &audio files…', self.choose_files),
                ('Add &folder (direct files)…', self.choose_folder),
                ('&New job', lambda event: self.controller.set_files(()))])
            settings = wx.FlexGridSizer(cols=4, hgap=6, vgap=4)
            self.settings_controls = []
            def setting(label, control):
                settings.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                control.SetName(label.replace('&', ''))
                settings.Add(control, 1, wx.EXPAND)
                self.settings_controls.append(control)
                return control
            self.language = setting('&Language (auto or code)', wx.TextCtrl(panel, value='auto'))
            self.task = setting('Recognition tas&k', wx.Choice(panel, choices=['Transcribe spoken language', 'Translate to English']))
            self.device = setting('Processing &device', wx.Choice(panel, choices=['cpu', 'cuda']))
            self.compute = setting('Compute &type', wx.Choice(panel, choices=['int8', 'int8_float16', 'int8_float32', 'float16', 'float32']))
            for choice in (self.task, self.device, self.compute):
                choice.SetSelection(0)
            settings.AddGrowableCol(1)
            settings.AddGrowableCol(3)
            layout.Add(settings, 0, wx.EXPAND | wx.ALL, 6)
            formats = wx.BoxSizer(wx.HORIZONTAL)
            formats.Add(wx.StaticText(panel, label='Output formats:'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            self.formats = {}
            for name in FORMATS:
                control = wx.CheckBox(panel, label=name.upper())
                control.SetName(name.upper() + ' transcript output')
                control.SetValue(True)
                formats.Add(control, 0, wx.RIGHT, 12)
                self.formats[name] = control
            layout.Add(formats, 0, wx.EXPAND | wx.ALL, 6)
            input_page.SetSizer(layout)
            self.pages.AddPage(input_page, 'New transcription and runtime')
            panel, layout = job_page, wx.BoxSizer(wx.VERTICAL)
            self.jobs = field('Saved transcription &jobs', wx.ListBox, proportion=1, size=(-1, 100))
            self.jobs.Bind(wx.EVT_LISTBOX, self.select_job)
            self.refresh_button, = buttons([('&Refresh jobs', lambda event: self.controller.refresh())])
            self.results = field('Completed transcription res&ults', wx.ListBox, proportion=1, size=(-1, 100))
            self.results.Bind(wx.EVT_LISTBOX, lambda event: self.render())
            self.edit_button, self.folder_button = buttons([
                ('&Edit TXT reading copy', self.edit), ('Open result f&older', self.open_folder)])
            job_page.SetSizer(layout)
            self.pages.AddPage(job_page, 'Saved jobs and results')
            root_layout.Add(self.pages, 1, wx.EXPAND | wx.ALL, 6)
            panel, layout = self, root_layout
            self.start_button, self.pause_button, self.cancel_button = buttons([
                ('&Start / resume (F6)', self.start),
                ('&Pause (F8)', lambda event: self.controller.request_stop('pause')),
                ('&Cancel operation', lambda event: self.controller.request_stop('cancel'))])
            self.status = field('Transcription status (Ctrl+Alt+G)', wx.TextCtrl,
                                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 100))
            self.query_button, self.close_button = buttons([
                ('Read pro&gress (Ctrl+Alt+G)', lambda event: self.status.SetFocus()),
                ('Close (Esc)', self.close)])
            self.SetSizer(layout)
            self.SetEscapeId(self.close_button.GetId())
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            accelerators = []
            for flags, key, handler in [
                    (wx.ACCEL_NORMAL, wx.WXK_F6, self.start),
                    (wx.ACCEL_NORMAL, wx.WXK_F8, lambda event: self.controller.request_stop('pause')),
                    (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('G'), lambda event: self.status.SetFocus())]:
                identifier = wx.NewIdRef()
                self.Bind(wx.EVT_MENU, handler, id=identifier)
                accelerators.append((flags, key, identifier))
            self.SetAcceleratorTable(wx.AcceleratorTable(accelerators))
            self.SetMinSize((750, 740))
            self.render()

        def render(self, focus=False):
            controller = self.controller
            if controller.closed:
                return
            self.manifest.ChangeValue(controller.manifest)
            self.dependencies.ChangeValue(controller.dependency)
            if controller.record is None:
                self.files.Set([readable(Path(path).name) for path in controller.paths])
            else:
                labels = controller.record.get('source_labels', {})
                labels = labels if isinstance(labels, dict) else {}
                self.files.Set([readable(labels.get(item['file'], f'Audio {index}'))[:200]
                                for index, item in enumerate(controller.record['request']['inputs'], 1)])
            selected_job = next((i for i, row in enumerate(controller.jobs)
                                 if row['id'] == controller.job_id), -1)
            self.jobs.Set([f'{row["id"][:16]} — {row["status"]}; '
                           f'{row["completed"]} of {row["total"] if row["total"] is not None else "unknown"}'
                           for row in controller.jobs])
            self.jobs.SetSelection(selected_job)
            if controller.record is not None and self._shown_record != controller.job_id:
                request = controller.record['request']
                settings = request['settings']
                self.language.ChangeValue(settings['language'])
                self.task.SetSelection(0 if settings['task'] == 'transcribe' else 1)
                self.device.SetStringSelection(settings['device'])
                self.compute.SetStringSelection(settings['compute_type'])
                for name, control in self.formats.items():
                    control.SetValue(name in request['formats'])
                self.pages.SetSelection(1)
            self._shown_record = controller.job_id
            selection = self.results.GetSelection()
            self.results.Set([readable(row['label'])[:200] + ' — ' + ', '.join(row['formats'])
                              + ('; verified' if row.get('verified', False) else '; needs review')
                              for row in controller.outputs])
            if controller.outputs:
                self.results.SetSelection(min(max(selection, 0), len(controller.outputs) - 1))
            self.status.ChangeValue(controller.status)
            mutable = (self.choose_runtime, self.verify, self.files, self.choose_files_button,
                       self.folder_inputs_button, self.new_button, self.jobs, self.refresh_button, self.results)
            for control in mutable:
                control.Enable(not controller.busy)
            for control in self.settings_controls + list(self.formats.values()):
                control.Enable(not controller.busy and controller.job_id is None)
            self.start_button.Enable(not controller.busy and bool(controller.manifest)
                                     and bool(controller.paths or controller.job_id))
            self.pause_button.Enable(controller.busy and controller.mode == 'run')
            self.cancel_button.Enable(controller.busy)
            output = self.selected_output()
            self.edit_button.Enable(not controller.busy and output is not None and 'txt' in output['formats'])
            self.folder_button.Enable(not controller.busy and output is not None)
            if focus:
                self.status.SetFocus()

        def choose_manifest(self, event=None):
            with wx.FileDialog(self, 'Choose a reviewed local transcription runtime manifest',
                               wildcard='Runtime manifest (*.json)|*.json',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
                if chooser.ShowModal() == wx.ID_OK:
                    self.controller.set_manifest(chooser.GetPath())

        def choose_files(self, event=None):
            extensions = ';'.join('*' + value for value in sorted(AUDIO_EXTENSIONS))
            with wx.FileDialog(self, 'Choose local audio or video files',
                               wildcard=f'Audio and video|{extensions}',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as chooser:
                if chooser.ShowModal() == wx.ID_OK:
                    self.controller.set_files(chooser.GetPaths())

        def choose_folder(self, event=None):
            with wx.DirDialog(self, 'Add audio from this folder only; subfolders are not scanned',
                              style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as chooser:
                if chooser.ShowModal() == wx.ID_OK:
                    self.controller.add_folder(chooser.GetPath())

        def select_job(self, event=None):
            index = self.jobs.GetSelection()
            if 0 <= index < len(self.controller.jobs):
                self.controller.inspect(self.controller.jobs[index]['id'])

        def start(self, event=None):
            try:
                settings = TranscriptionSettings(language=self.language.GetValue().strip(),
                    task='translate' if self.task.GetSelection() == 1 else 'transcribe',
                    device=self.device.GetStringSelection(), compute_type=self.compute.GetStringSelection())
                self.controller.start(settings, [name for name, control in self.formats.items() if control.GetValue()])
            except Exception as exc:
                self.controller.report(exc, focus=True)

        def selected_output(self):
            index = self.results.GetSelection()
            return self.controller.outputs[index] if 0 <= index < len(self.controller.outputs) else None

        def edit(self, event=None):
            output = self.selected_output()
            if output is None:
                return
            def open_reader(slot):
                from .speech_dialog import show_speech_reader
                show_speech_reader(frame, text='', title='Transcript reading copy',
                                   source_guard=self.controller.guard, recovery_slot=slot)
            self.controller.edit(output['file'], open_reader)

        def open_folder(self, event=None):
            output = self.selected_output()
            if output is not None:
                self.controller.open_folder(output['file'], wx.LaunchDefaultApplication)

        def Destroy(self):
            """Detach callbacks before wx schedules deferred native deletion."""
            controller = getattr(self, 'controller', None)
            if controller is not None:
                controller.dispose()
            return super().Destroy()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.controller.dispose()
            event.Skip()

        def close(self, event=None):
            if not self.controller.close():
                if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                    event.Veto()
                return
            self.EndModal(wx.ID_CANCEL)

    return TranscriptionDialog()


def show_transcription(frame, *, job_id=None):
    dialog = create_transcription_dialog(frame)
    try:
        dialog.CentreOnParent()
        if job_id is not None:
            dialog.pages.SetSelection(1)
            wx.CallAfter(dialog.controller.inspect, job_id)
        dialog.ShowModal()
    finally:
        dialog.controller.dispose()
        dialog.Destroy()
