"""Native transcription workspace using the desktop's one background job runner."""
from hashlib import sha256
from pathlib import Path
import threading

from ..durable_state import local_path
from ..transcription import (EngineSpec, MAX_WORKER_BYTES, TranscriptionService,
                             TranscriptionSettings, _bounded_bytes)
from ..transcription_inputs import select_inputs


def describe_job(job):
    receipt = job.get('receipt') or {}
    lines = [job['title'], 'State: ' + job['state'],
        'Verified files: ' + str(receipt.get('done', 0)) + '/' + str(len(job['inputs'])),
        'One file is recognized at a time. An incomplete file restarts from the beginning.',
        'Each explicit run has a 24-hour wall-time limit, including pauses. Completed outputs are retained.']
    if job.get('error'):
        lines.append(job['error'])
    lines.extend(receipt.get('errors', []))
    for row in receipt.get('files', []):
        if row.get('error'):
            lines.append(row['source']['name'] + ': ' + row['error'])
        lines.extend(row.get('warnings', []))
    return '\n'.join(lines)[:20000]


def configure_engine(parent, service, *, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    dialog = wx.Dialog(parent, title='Reviewed local transcription engine', size=(850, 420),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    note = wx.StaticText(dialog, label='Select an already installed Python interpreter, the reviewed worker, '
        'and local CTranslate2 models. Recognition stays local. This window does not install or download models.')
    note.Wrap(780)
    layout.Add(note, 0, wx.EXPAND | wx.ALL, 8)
    fields = {}
    try:
        previous = service.engine()
    except (ValueError, OSError):
        previous = None
    for key, label, directory in (('python_path', '&Python executable', False),
                                  ('worker_path', '&Reviewed worker script', False),
                                  ('models_dir', 'Local &models folder', True)):
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        entry = wx.TextCtrl(dialog, value=getattr(previous, key, '') if previous else '')
        entry.SetName(label.replace('&', ''))
        fields[key] = entry
        row.Add(entry, 1, wx.EXPAND | wx.ALL, 5)
        browse = wx.Button(dialog, label='Browse ' + label.replace('&', ''))
        def choose(event, control=entry, is_directory=directory, title=label):
            picker = (wx.DirDialog(dialog, title.replace('&', '')) if is_directory
                      else wx.FileDialog(dialog, title.replace('&', ''), style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST))
            try:
                if picker.ShowModal() == wx.ID_OK:
                    control.ChangeValue(picker.GetPath())
            finally:
                picker.Destroy()
        browse.Bind(wx.EVT_BUTTON, choose)
        row.Add(browse, 0, wx.ALL, 5)
        layout.Add(row, 0, wx.EXPAND)
    status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Reviewed engine validation result')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 6)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    save = wx.Button(dialog, label='Verify and &save engine profile')
    cancel = wx.Button(dialog, wx.ID_CANCEL, label='Cancel')
    buttons.Add(save, 0, wx.ALL, 5)
    buttons.Add(cancel, 0, wx.ALL, 5)
    layout.Add(buttons)
    dialog.SetSizer(layout)
    accepted = None
    def commit(event):
        nonlocal accepted
        try:
            worker = _bounded_bytes(fields['worker_path'].GetValue().strip(), MAX_WORKER_BYTES)
            spec = EngineSpec(fields['python_path'].GetValue().strip(), fields['worker_path'].GetValue().strip(),
                              sha256(worker).hexdigest(), fields['models_dir'].GetValue().strip())
            accepted = service.configure_engine(spec)
            dialog.EndModal(wx.ID_OK)
        except (ValueError, OSError) as exc:
            status.ChangeValue(str(exc))
    save.Bind(wx.EVT_BUTTON, commit)
    try:
        dialog.ShowModal()
        return accepted
    finally:
        dialog.Destroy()


def create_transcription_dialog(frame, *, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class TranscriptionDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Local transcription — audio originals and transcript revisions',
                size=(1000, 800), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.library_root = local_path(frame.library.root, exists=True)
            self.service = TranscriptionService(self.library_root / 'state' / 'transcription')
            self.alive, self.busy, self.paused, self.pausable = True, False, False, False
            self.cancel = threading.Event()
            self.files, self.current_job, self.artifacts, self.revision = [], None, [], None
            self.editor_readable = True
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            layout = wx.BoxSizer(wx.VERTICAL)
            self.profile_status = wx.StaticText(panel)
            self.profile_status.SetName('Local transcription engine capability')
            self.profile_status.Wrap(940)
            layout.Add(self.profile_status, 0, wx.EXPAND | wx.ALL, 6)
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.buttons = {}
            for key, label, handler in [('engine', 'Configure reviewed &engine', self.on_engine),
                                       ('files', 'Choose audio &files', self.on_choose_files),
                                       ('folder', 'Choose audio f&older', self.on_choose_folder),
                                       ('jobs', 'Saved transcription &jobs', self.on_jobs)]:
                button = wx.Button(panel, label=label)
                button.Bind(wx.EVT_BUTTON, handler)
                self.buttons[key] = button
                row.Add(button, 0, wx.ALL, 4)
            layout.Add(row, 0, wx.EXPAND)
            self.recursive = wx.CheckBox(panel, label='Include subfolders when choosing a folder')
            self.recursive.SetName('Include subfolders when choosing a folder')
            self.recursive.SetValue(False)
            layout.Add(self.recursive, 0, wx.ALL, 4)
            layout.Add(wx.StaticText(panel, label='Selected local audio inputs; private managed copies are created when Run is chosen'),
                       0, wx.ALL, 4)
            self.selected_files = wx.ListBox(panel, size=(-1, 85))
            self.selected_files.SetName('Explicitly selected audio input files')
            layout.Add(self.selected_files, 0, wx.EXPAND | wx.ALL, 4)
            model_scope = wx.StaticText(panel, label='tiny.en is English-only. Choose en or auto; another source language needs '
                'an installed multilingual model. Whisper translation produces English. Runtime/model quality is qualified separately.')
            model_scope.Wrap(940)
            layout.Add(model_scope, 0, wx.EXPAND | wx.ALL, 4)
            grid = wx.FlexGridSizer(cols=4, hgap=8, vgap=4)
            grid.AddGrowableCol(1, 1)
            grid.AddGrowableCol(3, 1)
            self.settings_controls = {}
            def setting(key, label, *, value=None, choices=None):
                grid.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 3)
                control = wx.Choice(panel, choices=choices) if choices is not None else wx.TextCtrl(panel, value=str(value))
                if choices is not None:
                    control.SetStringSelection(str(value))
                control.SetName(label.replace('&', ''))
                self.settings_controls[key] = control
                grid.Add(control, 1, wx.EXPAND | wx.ALL, 3)
                return control
            setting('model', 'Local &model directory name', value='tiny.en')
            setting('language', 'Source &language code or auto', value='auto')
            setting('device', '&Device', value='cpu', choices=['cpu', 'cuda', 'auto'])
            setting('compute_type', '&Compute type', value='int8',
                    choices=['int8', 'float16', 'float32', 'default', 'auto', 'int8_float16', 'int8_float32',
                             'int8_bfloat16', 'int16', 'bfloat16'])
            setting('task', '&Task; translation target is English', value='transcribe', choices=['transcribe', 'translate'])
            setting('output_mode', 'TXT &grouping', value='separate', choices=['separate', 'one_per_folder', 'one_whole_job'])
            setting('beam_size', '&Beam size, 1 to 20', value='1')
            setting('title', 'Job &name', value='Transcription')
            layout.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
            switches = wx.BoxSizer(wx.HORIZONTAL)
            for key, label, value in [('vad_filter', 'Voice activity detection', True),
                    ('condition_on_previous_text', 'Use previous text context', True),
                    ('multilingual', 'Multilingual decoding', False), ('include_headers', 'TXT source headers', False)]:
                control = wx.CheckBox(panel, label=label)
                control.SetValue(value)
                control.SetName(label)
                self.settings_controls[key] = control
                switches.Add(control, 0, wx.ALL, 4)
            layout.Add(switches, 0, wx.EXPAND)
            formats = wx.BoxSizer(wx.HORIZONTAL)
            formats.Add(wx.StaticText(panel, label='Requested transcript formats:'), 0, wx.ALL, 5)
            for fmt in ('txt', 'srt', 'vtt', 'json'):
                control = wx.CheckBox(panel, label=fmt.upper())
                control.SetValue(True)
                control.SetName('Create ' + fmt.upper() + ' transcript')
                self.settings_controls['output_' + fmt] = control
                formats.Add(control, 0, wx.ALL, 4)
            layout.Add(formats)
            row = wx.BoxSizer(wx.HORIZONTAL)
            for key, label, handler in [('run', '&Run / resume selected job', self.on_run),
                    ('pause', '&Pause', self.on_pause), ('cancel', '&Cancel current transcription', self.on_cancel)]:
                button = wx.Button(panel, label=label)
                button.Bind(wx.EVT_BUTTON, handler)
                self.buttons[key] = button
                row.Add(button, 0, wx.ALL, 4)
            layout.Add(row, 0, wx.EXPAND)
            self.status = wx.TextCtrl(panel, size=(-1, 90), style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Transcription status, verified counts and warnings')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
            layout.Add(wx.StaticText(panel, label='Verified transcript output to read or export'), 0, wx.ALL, 4)
            self.artifact_choice = wx.Choice(panel)
            self.artifact_choice.SetName('Verified transcript output')
            self.artifact_choice.Bind(wx.EVT_CHOICE, self.on_artifact)
            layout.Add(self.artifact_choice, 0, wx.EXPAND | wx.ALL, 4)
            self.editor = wx.TextCtrl(panel, size=(-1, 180), style=wx.TE_MULTILINE)
            self.editor.SetName('Transcript text; save edits as a new derived revision')
            layout.Add(self.editor, 1, wx.EXPAND | wx.ALL, 4)
            row = wx.BoxSizer(wx.HORIZONTAL)
            for key, label, handler in [('revision', 'Save edited TXT as new re&vision', self.on_revision),
                    ('export', 'E&xport selected output / saved revision', self.on_export),
                    ('close', '&Close', self.on_close)]:
                button = wx.Button(panel, label=label)
                button.Bind(wx.EVT_BUTTON, handler)
                self.buttons[key] = button
                row.Add(button, 0, wx.ALL, 4)
            layout.Add(row)
            panel.SetSizer(layout)
            panel.FitInside()
            outer = wx.BoxSizer(wx.VERTICAL)
            outer.Add(panel, 1, wx.EXPAND)
            self.SetSizer(outer)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.refresh_capability()
            self.report('Choose local audio files, a folder or a saved job. Run snapshots selected audio and invokes only the reviewed local worker. '
                        'Restored jobs require an explicit Run and a newly selected engine profile.')
            self.controls()

        def report(self, message):
            if self.alive:
                self.status.ChangeValue(str(message))

        def current_root(self):
            if local_path(frame.library.root, exists=True) != self.library_root:
                raise ValueError('The library location changed. Reopen transcription for the selected library.')

        def refresh_capability(self):
            result = self.service.capability()
            digest = '\nReviewed worker SHA-256: ' + result['worker_sha256'] if 'worker_sha256' in result else ''
            self.profile_status.SetLabel(result['state'] + ': ' + result['message'] + digest)
            self.profile_status.Wrap(940)

        def controls(self):
            if not self.alive:
                return
            for key, control in self.settings_controls.items():
                control.Enable(not self.busy and self.current_job is None)
            for key in ('engine', 'files', 'folder', 'jobs', 'run'):
                self.buttons[key].Enable(not self.busy)
            self.recursive.Enable(not self.busy)
            self.buttons['pause'].Enable(self.busy and self.pausable and self.current_job is not None)
            self.buttons['cancel'].Enable(self.busy)
            self.artifact_choice.Enable(not self.busy and bool(self.artifacts))
            self.editor.Enable(not self.busy and bool(self.artifacts))
            self.buttons['export'].Enable(not self.busy and bool(self.artifacts))
            index = self.artifact_choice.GetSelection()
            editable = (0 <= index < len(self.artifacts) and self.artifacts[index]['format'] == 'txt'
                        and self.editor_readable)
            self.editor.SetEditable(editable and not self.busy)
            self.buttons['revision'].Enable(not self.busy and editable)

        def settings(self):
            value = {}
            for key, control in self.settings_controls.items():
                if key == 'title':
                    continue
                if key in ('device', 'compute_type', 'task', 'output_mode'):
                    value[key] = control.GetStringSelection()
                elif key in ('model', 'language'):
                    value[key] = control.GetValue().strip()
                elif key == 'beam_size':
                    raw = control.GetValue().strip()
                    if len(raw) > 2 or not raw.isascii() or not raw.isdecimal():
                        raise ValueError('Choose beam size 1 to 20.')
                    value[key] = int(raw)
                else:
                    value[key] = control.GetValue()
            return TranscriptionSettings(**value)

        def begin(self, title, operation, complete, *, pausable=False):
            if self.busy or not self.alive:
                return False
            self.current_root()
            if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                self.report('Another operation is active. Finish or cancel it before starting transcription.')
                return False
            self.busy, self.paused = True, False
            self.pausable = pausable
            self.cancel.clear()
            self.controls()
            def done(value):
                self.busy, self.paused = False, False
                if self.alive:
                    self.buttons['pause'].SetLabel('&Pause')
                    complete(value)
                    self.controls()
            def failed(error):
                self.busy, self.paused = False, False
                if self.alive:
                    self.buttons['pause'].SetLabel('&Pause')
                    self.report(str(error))
                    self.controls()
            frame.run_job(title, operation, done=done, failed=failed)
            return True

        def on_engine(self, event=None):
            try:
                self.current_root()
                configure_engine(self, self.service, wx_module=wx)
                self.refresh_capability()
            except (ValueError, OSError) as exc:
                self.report(exc)

        def set_files(self, files):
            if self.busy:
                raise ValueError('Wait for the current transcription to finish.')
            self.files, self.current_job, self.artifacts, self.revision = list(files), None, [], None
            self.selected_files.Set([str(path) for path in self.files])
            self.artifact_choice.Clear()
            self.editor.ChangeValue('')
            self.report(str(len(self.files)) + ' audio inputs selected. Run creates a private snapshot; originals remain unchanged.')
            self.controls()

        def on_choose_files(self, event=None):
            picker = wx.FileDialog(self, 'Choose explicit local audio inputs',
                wildcard='Audio and video|*.wav;*.mp3;*.m4a;*.mp4;*.flac;*.ogg;*.opus;*.aac;*.wma;*.webm;*.mkv;*.3gp;*.amr;*.m4b|All files|*.*',
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
            try:
                if picker.ShowModal() == wx.ID_OK:
                    self.set_files(picker.GetPaths())
            finally:
                picker.Destroy()

        def on_choose_folder(self, event=None):
            if self.busy or not self.alive:
                return
            picker = wx.DirDialog(self, 'Choose a local audio folder; only the displayed files will be submitted')
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                folder, recursive = picker.GetPath(), self.recursive.GetValue()
                def selected(selection):
                    self.set_files(selection.files)
                    self.report(selection.describe())
                self.begin('Reviewing selected audio folder',
                    lambda: select_inputs(folders=(folder,), recursive=recursive,
                        cancelled=lambda: self.cancel.is_set() or frame.cancel_event.is_set()), selected)
            except (ValueError, OSError) as exc:
                self.report(exc)
            finally:
                picker.Destroy()

        def on_run(self, event=None):
            try:
                self.current_root()
                engine = self.service.engine()
                identity = self.current_job
                settings = self.settings() if identity is None else None
                files, title = tuple(self.files), self.settings_controls['title'].GetValue()
                if identity is None and not files:
                    raise ValueError('Choose audio files or a saved transcription job first.')
                def operation():
                    progress = getattr(frame, 'operation_progress', None)
                    token = progress.event.token if progress is not None else None
                    def advanced(done, total, phase):
                        if progress is not None:
                            if phase == 'paused':
                                progress.pause('Transcription paused; incomplete file will restart', token=token)
                            else:
                                if progress.event.state == 'paused':
                                    progress.resume('Resuming local transcription', token=token)
                                progress.update(done, total if done is not None else None, phase, token=token)
                        label = str(done) + '/' + str(total) + ': ' + phase if done is not None and total is not None else phase
                        wx.CallAfter(self.report, label)
                    stopped = lambda: self.cancel.is_set() or frame.cancel_event.is_set()
                    current = identity
                    if current is None:
                        def copied(done, total, phase):
                            advanced(None, None, phase + ': ' + str(done) + '/' + str(total))
                        current = self.service.submit(files, settings, title=title, cancelled=stopped, progress=copied)
                        self.current_job = current
                        wx.CallAfter(self.controls)
                    result = self.service.run(current, engine=engine, cancelled=stopped, progress=advanced)
                    # Large verified transcript reads stay on this worker too.
                    return (result, None) if result['state'] == 'cancelled' else self.job_view(result, cancelled=stopped)
                self.begin('Local transcription', operation, lambda result: self.load_job(*result), pausable=True)
            except (ValueError, OSError) as exc:
                self.report(exc)

        def job_view(self, job, *, cancelled):
            artifacts = (job.get('receipt') or {}).get('artifacts', [])
            selected = next((item for item in artifacts if item['format'] == 'txt'), artifacts[0] if artifacts else None)
            value = (self.service.read_transcript(job['id'], selected['path'], cancelled=cancelled)
                     if selected is not None else None)
            return job, value

        def load_job(self, job, text=None):
            self.current_job, self.revision = job['id'], None
            self.selected_files.Set([row['name'] + ' — ' + row['sha256'][:12] for row in job['inputs']])
            self.artifacts = (job.get('receipt') or {}).get('artifacts', [])
            self.artifact_choice.Set([item['format'].upper() + ': ' + item['path'] for item in self.artifacts])
            self.editor.ChangeValue('')
            for key, control in self.settings_controls.items():
                value = job['title'] if key == 'title' else job['settings'][key]
                if key in ('device', 'compute_type', 'task', 'output_mode'):
                    control.SetStringSelection(value)
                elif key in ('title', 'model', 'language', 'beam_size'):
                    control.ChangeValue(str(value))
                else:
                    control.SetValue(value)
            self.report(describe_job(job))
            if self.artifacts:
                index = next((i for i, item in enumerate(self.artifacts) if item['format'] == 'txt'), 0)
                self.artifact_choice.SetSelection(index)
                if text is not None:
                    self.display_artifact(text)
                else:
                    self.editor_readable = False
                    self.editor.ChangeValue('Select an output to read its verified contents.')
            self.controls()

        def on_pause(self, event=None):
            try:
                if self.busy and self.current_job is not None:
                    self.service.set_paused(self.current_job, not self.paused)
                    self.paused = not self.paused
                    self.buttons['pause'].SetLabel('Continue &paused job' if self.paused else '&Pause')
                    self.report('Pause requested; completed files are retained.' if self.paused else 'Resume requested.')
            except (ValueError, OSError) as exc:
                self.report(exc)

        def on_cancel(self, event=None):
            self.cancel.set()
            self.report('Cancellation requested. The local worker is being stopped; verified completed outputs remain available.')

        def on_jobs(self, event=None):
            try:
                rows = self.service.list()
                chooser = wx.SingleChoiceDialog(self, 'Select a saved job. Resume is always explicit.', 'Local transcription jobs',
                    [row['title'] + ' — ' + row['state'] for row in rows])
                try:
                    if chooser.ShowModal() == wx.ID_OK and 0 <= chooser.GetSelection() < len(rows):
                        identity = rows[chooser.GetSelection()]['id']
                        def operation():
                            stopped = lambda: self.cancel.is_set() or frame.cancel_event.is_set()
                            return self.job_view(self.service.get(identity, cancelled=stopped), cancelled=stopped)
                        self.begin('Verifying saved transcription', operation, lambda result: self.load_job(*result))
                finally:
                    chooser.Destroy()
            except (ValueError, OSError) as exc:
                self.report(exc)

        def display_artifact(self, value):
            self.revision = None
            self.editor_readable = True
            if len(value) > 2_000_000:
                self.editor_readable = False
                self.editor.ChangeValue('This transcript exceeds the editor limit. Export the verified original output to read it externally.')
                self.editor.SetEditable(False)
                self.buttons['revision'].Disable()
                return
            self.editor.ChangeValue(value)

        def on_artifact(self, event=None):
            try:
                index = self.artifact_choice.GetSelection()
                if not 0 <= index < len(self.artifacts):
                    return
                identity, name = self.current_job, self.artifacts[index]['path']
                self.begin('Reading verified transcript', lambda: self.service.read_transcript(identity, name,
                    cancelled=lambda: self.cancel.is_set() or frame.cancel_event.is_set()), self.display_artifact)
            except (ValueError, OSError, UnicodeError) as exc:
                self.report(exc)

        def on_revision(self, event=None):
            try:
                index = self.artifact_choice.GetSelection()
                if not 0 <= index < len(self.artifacts):
                    return
                identity, name, value = self.current_job, self.artifacts[index]['path'], self.editor.GetValue()
                def saved(revision):
                    self.revision = revision['id']
                    self.report('Saved new derived TXT revision ' + revision['id'] + '. The worker transcript remains unchanged.')
                self.begin('Saving transcript revision', lambda: self.service.revise(identity, name, value), saved)
            except (ValueError, OSError) as exc:
                self.report(exc)

        def on_export(self, event=None):
            index = self.artifact_choice.GetSelection()
            if not 0 <= index < len(self.artifacts):
                return
            item = self.artifacts[index]
            picker = wx.FileDialog(self, 'Export to a NEW file', defaultFile=('edited-' if self.revision else '') + Path(item['path']).name,
                wildcard=item['format'].upper() + '|*.' + item['format'], style=wx.FD_SAVE)
            try:
                if picker.ShowModal() == wx.ID_OK:
                    destination, identity, revision = picker.GetPath(), self.current_job, self.revision
                    self.begin('Exporting verified transcript',
                        lambda: self.service.export(identity, item['path'], destination, revision_id=revision),
                        lambda path: self.report('Saved new transcript file: ' + str(path)))
            except (ValueError, OSError) as exc:
                self.report(exc)
            finally:
                picker.Destroy()

        def on_close(self, event=None):
            self.cancel.set()
            self.alive = False
            if self.IsModal():
                self.EndModal(wx.ID_CLOSE)
            else:
                self.Hide()

    return TranscriptionDialog()


def show_transcription(frame):
    dialog = create_transcription_dialog(frame)
    try:
        dialog.ShowModal()
    finally:
        dialog.cancel.set()
        dialog.alive = False
        dialog.Destroy()


def install_transcription_actions(frame):
    import wx
    bar = frame.menu_bar
    menu = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise RuntimeError('Install the shared Tools menu before transcription actions.')
    identity = wx.NewIdRef()
    menu.Append(identity, 'Local audio &transcription...')
    frame.Bind(wx.EVT_MENU, lambda event: show_transcription(frame), id=int(identity))
