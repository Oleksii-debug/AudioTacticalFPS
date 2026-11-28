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
import os
import re

from ..speech.reading import ReadingDocument
from ..durable_state import local_path, regular
from ..transcription import PROTOCOLS, RECEIPTS_PROTOCOL, V5_PROTOCOL, settings as validate_settings


DEVICES = (('cpu', 'int8', 'CPU — int8'), ('cpu', 'float32', 'CPU — float32'),
           ('cuda', 'float16', 'CUDA GPU — float16'),
           ('cuda', 'int8_float16', 'CUDA GPU — int8 / float16'))
FORMATS = ('txt', 'srt', 'vtt', 'json')
MEDIA_FILTER = ('Audio or video|*.wav;*.mp3;*.m4a;*.m4b;*.flac;*.ogg;*.opus;*.aac;'
                '*.mp4;*.mkv;*.webm;*.wma;*.aiff;*.aif;*.mov;*.avi|All files|*.*')
HELP = (
    'Use an existing local Python runtime, the reviewed transcription worker, and a '
    'concrete local model folder. Inspect checks their identity without running them. '
    'Registration requires your explicit trust of those exact files. No model is '
    'downloaded, and no audio is uploaded. Registration does not establish recognition '
    'accuracy or language coverage.\n\n'
    'Choose files or a folder in New transcription. Folder recursion is optional. '
    'Auto asks the selected model to detect the spoken language. An explicit language '
    'code avoids detection; availability depends on that model. These admitted workers '
    'retain the source language; speech translation is unsupported. CPU and CUDA are explicit choices; CUDA needs a compatible '
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


class TranscriptionController:
    """Bind one exact library/workspace; construct its existing service on the worker."""

    def __init__(self, app, workspace, *, service=None):
        self.app, self.workspace, self._service = app, workspace, service
        self.private_root = local_path(app.root, exists=True)
        info = regular(self.private_root, directory=True)
        self._root_identity = info.st_dev, info.st_ino

    @property
    def service(self):
        if self._service is None:
            self._service = self.app.transcription(self.workspace)
        return self._service

    def check_root(self, root):
        current = local_path(root, exists=True)
        info = regular(current, directory=True)
        if current != self.private_root or (info.st_dev, info.st_ino) != self._root_identity:
            raise ValueError('The library folder changed. Close and reopen transcription.')

    def check_frame(self, frame):
        if frame.library is not self.app:
            raise ValueError('The library folder changed. Reopen transcription.')
        active = getattr(frame, 'active', None)
        if not active or active[0] != self.workspace:
            raise ValueError('The active workspace changed. Reopen transcription.')
        self.check_root(frame.library.root)
        with self.app.read(self.workspace):
            pass


class TranscriptionActions:
    """Testable native actions: immutable job choices and guarded callbacks."""

    def configure(self, frame, controller):
        self.frame, self.controller = frame, controller
        self.library = frame.library
        self.progress_panel = getattr(frame, 'progress_panel', None)
        self.desktop_integration = getattr(frame, 'desktop_integration', None)
        self.pending = self.closed = self.close_when_idle = False
        self.cancel_event = Event()
        self._progress_lock = Lock()
        self._latest_progress = None
        self._epoch = 0
        self._active_job_id = None
        self._stop_intent = None
        self._kind = ''
        self.profiles, self.job_rows, self.sources = (), (), ()
        self.current_job = self.document = self.transcript_source_id = None
        self.saved_text = ''
        self.inspection = None
        self._unregister_save = lambda: None
        from .save_all import coordinator_for
        coordinator = coordinator_for(frame)
        if coordinator is not None:
            self._unregister_save = coordinator.register(
                'transcription-' + uuid4().hex, 'Transcription reading copy',
                dirty=lambda: not self.closed and self.is_dirty(),
                save=lambda completed: self.save_edits(completed=completed),
                focus=lambda: self.editor.SetFocus() if not self.closed else None)
        self._enable()

    def report(self, message, *, focus=False):
        if not self.closed:
            self.status.ChangeValue(str(message))
            if focus:
                self.status.SetFocus()

    def _check_root(self):
        if self.closed or getattr(self.frame, '_closing', False):
            raise ValueError('This transcription workspace has closed.')
        self.controller.check_frame(self.frame)

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
        self.save_edits_button.Enable(have_text and self.is_dirty())
        self.start_button.Enable(not self.pending and
            (self.current_job is None or self.current_job.get('status') != 'completed'))
        saved_active = bool(self.current_job and self.current_job.get('status') != 'completed')
        self.pause_button.Enable(self.pending and self._kind == 'transcribe' or
                                 not self.pending and saved_active)
        self.cancel_button.Enable(self.pending or saved_active)
        self.task.Enable(False)  # The admitted worker protocols support transcription only.
        index = self.runtime.GetSelection()
        protocol = (self.profiles[index].get('configuration', {}).get('protocol')
                    if 0 <= index < len(self.profiles) else None)
        self.headers.Enable(editable and protocol != V5_PROTOCOL)
        if editable and protocol == V5_PROTOCOL:
            self.headers.SetValue(False)

    def _usable(self):
        if self.closed or self.pending:
            return False
        try:
            self._check_root()
        except (ValueError, OSError) as exc:
            self.report(str(exc), focus=True)
            return False
        if getattr(self.frame, 'working', False) or getattr(self.frame, '_closing', False):
            self.report('Another operation is active. Finish or cancel it before starting this action.', focus=True)
            return False
        return True

    def _cancelled(self):
        if self.closed or self.cancel_event.is_set() or self.frame.cancel_event.is_set():
            return True
        try:
            self._check_root()
        except (ValueError, OSError, RuntimeError):
            return True
        return False

    def submit(self, title, operation, complete, *, kind='metadata', settled=None):
        if not self._usable():
            return False
        self._epoch += 1
        epoch = self._epoch
        self.pending, self._kind = True, kind
        self._active_job_id, self._stop_intent = None, None
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
            except (ValueError, OSError) as exc:
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

            def update(*values):
                event = (values[0] if len(values) == 1 and isinstance(values[0], dict) else
                         {'done': values[0], 'total': values[1], 'phase': values[2]}
                         if len(values) == 3 else None)
                if self.closed or epoch != self._epoch or event is None:
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

        settlement_sent = False
        def settle(result=None, error=None):
            nonlocal settlement_sent
            if not settlement_sent:
                settlement_sent = True
                if settled is not None:
                    settled(result, error)

        def finished(result):
            if epoch != self._epoch or self.closed:
                settle(error=RuntimeError('The transcription view closed before save confirmation.'))
                return
            self.pending = False
            with self._progress_lock:
                self._latest_progress = None
            self._enable()
            try:
                if current():
                    complete(result)
                else:
                    settle(error=RuntimeError('The library or workspace changed before save confirmation.'))
                    return
            except Exception as exc:
                settle(error=exc)
                raise
            else:
                settle(result=result)
            finally:
                if self.close_when_idle and not self.pending:
                    self.close()

        def failed(error):
            if epoch != self._epoch or self.closed:
                settle(error=error)
                return
            self.pending = False
            with self._progress_lock:
                self._latest_progress = None
            self._enable()
            if current():
                message = ('Operation stopped before completion. ' if isinstance(error, InterruptedError)
                           else 'Operation did not complete. ')
                self.report(message + str(error) + '\nRefresh saved jobs to inspect retained work.', focus=True)
            settle(error=error)
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

    def refresh(self, event=None, *, selected_job=None):
        def loaded(value):
            self.profiles, self.job_rows = tuple(value['profiles']), tuple(value['jobs'])
            self.runtime.SetItems([f"{row['name']} — {row.get('configuration', {}).get('protocol', 'unknown protocol')} — "
                f"{'trusted' if row.get('trusted') else 'not trusted'}; {row.get('qualification', 'unqualified')}"
                for row in self.profiles])
            if self.profiles:
                self.runtime.SetSelection(0)
            self._render_jobs()
            self.report(f'{len(self.profiles)} registered runtimes; {len(self.job_rows)} saved jobs. '
                        'Select local media to start, or load a saved job. No inference starts automatically.')
            if selected_job is not None:
                job = next((row for row in self.job_rows if row['id'] == selected_job), None)
                if job is None:
                    self.report('The selected job is unavailable in this workspace.', focus=True)
                else:
                    self.publish_job(job)
            self._enable()
        self.submit('Loading local transcription profiles and jobs',
            lambda service, cancel, progress: {'profiles': service.list_runtimes(), 'jobs': service.list()}, loaded)

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

    def _settings(self, *, protocol):
        language = self.language.GetValue().strip().lower()
        if not re.fullmatch(r'auto|[a-z]{2,3}', language):
            raise ValueError('Enter auto or a language code such as uk or en.')
        if self.task.GetSelection() != 0:
            raise ValueError('This admitted worker supports original-language transcription only; speech translation is unavailable.')
        index = self.device.GetSelection()
        if not 0 <= index < len(DEVICES):
            raise ValueError('Choose CPU or CUDA and its compute type.')
        device, compute, _ = DEVICES[index]
        options = dict(model=self.model.GetValue().strip(), language=language, task='transcribe',
            device=device, compute_type=compute, beam_size=self.beam.GetValue(),
            parallel_files=self.parallel.GetValue(), vad_filter=bool(self.vad.GetValue()),
            condition_on_previous_text=bool(self.context.GetValue()),
            multilingual=bool(self.multilingual.GetValue()), include_headers=bool(self.headers.GetValue()),
            word_timestamps=bool(self.words.GetValue()))
        # Recognition always retains all original formats. Export subset choices
        # never alter the immutable recognition settings or completed receipts.
        return validate_settings(options, protocol=protocol)

    def _export_formats(self):
        formats = tuple(name for name in FORMATS if self.formats[name].GetValue())
        if not formats:
            raise ValueError('Select at least one format to export.')
        return formats

    def _new_job_choices(self):
        profile = self._selected_profile()
        protocol = profile['configuration']['protocol']
        sources, options = tuple(self.sources), self._settings(protocol=protocol)
        if not sources:
            raise ValueError('Choose at least one local audio file or folder.')
        return {'profile_id': profile['id'], 'protocol': protocol, 'sources': sources,
                'options': options, 'title': self.title.GetValue().strip() or 'Transcription',
                'recursive': bool(self.recursive.GetValue())}

    @staticmethod
    def _retain(service, choices, cancel, progress):
        retained = service.submit_sources(choices['sources'], profile_id=choices['profile_id'],
            protocol=choices['protocol'], options=choices['options'], title=choices['title'],
            recursive=choices['recursive'], cancelled=cancel)
        job = service.get(retained['id'])
        progress({'phase': 'Inputs retained', 'done': retained['files'], 'total': retained['files'],
                  'current': f"{retained['excluded_files']} unsupported files excluded; saved job {job['id']}"})
        return job, retained

    def prepare(self, event=None):
        """Publish an immutable retained job for review without starting inference."""
        if not self._usable() or self.current_job is not None or not self.confirm_discard():
            return
        try:
            choices = self._new_job_choices()
            def retained(value):
                job, intake = value
                self.publish_job(job)
                self.report(self.status.GetValue() + f"\nRetained files: {intake['files']}; "
                            f"unsupported files excluded: {intake['excluded_files']}. No inference has run.")
            self.submit('Retaining selected local media',
                lambda service, cancel, progress: self._retain(service, choices, cancel, progress),
                retained)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def start(self, event=None):
        if not self._usable() or not self.confirm_discard():
            return
        try:
            if self.current_job is None:
                choices = self._new_job_choices()
                def run(service, cancel, progress):
                    job, _ = self._retain(service, choices, cancel, progress)
                    self._active_job_id = job['id']
                    if cancel():
                        (service.pause if self._stop_intent == 'paused' else service.cancel)(job['id'])
                        return service.get(job['id'])
                    return service.run(job['id'], cancelled=cancel, progress=progress)
            else:
                identity = self.current_job['id']
                if self.current_job['status'] == 'completed':
                    raise ValueError('This job is complete. Choose New job for another transcription.')
                def run(service, cancel, progress):
                    self._active_job_id = identity
                    if cancel():
                        (service.pause if self._stop_intent == 'paused' else service.cancel)(identity)
                        return service.get(identity)
                    return service.resume(identity, cancelled=cancel, progress=progress)
            self.submit('Transcribing local audio', run, self.publish_job, kind='transcribe')
        except Exception as exc:
            self.report(str(exc), focus=True)

    def publish_job(self, job):
        self.current_job = job
        self.job_rows = (job,) + tuple(row for row in self.job_rows if row['id'] != job['id'])
        self._render_jobs()
        self.result_source.SetItems([row['original_name'] + ' — ' + row['filename']
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
        summary += '\nWorker contract: ' + job['protocol'] + '; retained outputs: TXT, SRT, VTT, JSON.'
        originals = {item['filename']: item['original_name'] for item in job.get('sources', ())}
        for attempt in job.get('effective_runs', ()):
            effective = attempt.get('settings', {})
            summary += (f"\n{originals.get(attempt.get('source'), attempt.get('source', 'Source'))}: "
                        f"attempt {attempt.get('attempt', '?')}; beam={effective.get('beam_size', '?')}, "
                        f"VAD={effective.get('vad_filter', '?')}, "
                        f"context={effective.get('condition_on_previous_text', '?')}.")
        if job.get('last_error'):
            summary += '\n' + str(job['last_error'])
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
                    lambda service, cancel, progress: service.get(identity), self.publish_job)

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
        try:
            if self._kind == 'transcribe' and self._active_job_id is not None:
                service = self.controller.service
                (service.pause if pause else service.cancel)(self._active_job_id)
        except Exception as exc:
            self.report('Could not record the stop request: ' + str(exc), focus=True)
            return
        self.cancel_event.set()
        self.frame.cancel_event.set()
        self.report(('Pause' if pause else 'Cancellation') + ' requested. Waiting for a safe checkpoint; '
                    'completed files remain saved. Preparation may stop before a new job is created.')

    def pause(self, event=None):
        if self.pending:
            self.request_stop(pause=True)
        elif self.current_job is not None and self.current_job.get('status') != 'completed':
            identity = self.current_job['id']
            self.submit('Pausing the saved transcription job',
                lambda service, cancel, progress: service.pause(identity), self.publish_job)

    def cancel(self, event=None):
        if self.pending:
            self.request_stop()
        elif self.current_job is not None and self.current_job.get('status') != 'completed':
            identity = self.current_job['id']
            self.submit('Cancelling the saved transcription job',
                lambda service, cancel, progress: service.cancel(identity), self.publish_job)

    def close(self, event=None):
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
        try:
            self._unregister_save()
        finally:
            self.finish_close()

    def _paths(self):
        index = self.protocol.GetSelection()
        if not 0 <= index < len(PROTOCOLS):
            raise ValueError('Choose the exact reviewed worker protocol.')
        return (*tuple(control.GetValue().strip() for control in
                       (self.python_path, self.worker_path, self.model_path)), PROTOCOLS[index])

    def runtime_changed(self, event=None):
        self.inspection = None
        self.trust.SetValue(False)
        self.runtime_details.ChangeValue('Inspect these exact files before trusting and registering this runtime.')
        if event is not None:
            event.Skip()

    def inspect_runtime(self, event=None):
        if not self._usable():
            return
        try:
            paths = self._paths()
        except (ValueError, OSError) as exc:
            self.report(str(exc), focus=True)
            return
        self.inspection = None
        self.trust.SetValue(False)
        def inspected(result):
            if paths != self._paths():
                self.runtime_changed()
                return
            self.inspection = (paths, result)
            summary = {key: result[key] for key in ('python', 'worker', 'models_dir', 'protocol',
                'python_sha256', 'worker_sha256', 'python_binding', 'runtime_inventory',
                'models_inventory', 'fingerprint', 'inference_qualified') if key in result}
            self.runtime_details.ChangeValue(json.dumps(summary, ensure_ascii=False, indent=2))
            self.report('Files inspected without execution. Review their origin and licence, then explicitly '
                        'trust and register them. Recognition quality remains unqualified.')
        self.submit('Inspecting existing local transcription files',
            lambda service, cancel, progress: service.inspect_runtime(*paths[:3], protocol=paths[3],
                cancelled=cancel, progress=progress), inspected)

    def register_runtime(self, event=None):
        if not self._usable():
            return
        try:
            paths = self._paths()
        except (ValueError, OSError) as exc:
            self.report(str(exc), focus=True)
            return
        if self.inspection is None or self.inspection[0] != paths or self.trust.GetValue() is not True:
            self.report('Inspect these exact files, then check the explicit trust confirmation.', focus=True)
            return
        name, fingerprint = self.runtime_name.GetValue().strip(), self.inspection[1]['fingerprint']
        def registered(profile):
            self.profiles = tuple(row for row in self.profiles if row['id'] != profile['id']) + (profile,)
            self.runtime.SetItems([f"{row['name']} — {row.get('configuration', {}).get('protocol', 'unknown protocol')} — "
                f"{'trusted' if row.get('trusted') else 'not trusted'}; {row.get('qualification', 'unqualified')}"
                for row in self.profiles])
            self.runtime.SetSelection(len(self.profiles) - 1)
            self.report('Runtime registered using the verified local files. No inference has run. '
                        'Choose media in New transcription to start.')
            self._enable()
        self.submit('Registering the reviewed local runtime',
            lambda service, cancel, progress: service.register_runtime(name, *paths[:3], protocol=paths[3],
                owner_trusted=True, expected_fingerprint=fingerprint, cancelled=cancel, progress=progress), registered)

    def rebind_runtime(self, event=None):
        if not self._usable() or not self.current_job or not self.confirm_discard():
            return
        try:
            profile, identity = self._selected_profile(), self.current_job['id']
            if not self.confirm_rebind(profile):
                return
            self.submit('Binding reviewed runtime to saved job',
                lambda service, cancel, progress: service.rebind_runtime(identity, profile['id'],
                    cancelled=cancel, progress=progress), self.publish_job)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def copy_job(self, event=None):
        """Use reviewed retained inputs for a separate job; never reuse rejected output."""
        if not self._usable() or not self.current_job or not self.confirm_discard():
            return
        try:
            profile, identity = self._selected_profile(), self.current_job['id']
            if profile['configuration']['protocol'] != self.current_job['protocol']:
                raise ValueError('A copy keeps the saved worker protocol; choose a trusted profile for that protocol.')
            if not self.confirm_copy(profile):
                return
            def copied(job):
                self.publish_job(job)
                self.report(self.status.GetValue() + '\nNew job from verified retained originals. '
                            'Old results remain separate; no inference has run.')
            self.submit('Copying verified retained transcription inputs',
                lambda service, cancel, progress: service.copy_job(identity, profile_id=profile['id'],
                    cancelled=cancel), copied)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def review_interruption(self, event=None):
        if not self._usable() or not self.current_job or not self.confirm_discard():
            return
        if (self.current_job.get('status') == 'completed'
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
        identity, source = self.current_job['id'], sources[index]['filename']
        edited = bool(self.latest_edit.GetValue())
        def read(service, cancel, progress):
            original_text = service.text(identity, source, cancelled=cancel)
            job = service.get(identity)
            retained = next(row for row in job['sources'] if row['filename'] == source)
            provenance = {'job': identity, 'workspace': self.controller.workspace, 'source': source,
                          'source_sha256': retained['sha256'], 'protocol': job['protocol'],
                          'kind': 'unverified recognition derivative'}
            original = {'text': original_text, 'provenance': provenance}
            edits = [row for row in service.edits(identity) if row['source'] == source] if edited else []
            latest = ({'text': edits[-1]['text'],
                       'provenance': {**provenance, 'edit_revision': edits[-1]['id'],
                                      'kind': 'separately saved user edit'}} if edits else original)
            if cancel():
                raise InterruptedError('Transcript reading cancelled.')
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
        return self.document is not None and self.editor.GetValue() != self.saved_text

    def _capture_edit(self):
        if self.document is None:
            raise ValueError('Load a transcript first.')
        self.document.set_text(self.editor.GetValue(), label='Edit transcript reading copy')
        return self.document.text

    def save_edits(self, event=None, *, completed=None):
        from .save_all import SaveResult
        replied = False
        def reply(saved, message=''):
            nonlocal replied
            if not replied:
                replied = True
                if completed is not None:
                    completed(SaveResult(saved, message))
        if not self._usable() or self.document is None:
            reply(False, 'Transcript save is unavailable or another operation is active.')
            return
        if not self.is_dirty():
            reply(True)
            return
        try:
            text = self._capture_edit()
            identity, source = self.current_job['id'], self.transcript_source_id
            def saved(receipt):
                self.saved_text = text
                self.provenance.ChangeValue('Saved separate edited derivative. Original recognition text unchanged.\n' +
                    json.dumps(receipt, ensure_ascii=False, indent=2))
                self.report('Edited reading copy saved as a separate derivative.')
                self._enable()
            accepted = self.submit('Saving transcript derivative',
                lambda service, cancel, progress: {'revision': service.save_edit(identity, source, text, cancelled=cancel),
                    'job': identity, 'source': source}, saved,
                settled=lambda result, error: reply(error is None, str(error) if error else ''))
            if not accepted:
                reply(False, 'The host did not admit this transcript save.')
        except Exception as exc:
            reply(False, str(exc))
            self.report(str(exc), focus=True)

    def undo(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            self._capture_edit()
            self.document.undo()
            self.editor.ChangeValue(self.document.text)
            self._enable()
        except Exception as exc:
            self.report(str(exc), focus=True)

    def redo(self, event=None):
        if not self._usable() or self.document is None:
            return
        try:
            self._capture_edit()
            self.document.redo()
            self.editor.ChangeValue(self.document.text)
            self._enable()
        except Exception as exc:
            self.report(str(exc), focus=True)

    def export_to(self, directory):
        if not self._usable() or self.current_job is None:
            return
        if self.is_dirty():
            self.report('Save the edited reading copy before exporting, or undo the unsaved edit.', focus=True)
            return
        identity = self.current_job['id']
        try:
            formats = self._export_formats()
        except (ValueError, OSError) as exc:
            self.report(str(exc), focus=True)
            return
        library_root = self.controller.private_root
        def export(service, cancel, progress):
            if cancel():
                raise InterruptedError('Export cancelled before publication.')
            destination = local_path(directory)
            root = local_path(library_root, exists=True)
            regular(destination.parent, directory=True)
            if destination.is_relative_to(root) or root.is_relative_to(destination):
                raise ValueError('Choose an export folder outside the private library.')
            if os.path.lexists(destination):
                raise FileExistsError('Choose a new transcription export folder.')
            return {'exported_to': str(service.export(identity, destination, formats=formats, cancelled=cancel))}
        self.submit('Exporting verified transcription results',
            export,
            lambda receipt: self.report('Export saved.\n' + json.dumps(receipt, ensure_ascii=False, indent=2)))


def build_transcription_dialog(frame, controller=None):
    """Create native controls; callers own ShowModal/Destroy and may inject a service."""
    import wx
    if getattr(frame, 'active', None) is None:
        raise ValueError('Select an active workspace before opening transcription.')
    controller = controller or TranscriptionController(frame.library, frame.active[0])

    class Dialog(wx.Dialog, TranscriptionActions):
        def __init__(self):
            wx.Dialog.__init__(self, frame, title='Local transcription workstation', size=(980, 860),
                               style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
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
            results = wx.Panel(self.pages)
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
            button(new, row, 'Prepare saved job', self.prepare, new_option=True)
            box.Add(row)
            self.recursive = wx.CheckBox(new, label='Include subfolders of selected folders')
            self.recursive.SetName('Include subfolders of selected folders')
            box.Add(self.recursive, 0, wx.ALL, 5)
            self.mutable.append(self.recursive)
            self.new_controls.append(self.recursive)
            self.language = field(new, box, 'Source lan&guage code or auto', value='auto', new_option=True)
            self.model = field(new, box, 'Existing local model name', value='small', new_option=True)
            self.task = field(new, box, 'Transcription task', wx.Choice,
                choices=['Original-language transcription; speech translation is unsupported'])
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
            self.parallel = wx.SpinCtrl(new, min=1, max=8, initial=1)
            self.parallel.SetName('Parallel files, one to eight')
            row.Add(self.parallel, 0, wx.ALL, 5)
            box.Add(row)
            self.mutable.extend((self.beam, self.parallel))
            self.new_controls.extend((self.beam, self.parallel))
            row = wx.WrapSizer(wx.HORIZONTAL)
            for attr, label, default in (('vad', 'Voice activity detection', True),
                    ('context', 'Use previous text as context', True),
                    ('multilingual', 'Allow multilingual segments', False),
                    ('headers', 'Include text headers; unsupported by V5', False),
                    ('words', 'Retain available word timestamps', True)):
                control = wx.CheckBox(new, label=label)
                control.SetName(label)
                control.SetValue(default)
                setattr(self, attr, control)
                row.Add(control, 0, wx.ALL, 5)
                self.mutable.append(control)
                self.new_controls.append(control)
            box.Add(row, 0, wx.EXPAND)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(new, label='Export formats; all four remain retained in the library'), 0,
                    wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.formats = {}
            for name in FORMATS:
                control = wx.CheckBox(new, label=name.upper())
                control.SetName('Output ' + name.upper())
                control.SetValue(True)
                row.Add(control, 0, wx.ALL, 5)
                self.formats[name] = control
                self.mutable.append(control)
            box.Add(row)
            new.SetSizer(box)

            box = wx.BoxSizer(wx.VERTICAL)
            text = wx.StaticText(runtimes, label='Point to existing, reviewed files. Inspection does not execute them. '
                'Registration records their exact identity and your trust; quality remains unqualified.')
            text.Wrap(880)
            box.Add(text, 0, wx.ALL | wx.EXPAND, 6)
            self.runtime_name = field(runtimes, box, 'Runtime profile name', value='My local Whisper runtime')
            self.protocol = field(runtimes, box, 'Exact reviewed worker protocol', wx.Choice, choices=list(PROTOCOLS))
            self.protocol.SetSelection(0)
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
            button(results, row, 'New job from retained originals…', self.copy_job)
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
            self.save_edits_button = button(results, row, 'Sa&ve edits', self.save_edits, reading=True)
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
            self.SetMinSize((700, 620))
            self.SetEscapeId(wx.ID_CLOSE)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.configure(frame, controller)
            for control in (self.python_path, self.worker_path, self.model_path):
                control.Bind(wx.EVT_TEXT, self.runtime_changed)
            self.editor.Bind(wx.EVT_TEXT, lambda event: self._enable())
            self.protocol.Bind(wx.EVT_CHOICE, self.runtime_changed)
            self.runtime.Bind(wx.EVT_CHOICE, lambda event: self._enable())
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.flush_progress, self.timer)
            self.timer.Start(250)
            save_id = wx.NewIdRef()
            self.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_CTRL, ord('S'), int(save_id))]))
            self.Bind(wx.EVT_MENU, self.save_edits, id=int(save_id))
            self.runtime.SetFocus()

        def confirm_discard(self):
            return not self.is_dirty() or wx.MessageBox(
                'Discard the unsaved reading-copy edits? The original transcript and saved edits remain available.',
                'Unsaved transcript edits', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

        def confirm_rebind(self, profile):
            return wx.MessageBox('Bind this saved job to the selected reviewed runtime: ' + profile['name'] +
                '? The service will verify the saved worker and model identities before resuming.', 'Rebind saved job',
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

        def confirm_copy(self, profile):
            return wx.MessageBox('Create a separate ready job from the verified retained original inputs using ' +
                profile['name'] + '? Saved options and protocol are kept. This explicitly allows the newly '
                'reviewed runtime/model bytes; old output is not reused and no inference starts now.',
                'New transcription from retained originals',
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

        def confirm_worker_stopped(self):
            return wx.MessageBox('Confirm that the previous external transcription worker has stopped. '
                'Check its process on this computer before allowing another worker for the same job. '
                'Have you confirmed that it is stopped?', 'Review interrupted worker',
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES

        def finish_close(self):
            self.timer.Stop()
            self.EndModal(wx.ID_CLOSE)

        def _show_sources(self):
            self.source_paths.ChangeValue('\n'.join(str(path) for path in self.sources))

        def choose_files(self, event=None):
            if not self._usable() or self.current_job is not None:
                return
            with wx.FileDialog(self, 'Choose local audio or video files', wildcard=MEDIA_FILTER,
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as picker:
                if picker.ShowModal() == wx.ID_OK:
                    self.sources = tuple(dict.fromkeys((*self.sources, *(Path(path) for path in picker.GetPaths()))))
                    self._show_sources()

        def choose_folder(self, event=None):
            if not self._usable() or self.current_job is not None:
                return
            with wx.DirDialog(self, 'Choose a folder containing local audio or video',
                    style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as picker:
                if picker.ShowModal() == wx.ID_OK:
                    self.sources = tuple(dict.fromkeys((*self.sources, Path(picker.GetPath()))))
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
            try:
                if picker.ShowModal() == wx.ID_OK:
                    control.SetValue(picker.GetPath())
            finally:
                picker.Destroy()

        def choose_export(self, event=None):
            if not self._usable() or self.current_job is None:
                self.report('Load a saved job before exporting.', focus=True)
                return
            with wx.DirDialog(self, 'Choose where to create a new transcription export folder',
                    style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as picker:
                if picker.ShowModal() == wx.ID_OK:
                    target = Path(picker.GetPath()) / ('transcription-' + uuid4().hex[:12])
                    self.export_to(target)

        def read_aloud(self, event=None):
            if not self._usable() or self.document is None:
                return
            from .speech_dialog import show_speech_reader
            identity, source = self.current_job['id'], self.transcript_source_id
            def guard():
                self._check_root()
                if self.current_job is None or self.current_job['id'] != identity or self.transcript_source_id != source:
                    raise ValueError('The selected transcription source changed. Open its reading copy again.')
            show_speech_reader(self, text=self.editor.GetValue(), title='Unverified transcription reading copy',
                source_guard=guard, recovery_slot='transcription-' + self.controller.workspace + '-' + identity + '-' + source)

        def help(self, event=None):
            with wx.Dialog(self, title='Transcription help', size=(750, 620),
                    style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER) as dialog:
                box = wx.BoxSizer(wx.VERTICAL)
                text = wx.TextCtrl(dialog, value=HELP, style=wx.TE_MULTILINE | wx.TE_READONLY)
                text.SetName('Local transcription help')
                box.Add(text, 1, wx.EXPAND | wx.ALL, 10)
                box.Add(dialog.CreateButtonSizer(wx.OK), 0, wx.ALIGN_RIGHT | wx.ALL, 8)
                dialog.SetSizer(box)
                text.SetFocus()
                dialog.ShowModal()

    return Dialog()


def show_transcription_dialog(frame, controller=None, *, selected_job=None):
    import wx
    if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
        frame.report('Finish or cancel the current operation before opening transcription.')
        return
    dialog = build_transcription_dialog(frame, controller)
    try:
        wx.CallAfter(lambda: dialog.refresh(selected_job=selected_job) if not dialog.closed else None)
        dialog.ShowModal()
    finally:
        dialog.closed = True
        dialog.cancel_event.set()
        try:
            dialog._unregister_save()
        finally:
            try:
                dialog.timer.Stop()
            finally:
                dialog.Destroy()


def show_transcription(frame, *, selected_job=None):
    """Keep the existing dashboard/native entrypoint and exact workspace binding."""
    if getattr(frame, 'active', None) is None:
        frame.report('Select an active workspace before opening transcription.')
        return
    return show_transcription_dialog(frame, selected_job=selected_job)


def install_transcription_menu(frame, menu):
    """Install into the host-owned menu before its shared shortcut capture."""
    import wx
    def open_dialog(event=None):
        if getattr(frame, '_closing', False):
            return
        if getattr(frame, 'working', False) or getattr(frame, 'busy', False):
            frame.report('Wait for the current operation or cancel it before opening transcription.')
            return
        try:
            active = getattr(frame, 'active', None)
            if active is None:
                raise ValueError('Select an active workspace before opening transcription.')
            controller = TranscriptionController(frame.library, active[0])
            show_transcription_dialog(frame, controller)
        except Exception as exc:
            frame.report('Transcription could not open: ' + str(exc))
    item = menu.Append(wx.ID_ANY, 'Local &transcription…')
    frame.Bind(wx.EVT_MENU, open_dialog, item)
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('transcription.open', 'Transcription: local audio and video'), item)
    return open_dialog


def install_transcription_actions(frame):
    """Keep the existing host menu and semantic shortcut identity."""
    install_transcription_menu(frame, frame.menu_bar.GetMenu(0))
