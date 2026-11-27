"""Keyboard-first native production workspace; no waveform or drag operation required."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import threading

from ..durable_state import atomic_json, encode, read_json
from ..research.export_file import write_binary_export, export_completion
from ..studio.project import Project, parse_script
from ..studio.render import ProductionService
from .operation_progress import operation_outcome
from .progress_panel import NativeProgressPanel
from ..speech.progress_speech import activate_progress_voice, play_when_quiet
from .export_destination import confirm_export_destination

SCRIPT_HELP = '''Write ordinary prose for one narrator, or use named speaking roles:
Narrator [center]: The team enters the archive.
Alice [left]: I found the letter.
Bob [right]: Let us read it carefully.

Optional directions, each on its own line:
[Scene: Chapter title]
[Music: suspense; tempo=80; gain=-24]
[Pause: 2]
[Duration: 3600]

Music moods: calm, suspense, hopeful, sad, energetic.
Default music is locally composed procedural instrumental, with no remote request.
Role voices, gain and position can be edited before rendering. Source prose is never
rewritten automatically. Generated scripts are separate from saved quotations.
Ctrl+S saves the project and draft. Ctrl+Shift+S exports project JSON.
F6 renders or resumes. F8 pauses after cancelling the current audio process.
WAV master, chapter files, speaker stems, subtitles and the source script are exported
to a new project folder. Unavailable models/codecs report a clear error.'''


class StudioController:
    """UI-independent draft/revision contract, also exercised by native smoke checks."""
    def __init__(self, library, *, service=None):
        self.service = service or ProductionService(library.root / 'state' / 'studio')
        self.draft = library.root / 'state' / 'studio' / 'draft.json'
        self.project = None

    def autosave(self, text, title, voice_ids):
        atomic_json(self.draft, {'schema': 1, 'text': text, 'title': title,
                    'voice_ids': list(voice_ids), 'project_id': self.project.id if self.project else None},
                    replace=self.draft.exists())

    def save(self, text, title, voice_ids):
        # Keep even an invalid script draft, and report the validation failure.
        self.autosave(text, title, voice_ids)
        previous_project = self.project
        if previous_project and text == previous_project.original_script:
            # JSON projects can contain recorded clips, source bindings and
            # non-default render settings which the prose grammar cannot express.
            # Saving an unchanged script must preserve that complete performance.
            project = replace(previous_project, title=title)
        else:
            project = parse_script(text, title=title, voice_ids=voice_ids,
                                   mode=previous_project.mode if previous_project else 'faithful',
                                   project_id=previous_project.id if previous_project else None)
        if previous_project and text != previous_project.original_script:
            attached = {scene.id: scene for scene in previous_project.scenes if scene.cues}
            current = {scene.id: scene for scene in project.scenes}
            if any(identity not in current or current[identity].title != old.title
                   for identity, old in attached.items()):
                raise ValueError('A scene with attached audio changed its identity. '
                                 'Move or remove its clips in the scene audio editor before changing scene structure.')
            scenes = tuple(replace(scene, cues=attached[scene.id].cues)
                           if scene.id in attached else scene for scene in project.scenes)
            previous = {role.name.casefold(): role for role in self.project.roles}
            roles = tuple(replace(role, voice_id=previous[role.name.casefold()].voice_id,
                                  language=previous[role.name.casefold()].language,
                                  pan=previous[role.name.casefold()].pan,
                                  gain_db=previous[role.name.casefold()].gain_db,
                                  rate=previous[role.name.casefold()].rate,
                                  volume=previous[role.name.casefold()].volume)
                          if role.name.casefold() in previous else role for role in project.roles)
            project = replace(project, roles=roles, scenes=scenes,
                              sample_rate=previous_project.sample_rate,
                              schema=previous_project.schema, revision=previous_project.revision)
        if previous_project and project.to_dict() != previous_project.to_dict():
            project = replace(project, revision=previous_project.revision + 1)
        self.service.projects.save(project,
            expected_revision=previous_project.revision if previous_project else None)
        self.project = project
        return project


def show_studio(frame):
    import wx
    controller = StudioController(frame.library)
    service = controller.service
    class StudioDialog(wx.Dialog):
        def Destroy(self):
            if getattr(self, '_destroy_requested', False):
                return False
            self._destroy_requested = True
            callback = getattr(self, 'dispose', None)
            if callback is not None:
                callback()
            return super().Destroy() if self else False

    dialog = StudioDialog(frame, title='Audio Studio — script, voices and production', size=(980, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-studio')
    cancel = threading.Event()
    voices, current_job, alive, busy = (), None, True, False
    draft_dirty = False
    saved_signature = None
    owned_progress_voices = set()
    cleanup_retry_registered = False
    cleanup_started = False
    native_id = dialog.GetId()
    dialog._native_id = native_id
    dialog._destroy_requested = False

    def dispose():
        nonlocal alive
        alive = False
        cancel.set()

    def destroyed(event):
        if event.GetId() == native_id:
            dispose()
        event.Skip()

    dialog.dispose = dispose
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.TOP, 6)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 6)
        return control

    title = field('Project &title', value='Audio production')
    voice_choice = field('Default &voice (local; role overrides below)', wx.Choice)
    editor = field('&Script — prose or named roles; Help explains directions', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_DONTWRAP)
    formats = ['wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma']
    format_choice = field('Final audio &format (WAV master is always retained)', wx.Choice, choices=formats)
    format_choice.SetSelection(0)
    stems = wx.CheckBox(dialog, label='Export separate &speaker and music stems')
    stems.SetValue(True)
    layout.Add(stems, 0, wx.ALL, 6)
    status = field('Production and save status', style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value='Loading installed local voices. No audio starts automatically.', size=(-1, 100))
    buttons = {}
    for row in ((('save', '&Save (Ctrl+S)'), ('open', '&Open project'), ('saveas', 'Save &as (Ctrl+Shift+S)'),
                 ('roles', 'Edit &roles and voices'), ('help', '&Help')),
                (('render', '&Render / resume (F6)'), ('pause', '&Pause (F8)'), ('jobs', 'Saved &jobs'),
                 ('preview', 'Listen to &output'), ('close', '&Close')),
                (('progress_voice', 'Use voice for progress...'),
                 ('navigation', 'Playback navigation...'), ('pause_preview', 'Pause/resume playback'),
                 ('stop_preview', 'Stop playback'), ('output_device', 'Playback output...'))):
        box = wx.BoxSizer(wx.HORIZONTAL)
        for identity, label in row:
            button = wx.Button(dialog, label=label)
            buttons[identity] = button
            if identity == 'progress_voice':
                button.SetName('Use selected local voice for progress')
            box.Add(button, 0, wx.ALL, 4)
        layout.Add(box, 0, wx.EXPAND)
    dialog.SetSizer(layout)

    def report(message, focus=False):
        if alive:
            status.ChangeValue(str(message))
            if focus:
                status.SetFocus()

    dialog.library, dialog.status = frame.library, status
    dialog.report = lambda message: report(message, True)
    dialog.host, dialog.layout, dialog.working = dialog, layout, False
    progress_panel = NativeProgressPanel(dialog, accessible_name='Audio Studio operation progress',
        details_name='Audio Studio current progress and phase', query_name='Query Audio Studio progress')
    parent_panel = getattr(frame, 'progress_panel', None)
    if parent_panel is not None:
        progress_panel.hub = parent_panel.hub
    progress = progress_panel.legacy
    dialog.progress_panel, dialog.operation_progress = progress_panel, progress
    parent_progress = getattr(frame, 'operation_progress', None)
    if parent_progress is not None and parent_progress.speech_provider is not None:
        try:
            progress.set_speech_provider(parent_progress.speech_provider.copy_selection(), root=dialog.library.root)
        except (ValueError, OSError, RuntimeError) as exc:
            report(f'Select a progress voice again: {exc}')

    def selected_voices():
        if not alive:
            return ()
        selected = voice_choice.GetSelection()
        if 0 <= selected < len(voices):
            # One chosen voice is the safe default; distinct casting is explicit.
            return (voices[selected].id,)
        return ()

    def controls():
        if not alive:
            return
        dialog.working = busy
        progress_panel.settings_button.Enable(not busy)
        for name in ('render', 'open', 'roles', 'jobs'):
            buttons[name].Enable(not busy)
        buttons['pause'].Enable(busy)
        buttons['preview'].Enable(not busy and current_job is not None)
        buttons['navigation'].Enable(not busy and getattr(dialog, '_playback_navigation', None) is not None)
        buttons['pause_preview'].Enable(not busy and getattr(dialog, '_audio_player', None) is not None)
        buttons['stop_preview'].Enable(getattr(dialog, '_audio_player', None) is not None)
        buttons['output_device'].Enable(not busy)
        selected = voice_choice.GetSelection()
        buttons['progress_voice'].Enable(not busy and 0 <= selected < len(voices)
                                          and 'volume' in voices[selected].capabilities)
        editor.Enable(not busy)
        title.Enable(not busy)
        voice_choice.Enable(not busy)

    def run(operation, complete, *, phase='Audio Studio operation', with_progress=False):
        nonlocal busy
        if busy or not alive:
            return
        busy = True
        cancel.clear()
        controls()
        token, observer_available = None, True
        job_identity = current_job
        finished = threading.Event()
        try:
            progress.reporting_error = None
        except Exception:
            pass

        def unavailable(exc):
            nonlocal observer_available
            observer_available = False
            # Feedback must not change a committed render's business result or
            # overwrite the callback's detailed output location with an error.
            try:
                progress.reporting_error = 'Progress feedback is unavailable: ' + str(exc)
            except Exception:
                pass
            if token is not None:
                try:
                    progress.finalize(token=token)
                except Exception:
                    pass

        def observe(method, *args, **kwargs):
            if alive and observer_available:
                try:
                    return getattr(progress, method)(*args, **kwargs)
                except Exception as exc:
                    unavailable(exc)
            return None

        token = observe('start', phase)

        def measured_phase(message):
            # The production service currently emits exact phase receipts, not
            # an overall measured unit total. Keep its gauge indeterminate.
            if finished.is_set():
                return
            try:
                description = ' '.join(str(message).split())[:512] or phase
                observe('update', None, None, description, token=token)
                def show_phase():
                    if alive and not finished.is_set():
                        report(message)
                wx.CallAfter(show_phase)
            except Exception as exc:
                # A detached native label or a stopped UI event loop cannot
                # interrupt the production service after it saved a receipt.
                unavailable(exc)

        def failure_outcome(exc, *, worker=False):
            from concurrent.futures import CancelledError
            from carr_library.audio.models import AudioCancelled
            interrupted = worker and isinstance(exc, (InterruptedError, AudioCancelled, CancelledError))
            state = 'paused' if interrupted else 'failed'
            if worker and with_progress and job_identity is not None:
                try:
                    saved_state = operation_outcome(service.get(job_identity))
                    if interrupted and saved_state in ('paused', 'cancelled', 'failed'):
                        state = saved_state
                except Exception:
                    pass  # A missing receipt cannot hide the original error.
            return state

        def failed_operation(exc, *, worker=False):
            if not alive:
                finish_detached_task(error=exc, worker=worker)
                return
            state = failure_outcome(exc, worker=worker)
            if state == 'cancelled':
                observe('cancel', phase + ': cancelled', token=token)
            elif state == 'paused':
                observe('pause', phase + ': paused; completed chunks retained', token=token)
            else:
                observe('fail', phase + ': failed', token=token)
            observe('finalize', token=token)
            report(str(exc), True)

        task = getattr(getattr(progress, 'tracker', None), 'task', None)

        def perform():
            from carr_library.progress import ProgressTask, bind_progress
            if isinstance(task, ProgressTask):
                with bind_progress(task):
                    return operation(measured_phase) if with_progress else operation()
            return operation(measured_phase) if with_progress else operation()

        def finish_detached_task(result=None, *, error=None, worker=False, applied=False):
            # The shared task outlives its native observer. Only a returned
            # durable receipt or a completed result callback can claim success;
            # an unapplied value may still require publication by that callback.
            from carr_library.progress import ProgressTask
            if not isinstance(task, ProgressTask) or task.snapshot().terminal:
                return
            try:
                if error is not None:
                    outcome = failure_outcome(error, worker=worker)
                elif applied or isinstance(result, dict) and any(key in result for key in ('status', 'state')):
                    outcome = operation_outcome(result)
                else:
                    outcome = 'unavailable'
                message = (phase + ': ' + outcome + '; the view has closed. ' +
                           ('The returned saved receipt is retained.' if outcome == 'succeeded'
                            else 'Review the saved operation before continuing.'))
                if outcome == 'succeeded':
                    task.succeed(durable=True, message=message)
                elif outcome == 'failed':
                    task.fail(message)
                elif outcome == 'cancelled':
                    task.cancelled(message)
                elif outcome == 'paused':
                    task.pause(message)
                else:
                    task.unavailable(message)
            except (Exception, KeyboardInterrupt):
                if not task.snapshot().terminal:
                    task.unavailable('The view closed before its operation outcome could be confirmed.')

        try:
            future = executor.submit(perform)
        except Exception as exc:
            finished.set()
            busy = False
            controls()
            failed_operation(exc)
            return

        def discard_result(done):
            nonlocal busy
            finished.set()
            busy = False
            try:
                result = done.result()
            except (Exception, KeyboardInterrupt) as exc:
                finish_detached_task(error=exc, worker=True)
                return
            # Keep result cleanup independent of a delivered GUI callback, and
            # retain a failed owner before recording the shared task outcome.
            if callable(getattr(result, 'close', None)):
                owned_progress_voices.add(result)
                retire_progress_voice(result)
            finish_detached_task(result)

        def finish(done):
            if not alive:
                discard_result(done)
                return
            def apply():
                nonlocal busy
                finished.set()
                if not alive:
                    discard_result(done)
                    return
                busy = False
                controls()
                try:
                    result = done.result()
                except Exception as exc:
                    failed_operation(exc, worker=True)
                    return
                try:
                    outcome = operation_outcome(result)
                except Exception as exc:
                    unavailable(exc)
                    return
                try:
                    # Completion may verify or publish a final artifact. It is
                    # business work and must finish before any success signal.
                    if outcome == 'succeeded':
                        complete(result)
                    else:
                        report(phase + ': ' + outcome + '. Saved work is retained.', True)
                except Exception as exc:
                    failed_operation(exc)
                    return
                if not alive:
                    finish_detached_task(result, applied=outcome == 'succeeded')
                    return
                if outcome == 'succeeded':
                    observe('succeed', phase + ': verified complete', token=token)
                elif outcome == 'paused':
                    observe('pause', phase + ': needs attention', token=token)
                elif outcome == 'cancelled':
                    observe('cancel', phase + ': cancelled', token=token)
                else:
                    observe('fail', phase + ': failed', token=token)
                observe('finalize', token=token)
            wx.CallAfter(apply)
        future.add_done_callback(finish)

    def retire_progress_voice(provider):
        # Keep failed cleanup reachable independently of a delivered UI callback.
        nonlocal cleanup_retry_registered
        try:
            if provider.close() is False:
                raise RuntimeError('A progress voice has not confirmed closure.')
        except (Exception, KeyboardInterrupt):
            owned_progress_voices.add(provider)
            if not cleanup_retry_registered:
                import atexit
                atexit.register(release_service)
                cleanup_retry_registered = True
            return False
        owned_progress_voices.discard(provider)
        return True

    def select_progress_voice(event=None):
        if not alive:
            return
        selected = voice_choice.GetSelection()
        if not 0 <= selected < len(voices):
            return
        root = Path(dialog.library.root)
        voice_id = voices[selected].id

        def activate():
            provider = activate_progress_voice(root, voice_id, cancelled=cancel.is_set)
            # The executor owns this result before any optional GUI delivery.
            owned_progress_voices.add(provider)
            return provider

        def activated(provider):
            try:
                progress.set_speech_provider(provider, root=root)
            except (Exception, KeyboardInterrupt):
                retire_progress_voice(provider)
                raise
            owned_progress_voices.discard(provider)  # Ownership transferred.
            if parent_progress is not None:
                copied = provider.copy_selection()
                owned_progress_voices.add(copied)
                try:
                    parent_progress.set_speech_provider(copied, root=root)
                except (Exception, KeyboardInterrupt):
                    retire_progress_voice(copied)
                    raise
                owned_progress_voices.discard(copied)
            progress.settings_dialog()
            if not alive:
                return
            if parent_progress is not None and not parent_progress.closed:
                parent_progress.reload_preferences()
            report(f'Progress voice selected for this session: {provider.label}. '
                   f'Sound mode: {progress.preferences.mode}.')

        run(activate, activated, phase='Selecting a local progress voice')

    def save(event=None):
        nonlocal draft_dirty, saved_signature
        if not alive:
            return
        try:
            signature = (editor.GetValue(), title.GetValue(), selected_voices())
            result = controller.save(*signature)
            draft_dirty = False
            saved_signature = signature
            report(f'Saved project revision {result.revision}. {len(result.scenes)} scenes, {len(result.roles)} roles.', True)
            return result
        except Exception as exc:
            report(f'Project validation failed: {exc} The editable draft was retained if its save succeeded.', True)
            return None

    def changed(event):
        nonlocal draft_dirty, current_job
        if not alive:
            return
        draft_dirty = True
        current_job = None
        event.Skip()

    def autosave(event=None):
        nonlocal draft_dirty
        if not alive:
            return
        if not draft_dirty or busy:
            return
        try:
            controller.autosave(editor.GetValue(), title.GetValue(), selected_voices())
            draft_dirty = False
        except Exception as exc:
            report(f'Automatic draft save failed: {exc}', True)

    def open_project(event=None):
        if not alive:
            return
        chooser = wx.FileDialog(dialog, 'Open project JSON or UTF-8 script',
                                wildcard='Project and script (*.json;*.txt;*.md)|*.json;*.txt;*.md',
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if chooser.ShowModal() != wx.ID_OK or not alive or not chooser:
                return
            path = Path(chooser.GetPath())
            if path.stat().st_size > 16 * 1024**2:
                raise ValueError('The script exceeds the editable text budget; split it by chapters.')
            if path.suffix.casefold() == '.json':
                project = Project.from_dict(read_json(path))
                controller.service.register_project_inputs(project, imported=True)
                controller.service.projects.save(project)
                controller.project = project
                editor.SetValue(project.original_script)
                title.SetValue(project.title)
            else:
                controller.project = None
                editor.SetValue(path.read_text(encoding='utf-8-sig'))
                title.SetValue(path.stem)
            report('Opened. Review voices and directions before rendering.', True)
        except Exception as exc:
            report(str(exc), True)
        finally:
            if chooser:
                chooser.Destroy()

    def save_as(event=None):
        if not alive:
            return
        library = frame.library
        project = save()
        if project is None:
            return
        chooser = wx.FileDialog(dialog, 'Save audio project as', defaultFile='audio-project.json',
                                wildcard='Project JSON (*.json)|*.json', style=wx.FD_SAVE)
        try:
            if chooser.ShowModal() == wx.ID_OK and alive and chooser:
                path = Path(chooser.GetPath())
                if controller.draft.resolve() != (library.root / 'state' / 'studio' / 'draft.json').resolve():
                    raise ValueError('The library changed. Reopen the studio before exporting.')
                approval = confirm_export_destination(dialog, path, private_root=library.root,
                                                       title='Replace audio project')
                if not alive or approval is None:
                    return
                if frame.library is not library:
                    raise ValueError('The library changed. Reopen the studio before exporting.')
                receipt = write_binary_export(path, encode(project.to_dict()), private_root=library.root,
                                    approval=approval)
                report(export_completion('Project exported. Voice credentials and source library files are not embedded.', receipt), True)
        except Exception as exc:
            report(str(exc), True)
        finally:
            if chooser:
                chooser.Destroy()

    def edit_roles(event=None):
        nonlocal current_job
        if not alive:
            return
        project = save()
        if project is None:
            return
        chooser = wx.SingleChoiceDialog(dialog, 'Choose a speaking role.', 'Voice and position',
                                        [f'{r.name}; voice {r.voice_id}; pan {r.pan}; gain {r.gain_db} dB' for r in project.roles])
        try:
            if chooser.ShowModal() != wx.ID_OK or not alive or not chooser:
                return
            index = chooser.GetSelection()
        finally:
            if chooser:
                chooser.Destroy()
        if not alive or not 0 <= index < len(project.roles):
            return
        role = project.roles[index]
        detail = wx.Dialog(dialog, title='Role: ' + role.name)
        box = wx.BoxSizer(wx.VERTICAL)
        def item(label, control):
            box.Add(wx.StaticText(detail, label=label), 0, wx.ALL, 5)
            control.SetName(label)
            box.Add(control, 0, wx.EXPAND | wx.ALL, 5)
            return control
        choice = item('Installed voice', wx.Choice(detail, choices=[v.name + '; ' + v.language for v in voices]))
        choice.SetSelection(next((i for i, v in enumerate(voices) if v.id == role.voice_id), 0 if voices else wx.NOT_FOUND))
        pan = item('Pan: -1 left, 0 center, 1 right', wx.SpinCtrlDouble(detail, min=-1, max=1, initial=role.pan, inc=.1))
        gain = item('Gain in decibels', wx.SpinCtrlDouble(detail, min=-60, max=12, initial=role.gain_db, inc=1))
        rate = item('Synthesis rate, supported voices only', wx.SpinCtrl(detail, min=-10, max=10, initial=role.rate))
        volume = item('Synthesis volume', wx.SpinCtrl(detail, min=0, max=100, initial=role.volume))
        box.Add(detail.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 5)
        detail.SetSizerAndFit(box)
        try:
            if detail.ShowModal() != wx.ID_OK or not alive or not detail:
                return
            selection = choice.GetSelection()
            if not 0 <= selection < len(voices):
                raise ValueError('Choose an installed voice.')
            voice = voices[selection]
            if rate.GetValue() != 0 and 'rate' not in voice.capabilities:
                raise ValueError('This voice does not support synthesis-rate control.')
            edited = replace(role, voice_id=voice.id, pan=pan.GetValue(), gain_db=gain.GetValue(),
                             rate=rate.GetValue(), volume=volume.GetValue())
            updated = replace(project, roles=tuple(edited if i == index else r for i, r in enumerate(project.roles)),
                              revision=project.revision + 1)
            service.projects.save(updated, expected_revision=project.revision)
            controller.project = updated
            current_job = None
            report('Role settings saved. The next render reuses unaffected speech chunks.', True)
        except Exception as exc:
            report(str(exc), True)
        finally:
            if detail:
                detail.Destroy()

    def render(event=None):
        nonlocal current_job
        if not alive:
            return
        if busy:
            return
        if current_job is None:
            project = save()
            if project is None:
                return
            chooser = wx.DirDialog(dialog, 'Choose the parent folder for new audio outputs')
            try:
                if chooser.ShowModal() != wx.ID_OK or not alive or not chooser:
                    return
                current_job = service.submit(project, Path(chooser.GetPath()),
                    format=formats[format_choice.GetSelection()], stems=stems.GetValue())
            except Exception as exc:
                report(str(exc), True)
                return
            finally:
                if chooser:
                    chooser.Destroy()
        if not alive:
            return
        approved_inputs = ()
        try:
            pending_inputs = service.restored_inputs_to_review(current_job)
            if pending_inputs:
                review = wx.MessageDialog(dialog,
                    'This older backup did not retain these audio inputs. Review their local paths:\n\n'
                    + '\n'.join(pending_inputs)
                    + '\n\nAllow this project to read these local files and include them in future backups?',
                    'Review restored audio inputs', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                try:
                    if review.ShowModal() != wx.ID_YES or not alive or not review:
                        return
                    approved_inputs = pending_inputs
                finally:
                    if review:
                        review.Destroy()
            if not alive:
                return
            restored = service.get(current_job).get('recovery', {}).get('requires_output_directory') is True
            if restored:
                chooser = wx.DirDialog(dialog, 'Choose a new output folder for the restored production job')
                try:
                    if chooser.ShowModal() != wx.ID_OK or not alive or not chooser:
                        return
                    service.relocate_restored_job(current_job, Path(chooser.GetPath()))
                finally:
                    if chooser:
                        chooser.Destroy()
        except Exception as exc:
            report(str(exc), True)
            return
        if not alive:
            return
        identity = current_job
        def render_selected(measured):
            if approved_inputs:
                measured('Verifying explicitly approved restored audio inputs')
                service.approve_restored_inputs(approved_inputs, cancelled=cancel.is_set)
            return service.run(identity, cancelled=cancel.is_set, progress=measured)
        run(render_selected,
            lambda result: report('Production complete. ' + result['destination'] +
                '\nWAV, chapters, requested stems, subtitles and checksums are saved.', True),
            phase='Rendering audio production', with_progress=True)

    def jobs(event=None):
        nonlocal current_job
        if not alive:
            return
        try:
            rows = service.jobs()
            chooser = wx.SingleChoiceDialog(dialog, 'Select a saved job to inspect, resume, or replay.', 'Production jobs',
                [f'{row["project"]["title"]}; {row["status"]}; {row["progress"]}' for row in rows])
            try:
                if chooser.ShowModal() != wx.ID_OK or not alive or not chooser:
                    return
                index = chooser.GetSelection()
            finally:
                if chooser:
                    chooser.Destroy()
            if not alive or not 0 <= index < len(rows):
                return
            row = rows[index]
            project = Project.from_dict(row['project'])
            controller.project = service.projects.load(project.id)
            editor.ChangeValue(project.original_script)
            title.ChangeValue(project.title)
            current_job = row['id']
            report(f'{row["status"]}. {row["error"]}\nChoose Render / resume to continue or Listen to output after completion.', True)
            controls()
        except Exception as exc:
            report(str(exc), True)

    def stop_preview(event=None):
        if not alive:
            return
        dialog._playback_generation = getattr(dialog, '_playback_generation', 0) + 1
        navigation = getattr(dialog, '_playback_navigation', None)
        if navigation is not None:
            navigation.invalidate()
        try:
            player = getattr(dialog, '_audio_player', None)
            if player is not None:
                player.stop()
            report('Stopped the rendered-master preview.')
        except Exception as exc:
            report(f'Playback stop needs attention: {exc}', True)

    def choose_playback_output(event=None):
        if not alive:
            return
        player = getattr(dialog, '_audio_player', None)
        generation = getattr(dialog, '_playback_generation', 0)

        def guard():
            if (not alive or not dialog or busy
                    or generation != getattr(dialog, '_playback_generation', 0)
                    or getattr(dialog, '_audio_player', None) is not player):
                raise ValueError('The Studio or playback changed. Choose its output again when idle.')
            if player is not None and player.state in ('playing', 'paused'):
                raise ValueError('Stop the Studio preview before changing its playback output.')

        try:
            from .output_devices import choose_output_device
            guard()
            choice = choose_output_device(dialog, current=getattr(dialog, '_output_device', None), guard=guard)
            if choice is None:
                return
            guard()
            dialog._playback_generation = generation + 1
            navigation = getattr(dialog, '_playback_navigation', None)
            if navigation is not None:
                navigation.invalidate()
            if player is not None:
                if player.close() is False:
                    raise RuntimeError('The previous playback device has not confirmed closure.')
                player.select_output(choice.device)
            dialog._output_device = choice.device
            label = choice.device.name if choice.device is not None else 'the Windows system default'
            report(f'Preview output selected: {label}. Listen to output when ready.', True)
        except Exception as exc:
            report(f'Playback output was not changed. {exc}', True)

    def playback_navigation(event=None):
        if not alive:
            return
        try:
            if busy:
                raise ValueError('Wait for the current Studio operation before changing playback.')
            navigation = getattr(dialog, '_playback_navigation', None)
            if navigation is None:
                raise ValueError('Listen to a rendered output before opening its playback navigation.')
            navigation.show()
        except Exception as exc:
            report(f'Playback navigation could not be opened. {exc}', True)

    def pause_preview(event=None):
        if not alive or busy:
            return
        try:
            player = getattr(dialog, '_audio_player', None)
            if player is None or player.state not in ('playing', 'paused'):
                raise ValueError('Listen to the rendered master before pausing or resuming it.')
            if player.state == 'playing':
                player.pause()
                report('Rendered-master preview paused.')
            else:
                generation = dialog._playback_generation
                def resumed():
                    if player.state != 'paused':
                        return
                    player.resume()
                    report('Rendered-master preview resumed.')
                play_when_quiet((progress, parent_progress), resumed, schedule=wx.CallLater,
                    alive=lambda: alive and generation == dialog._playback_generation,
                    on_error=lambda exc: report(f'Playback did not resume. {exc}', True))
        except Exception as exc:
            report(f'Playback pause/resume needs attention: {exc}', True)

    def preview(event=None):
        if not alive:
            return
        try:
            if current_job is None:
                return
            row = service.get(current_job)
            source = Path(row['destination']) / 'master.wav'
            if not source.exists():
                raise ValueError('There is no completed master yet; render or resume the job.')
            from ..speech.player import WavePlayer
            from .playback_navigation import PlaybackNavigationHost
            player = getattr(dialog, '_audio_player', None)
            if player is None:
                player = WavePlayer()
                dialog._audio_player = player
                selected_output = getattr(dialog, '_output_device', None)
                if selected_output is not None:
                    player.select_output(selected_output)
            dialog._playback_generation = getattr(dialog, '_playback_generation', 0) + 1
            generation = dialog._playback_generation
            navigation = getattr(dialog, '_playback_navigation', None)
            if navigation is None:
                navigation = PlaybackNavigationHost(dialog, player,
                    generation=lambda: dialog._playback_generation, alive=lambda: alive,
                    stop_current=stop_preview, progress=(progress, parent_progress),
                    report=report, wx_module=wx)
                dialog._playback_navigation = navigation
            navigation.invalidate()
            progress.register_player(player)
            if parent_progress is not None:
                parent_progress.register_player(player)
            def playing():
                player.play(source)
                navigation.bind()
                controls()
                report('Playing the rendered master. Close this dialog to stop playback.', True)
            play_when_quiet((progress, parent_progress), playing, schedule=wx.CallLater,
                alive=lambda: alive and generation == dialog._playback_generation,
                on_error=lambda exc: report(f'Playback did not start. {exc}', True))
        except Exception as exc:
            report(str(exc), True)

    def help_text(event=None):
        if not alive:
            return
        from .desktop_app import show_text
        show_text(dialog, 'Audio Studio keyboard and script guide', SCRIPT_HELP)

    def release_service():
        # This runs behind owned discovery/render on the existing executor.
        nonlocal cleanup_retry_registered
        pending = []
        # An already detached panel retries only retained Python/native owners,
        # without querying a destroyed control. Accepted selections also need
        # this owner when their first close could not confirm native silence.
        try:
            if progress.close() is False:
                raise RuntimeError('The progress view retains an unconfirmed voice owner.')
        except (Exception, KeyboardInterrupt) as exc:
            pending.append(str(exc)[:1000])
        for provider in tuple(owned_progress_voices):
            if not retire_progress_voice(provider):
                pending.append('A progress voice has not confirmed closure.')
        try:
            if service.close() is False:
                raise RuntimeError('An owned voice worker has not confirmed closure.')
        except (Exception, KeyboardInterrupt) as exc:
            pending.append(str(exc)[:1000])
        import atexit
        if pending and not cleanup_retry_registered:
            atexit.register(release_service)
            cleanup_retry_registered = True
        elif not pending and cleanup_retry_registered:
            atexit.unregister(release_service)
            cleanup_retry_registered = False
        if pending:
            notice = 'Studio voice cleanup needs attention: ' + ' '.join(pending)
            def notify():
                try:
                    if frame and not getattr(frame, '_closing', False):
                        frame.report(notice)
                except (RuntimeError, AttributeError):
                    pass
            try:
                wx.CallAfter(notify)
            except RuntimeError:
                pass

    def cleanup():
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        dialog._playback_generation = getattr(dialog, '_playback_generation', 0) + 1
        try:
            timer.Stop()
        except RuntimeError:
            pass
        close_notices = []
        navigation = getattr(dialog, '_playback_navigation', None)
        if navigation is not None:
            try:
                navigation.close()
            except (OSError, RuntimeError) as exc:
                close_notices.append(f'Playback navigation timer needs attention: {exc}')
        if getattr(dialog, '_audio_player', None):
            silence_confirmed = False
            try:
                if dialog._audio_player.close() is False:
                    raise RuntimeError('The playback device has not confirmed closure.')
                silence_confirmed = True
            except (OSError, RuntimeError) as exc:
                close_notices.append(f'Playback device reported a close error: {exc}')
            # Each controller must retain uncertain native output even if a
            # different progress view has already been detached or destroyed.
            for observer in (progress, parent_progress):
                if observer is not None:
                    try:
                        observer.unregister_player(dialog._audio_player, silence_confirmed=silence_confirmed)
                    except Exception as exc:
                        close_notices.append(f'Playback progress feedback could not be refreshed: {exc}')
        if close_notices:
            report(' '.join(close_notices))

        try:
            if progress.close() is False:
                report('Progress voice cleanup remains pending; its owner will retry behind the worker.')
        except (Exception, KeyboardInterrupt) as exc:
            report(f'Progress voice cleanup needs attention: {exc}')
        # The same executor owns discovery/render and its registry cleanup.
        # Do not cancel the queued close or close an active borrowed registry.
        try:
            executor.submit(release_service)
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    def close(event=None):
        if not alive:
            return
        autosave()
        cancel.set()
        if current_job is not None and busy:
            service.request_pause(current_job)
        if busy:
            progress.pause('Audio production paused; completed chunks retained')
        cleanup()
        dispose()
        if dialog:
            dialog.EndModal(wx.ID_CLOSE)

    for name, callback in {'save': save, 'open': open_project, 'saveas': save_as, 'roles': edit_roles,
                           'render': render, 'pause': lambda event: cancel.set(), 'jobs': jobs,
                           'preview': preview, 'help': help_text, 'close': close,
                           'navigation': playback_navigation, 'pause_preview': pause_preview,
                           'stop_preview': stop_preview, 'output_device': choose_playback_output,
                           'progress_voice': select_progress_voice}.items():
        buttons[name].Bind(wx.EVT_BUTTON, callback)
    bindings = []
    for modifiers, key, callback in [(wx.ACCEL_CTRL, ord('S'), save),
        (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), save_as), (0, wx.WXK_F6, render),
        (0, wx.WXK_F8, lambda event: cancel.set())]:
        identity = int(wx.NewIdRef())
        dialog.Bind(wx.EVT_MENU, callback, id=identity)
        bindings.append((modifiers, key, identity))
    dialog.SetAcceleratorTable(wx.AcceleratorTable(bindings))
    editor.Bind(wx.EVT_TEXT, changed)
    title.Bind(wx.EVT_TEXT, changed)
    voice_choice.Bind(wx.EVT_CHOICE, lambda event: controls())
    dialog.Bind(wx.EVT_CLOSE, close)
    timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, autosave, timer)
    timer.Start(4000)

    def loaded(result):
        nonlocal voices, draft_dirty, saved_signature
        if not alive:
            return
        voices = result
        voice_choice.SetItems([v.name + '; ' + v.language for v in voices])
        if voices:
            voice_choice.SetSelection(0)
        if controller.draft.exists():
            try:
                draft = read_json(controller.draft)
                editor.ChangeValue(draft['text'])
                title.ChangeValue(draft['title'])
                if draft.get('project_id'):
                    controller.project = service.projects.load(draft['project_id'])
                choices = draft.get('voice_ids', ())
                for index, voice in enumerate(voices):
                    if voice.id in choices:
                        voice_choice.SetSelection(index)
                        break
            except Exception as exc:
                report(f'Saved draft needs review: {exc}', True)
                return
        draft_dirty = False
        controls()
        if controller.project is not None and (editor.GetValue(), title.GetValue()) == (
                controller.project.original_script, controller.project.title):
            saved_signature = (editor.GetValue(), title.GetValue(), selected_voices())
        report(f'{len(voices)} installed voices available. Enter a script and choose roles, then Render.'
               if voices else 'No local voice is installed. Configure a local SAPI/Piper voice before narration.', True)
        editor.SetFocus()

    from .save_all import SaveResult, coordinator_for
    coordinator = coordinator_for(frame)
    def unsaved():
        return (alive and bool(editor.GetValue())
                and (editor.GetValue(), title.GetValue(), selected_voices()) != saved_signature)
    def save_registered(completed):
        if not alive:
            completed(SaveResult(False, 'The Audio Studio view has closed.'))
        elif busy:
            completed(SaveResult(False, 'Audio production is running; pause it before saving the project.'))
        else:
            completed(SaveResult(save() is not None, 'Project validation or saving did not complete.'))
    def focus_registered():
        if alive and dialog:
            editor.SetFocus()
    unregister = (coordinator.register('studio.' + str(id(controller)), 'Audio Studio project',
                    dirty=unsaved, save=save_registered, focus=focus_registered)
                  if coordinator is not None else lambda: None)
    run(lambda: service.discover_voices(cancelled=cancel.is_set).voices(), loaded,
        phase='Checking installed local voices')
    try:
        dialog.ShowModal()
    finally:
        try:
            unregister()
            if alive:
                close()
        finally:
            dispose()
            cleanup()
            if dialog:
                dialog.Destroy()


def audio_conversion_settings(*, sample_rate, channels, bit_depth, bitrate, start, duration,
                              gain, tempo, loudness, cleanup):
    """Validate the displayed numeric controls before admitting audio work."""
    from ..audio import Effect
    from ..audio.models import finite, integer
    integer(sample_rate, 'Output sample rate', 8000, 192000)
    integer(channels, 'Output channels', 1, 2)
    if type(bit_depth) is not int or bit_depth not in (16, 24, 32):
        raise ValueError('PCM bit depth must be 16, 24 or 32.')
    integer(bitrate, 'Output bitrate', 32, 512)
    start = finite(start, 'Selection start', 0)
    duration = None if duration is None else finite(duration, 'Selection duration', .000001)
    gain = finite(gain, 'Gain', -60, 12)
    tempo = finite(tempo, 'Playback speed', .5, 2)
    if type(cleanup) is not bool:
        raise ValueError('Choose whether speech cleanup should be applied.')
    chain = [Effect('gain', gain_db=gain), Effect('tempo', factor=tempo)]
    if cleanup:
        chain += [Effect('denoise'), Effect('compressor')]
    if loudness is not None:
        chain.append(Effect('loudness', target_lufs=finite(loudness, 'Target loudness', -40, -9), peak_db=-1))
    return {'sample_rate': sample_rate, 'channels': channels, 'bit_depth': bit_depth,
            'bitrate': bitrate, 'start': start, 'duration': duration, 'effects': tuple(chain)}


def show_audio_conversion(frame):
    """Numeric, keyboard-driven offline DSP and conversion of a selected local file."""
    import wx
    import math
    import os
    from uuid import uuid4
    from ..durable_state import local_path
    from ..research.export_file import capture_export_destination
    selected_library = frame.library
    selected_root = local_path(selected_library.root, exists=True)
    def guard():
        if (getattr(frame, '_closing', False) or frame.library is not selected_library
                or local_path(frame.library.root, exists=True) != selected_root):
            raise ValueError('The library changed or closed. Reopen audio conversion.')
    def native_alive():
        return bool(frame) and not getattr(frame, '_closing', False)
    def report_error(exc):
        if native_alive():
            frame.report(str(exc))
    chooser = wx.FileDialog(frame, 'Choose a local audio or video source', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if chooser.ShowModal() != wx.ID_OK or not chooser or not native_alive():
            return
        guard()
        source = Path(chooser.GetPath())
    except Exception as exc:
        report_error(exc)
        return
    finally:
        if chooser:
            chooser.Destroy()
    dialog = wx.Dialog(frame, title='Convert audio and apply effects', size=(760, 750),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    panel = wx.ScrolledWindow(dialog, style=wx.VSCROLL)
    panel.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)
    def entry(label, value):
        layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 5)
        control = wx.TextCtrl(panel, value=value)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        return control
    start = entry('Start in seconds', '0')
    duration = entry('Duration in seconds; empty means remainder', '')
    sample_rate = entry('Output sample rate in Hz, 8000 to 192000; Opus requires 48000', '24000')
    channels = entry('Output channels: 1 for mono, 2 for stereo', '2')
    bit_depth = entry('PCM bit depth: 16 or 24; 32 for WAV or AIFF only', '16')
    bitrate = entry('Target compressed bitrate in kbps, 32 to 512; MP3, AAC, Opus and WMA', '192')
    gain = entry('Gain in decibels, -60 to 12', '0')
    tempo = entry('Playback speed without pitch change, 0.5 to 2', '1')
    loudness = entry('Target loudness in LUFS; empty leaves levels unchanged', '-18')
    effects = wx.CheckBox(panel, label='Apply speech noise reduction and gentle compression')
    effects.SetName('Apply speech noise reduction and gentle compression')
    layout.Add(effects, 0, wx.ALL, 6)
    notice = wx.StaticText(panel, label='The new filename selects its audio format. Bit depth applies to PCM/FLAC; '
        'bitrate applies to the listed compressed encoders. Ogg Vorbis uses the existing quality setting. '
        'The installed runtime verifies the actual rate and channels before saving. Originals stay unchanged.')
    notice.Wrap(690); layout.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
    panel.SetSizer(layout)
    outer = wx.BoxSizer(wx.VERTICAL); outer.Add(panel, 1, wx.EXPAND)
    outer.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 6)
    dialog.SetSizer(outer); panel.FitInside()
    try:
        if dialog.ShowModal() != wx.ID_OK or not dialog or not native_alive():
            return
        guard()
        options = audio_conversion_settings(sample_rate=int(sample_rate.GetValue()),
            channels=int(channels.GetValue()), bit_depth=int(bit_depth.GetValue()), bitrate=int(bitrate.GetValue()),
            start=float(start.GetValue()), duration=float(duration.GetValue()) if duration.GetValue().strip() else None,
            gain=float(gain.GetValue()), tempo=float(tempo.GetValue()),
            loudness=float(loudness.GetValue()) if loudness.GetValue().strip() else None,
            cleanup=effects.GetValue())
    except Exception as exc:
        report_error(exc)
        return
    finally:
        if dialog:
            dialog.Destroy()
    chooser = wx.FileDialog(frame, 'Save converted audio to a new file', defaultFile=source.stem + '-converted.wav',
                            wildcard='Audio (*.wav;*.flac;*.mp3;*.m4a;*.m4b;*.aac;*.ogg;*.opus;*.aiff;*.aif;*.wma)|*.wav;*.flac;*.mp3;*.m4a;*.m4b;*.aac;*.ogg;*.opus;*.aiff;*.aif;*.wma',
                            style=wx.FD_SAVE)
    try:
        if chooser.ShowModal() != wx.ID_OK or not chooser or not native_alive():
            return
        guard()
        output = Path(chooser.GetPath())
        if os.path.lexists(output):
            raise FileExistsError('Audio output already exists. Choose a new filename.')
        approval = capture_export_destination(output, private_root=selected_root)
        if not approval.require_absent:
            raise FileExistsError('Audio output already exists. Choose a new filename.')
    except Exception as exc:
        report_error(exc)
        return
    finally:
        if chooser:
            chooser.Destroy()
    def operation():
        guard()
        if output.suffix.lower() == '.opus' and options['sample_rate'] != 48000:
            raise ValueError('Select 48000 Hz explicitly for Opus output.')
        if output.suffix.lower() == '.flac' and options['bit_depth'] == 32:
            raise ValueError('Select 16 or 24 bits for this FLAC conversion profile.')
        from ..runtime_settings import audio_runtime
        from ..audio.models import AudioCleanupPending
        from ..durable_state import regular
        from ..studio.backup_inputs import _checked_digest, _stamp
        runtime = audio_runtime(output.parent, state_root=selected_root / 'state', input_roots=(source.parent,))
        service = ProductionService(selected_root / 'state' / 'studio')
        cancelled = frame.cancel_event.is_set
        def destination():
            guard()
            if capture_export_destination(output, private_root=selected_root, cancelled=cancelled) != approval:
                raise ValueError('The selected audio destination changed. Choose it again.')
        destination()
        with service._audio_admission(runtime, operation_id='conversion-inspect:' + uuid4().hex,
                                      cancelled=cancelled) as stopping:
            destination()
            inspected = runtime.inspect(source, cancelled=stopping)
        remaining = max(0, inspected.duration - options['start'])
        duration_seconds = remaining if options['duration'] is None else min(remaining, options['duration'])
        speed = next(effect.parameters['factor'] for effect in options['effects'] if effect.kind == 'tempo')
        expected_seconds = duration_seconds / speed
        reserved = 2 * math.ceil((expected_seconds + 1) * options['sample_rate']
            * options['channels'] * (options['bit_depth'] // 8)) + 1024**2 + runtime.budget.reserve_disk_bytes
        result = measurement = saved_identity = None
        warnings = []
        try:
            with service._audio_admission(runtime, operation_id='conversion:' + uuid4().hex,
                    disk_bytes=reserved, disk_path=output.parent, cancelled=cancelled) as stopping:
                destination()
                result = runtime.convert(source, output, **options, cancelled=stopping)
                saved_identity = regular(output)
                # The converter returned its verified committed result. An
                # optional measurement cannot reverse that completed save.
                try:
                    measurement = runtime.measure(output, cancelled=stopping)
                except AudioCleanupPending:
                    # The shared admission retains its lease until this exact
                    # native owner confirms quiescence, even after publication.
                    raise
                except (Exception, KeyboardInterrupt) as exc:
                    warnings.append(f'Loudness measurement did not complete: {exc}')
        except (Exception, KeyboardInterrupt) as exc:
            if result is None or saved_identity is None:
                raise
            warnings.append('The audio is saved and verified; shared resource release needs attention: '
                            + type(exc).__name__ + ': ' + str(exc)[:1000])
        if (_stamp(regular(output)) != _stamp(saved_identity)
                or saved_identity.st_size != result.size
                or _checked_digest(output, saved_identity, lambda: False) != result.sha256):
            raise ValueError('The saved conversion changed during final cleanup.')
        return result, measurement, ' '.join(warnings)
    def complete(result):
        if not native_alive():
            return
        guard()
        info, measured, warning = result
        frame.report(f'Saved {output}. Duration {info.duration:.2f} seconds; '
                     f'{info.sample_rate} Hz; {info.channels} channel(s). '
                     + (f'Measured loudness {measured.integrated_lufs} LUFS. ' if measured is not None else '')
                     + warning)
    frame.run_job('Converting local audio', operation,
                  complete)


def install_studio_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_command_identity, register_menu_command
    from .engine_settings import show_engine_settings
    from .studio_clips import show_clip_editor
    from .studio_pronunciation import show_studio_pronunciations
    from .recording_dialog import show_recording
    from .transcription_dialog import show_transcription
    from .publishing_dialog import show_publishing
    from .audio_metadata_dialog import show_audio_metadata
    menu = wx.Menu()
    for identity, title, shortcut, handler in [
            ('audio.studio', 'Script and audio production...', 'Ctrl+Alt+A', lambda e: show_studio(frame)),
            ('audio.clips', 'Edit scene audio clips...', '', lambda e: show_clip_editor(frame)),
            ('audio.pronunciation', 'Review spoken-line pronunciation...', '', lambda e: show_studio_pronunciations(frame)),
            ('audio.recording', 'Record microphone...', '', lambda e: show_recording(frame)),
            ('audio.publishing', 'Publish synchronized EPUB3 / DAISY...', '', lambda e: show_publishing(frame)),
            ('audio.convert', 'Convert audio and apply effects...', '', lambda e: show_audio_conversion(frame)),
            ('audio.metadata', 'Review audio metadata and cover...', '', lambda e: show_audio_metadata(frame)),
            ('audio.transcription', 'Local transcription...', '', lambda e: show_transcription(frame)),
            ('audio.engines', 'Local voice and music engines...', '', lambda e: show_engine_settings(frame))]:
        item = menu.Append(wx.ID_ANY, title + ('\t' + shortcut if shortcut else ''))
        if identity == 'audio.publishing':
            register_command_identity(frame.menu_bar, item, 'publish synchronized epub3')
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(identity, title, 'main', shortcut), item)
    frame.menu_bar.Append(menu, 'Audio St&udio')
