"""Protocol-specific native view opened by the shared transcription entrypoint.

Preparing a job retains owned inputs and freezes its settings. Running it is a
separate action. A completed status is never accepted without the service's
byte-bound receipt validation; no engine or private model ships in this module.
"""
from dataclasses import dataclass, field
from pathlib import Path
import threading

from ..transcription_receipt import DEFAULT_SETTINGS, _LANGUAGES, validate_settings
from ..transcription_service import TranscriptionService

_CHOICES = {
    'output_mode': (('separate', 'Separate text for each input'),
                    ('one_per_folder', 'One text for each selected folder'),
                    ('one_whole_job', 'One text for the whole job')),
    'task': (('transcribe', 'Transcribe in the original language'),
             ('translate', 'Translate speech to English')),
    'translation_target': (('en', 'English'),),
    'language': (('auto', 'Detect language'), ('uk', 'Українська (uk)'),
                 ('en', 'English (en)'), ('sk', 'Slovenčina (sk)')) +
                tuple((code, code) for code in sorted(_LANGUAGES - {'uk', 'en', 'sk'})),
    'device': (('cpu', 'CPU'), ('cuda', 'CUDA'), ('auto', 'Automatic local device')),
    'compute_type': (('int8', 'INT8'), ('auto', 'Automatic local compute'),
                     ('float16', 'Float16'), ('float32', 'Float32')),
}
_LABELS = {
    'output_mode': 'Text output grouping', 'task': 'Speech task',
    'translation_target': 'Speech translation target', 'language': 'Input language',
    'model': 'Local model directory name', 'device': 'Local processing device',
    'compute_type': 'Local compute type', 'beam_size': 'Beam size, 1 to 10',
    'parallel_files': 'Parallel input files, 1 to 20',
    'vad_filter': 'Filter non-speech with VAD', 'include_headers': 'Include source headers',
    'output_txt': 'Write TXT', 'output_srt': 'Write SRT', 'output_vtt': 'Write VTT',
    'output_json': 'Write JSON transcript', 'multilingual': 'Multilingual inference',
    'condition_on_previous_text': 'Condition on previous text',
}


def settings_from_controls(controls):
    """Read native typed controls and apply the same 17-field service contract."""
    values = {}
    for name in DEFAULT_SETTINGS:
        control = controls[name]
        if name in _CHOICES:
            selection = control.GetSelection()
            if type(selection) is not int or not 0 <= selection < len(_CHOICES[name]):
                raise ValueError('Choose a value for ' + _LABELS[name] + '.')
            values[name] = _CHOICES[name][selection][0]
        else:
            values[name] = control.GetValue()
    return validate_settings(values)


def apply_settings(controls, settings):
    values = validate_settings(settings)
    for name, value in values.items():
        if name in _CHOICES:
            controls[name].SetSelection(tuple(code for code, _ in _CHOICES[name]).index(value))
        else:
            controls[name].SetValue(value)


def _error_text(error):
    if isinstance(error, InterruptedError):
        return 'Stopped. Prepared inputs and completed files remain available for review.'
    if isinstance(error, OSError):
        return 'A local file could not be accessed: ' + (error.strerror or 'check the selected files and available space.')
    return ' '.join(str(error).split())[:500] or 'The local transcription operation could not complete.'



@dataclass(frozen=True)
class _VerifiedRun:
    """Present an accepted durable execution to the existing host finalizer."""
    summary: dict
    status: str = field(default='completed', init=False)


def _run_verified(service, identity, manifest, *, cancelled, advanced):
    """Use the reporter bound to this worker, independently of the visible task."""
    from ..progress import current_progress
    from ..transcription_host import report_progress
    task = current_progress()
    def observed(done, total, phase):
        if task is not None:
            try:
                report_progress(task, done, total, phase)
            except Exception:
                pass  # An observer cannot fail or retry a valid execution.
        try:
            advanced(done, total, phase)
        except Exception:
            pass  # A closing native view cannot change the service outcome.
    summary = service.run(identity, manifest, cancelled=cancelled, progress=observed)
    # run() returns only after exit, receipt/input/output validation and the
    # durable complete state. The immutable wrapper preserves that exact DTO;
    # the host finalizes 100% only after its real Future has returned.
    if task is not None:
        try:
            report_progress(task, summary['input_count'], summary['input_count'],
                            'Completed receipt and output checks')
        except Exception:
            pass
    return _VerifiedRun(summary)


class TranscriptionSession:
    """Keep a modal alive until its owned operation has actually settled."""
    def __init__(self, frame, *, dispatch, changed, report, close, request_stop=lambda: None):
        self.frame, self.library = frame, frame.library
        self.root = Path(self.library.root).absolute()
        self.dispatch, self.changed, self.report = dispatch, changed, report
        self.close_dialog, self.request_stop = close, request_stop
        self.cancel = threading.Event()
        self.busy = self.closed = self.close_requested = self.operation_finished = False
        self.generation, self.kind = 0, None

    def current(self):
        try:
            return (not self.closed and getattr(self.frame, 'library', None) is self.library
                    and Path(self.library.root).absolute() == self.root
                    and not getattr(self.frame, '_closing', False))
        except (TypeError, ValueError, OSError):
            return False

    def guard(self):
        if not self.current():
            raise InterruptedError('The selected library or transcription window changed.')

    def stopped(self):
        return (self.cancel.is_set() or not self.current()
                or self.frame.cancel_event.is_set())

    def _close(self):
        if not self.closed:
            self.closed = True
            self.cancel.set()
            self.close_dialog()

    def _settled(self, generation):
        if self.closed or generation != self.generation:
            return
        self.operation_finished = True
        if self.close_requested or not self.current():
            self.busy = False
            self._close()

    def submit(self, title, operation, complete, *, kind, total=None):
        if not self.current():
            self._close()
            return False
        if self.busy or getattr(self.frame, 'working', False):
            self.report('Finish or stop the current operation before starting another.')
            return False
        self.busy, self.operation_finished, self.kind = True, False, kind
        self.generation += 1
        generation = self.generation
        self.cancel.clear()
        self.changed()

        def arrived(result=None, error=None):
            if self.closed or generation != self.generation:
                return
            self.busy, self.operation_finished, self.kind = False, True, None
            if self.close_requested or not self.current():
                self._close()
                return
            self.changed()
            if error is not None:
                self.report(_error_text(error))
                return
            try:
                complete(result)
            except Exception as exc:
                self.report('The operation returned; its result needs review. ' + _error_text(exc))

        def perform():
            try:
                self.guard()
                return operation()
            finally:
                # This callback also settles a close if the main frame suppresses
                # its ordinary completion callback after a root/window change.
                try:
                    self.dispatch(self._settled, generation)
                except RuntimeError:
                    pass

        try:
            started = self.frame.run_job(title, perform,
                done=lambda value: arrived(value), failed=lambda error: arrived(error=error),
                progress_total=total, progress_unit='files')
        except Exception as exc:
            arrived(error=exc)
            return False
        if started is False or (self.busy and not getattr(self.frame, 'working', False)
                                and not self.operation_finished):
            self.busy, self.kind = False, None
            self.cancel.set()
            self.changed()
            self.report('The local operation did not start.')
            return False
        return True

    def stop(self):
        if not self.busy or self.closed:
            return
        self.cancel.set()
        try:
            self.request_stop()
        except Exception as exc:
            self.report(_error_text(exc))
        progress = getattr(self.frame, 'operation_progress', None)
        if progress is not None:
            try:
                progress.request_cancel()
            except Exception:
                pass  # The owned cancellation flag still reaches the worker.
        self.report('Stopping; waiting for the current worker and file checks to finish.')

    def request_close(self):
        if self.closed:
            return True
        self.close_requested = True
        if self.busy:
            self.stop()
            if not self.operation_finished:
                return False
        self._close()
        return True


class TranscriptionDialog:
    def __init__(self, frame, *, service=None):
        import wx
        self.wx, self.frame = wx, frame
        self.root = Path(frame.library.root).absolute()
        self.service = service or TranscriptionService(self.root)
        if Path(self.service.library_root).absolute() != self.root:
            raise ValueError('Transcription must belong to the selected library.')
        self.sources, self.manifest_path, self.current_job = (), '', None
        self.runtime_profile = None
        self.definition, self.summary, self.rows = None, None, ()
        self.pause_requested = False
        self.dialog = wx.Dialog(frame, title='Local speech transcription', size=(960, 870),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Local speech transcription workspace')
        self.fields, self.buttons = {}, {}
        try:
            self._build()
            self.session = TranscriptionSession(frame, dispatch=wx.CallAfter, changed=self.controls,
                report=self.report, close=self._finish_close, request_stop=self._request_stop)
            self._bind()
            self.controls()
        except BaseException:
            self.dialog.Destroy()
            raise

    def _build(self):
        wx, dialog = self.wx, self.dialog
        layout = wx.BoxSizer(wx.VERTICAL)
        body = wx.ScrolledWindow(dialog, style=wx.VSCROLL)
        body.SetScrollRate(0, 12)
        content = wx.BoxSizer(wx.VERTICAL)

        def text(label, *, value='', readonly=False):
            content.Add(wx.StaticText(body, label=label), 0, wx.ALL, 4)
            control = wx.TextCtrl(body, value=value, style=wx.TE_READONLY if readonly else 0)
            control.SetName(label.replace('&', ''))
            content.Add(control, 0, wx.EXPAND | wx.ALL, 4)
            return control

        self.scope = text('Transcription job scope',
            value='All jobs in this library; existing workspace jobs keep their own profiles.', readonly=True)
        self.title = text('Job t&itle', value='Local transcription')
        self.title.SetMaxLength(200)
        self.manifest = text('Explicitly selected local runtime manifest', readonly=True)
        self.runtime_identity = text('Prepared runtime identity; exact match required at Start', readonly=True)
        row = wx.WrapSizer(wx.HORIZONTAL)
        for name, label in (('files', 'Add audio/video &files...'), ('folder', 'Add fol&der...'),
                            ('remove', '&Remove selected input'), ('manifest', 'Choose runtime &manifest...'),
                            ('profiles', 'Pic&k saved runtime...'), ('engine_settings', 'Engine settin&gs...')):
            button = wx.Button(body, label=label)
            button.SetName(label.replace('&', '').replace('...', ''))
            self.buttons[name] = button
            row.Add(button, 0, wx.ALL, 3)
        content.Add(row, 0, wx.EXPAND)
        self.source_list = wx.ListBox(body, style=wx.LB_SINGLE, size=(-1, 90))
        self.source_list.SetName('Selected sources; prepared jobs show retained relative input names')
        content.Add(self.source_list, 0, wx.EXPAND | wx.ALL, 4)
        content.Add(wx.StaticText(body, label='Settings are frozen when Prepare succeeds. '
                    'Translate speech supports English as its explicit target.'), 0, wx.ALL, 4)
        grid = wx.FlexGridSizer(cols=2, vgap=5, hgap=10)
        grid.AddGrowableCol(1, 1)
        for name in DEFAULT_SETTINGS:
            if type(DEFAULT_SETTINGS[name]) is bool:
                continue
            grid.Add(wx.StaticText(body, label=_LABELS[name]), 0, wx.ALIGN_CENTER_VERTICAL)
            if name in _CHOICES:
                field = wx.Choice(body, choices=[label for _, label in _CHOICES[name]])
            elif name in ('beam_size', 'parallel_files'):
                field = wx.SpinCtrl(body, min=1, max=10 if name == 'beam_size' else 20, initial=1)
            else:
                field = wx.TextCtrl(body, value=str(DEFAULT_SETTINGS[name]))
                field.SetMaxLength(64)
            field.SetName(_LABELS[name])
            self.fields[name] = field
            grid.Add(field, 1, wx.EXPAND)
        content.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        boolean_grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=10)
        for name in DEFAULT_SETTINGS:
            if type(DEFAULT_SETTINGS[name]) is not bool:
                continue
            field = wx.CheckBox(body, label=_LABELS[name])
            field.SetName(_LABELS[name])
            self.fields[name] = field
            boolean_grid.Add(field, 0, wx.ALL, 2)
        content.Add(boolean_grid, 0, wx.EXPAND | wx.ALL, 4)
        body.SetSizer(content)
        layout.Add(body, 1, wx.EXPAND | wx.ALL, 4)
        self.jobs_list = wx.ListBox(dialog, style=wx.LB_SINGLE, size=(-1, 70))
        self.jobs_list.SetName('Saved local transcription jobs')
        layout.Add(wx.StaticText(dialog, label='Saved jobs; review their frozen settings before starting'), 0, wx.ALL, 3)
        layout.Add(self.jobs_list, 0, wx.EXPAND | wx.ALL, 3)
        self.outputs = wx.ListBox(dialog, style=wx.LB_SINGLE, size=(-1, 75))
        self.outputs.SetName('Outputs accepted after receipt and byte verification')
        layout.Add(wx.StaticText(dialog, label='Verified outputs'), 0, wx.ALL, 3)
        layout.Add(self.outputs, 0, wx.EXPAND | wx.ALL, 3)
        self.status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        self.status.SetName('Transcription operation status and measured progress')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for name, label in (
                ('new', '&New job'), ('refresh', 'Refresh saved &jobs'), ('load', 'Re&view selected job'),
                ('prepare', '&Prepare retained inputs'), ('start', '&Start prepared job'),
                ('pause', 'Pa&use'), ('resume', 'R&esume'), ('stop', 'S&top'),
                ('verify', 'Verify &outputs'), ('reading', 'Edit reading &copy'), ('close', 'C&lose')):
            button = wx.Button(dialog, id=wx.ID_CLOSE if name == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            self.buttons[name] = button
            buttons.Add(button, 0, wx.ALL, 3)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 3)
        dialog.SetSizer(layout)
        dialog.SetEscapeId(wx.ID_CLOSE)
        apply_settings(self.fields, dict(DEFAULT_SETTINGS))

    def _bind(self):
        handlers = dict(files=self.add_files, folder=self.add_folder, remove=self.remove_source,
            manifest=self.choose_manifest, profiles=self.choose_profile, engine_settings=self.engine_settings,
            new=self.new, refresh=self.refresh, load=self.load,
            prepare=self.prepare, start=self.start, pause=self.pause, resume=self.resume,
            stop=lambda event: self.session.stop(), verify=self.verify, reading=self.reading, close=self.close)
        for name, handler in handlers.items():
            self.buttons[name].Bind(self.wx.EVT_BUTTON, handler)
        self.jobs_list.Bind(self.wx.EVT_LISTBOX, lambda event: self.controls())
        self.outputs.Bind(self.wx.EVT_LISTBOX, lambda event: self.controls())
        self.source_list.Bind(self.wx.EVT_LISTBOX, lambda event: self.controls())
        self.dialog.Bind(self.wx.EVT_CLOSE, self.close)
        def destroyed(event):
            if event.GetEventObject() is self.dialog:
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
        editable = idle and self.current_job is None
        self.title.Enable(editable)
        for field in self.fields.values():
            field.Enable(editable)
        for name in ('files', 'folder'):
            self.buttons[name].Enable(editable)
        self.buttons['remove'].Enable(editable and self.source_list.GetSelection() >= 0)
        for name in ('manifest', 'profiles', 'engine_settings'):
            self.buttons[name].Enable(idle)
        for name in ('new', 'refresh'):
            self.buttons[name].Enable(idle)
        self.jobs_list.Enable(idle)
        self.buttons['load'].Enable(idle and self.jobs_list.GetSelection() >= 0)
        self.buttons['prepare'].Enable(editable and bool(self.sources) and bool(self.manifest_path))
        self.buttons['start'].Enable(idle and self.current_job is not None and bool(self.manifest_path))
        self.buttons['verify'].Enable(idle and self.current_job is not None)
        self.buttons['reading'].Enable(idle and self.summary is not None and self.outputs.GetSelection() >= 0)
        running = self.session.busy and self.session.kind == 'run' and not self.session.cancel.is_set()
        self.buttons['pause'].Enable(running and not self.pause_requested)
        self.buttons['resume'].Enable(running and self.pause_requested)
        self.buttons['stop'].Enable(self.session.busy and not self.session.cancel.is_set())

    def add_paths(self, paths):
        self.session.guard()
        if self.session.busy or self.current_job is not None:
            raise ValueError('Choose New job before changing prepared inputs.')
        chosen = tuple(str(Path(path).absolute()) for path in paths)
        if len(self.sources) + len(chosen) > 1000:
            raise ValueError('Choose no more than 1,000 file or folder selections.')
        self.sources += tuple(path for path in chosen if path not in self.sources)
        self.source_list.SetItems(list(self.sources))
        self.controls()

    def add_files(self, event=None):
        picker = self.wx.FileDialog(self.dialog, 'Choose local audio or video inputs',
            wildcard='Audio/video files|*.wav;*.mp3;*.m4a;*.mp4;*.flac;*.ogg;*.opus;*.aac;*.wma;*.webm;*.mkv;*.3gp;*.amr;*.m4b|All files (*.*)|*.*',
            style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST | self.wx.FD_MULTIPLE)
        try:
            if picker.ShowModal() == self.wx.ID_OK:
                self.add_paths(picker.GetPaths())
        except Exception as exc:
            self.report(_error_text(exc))
        finally:
            picker.Destroy()

    def add_folder(self, event=None):
        picker = self.wx.DirDialog(self.dialog, 'Choose an explicit folder of local audio or video',
                                   style=self.wx.DD_DEFAULT_STYLE | self.wx.DD_DIR_MUST_EXIST)
        try:
            if picker.ShowModal() == self.wx.ID_OK:
                self.add_paths((picker.GetPath(),))
        except Exception as exc:
            self.report(_error_text(exc))
        finally:
            picker.Destroy()

    def remove_source(self, event=None):
        if self.session.busy or self.current_job is not None:
            return
        index = self.source_list.GetSelection()
        if 0 <= index < len(self.sources):
            self.sources = self.sources[:index] + self.sources[index + 1:]
            self.source_list.SetItems(list(self.sources))
            self.controls()

    def choose_manifest(self, event=None):
        if self.session.busy:
            return
        picker = self.wx.FileDialog(self.dialog, 'Choose the reviewed local transcription runtime manifest',
            wildcard='Runtime manifest (*.json)|*.json', style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == self.wx.ID_OK:
                self.session.guard()
                self.manifest_path = picker.GetPath()
                self.runtime_profile = None
                self.manifest.ChangeValue(self.manifest_path)
                self.controls()
                self.report('Selected a local manifest. Its entire inventory and exact job identity are checked before execution.')
        except Exception as exc:
            self.report(_error_text(exc))
        finally:
            picker.Destroy()

    def _runtime_for(self, manifest_path, profile):
        if profile is None:
            return manifest_path
        from ..runtime_settings import EngineProfileStore
        identity, expected = profile
        reviewed = EngineProfileStore(self.root).admit(
            identity, engine='supplied-whisper', cancelled=self.session.stopped)
        if (reviewed.manifest.manifest_sha256 != expected
                or str(reviewed.manifest.path) != manifest_path):
            raise ValueError('The saved runtime selection changed. Review and select it again.')
        return str(reviewed.manifest.path)

    def choose_profile(self, event=None):
        if self.session.busy:
            return
        from ..runtime_settings import EngineProfileStore
        try:
            store = EngineProfileStore(self.root)
            profiles = tuple(row for row in store.snapshot().profiles
                             if row.enabled and row.engine == 'supplied-whisper')
            if not profiles:
                self.report('No enabled transcription runtime is registered. Use Engine settings to review an installed manifest.')
                return
            picker = self.wx.SingleChoiceDialog(self.dialog, 'Choose a saved local transcription runtime',
                'Saved transcription runtimes', [row.name for row in profiles])
            try:
                if picker.ShowModal() != self.wx.ID_OK:
                    return
                index = picker.GetSelection()
                if not 0 <= index < len(profiles):
                    raise ValueError('Choose a saved transcription runtime.')
                profile = profiles[index]
            finally:
                picker.Destroy()
            self.runtime_profile, self.manifest_path = None, ''
            self.manifest.ChangeValue('')
            def selected(reviewed):
                values = None
                if self.current_job is None:
                    values = settings_from_controls(self.fields)
                    values['model'] = reviewed.manifest.settings['model']
                    values = validate_settings(values)
                self.runtime_profile = (profile.id, reviewed.manifest.manifest_sha256)
                self.manifest_path = str(reviewed.manifest.path)
                self.manifest.ChangeValue(self.manifest_path)
                if values is not None:
                    apply_settings(self.fields, values)
                    self.runtime_identity.ChangeValue(reviewed.manifest.manifest_sha256)
                self.controls()
                self.report('Selected ' + profile.name + '. Prepare and Start recheck the local registration and all runtime files.')
            self.session.submit('Verifying saved transcription runtime',
                lambda: store.admit(profile.id, engine='supplied-whisper', cancelled=self.session.stopped),
                selected, kind='review')
        except Exception as exc:
            self.report(_error_text(exc))

    def engine_settings(self, event=None):
        if self.session.busy:
            return
        from .engine_settings import show_engine_settings
        try:
            self.session.guard()
            show_engine_settings(self.frame, parent=self.dialog)
            self.controls()
        except Exception as exc:
            self.report(_error_text(exc))

    def _selected_definition(self, identity, definition):
        self.session.guard()
        self.current_job, self.definition, self.summary = identity, definition, None
        self.outputs.SetItems([])
        self.title.ChangeValue(definition['title'])
        apply_settings(self.fields, definition['settings'])
        self.runtime_identity.ChangeValue(definition['runtime_manifest_sha256'])
        self.source_list.SetItems([row.get('path', row['relative']) + ' — ' + str(row['bytes']) + ' bytes'
                                   for row in definition['inputs']])
        self.controls()
        self.report('Prepared job: ' + str(len(definition['inputs'])) +
                    ' retained inputs. Settings are read-only. Start verifies the explicitly selected runtime.')

    def new(self, event=None):
        if self.session.busy:
            return
        self.session.guard()
        self.current_job = self.definition = self.summary = None
        self.sources, self.manifest_path, self.pause_requested = (), '', False
        self.runtime_profile = None
        self.title.ChangeValue('Local transcription')
        self.manifest.ChangeValue('')
        self.runtime_identity.ChangeValue('')
        self.source_list.SetItems([])
        self.outputs.SetItems([])
        apply_settings(self.fields, dict(DEFAULT_SETTINGS))
        self.controls()
        self.report('Choose inputs, a reviewed local runtime and settings, then Prepare. No worker has started.')

    def refresh(self, event=None):
        def loaded(rows):
            self.rows = tuple(rows)
            self.jobs_list.SetItems([row['title'] + ' — ' + str(row['inputs']) + ' retained inputs — ' + row['id'][:8]
                                     for row in self.rows])
            self.controls()
            self.report(str(len(rows)) + ' saved local jobs. Select a job and review its frozen settings.')
        self.session.submit('Reading saved transcription jobs',
            lambda: self.service.jobs(cancelled=self.session.stopped), loaded, kind='list')

    def load(self, event=None):
        index = self.jobs_list.GetSelection()
        if not 0 <= index < len(self.rows):
            self.report('Select a saved job first.')
            return
        identity = self.rows[index]['id']
        self.session.submit('Reviewing prepared transcription',
            lambda: self.service.definition(identity, verify_inputs=False, cancelled=self.session.stopped),
            lambda definition: self._selected_definition(identity, definition), kind='review')

    def prepare(self, event=None):
        try:
            if self.current_job is not None:
                raise ValueError('Choose New job before changing a prepared request.')
            settings = settings_from_controls(self.fields)
            sources, manifest, title = tuple(self.sources), self.manifest_path, self.title.GetValue()
            profile = self.runtime_profile
            if not title.strip() or len(title) > 200 or any(ord(char) < 32 for char in title):
                raise ValueError('Enter a nonempty job title of at most 200 characters.')
            if not sources or not manifest:
                raise ValueError('Choose explicit inputs and a reviewed local runtime first.')
            def work():
                selected_manifest = self._runtime_for(manifest, profile)
                identity = self.service.prepare(sources, selected_manifest, settings, title=title,
                                                cancelled=self.session.stopped)
                return identity, self.service.definition(identity, verify_inputs=False,
                                                          cancelled=self.session.stopped)
            self.session.submit('Retaining selected transcription inputs', work,
                lambda result: self._selected_definition(*result), kind='prepare')
        except Exception as exc:
            self.report(_error_text(exc))

    def _verified(self, summary):
        self.summary = summary
        self.outputs.SetItems([row['path'] + ' — ' + row['format'].upper() + ' — ' +
                              str(row['bytes']) + ' bytes' for row in summary['outputs']])
        if summary['outputs']:
            self.outputs.SetSelection(0)
        self.controls()
        notice = (' Historical results remain available; Start requires a newly checked local runtime.'
                  if summary.get('historical') else '')
        self.report('Verified ' + str(summary['output_count']) + ' outputs for ' +
                    str(summary['input_count']) + ' retained inputs. Reading copies preserve these originals.' + notice)

    def start(self, event=None):
        try:
            if self.current_job is None or not self.manifest_path:
                raise ValueError('Review a prepared job and explicitly choose its runtime manifest.')
            identity, manifest = self.current_job, self.manifest_path
            profile = self.runtime_profile
            self.summary, self.pause_requested = None, False
            self.outputs.SetItems([])
            def run():
                generation = self.session.generation
                selected_manifest = self._runtime_for(manifest, profile)
                return _run_verified(self.service, identity, selected_manifest, cancelled=self.session.stopped,
                    advanced=lambda done, total, phase:
                        self.wx.CallAfter(self._progress, generation, done, total, phase))
            self.session.submit('Running reviewed local transcription', run,
                lambda result: self._verified(result.summary),
                kind='run', total=len(self.definition['inputs']))
        except Exception as exc:
            self.report(_error_text(exc))

    def _progress(self, generation, done, total, phase):
        if (not self.session.closed and self.session.current()
                and self.session.busy and generation == self.session.generation):
            self.report(str(done) + '/' + str(total) + ' files: ' + str(phase))

    def _request_stop(self):
        if self.session.kind == 'run' and self.current_job is not None:
            self.service.control(self.current_job, 'stop')
        self.controls()

    def pause(self, event=None):
        if self.session.busy and self.session.kind == 'run' and not self.session.cancel.is_set():
            try:
                self.service.control(self.current_job, 'pause')
                self.pause_requested = True
                self.controls()
                self.report('Pause requested. Progress will report when the worker reaches its checkpoint.')
            except Exception as exc:
                self.report(_error_text(exc))

    def resume(self, event=None):
        if self.session.busy and self.session.kind == 'run' and not self.session.cancel.is_set():
            try:
                self.service.control(self.current_job, 'resume')
                self.pause_requested = False
                self.controls()
                self.report('Resume requested for this running job.')
            except Exception as exc:
                self.report(_error_text(exc))

    def verify(self, event=None):
        if self.current_job is None:
            return
        identity = self.current_job
        self.summary = None
        self.outputs.SetItems([])
        self.session.submit('Verifying completed transcription outputs',
            lambda: self.service.completed(identity, cancelled=self.session.stopped), self._verified, kind='verify')

    def reading(self, event=None):
        index = self.outputs.GetSelection()
        if self.current_job is None or self.summary is None or not 0 <= index < len(self.summary['outputs']):
            self.report('Verify and select one output first.')
            return
        identity, relative = self.current_job, self.summary['outputs'][index]['path']
        def open_copy(project):
            from ..reader.narration import NarrationStore
            from .narration_editor import open_narration_editor
            self.session.guard()
            open_narration_editor(self.dialog, NarrationStore(self.root), project, guard=self.session.guard)
        self.session.submit('Opening a verified transcript as a reading copy',
            lambda: self.service.reading_copy(identity, relative, cancelled=self.session.stopped),
            open_copy, kind='reading')

    def close(self, event=None):
        finished = self.session.request_close()
        if not finished and event is not None and callable(getattr(event, 'CanVeto', None)) and event.CanVeto():
            event.Veto()

    def _finish_close(self):
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self, *, on_ready=None):
        self.report('Local processing only. Prepare retains selected inputs; Start is a separate reviewed action. '
                    'No runtime, model, provider or language is downloaded automatically.')
        self.wx.CallAfter(self.refresh)
        if on_ready is not None:
            self.wx.CallAfter(on_ready, self)
        try:
            return self.dialog.ShowModal()
        finally:
            self.session.closed = True
            self.session.cancel.set()
            self.dialog.Destroy()


def show_transcription(frame, *, service=None, on_ready=None):
    return TranscriptionDialog(frame, service=service).show(on_ready=on_ready)
