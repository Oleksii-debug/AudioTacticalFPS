"""Native local transcription workstation on the isolated, durable service.

The native selection/progress flow is adapted from the earlier transcription
dialog. Inference, runtime verification, checkpoints and exports belong to the
shared transcription service; this module never imports an inference engine.
wx is imported only when a window is created. Headless action tests are not
evidence of native Windows or screen-reader acceptance.
"""
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4
import json
import re

from ..speech.reading import ReadingDocument
from ..durable_state import local_path, regular


DEVICES = (('cpu', 'int8', 'CPU — int8'), ('cpu', 'float32', 'CPU — float32'),
           ('cuda', 'float16', 'CUDA GPU — float16'),
           ('cuda', 'int8_float16', 'CUDA GPU — int8 / float16'))
FORMATS = ('txt', 'srt', 'vtt', 'json')
MEDIA_FILTER = ('Audio or video|*.wav;*.mp3;*.m4a;*.m4b;*.flac;*.ogg;*.opus;*.aac;'
                '*.mp4;*.mkv;*.webm;*.wma;*.3gp;*.amr|All files|*.*')
HELP = (
    'Use an existing local Python runtime, the reviewed transcription worker, and a '
    'concrete local model folder. Inspect checks their identity without running them. '
    'Registration requires your explicit trust of those exact files. No model is '
    'downloaded, and no audio is uploaded. Registration does not establish recognition '
    'accuracy or language coverage.\n\n'
    'Choose files or a folder in New transcription. Folder recursion is optional. '
    'Auto asks the selected model to detect the spoken language. An explicit language '
    'code avoids detection; availability depends on that model. Translate speech means '
    'English output only. CPU and CUDA are explicit choices; CUDA needs a compatible '
    'installed runtime.\n\n'
    'Start creates a durable job before inference. Pause checkpoints and stops the '
    'worker; Resume uses verified saved work. Cancellation and pause wait for a safe '
    'boundary. Completed files remain available when a later file fails. Closing during '
    'work requests a checkpoint and waits. Resume never substitutes newly edited options '
    'for a saved job. After restoring a backup, explicitly inspect and register a runtime '
    'again, then use Rebind runtime on the saved job.\n\n'
    'Saved jobs contains source results. Load transcript opens a ReadingDocument copy. '
    'Save edits writes a separate derivative; Undo and Redo operate on the reading copy. '
    'Read aloud opens the existing local voice reader. Recognition and edited text are '
    'unverified derivatives, not authenticated quotations. Export writes configured '
    'formats into a new subfolder. Edited text is separate from timed original subtitles.\n\n'
    'Use Tab and Shift+Tab between native controls, Ctrl+Tab between pages, and the '
    'labelled buttons to operate the workspace. Escape closes after work is stopped. '
    'Progress does not move keyboard focus. Screen-reader modifier keys are untouched.'
)


class ProfileTranscriptionController:
    """One library binding, with service construction deferred to a worker."""

    def __init__(self, private_root, *, service=None):
        self.private_root = Path(private_root).absolute()
        self._service = service

    @property
    def service(self):
        if self._service is None:
            from ..operations import ResourceBroker
            from ..transcription import TranscriptionService
            self._service = TranscriptionService(self.private_root / 'state' / 'transcription',
                broker=ResourceBroker.for_app(self.private_root))
        return self._service

    def check_root(self, root):
        if Path(root).absolute() != self.private_root:
            raise ValueError('The library folder changed. Close and reopen transcription.')


class TranscriptionActions:
    """Testable native actions: immutable job choices and guarded callbacks."""

    def configure(self, frame, controller):
        self.frame, self.controller = frame, controller
        self.library = frame.library
        self.pending = self.closed = self.close_when_idle = False
        self.cancel_event = Event()
        self._progress_lock = Lock()
        self._latest_progress = None
        self._epoch = 0
        self._active_job_id = None
        self._stop_intent = None
        self._pending_control = None
        self._kind = ''
        self.profiles, self.job_rows, self.sources = (), (), ()
        self.current_job = self.document = self.transcript_source_id = None
        self.saved_text = ''
        self.inspection = None
        self._enable()

    def report(self, message, *, focus=False):
        if not self.closed:
            self.status.ChangeValue(str(message))
            if focus:
                self.status.SetFocus()

    def _check_root(self):
        if self.closed or getattr(self.frame, '_closing', False):
            raise ValueError('This transcription view closed before the action completed.')
        if self.frame.library is not self.library:
            raise ValueError('The library folder changed. Close and reopen transcription.')
        self.controller.check_root(self.frame.library.root)

    def _enable(self):
        if self.closed:
            return
        for control in self.mutable:
            control.Enable(not self.pending)
        editable = not self.pending and self.current_job is None
        for control in self.new_controls:
            control.Enable(editable)
        have_text = not self.pending and self.document is not None
        self.editor.Enable(have_text)
        for button in self.read_buttons:
            button.Enable(have_text)
        self.start_button.Enable(not self.pending and
            (self.current_job is None or self.current_job.get('status') != 'complete'))
        self.pause_button.Enable(self.pending and self._kind == 'transcribe')
        self.cancel_button.Enable(self.pending)
        changed = getattr(self, '_state_changed', None)
        if changed is not None:
            changed()

    def _usable(self):
        if self.closed or self.pending:
            return False
        try:
            self._check_root()
        except ValueError as exc:
            self.report(str(exc), focus=True)
            return False
        if getattr(self.frame, 'working', False) or getattr(self.frame, '_closing', False):
            self.report('Another operation is active. Finish or cancel it before starting this action.', focus=True)
            return False
        return True

    def _cancelled(self):
        # Called by the existing worker owner, never fsync a control on wx.
        with self._progress_lock:
            command, identity = self._pending_control, self._active_job_id
            if command is not None and identity is not None:
                self._pending_control = None
        if command is not None and identity is not None:
            service = self.controller.service
            (service.pause if command == 'pause' else service.cancel)(identity)
        return (self.closed or self.cancel_event.is_set() or self.frame.cancel_event.is_set()
                or getattr(self.frame, '_closing', False) or self.frame.library is not self.library)

    def submit(self, title, operation, complete, *, kind='metadata'):
        if not self._usable():
            return False
        self._epoch += 1
        epoch = self._epoch
        self.pending, self._kind = True, kind
        self._active_job_id, self._stop_intent = None, None
        self._pending_control = None
        self.cancel_event.clear()
        with self._progress_lock:
            self._latest_progress = None
        self._enable()
        self.report(title + '…')

        def current():
            if self.closed or epoch != self._epoch:
                return False
            try:
                self._check_root()
            except ValueError as exc:
                self.report(str(exc), focus=True)
                return False
            return True

        def work():
            self._check_root()
            # The host binds the current operation in its worker context.
            # Obtain it here, after run_job starts, rather than retaining an
            # obsolete frame-level progress object across operation lifetimes.
            from ..progress import current_progress
            progress = current_progress()

            def update(event):
                if self.closed or epoch != self._epoch or not isinstance(event, dict):
                    return
                phase = str(event.get('phase') or event.get('status') or 'Transcribing')
                phase = ' '.join(phase.split())[:200] or 'Transcribing'
                done, total = event.get('done'), event.get('total')
                measured = (type(done) is int and type(total) is int and 0 <= done <= total and total > 0
                            and not event.get('indeterminate', False))
                message = phase + (f': {done} of {total} files' if measured else ' — working; total not yet known')
                if progress is not None:
                    # Runtime hashing, source retention and recognition use
                    # different units. Keep the overall task indeterminate;
                    # expose the current measured phase in its accessible
                    # message without inventing a combined percentage.
                    try:
                        # Cancellation can change the observer state while
                        # the worker is still publishing its final checkpoint.
                        # Observation must not reverse a durable job result.
                        if not progress.cancellation_requested:
                            progress.update(total=None, phase=phase, message=message)
                    except Exception as exc:
                        message += '\nGlobal progress reporting unavailable: ' + str(exc)[:400]
                if event.get('current'):
                    message += '\n' + str(event['current'])[:1000]
                with self._progress_lock:
                    self._latest_progress = (epoch, message)

            return operation(self.controller.service, self._cancelled, update)

        def finished(result):
            if epoch != self._epoch or self.closed:
                return
            self.pending = False
            with self._progress_lock:
                self._latest_progress = None
            self._enable()
            if current():
                complete(result)
            if not self.closed and self.close_when_idle and not self.pending:
                self.close()

        def failed(error):
            if epoch != self._epoch or self.closed:
                return
            self.pending = False
            with self._progress_lock:
                self._latest_progress = None
            self._enable()
            if current():
                message = ('Operation stopped before completion. ' if isinstance(error, InterruptedError)
                           else 'Operation did not complete. ')
                self.report(message + str(error) + '\nRefresh saved jobs to inspect retained work.', focus=True)
            if self.close_when_idle:
                self.close()

        try:
            accepted = self.frame.run_job(title, work, done=finished, failed=failed)
        except Exception as exc:
            failed(exc)
            return False
        if accepted is False:
            failed(RuntimeError('The host did not start this operation.'))
            return False
        return True

    def flush_progress(self, event=None):
        with self._progress_lock:
            update, self._latest_progress = self._latest_progress, None
        if not self.closed and self.pending and update is not None and update[0] == self._epoch:
            self.report(update[1])

    def refresh(self, event=None):
        def loaded(value):
            self.profiles, self.job_rows = tuple(value['profiles']), tuple(value['jobs'])
            self.runtime.SetItems([f"{row['name']} — {row.get('model_name', 'local model')} — "
                f"{'trusted' if row.get('trusted') else 'not trusted'}; {row.get('qualification', 'unqualified')}"
                for row in self.profiles])
            if self.profiles:
                self.runtime.SetSelection(0)
            self._render_jobs()
            self.report(f'{len(self.profiles)} registered runtimes; {len(self.job_rows)} saved jobs. '
                        'Select local media to start, or load a saved job. No inference starts automatically.')
        self.submit('Loading local transcription profiles and jobs',
            lambda service, cancel, progress: {'profiles': service.list_runtimes(),
                'jobs': getattr(service, 'list_profile_jobs', service.list_jobs)()}, loaded)

    def _render_jobs(self):
        self.jobs.SetItems([f"{row.get('title', 'Transcription')} — {row['status']} — "
            f"{row.get('done', 0)}/{row.get('total', len(row.get('sources', [])))} files — {row['id']}"
            for row in self.job_rows])
        identity = self.current_job['id'] if self.current_job else None
        index = next((i for i, row in enumerate(self.job_rows) if row['id'] == identity),
                     0 if self.job_rows else -1)
        self.jobs.SetSelection(index)

    def _selected_profile(self):
        index = self.runtime.GetSelection()
        if not 0 <= index < len(self.profiles):
            raise ValueError('Register and choose an existing local runtime first.')
        profile = self.profiles[index]
        if profile.get('trusted') is not True:
            raise ValueError('Inspect and explicitly trust this runtime before starting it.')
        return profile

    def _settings(self):
        language = self.language.GetValue().strip().lower()
        if not re.fullmatch(r'auto|[a-z]{2,3}', language):
            raise ValueError('Enter auto or a language code such as uk or en.')
        selection = self.task.GetSelection()
        if selection not in (0, 1):
            raise ValueError('Choose transcription or translation to English.')
        index = self.device.GetSelection()
        if not 0 <= index < len(DEVICES):
            raise ValueError('Choose CPU or CUDA and its compute type.')
        device, compute, _ = DEVICES[index]
        formats = {name: bool(control.GetValue()) for name, control in self.formats.items()}
        if not any(formats.values()):
            raise ValueError('Select at least one output format.')
        return dict(language=language, task=('transcribe', 'translate')[selection], device=device,
            compute_type=compute, beam_size=self.beam.GetValue(), parallel_files=self.parallel.GetValue(),
            vad_filter=bool(self.vad.GetValue()), condition_on_previous_text=bool(self.context.GetValue()),
            multilingual=bool(self.multilingual.GetValue()), include_headers=bool(self.headers.GetValue()),
            output_mode='separate', **{'output_' + key: value for key, value in formats.items()})

    def start(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        try:
            if self.current_job is None:
                profile = self._selected_profile()
                sources, settings = tuple(self.sources), self._settings()
                title, recursive = self.title.GetValue().strip() or 'Transcription', bool(self.recursive.GetValue())
                if not sources:
                    raise ValueError('Choose at least one local audio file or folder.')
                def run(service, cancel, progress):
                    job = service.create_job(profile['id'], sources, settings=settings,
                        title=title, recursive=recursive, cancel=cancel, progress=progress)
                    self._active_job_id = job['id']
                    if cancel():
                        (service.pause if self._stop_intent == 'paused' else service.cancel)(job['id'])
                        return service.get_job(job['id'])
                    return service.run(job['id'], cancel=cancel, progress=progress)
            else:
                identity = self.current_job['id']
                if self.current_job['status'] == 'complete':
                    raise ValueError('This job is complete. Choose New job for another transcription.')
                def run(service, cancel, progress):
                    self._active_job_id = identity
                    if cancel():
                        (service.pause if self._stop_intent == 'paused' else service.cancel)(identity)
                        return service.get_job(identity)
                    return service.resume(identity, cancel=cancel, progress=progress)
            self.submit('Transcribing local audio', run, self.publish_job, kind='transcribe')
        except Exception as exc:
            self.report(str(exc), focus=True)

    def publish_job(self, job):
        self.current_job = job
        self.job_rows = (job,) + tuple(row for row in self.job_rows if row['id'] != job['id'])
        self._render_jobs()
        self.result_source.SetItems([row.get('name', Path(row.get('original_path', '')).name) + ' — ' + row['id']
                                    for row in job.get('sources', ())])
        if job.get('sources'):
            self.result_source.SetSelection(0)
        self.document = self.transcript_source_id = None
        self.saved_text = ''
        self.editor.ChangeValue('')
        self.provenance.ChangeValue('Load a completed source result. Recognition text is an unverified derivative.')
        summary = (f"{job.get('title', 'Transcription')}: {job['status']}; "
                   f"{job.get('done', 0)} of {job.get('total', len(job.get('sources', [])))} files.\n"
                   f"Saved job: {job['id']}. Choose a source result and Load transcript.")
        settings = job.get('settings', {})
        summary += (f"\nSaved options: {settings.get('language', 'auto')}; {settings.get('task', 'transcribe')}; "
                    f"{settings.get('device', 'cpu')}/{settings.get('compute_type', 'int8')}. "
                    'Resume uses these saved options.')
        if job.get('error'):
            summary += '\n' + str(job['error'])
        self.report(summary)
        self._enable()

    def load_job(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        index = self.jobs.GetSelection()
        if not 0 <= index < len(self.job_rows):
            self.report('Choose a saved job.', focus=True)
            return
        identity = self.job_rows[index]['id']
        self.submit('Opening saved transcription job',
                    lambda service, cancel, progress: service.get_job(identity), self.publish_job)

    def new_job(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        self.current_job = self.document = self.transcript_source_id = None
        self.saved_text = ''
        self.editor.ChangeValue('')
        self.result_source.SetItems([])
        self.provenance.ChangeValue('No transcript is loaded.')
        self._enable()
        self.report('New job. Select a registered runtime, local files and options.')

    def request_stop(self, *, pause=False):
        if not self.pending or self.closed:
            return
        self._stop_intent = 'paused' if pause else 'cancelled'
        with self._progress_lock:
            if self._kind == 'transcribe':
                self._pending_control = 'pause' if pause else 'cancel'
        self.cancel_event.set()
        self.frame.cancel_event.set()
        self.report(('Pause' if pause else 'Cancellation') + ' requested. Waiting for a safe checkpoint; '
                    'completed files remain saved. Preparation may stop before a new job is created.')

    def pause(self, event=None):
        self.request_stop(pause=True)

    def cancel(self, event=None):
        self.request_stop()

    def close(self, event=None):
        if self.closed:
            return
        if self.pending:
            self.close_when_idle = True
            self.request_stop(pause=self._kind == 'transcribe')
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        if not self.confirm_discard():
            self.close_when_idle = False
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        self.closed = True
        self.finish_close()

    def _paths(self):
        return tuple(control.GetValue().strip() for control in
                     (self.python_path, self.worker_path, self.model_path))

    def runtime_changed(self, event=None):
        if self.closed:
            return
        self.inspection = None
        self.trust.SetValue(False)
        self.runtime_details.ChangeValue('Inspect these exact files before trusting and registering this runtime.')
        if event is not None:
            event.Skip()

    def inspect_runtime(self, event=None):
        if not self._usable():
            return
        paths = self._paths()
        self.inspection = None
        self.trust.SetValue(False)
        def inspected(result):
            if paths != self._paths():
                self.runtime_changed()
                return
            self.inspection = (paths, result)
            summary = {key: result[key] for key in ('python_executable', 'python_resolved',
                'python_identity', 'worker_path', 'worker_identity', 'model_directory', 'model_name',
                'model_bytes', 'model_content_fingerprint', 'fingerprint') if key in result}
            summary['model_file_count'] = len(result.get('model_files', {}))
            self.runtime_details.ChangeValue(json.dumps(summary, ensure_ascii=False, indent=2))
            self.report('Files inspected without execution. Review their origin and licence, then explicitly '
                        'trust and register them. Recognition quality remains unqualified.')
        self.submit('Inspecting existing local transcription files',
            lambda service, cancel, progress: service.inspect_runtime(*paths, cancel=cancel, progress=progress), inspected)

    def register_runtime(self, event=None):
        if not self._usable():
            return
        paths = self._paths()
        if self.inspection is None or self.inspection[0] != paths or self.trust.GetValue() is not True:
            self.report('Inspect these exact files, then check the explicit trust confirmation.', focus=True)
            return
        name, fingerprint = self.runtime_name.GetValue().strip(), self.inspection[1]['fingerprint']
        def registered(profile):
            self.profiles = tuple(row for row in self.profiles if row['id'] != profile['id']) + (profile,)
            self.runtime.SetItems([f"{row['name']} — {row.get('model_name', 'local model')} — "
                f"{'trusted' if row.get('trusted') else 'not trusted'}; {row.get('qualification', 'unqualified')}"
                for row in self.profiles])
            self.runtime.SetSelection(len(self.profiles) - 1)
            self.report('Runtime registered using the verified local files. No inference has run. '
                        'Choose media in New transcription to start.')
        self.submit('Registering the reviewed local runtime',
            lambda service, cancel, progress: service.register_runtime(name, *paths, owner_trusted=True,
                expected_fingerprint=fingerprint, cancel=cancel, progress=progress), registered)

    def rebind_runtime(self, event=None):
        if not self._usable() or not self.current_job or not self.confirm_discard():
            return
        try:
            profile, identity = self._selected_profile(), self.current_job['id']
            if not self.confirm_rebind(profile):
                return
            self.submit('Binding reviewed runtime to saved job',
                lambda service, cancel, progress: service.rebind_runtime(identity, profile['id']), self.publish_job)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def review_interruption(self, event=None):
        if not self._usable() or not self.current_job or not self.confirm_discard():
            return
        if (self.current_job.get('status') == 'complete'
                or not self.current_job.get('recovery_review_required')):
            self.report('This job does not require an interrupted-worker review.')
            return
        if not self.confirm_worker_stopped():
            return
        identity = self.current_job['id']
        self.submit('Recording interrupted worker review',
            lambda service, cancel, progress: service.acknowledge_interruption(identity, worker_stopped=True),
            self.publish_job)

    def load_transcript(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        index = self.result_source.GetSelection()
        sources = self.current_job.get('sources', ()) if self.current_job else ()
        if not 0 <= index < len(sources):
            self.report('Load a saved job and choose a completed source result.', focus=True)
            return
        identity, source = self.current_job['id'], sources[index]['id']
        edited = bool(self.latest_edit.GetValue())
        def read(service, cancel, progress):
            original = service.read_transcript(identity, source, cancel=cancel)
            latest = original
            if edited:
                try:
                    latest = service.read_transcript(identity, source, edited=True, cancel=cancel)
                except FileNotFoundError:
                    pass
            return {'original': original, 'latest': latest}
        def loaded(value):
            original, latest = value['original'], value['latest']
            document = ReadingDocument(original['text'], title='Transcription reading copy')
            document.set_text(latest['text'], label='Loaded saved transcript derivative')
            self.document, self.transcript_source_id = document, source
            self.saved_text = latest['text']
            self.editor.ChangeValue(document.text)
            self.provenance.ChangeValue('Unverified recognition derivative. Edits never overwrite the original.\n' +
                json.dumps(latest.get('provenance', {}), ensure_ascii=False, indent=2))
            self.report('Transcript loaded. Edit the reading copy or open the local voice reader.')
            self._enable()
            self.editor.SetFocus()
        self.submit('Loading transcript reading copy', read, loaded)

    def is_dirty(self):
        return not self.closed and self.document is not None and self.editor.GetValue() != self.saved_text

    def _capture_edit(self):
        if self.document is None:
            raise ValueError('Load a transcript first.')
        self.document.set_text(self.editor.GetValue(), label='Edit transcript reading copy')
        return self.document.text

    def save_edits(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            text = self._capture_edit()
            identity, source = self.current_job['id'], self.transcript_source_id
            def saved(receipt):
                self.saved_text = text
                self.provenance.ChangeValue('Saved separate edited derivative. Original recognition text unchanged.\n' +
                    json.dumps(receipt.get('provenance', {}), ensure_ascii=False, indent=2))
                self.report('Edited reading copy saved as a separate derivative.')
            self.submit('Saving transcript derivative',
                lambda service, cancel, progress: service.save_edit(identity, source, text), saved)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def undo(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            self._capture_edit()
            self.document.undo()
            self.editor.ChangeValue(self.document.text)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def redo(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            self._capture_edit()
            self.document.redo()
            self.editor.ChangeValue(self.document.text)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def export_to(self, directory):
        if not self._usable() or self.current_job is None:
            return
        if self.is_dirty():
            self.report('Save the edited reading copy before exporting, or undo the unsaved edit.', focus=True)
            return
        identity = self.current_job['id']
        settings = self.current_job['settings']
        formats = tuple(name for name in FORMATS if settings.get('output_' + name))
        library_root = self.controller.private_root
        def export(service, cancel, progress):
            if cancel():
                raise InterruptedError('Export cancelled before publication.')
            destination = local_path(directory)
            root = local_path(library_root, exists=True)
            regular(destination.parent, directory=True)
            if destination.is_relative_to(root) or root.is_relative_to(destination):
                raise ValueError('Choose an export folder outside the private library.')
            destination.mkdir()  # exclusive new folder; never reuse an existing export
            return service.export(identity, destination, formats=formats, include_edits=True, cancel=cancel)
        self.submit('Exporting verified transcription results',
            export,
            lambda receipt: self.report('Export saved.\n' + json.dumps(receipt, ensure_ascii=False, indent=2)))


def build_profile_page(parent, frame, *, controller=None, close_window=lambda: None,
                       state_changed=lambda: None):
    """Create native controls; callers own ShowModal/Destroy and may inject a service."""
    import wx
    controller = controller or ProfileTranscriptionController(frame.library.root)

    class Dialog(wx.Panel, TranscriptionActions):
        def __init__(self):
            wx.Panel.__init__(self, parent)
            self._native_id = self.GetId()
            self._destroy_requested = False
            self._state_changed = state_changed
            self.SetName('Local transcription workstation')
            self.mutable, self.new_controls, self.read_buttons = [], [], []
            layout = wx.BoxSizer(wx.VERTICAL)
            note = wx.StaticText(self, label='Local media and installed models. No uploads or model downloads. '
                'Recognition text and edits are separate, unverified derivatives.')
            note.Wrap(900)
            layout.Add(note, 0, wx.ALL | wx.EXPAND, 8)
            self.pages = wx.Notebook(self)
            self.pages.SetName('Transcription workstation pages')
            new = wx.ScrolledWindow(self.pages)
            new.SetScrollRate(0, 16)
            runtimes = wx.ScrolledWindow(self.pages)
            runtimes.SetScrollRate(0, 16)
            results = wx.ScrolledWindow(self.pages)
            results.SetScrollRate(0, 16)
            # The embedded page shares its window with the manifest controls
            # and shared progress. Keep page content scrollable at the dialog's
            # minimum size instead of imposing the old standalone height.
            for page in (new, runtimes, results):
                page.SetMinSize((560, 220))
            self.pages.AddPage(new, 'New transcription')
            self.pages.AddPage(runtimes, 'Local runtime')
            self.pages.AddPage(results, 'Saved jobs and results')
            layout.Add(self.pages, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

            def field(parent, box, label, factory=wx.TextCtrl, *, proportion=0, new_option=False, **kwargs):
                box.Add(wx.StaticText(parent, label=label), 0, wx.TOP | wx.LEFT | wx.RIGHT, 5)
                control = factory(parent, **kwargs)
                control.SetName(label.replace('&', ''))
                box.Add(control, proportion, wx.EXPAND | wx.ALL, 5)
                self.mutable.append(control)
                if new_option:
                    self.new_controls.append(control)
                return control

            def button(parent, row, label, action, *, new_option=False, reading=False):
                control = wx.Button(parent, label=label)
                control.SetName(label.replace('&', ''))
                control.Bind(wx.EVT_BUTTON, action)
                row.Add(control, 0, wx.ALL, 4)
                self.mutable.append(control)
                if new_option:
                    self.new_controls.append(control)
                if reading:
                    self.read_buttons.append(control)
                return control

            box = wx.BoxSizer(wx.VERTICAL)
            self.runtime = field(new, box, 'Registered transcription &runtime', wx.Choice)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(new, row, 'Manage local runtime', lambda event: self.pages.SetSelection(1))
            button(new, row, 'Refresh profiles and jobs', self.refresh)
            box.Add(row)
            self.title = field(new, box, 'Job &title', value='Transcription', new_option=True)
            self.source_paths = field(new, box, 'Selected local audio or video',
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80), new_option=True)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(new, row, 'Add &files…', self.choose_files, new_option=True)
            button(new, row, 'Add f&older…', self.choose_folder, new_option=True)
            button(new, row, 'Clear files', self.clear_sources, new_option=True)
            box.Add(row)
            self.recursive = wx.CheckBox(new, label='Include subfolders of selected folders')
            self.recursive.SetName('Include subfolders of selected folders')
            box.Add(self.recursive, 0, wx.ALL, 5)
            self.mutable.append(self.recursive)
            self.new_controls.append(self.recursive)
            self.language = field(new, box, 'Source lan&guage code or auto', value='auto', new_option=True)
            self.task = field(new, box, 'Transcription task', wx.Choice, new_option=True,
                choices=['Transcribe in the source language', 'Translate speech to English'])
            self.task.SetSelection(0)
            self.device = field(new, box, 'Processing &device and compute type', wx.Choice,
                choices=[row[2] for row in DEVICES], new_option=True)
            self.device.SetSelection(0)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(new, label='Beam size'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.beam = wx.SpinCtrl(new, min=1, max=20, initial=5)
            self.beam.SetName('Beam size, one to twenty')
            row.Add(self.beam, 0, wx.ALL, 5)
            row.Add(wx.StaticText(new, label='Parallel files'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.parallel = wx.SpinCtrl(new, min=1, max=4, initial=1)
            self.parallel.SetName('Parallel files, one to four')
            row.Add(self.parallel, 0, wx.ALL, 5)
            box.Add(row)
            self.mutable.extend((self.beam, self.parallel))
            self.new_controls.extend((self.beam, self.parallel))
            row = wx.WrapSizer(wx.HORIZONTAL)
            for attr, label, default in (('vad', 'Voice activity detection', True),
                    ('context', 'Use previous text as context', True),
                    ('multilingual', 'Allow multilingual segments', False),
                    ('headers', 'Include text headers', False)):
                control = wx.CheckBox(new, label=label)
                control.SetName(label)
                control.SetValue(default)
                setattr(self, attr, control)
                row.Add(control, 0, wx.ALL, 5)
                self.mutable.append(control)
                self.new_controls.append(control)
            box.Add(row, 0, wx.EXPAND)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(new, label='Output formats'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.formats = {}
            for name in FORMATS:
                control = wx.CheckBox(new, label=name.upper())
                control.SetName('Output ' + name.upper())
                control.SetValue(name in ('txt', 'json'))
                row.Add(control, 0, wx.ALL, 5)
                self.formats[name] = control
                self.mutable.append(control)
                self.new_controls.append(control)
            box.Add(row)
            new.SetSizer(box)

            box = wx.BoxSizer(wx.VERTICAL)
            text = wx.StaticText(runtimes, label='Point to existing, reviewed files. Inspection does not execute them. '
                'Registration records their exact identity and your trust; quality remains unqualified.')
            text.Wrap(880)
            box.Add(text, 0, wx.ALL | wx.EXPAND, 6)
            self.runtime_name = field(runtimes, box, 'Runtime profile name', value='My local Whisper runtime')
            for attr, label, directory in (('python_path', 'Python executable', False),
                    ('worker_path', 'Reviewed transcription worker', False),
                    ('model_path', 'Existing local model directory', True)):
                control = field(runtimes, box, label)
                setattr(self, attr, control)
                row = wx.BoxSizer(wx.HORIZONTAL)
                button(runtimes, row, 'Browse ' + label.lower() + '…',
                    lambda event, target=control, is_directory=directory: self.choose_runtime_path(target, is_directory))
                box.Add(row)
            self.runtime_details = field(runtimes, box, 'Runtime inspection and fingerprint',
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 130))
            self.trust = wx.CheckBox(runtimes, label='I trust these inspected runtime, worker and model files and have reviewed their licence')
            self.trust.SetName('Explicit trust of inspected runtime, worker and model files')
            box.Add(self.trust, 0, wx.ALL, 5)
            self.mutable.append(self.trust)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(runtimes, row, '&Inspect files', self.inspect_runtime)
            button(runtimes, row, '&Register trusted runtime', self.register_runtime)
            box.Add(row)
            runtimes.SetSizer(box)

            box = wx.BoxSizer(wx.VERTICAL)
            self.jobs = field(results, box, 'Saved transcription &jobs', wx.Choice)
            row = wx.WrapSizer(wx.HORIZONTAL)
            button(results, row, 'Load selected job', self.load_job)
            button(results, row, 'Refresh saved jobs', self.refresh)
            button(results, row, 'New job', self.new_job)
            button(results, row, 'Rebind runtime…', self.rebind_runtime)
            button(results, row, 'Review interrupted worker…', self.review_interruption)
            box.Add(row, 0, wx.EXPAND)
            self.result_source = field(results, box, 'Transcription source &result', wx.Choice)
            self.latest_edit = wx.CheckBox(results, label='Load the latest saved edit when available')
            self.latest_edit.SetName('Load the latest saved edit when available')
            self.latest_edit.SetValue(True)
            self.mutable.append(self.latest_edit)
            box.Add(self.latest_edit, 0, wx.ALL, 5)
            row = wx.BoxSizer(wx.HORIZONTAL)
            button(results, row, 'L&oad transcript', self.load_transcript)
            button(results, row, '&Export results…', self.choose_export)
            box.Add(row)
            self.provenance = field(results, box, 'Transcript provenance', value='No transcript is loaded.',
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
            self.editor = field(results, box, 'Transcript reading cop&y', proportion=1,
                style=wx.TE_MULTILINE | wx.TE_DONTWRAP)
            row = wx.WrapSizer(wx.HORIZONTAL)
            button(results, row, 'Sa&ve edits', self.save_edits, reading=True)
            button(results, row, '&Undo reading edit', self.undo, reading=True)
            button(results, row, 'Redo reading edit', self.redo, reading=True)
            button(results, row, 'Read &aloud…', self.read_aloud, reading=True)
            box.Add(row, 0, wx.EXPAND)
            results.SetSizer(box)

            layout.Add(wx.StaticText(self, label='Transcription progress and result'), 0, wx.LEFT | wx.TOP, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
            self.status.SetName('Transcription progress and result')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            row = wx.WrapSizer(wx.HORIZONTAL)
            self.start_button = wx.Button(self, label='&Start / resume')
            self.pause_button = wx.Button(self, label='&Pause at checkpoint')
            self.cancel_button = wx.Button(self, label='&Cancel operation')
            self.help_button = wx.Button(self, label='&Help')
            self.close_button = wx.Button(self, wx.ID_CLOSE, 'C&lose')
            for control, action in ((self.start_button, self.start), (self.pause_button, self.pause),
                    (self.cancel_button, self.cancel), (self.help_button, self.help), (self.close_button, self.close)):
                control.SetName(control.GetLabel().replace('&', ''))
                control.Bind(wx.EVT_BUTTON, action)
                row.Add(control, 0, wx.ALL, 5)
            layout.Add(row, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((700, 460))
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.configure(frame, controller)
            for control in (self.python_path, self.worker_path, self.model_path):
                control.Bind(wx.EVT_TEXT, self.runtime_changed)
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.flush_progress, self.timer)
            self.timer.Start(250)
            self.runtime.SetFocus()

        def dispose(self):
            # Native parent destruction may precede Python wrapper invalidation.
            # Only Python state is touched; another host task is not cancelled.
            self.closed = True
            self._epoch += 1
            self.cancel_event.set()
            with self._progress_lock:
                self._latest_progress = None
                if self._kind == 'transcribe':
                    self._pending_control = 'cancel'

        def Destroy(self):
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            if hasattr(self, 'cancel_event'):
                self.dispose()
            return super().Destroy()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.dispose()
            event.Skip()

        def ask(self, message, title):
            if self.closed:
                return False
            picker = wx.MessageDialog(self, message, title,
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
            try:
                result = picker.ShowModal()
                if self.closed or not self or not picker:
                    return False
                self._check_root()
                return result == wx.ID_YES
            finally:
                if picker:
                    picker.Destroy()

        def confirm_discard(self):
            return not self.closed and (not self.is_dirty() or self.ask(
                'Discard unsaved reading-copy edits? Original and saved derivatives remain available.',
                'Unsaved transcript edits'))

        def confirm_rebind(self, profile):
            return self.ask('Bind the saved job to the reviewed runtime: ' + profile['name'] +
                '? Model compatibility is checked before resuming.', 'Rebind saved job')

        def confirm_worker_stopped(self):
            return self.ask('Confirm the previous external transcription worker has stopped. '
                'Check its process before allowing another worker for this job.', 'Review interrupted worker')

        def finish_close(self):
            # Normal close is still on the live wx thread; parent destruction
            # uses pure dispose and leaves timer ownership to native deletion.
            if self:
                self.timer.Stop()
            close_window()

        def _show_sources(self):
            self.source_paths.ChangeValue('\n'.join(str(path) for path in self.sources))

        def selected_paths(self, picker, *, multiple=False):
            try:
                result = picker.ShowModal()
                if result != wx.ID_OK or self.closed or not self or not picker:
                    return None
                self._check_root()
                return tuple(picker.GetPaths()) if multiple else (picker.GetPath(),)
            finally:
                if picker:
                    picker.Destroy()

        def choose_files(self, event=None):
            if not self._usable() or self.current_job is not None:
                return
            paths = self.selected_paths(wx.FileDialog(self, 'Choose local audio or video files',
                wildcard=MEDIA_FILTER, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE), multiple=True)
            if paths is not None and not self.closed:
                self.sources = tuple(dict.fromkeys((*self.sources, *(Path(path) for path in paths))))
                self._show_sources()

        def choose_folder(self, event=None):
            if not self._usable() or self.current_job is not None:
                return
            paths = self.selected_paths(wx.DirDialog(self, 'Choose a folder containing local audio or video',
                style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST))
            if paths is not None and not self.closed:
                self.sources = tuple(dict.fromkeys((*self.sources, Path(paths[0]))))
                self._show_sources()

        def clear_sources(self, event=None):
            if self._usable() and self.current_job is None:
                self.sources = ()
                self._show_sources()

        def choose_runtime_path(self, control, directory):
            if not self._usable():
                return
            picker = (wx.DirDialog(self, 'Choose the existing local model directory',
                style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) if directory else
                wx.FileDialog(self, 'Choose the reviewed local executable or worker file',
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST))
            paths = self.selected_paths(picker)
            if paths is not None and not self.closed:
                control.SetValue(paths[0])

        def choose_export(self, event=None):
            if not self._usable() or self.current_job is None:
                self.report('Load a saved job before exporting.', focus=True)
                return
            paths = self.selected_paths(wx.DirDialog(self, 'Choose where to create a new transcription export folder',
                style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST))
            if paths is not None and not self.closed:
                self.export_to(Path(paths[0]) / ('transcription-' + uuid4().hex[:12]))

        def read_aloud(self, event=None):
            if not self._usable() or self.document is None:
                return
            identity, source = self.current_job['id'], self.transcript_source_id
            text = self._capture_edit()
            from ..transcription.reading import prepare_reading_copy
            def prepared(value):
                if self.closed:
                    return
                self._check_root()
                from .speech_dialog import show_speech_reader
                show_speech_reader(self.frame, text='', title='Unverified transcription reading copy',
                    source_guard=self._check_root, recovery_slot=value['slot'])
            self.submit('Preparing the separate transcript reading copy',
                lambda service, cancel, progress: prepare_reading_copy(self.library.root, service,
                    identity, source, edited_text=text, cancelled=cancel), prepared)

        def help(self, event=None):
            if self.closed:
                return
            dialog = wx.Dialog(self, title='Transcription help', size=(750, 620),
                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            try:
                box = wx.BoxSizer(wx.VERTICAL)
                text = wx.TextCtrl(dialog, value=HELP, style=wx.TE_MULTILINE | wx.TE_READONLY)
                text.SetName('Local transcription help')
                box.Add(text, 1, wx.EXPAND | wx.ALL, 10)
                box.Add(dialog.CreateButtonSizer(wx.OK), 0, wx.ALIGN_RIGHT | wx.ALL, 8)
                dialog.SetSizer(box)
                text.SetFocus()
                dialog.ShowModal()
            finally:
                if dialog:
                    dialog.Destroy()

    return Dialog()

