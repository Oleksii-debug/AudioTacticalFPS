"""Keyboard-first native production workspace; no waveform or drag operation required."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import threading

from ..durable_state import atomic_json, atomic_bytes, read_json
from ..studio.project import Project, parse_script
from ..studio.render import ProductionService
from .operation_progress import OperationProgress, operation_outcome
from ..speech.progress_speech import activate_progress_voice, play_when_quiet

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
    dialog = wx.Dialog(frame, title='Audio Studio — script, voices and production', size=(980, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-studio')
    cancel = threading.Event()
    voices, current_job, alive, busy = (), None, True, False
    draft_dirty = False

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
    progress_label = wx.StaticText(dialog, label='Ready')
    progress_gauge = wx.Gauge(dialog, range=100, style=wx.GA_HORIZONTAL)
    layout.Add(progress_label, 0, wx.EXPAND | wx.ALL, 6)
    layout.Add(progress_gauge, 0, wx.EXPAND | wx.ALL, 6)
    buttons = {}
    for row in ((('save', '&Save (Ctrl+S)'), ('open', '&Open project'), ('saveas', 'Save &as (Ctrl+Shift+S)'),
                 ('roles', 'Edit &roles and voices'), ('help', '&Help')),
                (('render', '&Render / resume (F6)'), ('pause', '&Pause (F8)'), ('jobs', 'Saved &jobs'),
                 ('preview', 'Listen to &output'), ('close', '&Close')),
                (('progress_voice', 'Use voice for progress...'),)):
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
    progress = OperationProgress(dialog, progress_gauge, progress_label, wx_module=wx)
    parent_progress = getattr(frame, 'operation_progress', None)
    if parent_progress is not None and parent_progress.speech_provider is not None:
        try:
            progress.set_speech_provider(parent_progress.speech_provider.copy_selection(), root=dialog.library.root)
        except (ValueError, OSError, RuntimeError) as exc:
            report(f'Select a progress voice again: {exc}')

    def selected_voices():
        selected = voice_choice.GetSelection()
        if 0 <= selected < len(voices):
            # One chosen voice is the safe default; distinct casting is explicit.
            return (voices[selected].id,)
        return ()

    def controls():
        for name in ('render', 'open', 'roles', 'jobs'):
            buttons[name].Enable(not busy)
        buttons['pause'].Enable(busy)
        buttons['preview'].Enable(not busy and current_job is not None)
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
            if observer_available:
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

        def failed_operation(exc, *, worker=False):
            from carr_library.audio.models import AudioCancelled
            interrupted = worker and isinstance(exc, (InterruptedError, AudioCancelled))
            state = 'paused' if interrupted else 'failed'
            if worker and with_progress and job_identity is not None:
                try:
                    saved_state = operation_outcome(service.get(job_identity))
                    if interrupted and saved_state in ('paused', 'cancelled', 'failed'):
                        state = saved_state
                except Exception:
                    pass  # A missing receipt cannot hide the original error.
            if state == 'cancelled':
                observe('cancel', phase + ': cancelled', token=token)
            elif state == 'paused':
                observe('pause', phase + ': paused; completed chunks retained', token=token)
            else:
                observe('fail', phase + ': failed', token=token)
            observe('finalize', token=token)
            report(str(exc), True)

        try:
            future = executor.submit(lambda: operation(measured_phase) if with_progress else operation())
        except Exception as exc:
            finished.set()
            busy = False
            controls()
            failed_operation(exc)
            return

        def finish(done):
            def apply():
                nonlocal busy
                finished.set()
                if not alive:
                    return
                busy = False
                controls()
                try:
                    result = done.result()
                except Exception as exc:
                    failed_operation(exc, worker=True)
                    return
                try:
                    # Completion may verify or publish a final artifact. It is
                    # business work and must finish before any success signal.
                    complete(result)
                except Exception as exc:
                    failed_operation(exc)
                    return
                if not alive:
                    return
                try:
                    outcome = operation_outcome(result)
                except Exception as exc:
                    unavailable(exc)
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

    def select_progress_voice(event=None):
        selected = voice_choice.GetSelection()
        if not 0 <= selected < len(voices):
            return
        root = Path(dialog.library.root)
        voice_id = voices[selected].id

        def activated(provider):
            try:
                progress.set_speech_provider(provider, root=root)
                if parent_progress is not None:
                    parent_progress.set_speech_provider(provider.copy_selection(), root=root)
            except Exception:
                provider.close()
                raise
            progress.settings_dialog()
            if parent_progress is not None:
                parent_progress.reload_preferences()
            report(f'Progress voice selected for this session: {provider.label}. '
                   f'Sound mode: {progress.preferences.mode}.')

        run(lambda: activate_progress_voice(root, voice_id, cancelled=cancel.is_set), activated,
            phase='Selecting a local progress voice')

    def save(event=None):
        nonlocal draft_dirty
        try:
            result = controller.save(editor.GetValue(), title.GetValue(), selected_voices())
            draft_dirty = False
            report(f'Saved project revision {result.revision}. {len(result.scenes)} scenes, {len(result.roles)} roles.', True)
            return result
        except Exception as exc:
            report(f'Project validation failed: {exc} The editable draft was retained if its save succeeded.', True)
            return None

    def changed(event):
        nonlocal draft_dirty, current_job
        draft_dirty = True
        current_job = None
        event.Skip()

    def autosave(event=None):
        nonlocal draft_dirty
        if not draft_dirty or busy:
            return
        try:
            controller.autosave(editor.GetValue(), title.GetValue(), selected_voices())
            draft_dirty = False
        except Exception as exc:
            report(f'Automatic draft save failed: {exc}', True)

    def open_project(event=None):
        chooser = wx.FileDialog(dialog, 'Open project JSON or UTF-8 script',
                                wildcard='Project and script (*.json;*.txt;*.md)|*.json;*.txt;*.md',
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            path = Path(chooser.GetPath())
            if path.stat().st_size > 16 * 1024**2:
                raise ValueError('The script exceeds the editable text budget; split it by chapters.')
            if path.suffix.casefold() == '.json':
                project = Project.from_dict(read_json(path))
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
            chooser.Destroy()

    def save_as(event=None):
        project = save()
        if project is None:
            return
        chooser = wx.FileDialog(dialog, 'Save audio project as', defaultFile='audio-project.json',
                                wildcard='Project JSON (*.json)|*.json', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if chooser.ShowModal() == wx.ID_OK:
                path = Path(chooser.GetPath())
                atomic_json(path, project.to_dict(), replace=path.exists())
                report('Project exported. Voice credentials and source library files are not embedded.', True)
        except Exception as exc:
            report(str(exc), True)
        finally:
            chooser.Destroy()

    def edit_roles(event=None):
        nonlocal current_job
        project = save()
        if project is None:
            return
        chooser = wx.SingleChoiceDialog(dialog, 'Choose a speaking role.', 'Voice and position',
                                        [f'{r.name}; voice {r.voice_id}; pan {r.pan}; gain {r.gain_db} dB' for r in project.roles])
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            index = chooser.GetSelection()
        finally:
            chooser.Destroy()
        if not 0 <= index < len(project.roles):
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
            if detail.ShowModal() != wx.ID_OK:
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
            detail.Destroy()

    def render(event=None):
        nonlocal current_job
        if busy:
            return
        if current_job is None:
            project = save()
            if project is None:
                return
            chooser = wx.DirDialog(dialog, 'Choose the parent folder for new audio outputs')
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                current_job = service.submit(project, Path(chooser.GetPath()),
                    format=formats[format_choice.GetSelection()], stems=stems.GetValue())
            except Exception as exc:
                report(str(exc), True)
                return
            finally:
                chooser.Destroy()
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
                    if review.ShowModal() != wx.ID_YES:
                        return
                    approved_inputs = pending_inputs
                finally:
                    review.Destroy()
            restored = service.get(current_job).get('recovery', {}).get('requires_output_directory') is True
            if restored:
                chooser = wx.DirDialog(dialog, 'Choose a new output folder for the restored production job')
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    service.relocate_restored_job(current_job, Path(chooser.GetPath()))
                finally:
                    chooser.Destroy()
        except Exception as exc:
            report(str(exc), True)
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
        try:
            rows = service.jobs()
            chooser = wx.SingleChoiceDialog(dialog, 'Select a saved job to inspect, resume, or replay.', 'Production jobs',
                [f'{row["project"]["title"]}; {row["status"]}; {row["progress"]}' for row in rows])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not 0 <= index < len(rows):
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

    def preview(event=None):
        try:
            if current_job is None:
                return
            row = service.get(current_job)
            source = Path(row['destination']) / 'master.wav'
            if not source.exists():
                raise ValueError('There is no completed master yet; render or resume the job.')
            from ..speech.player import WavePlayer
            player = getattr(dialog, '_audio_player', None) or WavePlayer()
            dialog._audio_player = player
            progress.register_player(player)
            if parent_progress is not None:
                parent_progress.register_player(player)
            def playing():
                player.play(source)
                report('Playing the rendered master. Close this dialog to stop playback.', True)
            play_when_quiet((progress, parent_progress), playing, schedule=wx.CallLater,
                alive=lambda: alive, on_error=lambda exc: report(f'Playback did not start. {exc}', True))
        except Exception as exc:
            report(str(exc), True)

    def help_text(event=None):
        from .desktop_app import show_text
        show_text(dialog, 'Audio Studio keyboard and script guide', SCRIPT_HELP)

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        autosave()
        cancel.set()
        if current_job is not None and busy:
            service.request_pause(current_job)
        timer.Stop()
        close_notices = []
        if getattr(dialog, '_audio_player', None):
            silence_confirmed = False
            try:
                dialog._audio_player.close()
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
        if busy:
            progress.pause('Audio production paused; completed chunks retained')
        progress.close()
        alive = False
        # Queue owned registry cleanup after the cancelled render/discovery.
        # Keep it off the UI thread and do not tear down a borrowed registry.
        executor.submit(service.close)
        executor.shutdown(wait=False, cancel_futures=False)
        dialog.EndModal(wx.ID_CLOSE)

    for name, callback in {'save': save, 'open': open_project, 'saveas': save_as, 'roles': edit_roles,
                           'render': render, 'pause': lambda event: cancel.set(), 'jobs': jobs,
                           'preview': preview, 'help': help_text, 'close': close,
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
        nonlocal voices, draft_dirty
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
        report(f'{len(voices)} installed voices available. Enter a script and choose roles, then Render.'
               if voices else 'No local voice is installed. Configure a local SAPI/Piper voice before narration.', True)
        editor.SetFocus()

    run(lambda: service.discover_voices(cancelled=cancel.is_set).voices(), loaded,
        phase='Checking installed local voices')
    try:
        dialog.ShowModal()
    finally:
        if alive:
            close()
        dialog.Destroy()


def show_audio_conversion(frame):
    """Numeric, keyboard-driven offline DSP and conversion of a selected local file."""
    import wx
    from ..audio import AudioRuntime, Effect
    chooser = wx.FileDialog(frame, 'Choose a local audio or video source', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return
        source = Path(chooser.GetPath())
    finally:
        chooser.Destroy()
    dialog = wx.Dialog(frame, title='Convert audio and apply effects', size=(700, 550),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    def entry(label, value):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 5)
        control = wx.TextCtrl(dialog, value=value)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        return control
    start = entry('Start in seconds', '0')
    duration = entry('Duration in seconds; empty means remainder', '')
    gain = entry('Gain in decibels, -60 to 12', '0')
    tempo = entry('Playback speed without pitch change, 0.5 to 2', '1')
    loudness = entry('Target loudness in LUFS; empty leaves levels unchanged', '-18')
    effects = wx.CheckBox(dialog, label='Apply speech noise reduction and gentle compression')
    layout.Add(effects, 0, wx.ALL, 6)
    layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 6)
    dialog.SetSizer(layout)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
        start_value, duration_value = float(start.GetValue()), float(duration.GetValue()) if duration.GetValue().strip() else None
        gain_value, tempo_value = float(gain.GetValue()), float(tempo.GetValue())
        if not -60 <= gain_value <= 12 or not .5 <= tempo_value <= 2:
            raise ValueError('Gain or speed is outside the displayed range.')
        chain = [Effect('gain', gain_db=gain_value), Effect('tempo', factor=tempo_value)]
        if effects.GetValue():
            chain += [Effect('denoise'), Effect('compressor')]
        if loudness.GetValue().strip():
            chain.append(Effect('loudness', target_lufs=float(loudness.GetValue()), peak_db=-1))
    except Exception as exc:
        frame.report(str(exc))
        return
    finally:
        dialog.Destroy()
    chooser = wx.FileDialog(frame, 'Save converted audio to a new file', defaultFile=source.stem + '-converted.wav',
                            wildcard='Audio (*.wav;*.flac;*.mp3;*.m4a;*.ogg;*.opus)|*.wav;*.flac;*.mp3;*.m4a;*.ogg;*.opus',
                            style=wx.FD_SAVE)
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return
        output = Path(chooser.GetPath())
    finally:
        chooser.Destroy()
    def operation():
        from ..runtime_settings import audio_runtime
        runtime = audio_runtime(output.parent, state_root=frame.library.root / 'state', input_roots=(source.parent,))
        result = runtime.convert(source, output, start=start_value, duration=duration_value,
                                  effects=chain, cancelled=frame.cancel_event.is_set)
        measurement = runtime.measure(output, cancelled=frame.cancel_event.is_set)
        return result, measurement
    frame.run_job('Converting local audio', operation,
        lambda result: frame.report(f'Saved {output}. Duration {result[0].duration:.2f} seconds. '
                                    f'Measured loudness {result[1].integrated_lufs} LUFS.'))


def install_studio_actions(frame):
    import wx
    from .engine_settings import show_engine_settings
    from .studio_clips import show_clip_editor
    from .publishing_dialog import show_publishing
    menu = wx.Menu()
    for title, handler in [('Script and audio production...\tCtrl+Alt+A', lambda e: show_studio(frame)),
                            ('Edit scene audio clips...', lambda e: show_clip_editor(frame)),
                            ('Publish synchronized EPUB3...', lambda e: show_publishing(frame)),
                            ('Convert audio and apply effects...', lambda e: show_audio_conversion(frame)),
                            ('Local voice and music engines...', lambda e: show_engine_settings(frame))]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.menu_bar.Append(menu, 'Audio St&udio')
