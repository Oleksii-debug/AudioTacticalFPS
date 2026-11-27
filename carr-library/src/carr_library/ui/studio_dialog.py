"""Keyboard-first native production workspace; no waveform or drag operation required."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import math
from pathlib import Path
import threading
from uuid import uuid4

from ..durable_state import atomic_json, atomic_bytes, local_path, read_json
from ..studio.project import Project, parse_script
from ..studio.render import ProductionService
from ..progress import ProgressError, ProgressState, bind_progress, operation_outcome
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
[Pronunciation: spoken text for the preceding line]
[Вимова: варіант попередньої репліки з наголосами]
[Delivery: rate=2; volume=85; pitch=55; gain=-4; pause=0.4]
[Подача: rate=2; volume=85; gain=-4; pause=0.4]
Apply any of these settings to the preceding line. Native speech controls require voice support.

Music moods: calm, suspense, hopeful, sad, energetic.
Default music is locally composed procedural instrumental, with no remote request.
After [Music: suspense; engine=ace-step], use [Music prompt: quiet cello and rain].
Role voices, gain and position can be edited before rendering. Source prose is never
rewritten automatically. Generated scripts are separate from saved quotations.
Ctrl+S saves the project and draft. Ctrl+Shift+S exports project JSON.
F6 renders or resumes. F8 pauses after cancelling the current audio process.
WAV master, chapter files, speaker stems, subtitles and the source script are exported
to a new project folder. Listen to scene or line plays the selected part of a completed master.
Unavailable models/codecs report a clear error.'''


def _keep_spoken_edits(previous, revised):
    """Retain separately saved spoken text when the script keeps its source line."""
    if not previous.original_script or not any(
            line.spoken_text for scene in previous.scenes for line in scene.lines):
        return revised
    try:
        original = parse_script(previous.original_script, mode=previous.mode, project_id=previous.id)
    except ValueError:
        # Imported projects may retain source text from before the current script grammar.
        return revised
    raw_roles = {role.id: role.name.casefold() for role in original.roles}
    saved_roles = {role.id: role.name.casefold() for role in previous.roles}
    fresh_roles = {role.id: role.name.casefold() for role in revised.roles}
    raw_scenes = {(scene.id, scene.title): scene for scene in original.scenes}
    saved_scenes = {(scene.id, scene.title): scene for scene in previous.scenes}

    def key(line, roles):
        return roles[line.role], line.text, line.spoken_text

    scenes = []
    for scene in revised.scenes:
        identity = (scene.id, scene.title)
        raw, saved = raw_scenes.get(identity), saved_scenes.get(identity)
        if raw is None or saved is None:
            scenes.append(scene)
            continue
        source_lines = {line.id: line for line in raw.lines}
        source_keys = Counter(key(line, raw_roles) for line in raw.lines)
        fresh_keys = Counter(key(line, fresh_roles) for line in scene.lines)
        overrides = {}
        for line in saved.lines:
            original_line = source_lines.get(line.id)
            if (original_line is None or not original_line.spoken_text
                    or not line.spoken_text or line.spoken_text == original_line.spoken_text
                    or line.text != original_line.text
                    or saved_roles[line.role] != raw_roles[original_line.role]):
                continue
            anchor = key(original_line, raw_roles)
            if not fresh_keys[anchor]:
                continue
            if source_keys[anchor] != 1 or fresh_keys[anchor] != 1:
                raise ValueError('Make the edited spoken line unique before saving the script.')
            overrides[anchor] = line.spoken_text
        if overrides:
            scene = replace(scene, lines=tuple(
                replace(line, spoken_text=overrides.get(key(line, fresh_roles), line.spoken_text))
                for line in scene.lines))
        scenes.append(scene)
    return replace(revised, scenes=tuple(scenes))


def _playback_ranges(result, *, scene_id=None):
    """Read only the producer's measured playback ranges, in scene or line order."""
    if scene_id is None:
        rows = ((row['id'], row['title'], row) for row in result.get('chapters', ()))
    else:
        rows = ((row['line'], row['text'], row) for row in result.get('lines', ())
                if row['scene'] == scene_id)
    duration = result.get('duration')
    ranges = []
    for index, (identity, label, row) in enumerate(rows, 1):
        start, end = row['start'], row['end']
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (start, end, duration)) or not 0 <= start < min(end, duration)
                or end > duration + 1e-6):
            raise ValueError('The completed master has invalid playback timing.')
        end = min(end, duration)
        spoken_label = ' '.join(label.split())[:120]
        ranges.append((identity, f'{index}. {spoken_label}', start, end))
    return ranges


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
            project = _keep_spoken_edits(previous_project, project)
            attached = {scene.id: scene for scene in previous_project.scenes if scene.cues or scene.effects}
            current = {scene.id: scene for scene in project.scenes}
            if any(identity not in current or current[identity].title != old.title
                   for identity, old in attached.items()):
                raise ValueError('A scene with attached audio changed its identity. '
                                 'Move or remove its clips in the scene audio editor before changing scene structure.')
            scenes = tuple(replace(scene, cues=attached[scene.id].cues,
                                   effects=attached[scene.id].effects)
                           if scene.id in attached else scene for scene in project.scenes)
            previous = {role.name.casefold(): role for role in self.project.roles}
            current_roles = {role.name.casefold() for role in project.roles}
            if any(role.effects and name not in current_roles for name, role in previous.items()):
                raise ValueError('A speaking role with saved effects was removed. Clear its effects in the scene editor before removing that role.')
            roles = tuple(replace(role, voice_id=previous[role.name.casefold()].voice_id,
                                  language=previous[role.name.casefold()].language,
                                  pan=previous[role.name.casefold()].pan,
                                  gain_db=previous[role.name.casefold()].gain_db,
                                  rate=previous[role.name.casefold()].rate,
                                  volume=previous[role.name.casefold()].volume,
                                  effects=previous[role.name.casefold()].effects)
                          if role.name.casefold() in previous else role for role in project.roles)
            project = replace(project, roles=roles, scenes=scenes, effects=previous_project.effects,
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
    saved_signature = None
    playback_generation = 0
    owned_progress_voices = set()

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
                (('preview_part', 'Listen to scene or &line'), ('stop_playback', 'Stop playback'),
                 ('progress_voice', 'Use voice for progress...'))):
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

    # Reuse the same native observer and state contract as the main window.
    # Its modal view keeps query/mute controls reachable while Studio is busy.
    from .progress_panel import NativeProgressPanel
    dialog.library, dialog.host, dialog.layout = frame.library, dialog, layout
    dialog.status = status
    dialog.report = lambda message: report(message, True)
    dialog.working = False
    parent_progress = getattr(frame, 'progress_panel', None)
    selected_provider = getattr(parent_progress, 'speech_provider', None)
    dialog.progress_speech_provider = None
    if callable(getattr(selected_provider, 'copy_selection', None)):
        try:
            dialog.progress_speech_provider = selected_provider.copy_selection()
        except (ValueError, OSError, RuntimeError) as exc:
            report(f'Select a progress voice again: {exc}')
    dialog.progress_media_busy = lambda: (
        getattr(getattr(dialog, '_audio_player', None), 'state', 'stopped') == 'playing')
    progress = NativeProgressPanel(dialog)
    dialog.progress_panel = dialog.operation_progress = progress
    if parent_progress is not None:
        progress.hub = parent_progress.hub
    layout.Detach(progress.panel)
    layout.Insert(layout.GetItemCount() - 2, progress.panel, 0, wx.EXPAND | wx.ALL, 6)
    progress.gauge.SetName('Audio Studio operation progress')
    progress.text.SetName('Audio Studio current progress and phase')
    progress.query_button.SetName('Query Audio Studio progress')

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
        for name in ('preview', 'preview_part'):
            buttons[name].Enable(not busy and current_job is not None)
        selected = voice_choice.GetSelection()
        buttons['progress_voice'].Enable(not busy and 0 <= selected < len(voices)
                                          and 'volume' in voices[selected].capabilities)
        editor.Enable(not busy)
        title.Enable(not busy)
        voice_choice.Enable(not busy)
        format_choice.Enable(not busy)
        stems.Enable(not busy)

    def run(operation, complete, *, phase='Audio Studio operation', with_progress=False):
        nonlocal busy
        if busy or not alive:
            return
        busy = True
        dialog.working = True
        cancel.clear()
        controls()
        task, observer_error = None, None
        job_identity = current_job
        finished = threading.Event()

        def observe(callback):
            nonlocal observer_error
            try:
                return callback()
            except Exception as exc:
                observer_error = exc

        task = observe(lambda: progress.begin(phase))

        def measured(message):
            if finished.is_set():
                return
            description = ' '.join(str(message).split())[:512] or phase
            if task is not None and not cancel.is_set() and task.snapshot().state == ProgressState.RUNNING:
                observe(lambda: task.update(phase=description))
            def show_phase():
                if alive and not finished.is_set():
                    report(description)
            observe(lambda: wx.CallAfter(show_phase))

        def execute():
            if task is None:
                return operation(measured) if with_progress else operation()
            with bind_progress(task):
                return operation(measured) if with_progress else operation()

        def finish_progress(outcome):
            if observer_error is None:
                observe(lambda: progress.finish(task, outcome, phase + ': ' + outcome))
            if observer_error is not None:
                warning = 'Work returned ' + outcome + '; progress feedback is unavailable: ' + str(observer_error)
                observe(lambda: progress.reporting_unavailable(task, outcome, warning))

        def failed_operation(exc, *, worker=False):
            from carr_library.audio.models import AudioCancelled
            interrupted = worker and isinstance(exc, (InterruptedError, AudioCancelled))
            outcome = 'paused' if interrupted else 'failed'
            if interrupted and with_progress and job_identity is not None:
                try:
                    saved = operation_outcome(service.get(job_identity))
                    if saved in ('paused', 'cancelled', 'failed'):
                        outcome = saved
                except Exception:
                    pass
            finish_progress(outcome)
            if alive:
                report(str(exc), True)

        def finish(done):
            def apply():
                nonlocal busy
                finished.set()
                busy = False
                dialog.working = False
                if alive:
                    controls()
                try:
                    result = done.result()
                except Exception as exc:
                    failed_operation(exc, worker=True)
                    return
                outcome = observe(lambda: operation_outcome(result))
                try:
                    if alive and outcome in ('succeeded', None):
                        complete(result)
                    elif alive:
                        report(phase + ': ' + outcome + '. Saved work is retained.', True)
                except Exception as exc:
                    failed_operation(exc)
                    return
                finish_progress(outcome or 'unavailable')
            wx.CallAfter(apply)

        try:
            future = executor.submit(execute)
        except Exception as exc:
            finished.set()
            busy = False
            dialog.working = False
            if alive:
                controls()
            failed_operation(exc)
            return
        future.add_done_callback(finish)
        return future

    def select_progress_voice(event=None):
        selected = voice_choice.GetSelection()
        if not 0 <= selected < len(voices):
            return
        root, voice_id = Path(dialog.library.root), voices[selected].id

        def activate():
            provider = activate_progress_voice(root, voice_id)
            owned_progress_voices.add(provider)
            return provider

        def activated(provider):
            try:
                progress.set_speech_provider(provider, root=root)
                owned_progress_voices.discard(provider)
                if parent_progress is not None:
                    copied = provider.copy_selection()
                    try:
                        parent_progress.set_speech_provider(copied, root=root)
                    except Exception:
                        copied.close()
                        raise
            except Exception:
                provider.close()
                raise
            progress.settings()
            if parent_progress is not None:
                parent_progress.preferences = parent_progress.store.load()
                parent_progress.feedback.set_preferences(
                    parent_progress._effective_preferences(parent_progress.preferences))
                parent_progress.mute.SetValue(parent_progress.preferences.muted)
                parent_progress.refresh_audio_state()
            report(f'Progress voice selected for this session: {provider.label}. '
                   f'Sound mode: {progress.preferences.mode}.')

        run(activate, activated, phase='Selecting a local progress voice')

    def save(event=None):
        nonlocal draft_dirty, saved_signature
        try:
            result = controller.save(editor.GetValue(), title.GetValue(), selected_voices())
            draft_dirty = False
            saved_signature = (editor.GetValue(), title.GetValue(), selected_voices())
            report(f'Saved project revision {result.revision}. {len(result.scenes)} scenes, {len(result.roles)} roles.', True)
            return result
        except Exception as exc:
            report(f'Project validation failed: {exc} The editable draft was retained if its save succeeded.', True)
            return None

    def changed(event):
        nonlocal draft_dirty, current_job
        draft_dirty = True
        current_job = None
        controls()
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
                controller.service.review_project_inputs(project)
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
        nonlocal current_job, draft_dirty, saved_signature
        if busy:
            return
        try:
            selection = format_choice.GetSelection()
            if not 0 <= selection < len(formats):
                raise ValueError('Choose a final audio format.')
            chosen_format, chosen_stems = formats[selection], stems.GetValue()
            previous = service.get(current_job) if current_job is not None else None
            if previous is not None and (previous['format'] not in formats or type(previous['stems']) is not bool):
                raise ValueError('The saved job has unsupported output settings.')
            changed_options = previous is not None and (previous['format'], previous['stems']) != (chosen_format, chosen_stems)
            if previous is None or changed_options:
                snapshot = Project.from_dict(previous['project']) if changed_options else None
                message = ('Output settings changed. Choose a folder for a NEW production job from the saved revision. '
                           'A historical revision is copied to a separate project; the previous job and project are retained.'
                           if changed_options else 'Choose the parent folder for new audio outputs')
                chooser = wx.DirDialog(dialog, message)
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        if changed_options:
                            format_choice.SetSelection(formats.index(previous['format']))
                            stems.SetValue(previous['stems'])
                            report('New output cancelled. Previous job and its output settings retained.', True)
                        return
                    if snapshot is None:
                        project = save()
                        if project is None:
                            return
                    else:
                        # Output options alone must not reparse a saved script or
                        # adopt the voices/cues of a newer editable revision.
                        latest = service.projects.load(snapshot.id)
                        project = (snapshot if latest.to_dict() == snapshot.to_dict() else
                                   replace(snapshot, id=uuid4().hex, revision=1))
                    current_job = service.submit(project, Path(chooser.GetPath()),
                        format=chosen_format, stems=chosen_stems)
                    if snapshot is not None:
                        controller.project = project
                        draft_dirty = True
                        saved_signature = (editor.GetValue(), title.GetValue(), selected_voices())
                finally:
                    chooser.Destroy()
        except Exception as exc:
            report(str(exc), True)
            return
        approved_inputs = ()
        try:
            pending_inputs = service.restored_inputs_to_review(current_job)
            if pending_inputs:
                review = wx.MessageDialog(dialog,
                    'This project references audio inputs that need approval on this machine. Review their local paths:\n\n'
                    + '\n'.join(pending_inputs)
                    + '\n\nAllow this project to read these local files and include them in future backups?',
                    'Review audio inputs', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
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
        if busy:
            return
        try:
            rows = service.jobs()
            chooser = wx.SingleChoiceDialog(dialog, 'Select a saved job to inspect, resume, or replay.', 'Production jobs',
                [f'{row["project"]["title"]}; {row["status"]}; {row["progress"]}; '
                 f'{row["format"]}; separate stems: {"Yes" if row["stems"] else "No"}; job {row["id"]}'
                 for row in rows])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not 0 <= index < len(rows):
                return
            row = rows[index]
            if row['format'] not in formats or type(row['stems']) is not bool:
                raise ValueError('The saved job has unsupported output settings.')
            project = Project.from_dict(row['project'])
            controller.project = service.projects.load(project.id)
            editor.ChangeValue(project.original_script)
            title.ChangeValue(project.title)
            format_choice.SetSelection(formats.index(row['format']))
            stems.SetValue(row['stems'])
            current_job = row['id']
            report(f'{row["status"]}. {row["error"]}\nFinal format: {row["format"]}; separate stems: '
                   + ('Yes' if row['stems'] else 'No')
                   + '. Choose Render / resume to continue or Listen to output after completion.', True)
            controls()
        except Exception as exc:
            report(str(exc), True)

    def master_to_play():
        if current_job is None:
            raise ValueError('Choose a completed production job first.')
        row = service.get(current_job)
        if row['status'] != 'complete':
            raise ValueError('There is no completed master yet. Render or resume the job.')
        directory = local_path(row['destination'], exists=True)
        source = local_path(directory / 'master.wav', root=directory, exists=True)
        receipts = [item for item in row['artifacts'] if Path(item['path']) == source]
        if len(receipts) != 1 or source.stat().st_size != receipts[0]['size']:
            raise ValueError('The rendered master no longer matches its saved output receipt.')
        return source, row['result']

    def play_output(source, message, *, start=0, end=None):
        nonlocal playback_generation
        from ..speech.player import WavePlayer
        player = getattr(dialog, '_audio_player', None) or WavePlayer()
        dialog._audio_player = player
        progress.register_player(player)
        if parent_progress is not None:
            parent_progress.register_player(player)
        playback_generation += 1
        generation = playback_generation

        def playing():
            player.play(source, start_seconds=start, end_seconds=end)
            report(message, True)
        play_when_quiet((progress, parent_progress), playing, schedule=wx.CallLater,
            alive=lambda: alive and generation == playback_generation,
            on_error=lambda exc: report(f'Playback did not start. {exc}', True))

    def preview(event=None):
        try:
            source, _ = master_to_play()
            play_output(source, 'Playing the rendered master. Use Stop playback to end listening.')
        except Exception as exc:
            report(str(exc), True)

    def choose_playback_item(message, choices):
        chooser = wx.SingleChoiceDialog(dialog, message, 'Listen to completed production', choices)
        try:
            return chooser.GetSelection() if chooser.ShowModal() == wx.ID_OK else -1
        finally:
            chooser.Destroy()

    def preview_part(event=None):
        try:
            source, result = master_to_play()
            chapters = _playback_ranges(result)
            if not chapters:
                raise ValueError('The saved job has no measured scene timing.')
            selected = choose_playback_item('Choose a scene.', [item[1] for item in chapters])
            if selected < 0:
                return
            scene_id, title, start, end = chapters[selected]
            lines = _playback_ranges(result, scene_id=scene_id)
            selected = choose_playback_item('Listen to the whole scene or choose a spoken line.',
                                            ['Whole scene', *(item[1] for item in lines)])
            if selected < 0:
                return
            if selected > 0:
                _, title, start, end = lines[selected - 1]
            play_output(source, f'Playing {title}.', start=start, end=end)
        except Exception as exc:
            report(str(exc), True)

    def stop_playback(event=None):
        nonlocal playback_generation
        playback_generation += 1
        player = getattr(dialog, '_audio_player', None)
        if player is not None:
            try:
                player.stop()
                report('Playback stopped.', True)
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
            progress.request_cancel()
        progress.close()
        alive = False
        from .worker_cleanup import close_after_worker
        class StudioResources:
            def close(self):
                try:
                    for provider in owned_progress_voices:
                        provider.close()
                finally:
                    service.close()
        close_after_worker(executor, StudioResources())
        dialog.EndModal(wx.ID_CLOSE)

    for name, callback in {'save': save, 'open': open_project, 'saveas': save_as, 'roles': edit_roles,
                           'render': render, 'pause': lambda event: cancel.set(), 'jobs': jobs,
                           'preview': preview, 'preview_part': preview_part, 'stop_playback': stop_playback,
                           'help': help_text, 'close': close,
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
    def voice_changed(event):
        changed(event)
        controls()
    voice_choice.Bind(wx.EVT_CHOICE, voice_changed)
    dialog.Bind(wx.EVT_CLOSE, close)
    timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, autosave, timer)
    timer.Start(4000)

    def loaded(result):
        nonlocal voices, draft_dirty, saved_signature
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
        return bool(editor.GetValue()) and (editor.GetValue(), title.GetValue(), selected_voices()) != saved_signature
    def save_registered(completed):
        if busy:
            completed(SaveResult(False, 'Audio production is running; pause it before saving the project.'))
        else:
            completed(SaveResult(save() is not None, 'Project validation or saving did not complete.'))
    unregister = (coordinator.register('studio.' + str(id(controller)), 'Audio Studio project',
                    dirty=unsaved, save=save_registered, focus=editor.SetFocus)
                  if coordinator is not None else lambda: None)
    run(lambda: service.discover_voices(cancelled=cancel.is_set).voices(), loaded,
        phase='Checking installed local voices')
    try:
        dialog.ShowModal()
    finally:
        unregister()
        if alive:
            close()
        dialog.Destroy()


def show_audio_conversion(frame, *, batch=False):
    """Numeric offline DSP with an optional ordered, saved effect chain."""
    import wx
    from ..audio import Effect
    from ..audio.effect_presets import checked_chain
    from ..audio.models import integer
    from ..durable_state import local_path
    library_root = local_path(frame.library.root, exists=True)
    chooser = wx.FileDialog(frame, 'Choose local audio or video sources' if batch else 'Choose a local audio or video source',
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | (wx.FD_MULTIPLE if batch else 0))
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return
        sources = tuple(Path(value).absolute() for value in chooser.GetPaths()) if batch else ()
        if batch and not 1 <= len(sources) <= 64:
            raise ValueError('Choose one to 64 local audio/video files for a batch.')
        source = sources[0] if batch else local_path(chooser.GetPath(), exists=True)
    finally:
        chooser.Destroy()
    dialog = wx.Dialog(frame, title='Convert audio batch and apply effects' if batch else 'Convert audio and apply effects', size=(760, 720),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    outer = wx.BoxSizer(wx.VERTICAL)
    panel = wx.ScrolledWindow(dialog)
    panel.SetScrollRate(0, 16)
    layout = wx.BoxSizer(wx.VERTICAL)
    def entry(label, value):
        layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 5)
        control = wx.TextCtrl(panel, value=value)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        return control
    start = entry('Start in seconds', '0')
    duration = entry('Duration in seconds; empty means remainder', '')
    output_rate = entry('Output sample rate in Hz, 8000 to 192000', '24000')
    output_channels = entry('Output channels, 1 or 2', '2')
    output_depth = entry('PCM bit depth, 16, 24 or 32', '16')
    output_bitrate = entry('Encoding bitrate in kbps for MP3, AAC, Opus and WMA', '192')
    batch_format = batch_resume = None
    if batch:
        from ..audio.conversion_batch import FORMATS
        layout.Add(wx.StaticText(panel, label='Batch output format'), 0, wx.ALL, 5)
        batch_format = wx.Choice(panel, choices=list(FORMATS))
        batch_format.SetName('Batch output format')
        batch_format.SetSelection(0)
        layout.Add(batch_format, 0, wx.EXPAND | wx.ALL, 5)
        batch_resume = wx.CheckBox(panel, label='Resume this exact saved batch; keep completed matching files')
        batch_resume.SetName('Resume matching audio batch')
        layout.Add(batch_resume, 0, wx.ALL, 5)
    gain = entry('Gain in decibels, -60 to 12', '0')
    tempo = entry('Playback speed without pitch change, 0.5 to 2', '1')
    loudness = entry('Target loudness in LUFS; empty leaves levels unchanged', '-18')
    effects = wx.CheckBox(panel, label='Apply speech noise reduction and gentle compression')
    effects.SetName('Apply speech noise reduction and gentle compression')
    layout.Add(effects, 0, wx.ALL, 6)
    advanced = wx.Button(panel, label='Edit ordered effect chain and presets')
    advanced.SetName('Edit ordered effect chain and presets')
    basic = wx.Button(panel, label='Use basic effect controls')
    basic.SetName('Use basic effect controls')
    basic.Disable()
    layout.Add(advanced, 0, wx.ALL, 6)
    layout.Add(basic, 0, wx.ALL, 6)
    status = wx.TextCtrl(panel, value='Basic controls are active. Choose output settings before reviewing an ordered chain.',
                         style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Audio conversion effect settings')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 6)
    panel.SetSizer(layout)
    outer.Add(panel, 1, wx.EXPAND | wx.ALL, 5)
    outer.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 6)
    dialog.SetSizer(outer)
    advanced_chain = None
    alive, pending = True, False
    original_controls = (gain, tempo, loudness, effects)

    def current_root():
        if local_path(frame.library.root, exists=True) != library_root:
            raise ValueError('The active library changed. Open audio conversion again.')

    def output_values():
        rate = integer(int(output_rate.GetValue()), 'Output sample rate', 8000, 192000)
        channels = integer(int(output_channels.GetValue()), 'Output channels', 1, 2)
        depth = int(output_depth.GetValue())
        if depth not in (16, 24, 32):
            raise ValueError('PCM bit depth must be 16, 24 or 32.')
        bitrate = integer(int(output_bitrate.GetValue()), 'Encoding bitrate', 32, 512)
        return rate, channels, depth, bitrate

    def basic_chain():
        gain_value, tempo_value = float(gain.GetValue()), float(tempo.GetValue())
        if not -60 <= gain_value <= 12 or not .5 <= tempo_value <= 2:
            raise ValueError('Gain or speed is outside the displayed range.')
        values = [Effect('gain', gain_db=gain_value), Effect('tempo', factor=tempo_value)]
        if effects.GetValue():
            values += [Effect('denoise'), Effect('compressor')]
        if loudness.GetValue().strip():
            values.append(Effect('loudness', target_lufs=float(loudness.GetValue()), peak_db=-1))
        return checked_chain(values, output_values()[0])

    def controls():
        for control in original_controls:
            control.Enable(not pending and advanced_chain is None)
        for control in (output_rate, output_channels, output_depth, output_bitrate,
                        *((batch_format, batch_resume) if batch else ())):
            control.Enable(not pending)
        advanced.Enable(not pending)
        basic.Enable(not pending and advanced_chain is not None)
        dialog.FindWindowById(wx.ID_OK).Enable(not pending)

    def use_basic(_event=None):
        nonlocal advanced_chain
        advanced_chain = None
        controls()
        status.ChangeValue('Basic controls are active. Choose output settings before reviewing an ordered chain.')
        gain.SetFocus()

    def edit_chain(_event=None):
        nonlocal pending
        if pending or getattr(frame, 'working', False):
            status.ChangeValue('Wait for the current operation or cancel it before reviewing effects.')
            return
        try:
            current_root()
            selected_rate = output_values()[0]
            frozen = advanced_chain if advanced_chain is not None else basic_chain()
            checked_chain(frozen, selected_rate)
        except Exception as exc:
            status.ChangeValue(str(exc))
            status.SetFocus()
            return
        pending = True
        controls()
        status.ChangeValue('Checking installed local audio effects…')

        def inspect():
            from ..runtime_settings import audio_runtime
            runtime = audio_runtime(library_root / 'state' / 'audio-conversion',
                                    state_root=library_root / 'state')
            if frame.cancel_event.is_set():
                raise InterruptedError('Effect review cancelled.')
            available = runtime.capabilities()['effects']
            if frame.cancel_event.is_set():
                raise InterruptedError('Effect review cancelled.')
            return available

        def failed(error):
            nonlocal pending
            if not alive:
                return
            pending = False
            controls()
            status.ChangeValue(f'Effect review did not open: {error}')

        def reviewed(available):
            nonlocal pending, advanced_chain
            if not alive:
                return
            try:
                current_root()
                from .effects_dialog import show_effect_chain
                value = show_effect_chain(dialog, library_root, frozen,
                                          sample_rate=selected_rate, available=available)
                if value is not None:
                    advanced_chain = value
                    status.ChangeValue(f'Ordered chain active: {len(value)} effects. Basic controls are inactive.')
                else:
                    status.ChangeValue('Effect review cancelled; the previous settings are retained.')
            finally:
                pending = False
                if alive:
                    controls()
                    advanced.SetFocus()

        try:
            frame.run_job('Checking local audio effects', inspect, reviewed, failed)
        except Exception as exc:
            failed(exc)

    advanced.Bind(wx.EVT_BUTTON, edit_chain)
    basic.Bind(wx.EVT_BUTTON, use_basic)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
        current_root()
        if pending:
            raise ValueError('Wait for the effect review to finish before converting audio.')
        start_value = float(start.GetValue())
        duration_value = float(duration.GetValue()) if duration.GetValue().strip() else None
        chain = advanced_chain if advanced_chain is not None else basic_chain()
        rate_value, channels_value, depth_value, bitrate_value = output_values()
        checked_chain(chain, rate_value)
        selected_format = batch_format.GetStringSelection() if batch else None
        resume_batch = bool(batch_resume.GetValue()) if batch else False
    except Exception as exc:
        frame.report(str(exc))
        return
    finally:
        alive = False
        dialog.Destroy()
    if batch:
        from .audio_conversion_dialog import start_audio_batch
        try:
            current_root()
            start_audio_batch(frame, sources, format=selected_format, resume=resume_batch,
                start=start_value, duration=duration_value, effects=chain, sample_rate=rate_value,
                channels=channels_value, bit_depth=depth_value, bitrate=bitrate_value)
        except Exception as exc:
            frame.report('Audio batch: ' + str(exc))
        return
    chooser = wx.FileDialog(frame, 'Save converted audio to a new file', defaultFile=source.stem + '-converted.wav',
                            wildcard='Audio (*.wav;*.flac;*.mp3;*.m4a;*.m4b;*.ogg;*.opus;*.aac;*.aiff;*.wma)|*.wav;*.flac;*.mp3;*.m4a;*.m4b;*.ogg;*.opus;*.aac;*.aiff;*.wma',
                            style=wx.FD_SAVE)
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return
        output = local_path(chooser.GetPath())
        current_root()
    finally:
        chooser.Destroy()
    def operation():
        from ..runtime_settings import audio_runtime
        # The exact selected library, input and immutable effect tuple were
        # captured before queueing; the worker reads no mutable wx controls.
        current_root()
        runtime = audio_runtime(output.parent, state_root=library_root / 'state', input_roots=(source.parent,))
        result = runtime.convert(source, output, start=start_value, duration=duration_value,
                                  effects=chain, sample_rate=rate_value, channels=channels_value,
                                  bit_depth=depth_value, bitrate=bitrate_value,
                                  cancelled=frame.cancel_event.is_set)
        measurement = runtime.measure(output, cancelled=frame.cancel_event.is_set)
        return result, measurement
    frame.run_job('Converting local audio', operation,
        lambda result: frame.report(f'Saved {output}. Duration {result[0].duration:.2f} seconds. '
                                    f'Measured loudness {result[1].integrated_lufs} LUFS.'))


def install_studio_actions(frame):
    import wx
    from .engine_settings import show_engine_settings
    from .studio_clips import show_clip_editor
    from .studio_pronunciation import show_studio_pronunciations
    from .audio_metadata_dialog import show_audio_metadata
    from .publishing_dialog import show_publishing
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu = wx.Menu()
    for identity, title, shortcut, handler in [
            ('audio.studio', 'Script and audio production...', 'Ctrl+Alt+A', lambda e: show_studio(frame)),
            ('audio.clips', 'Edit scene audio clips...', '', lambda e: show_clip_editor(frame)),
            ('audio.pronunciation', 'Review spoken-line pronunciation...', '', lambda e: show_studio_pronunciations(frame)),
            ('audio.publish_epub', 'Publish synchronized EPUB3...', '', lambda e: show_publishing(frame)),
            ('audio.publish_daisy', 'Publish DAISY 2.02...', '', lambda e: show_publishing(frame, initial_format='daisy-2.02')),
            ('audio.convert', 'Convert audio and apply effects...', '', lambda e: show_audio_conversion(frame)),
            ('audio.convert_batch', 'Convert a batch of audio files...', '', lambda e: show_audio_conversion(frame, batch=True)),
            ('audio.metadata', 'Review audio metadata and cover...', '', lambda e: show_audio_metadata(frame)),
            ('audio.engines', 'Local voice and music engines...', '', lambda e: show_engine_settings(frame))]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(identity, title, 'main', shortcut), item)
    frame.menu_bar.Append(menu, 'Audio St&udio')
    from .recording_dialog import install_recording_actions
    install_recording_actions(frame)
