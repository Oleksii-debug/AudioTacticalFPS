"""Keyboard-first native production workspace; no waveform or drag operation required."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import threading

from ..durable_state import atomic_json, encode, read_json
from ..research.export_file import write_binary_export
from .export_destination import confirm_export_destination
from ..studio.project import Project, parse_script
from ..studio.render import ProductionService
from ..progress import ProgressError, ProgressState, bind_progress, operation_outcome

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
        project = parse_script(text, title=title, voice_ids=voice_ids,
                               project_id=self.project.id if self.project else None)
        if self.project:
            previous = {role.name.casefold(): role for role in self.project.roles}
            roles = tuple(replace(role, voice_id=previous[role.name.casefold()].voice_id,
                                  language=previous[role.name.casefold()].language,
                                  pan=previous[role.name.casefold()].pan,
                                  gain_db=previous[role.name.casefold()].gain_db,
                                  rate=previous[role.name.casefold()].rate,
                                  volume=previous[role.name.casefold()].volume)
                          if role.name.casefold() in previous else role for role in project.roles)
            project = replace(project, roles=roles, revision=self.project.revision)
            if project.to_dict() != self.project.to_dict():
                project = replace(project, revision=self.project.revision + 1)
        self.service.projects.save(project)
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
                 ('preview', 'Listen to &output'), ('close', '&Close'))):
        box = wx.BoxSizer(wx.HORIZONTAL)
        for identity, label in row:
            button = wx.Button(dialog, label=label)
            buttons[identity] = button
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
    dialog.report = lambda message: report(message, True)
    dialog.working = False
    parent_progress = getattr(frame, 'progress_panel', None)
    dialog.progress_speech_provider = getattr(parent_progress, 'speech_provider', None)
    dialog.progress_media_busy = lambda: (
        getattr(getattr(dialog, '_audio_player', None), 'state', 'stopped') == 'playing')
    progress = NativeProgressPanel(dialog)
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
        buttons['preview'].Enable(not busy and current_job is not None)
        editor.Enable(not busy)
        title.Enable(not busy)
        voice_choice.Enable(not busy)

    def run(operation, complete, *, phase='Audio Studio operation', with_progress=False):
        nonlocal busy
        if busy:
            return
        busy = True
        dialog.working = True
        cancel.clear()
        controls()
        task = progress.begin(phase)

        def measured(message):
            description = ' '.join(str(message).split())[:512] or phase
            # Phase receipts do not provide a measured overall total.
            # Updates queued before cancellation must not resurrect a task.
            if not cancel.is_set() and task.snapshot().state == ProgressState.RUNNING:
                try:
                    task.update(phase=description)
                except ProgressError:
                    if not cancel.is_set():
                        raise
            wx.CallAfter(report, description)

        def execute():
            with bind_progress(task):
                return operation(measured) if with_progress else operation()

        def finish(done):
            def apply():
                nonlocal busy
                busy = False
                dialog.working = False
                if alive:
                    controls()
                try:
                    result = done.result()
                except Exception as exc:
                    outcome = 'paused' if isinstance(exc, InterruptedError) else 'failed'
                    if with_progress and current_job is not None:
                        try:
                            outcome = operation_outcome(service.get(current_job))
                        except (ValueError, OSError, RuntimeError):
                            pass
                    progress.finish(task, outcome, phase + ': ' + outcome)
                    if alive:
                        report(str(exc), True)
                    return
                outcome = operation_outcome(result)
                try:
                    # A paused/partial receipt must never announce completion.
                    if alive and outcome == 'succeeded':
                        complete(result)
                    elif alive:
                        report(phase + ': ' + outcome + '. Saved work is retained.', True)
                    progress.finish(task, outcome, phase + ': ' + outcome)
                except Exception as exc:
                    progress.reporting_unavailable(task, outcome,
                        'Work returned ' + outcome + '; progress display failed: ' + str(exc))
                    if alive:
                        report(str(exc), True)
            wx.CallAfter(apply)

        try:
            future = executor.submit(execute)
        except Exception as exc:
            busy = False
            dialog.working = False
            controls()
            progress.finish(task, 'failed', 'Could not start ' + phase)
            report(str(exc), True)
            return
        future.add_done_callback(finish)
        return future

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
        library = frame.library
        project = save()
        if project is None:
            return
        chooser = wx.FileDialog(dialog, 'Save audio project as', defaultFile='audio-project.json',
                                wildcard='Project JSON (*.json)|*.json', style=wx.FD_SAVE)
        try:
            if chooser.ShowModal() == wx.ID_OK:
                path = Path(chooser.GetPath())
                if controller.draft.resolve() != (library.root / 'state' / 'studio' / 'draft.json').resolve():
                    raise ValueError('The library changed. Reopen the studio before exporting.')
                approval = confirm_export_destination(dialog, path, private_root=library.root,
                                                       title='Replace audio project')
                if approval is None:
                    return
                if frame.library is not library:
                    raise ValueError('The library changed. Reopen the studio before exporting.')
                write_binary_export(path, encode(project.to_dict()), private_root=library.root,
                                    approval=approval)
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
        identity = current_job
        run(lambda measured: service.run(identity, cancelled=cancel.is_set, progress=measured),
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
            player.play(source)
            report('Playing the rendered master. Close this dialog to stop playback.', True)
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
        if getattr(dialog, '_audio_player', None):
            try:
                dialog._audio_player.close()
            except (OSError, RuntimeError) as exc:
                report(f'Playback device reported a close error: {exc}')
        if busy:
            progress.request_cancel()
        progress.close()
        alive = False
        from .worker_cleanup import close_after_worker
        close_after_worker(executor, service)
        dialog.EndModal(wx.ID_CLOSE)

    for name, callback in {'save': save, 'open': open_project, 'saveas': save_as, 'roles': edit_roles,
                           'render': render, 'pause': lambda event: cancel.set(), 'jobs': jobs,
                           'preview': preview, 'help': help_text, 'close': close}.items():
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
    voice_choice.Bind(wx.EVT_CHOICE, changed)
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
    run(lambda: service.voices.voices(), loaded, phase='Checking installed local voices')
    try:
        dialog.ShowModal()
    finally:
        unregister()
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
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu = wx.Menu()
    for identity, title, shortcut, handler in [
            ('audio.studio', 'Script and audio production...', 'Ctrl+Alt+A', lambda e: show_studio(frame)),
            ('audio.convert', 'Convert audio and apply effects...', '', lambda e: show_audio_conversion(frame)),
            ('audio.engines', 'Local voice and music engines...', '', lambda e: show_engine_settings(frame))]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(identity, title, 'main', shortcut), item)
    frame.menu_bar.Append(menu, 'Audio St&udio')
