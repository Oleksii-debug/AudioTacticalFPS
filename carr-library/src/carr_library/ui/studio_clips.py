"""Native scene clip controls; every operation is available from the keyboard."""
from pathlib import Path
import threading

import wx

from ..studio.clips import ClipEditor

ROLES = ('sfx', 'ambience', 'music', 'microphone')
FIELDS = (
    ('start', 'Start in scene, seconds', '0'),
    ('trim_start', 'Start in original audio, seconds', '0'),
    ('duration', 'Selected duration, seconds; blank uses the remaining audio', ''),
    ('gain_db', 'Gain, decibels: -60 to 12', '-6'),
    ('pan', 'Position: -1 left, 0 center, 1 right', '0'),
    ('fade_in', 'Fade in, seconds', '0'),
    ('fade_out', 'Fade out, seconds', '0'),
)


class ClipDialog(wx.Dialog):
    def __init__(self, frame, *, editor=None):
        super().__init__(frame, title='Scene audio clips', size=(950, 860),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame = frame
        self.library = frame.library
        self.library_root = Path(self.library.root)
        self._library_identity = self._library_stamp()
        self.cancelled = threading.Event()
        self.destroyed = False
        self.editor = editor or ClipEditor(self.library_root)
        self._owns_editor = editor is None
        self._service_release_requested = False
        self._worker_future = None
        self.projects = ()
        self.project = None
        self.pending = self.closing = self.closed = False
        self.player = None
        self.playback_generation = 0
        self.previews = []
        self.controls = []
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label=(
            'Add a local file or a saved microphone recording. Edits preserve the original audio.\n'
            'Save settings before Preview or Render saved project. Rendering creates new output files; '
            'it never starts playback automatically.')),
            0, wx.EXPAND | wx.ALL, 8)

        def field(label, kind=wx.Choice, **options):
            layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.TOP, 6)
            control = kind(self, **options)
            control.SetName(label)
            layout.Add(control, 0, wx.EXPAND | wx.ALL, 6)
            self.controls.append(control)
            return control

        self.project_choice = field('Project')
        self.scene_choice = field('Scene')
        self.clip_choice = field('Clip and saved timing')
        self.clip_title = field('Clip name', wx.TextCtrl, value='')
        grid = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        grid.AddGrowableCol(1)
        self.fields = {}
        for name, label, value in FIELDS:
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control = wx.TextCtrl(self, value=value)
            control.SetName(label)
            grid.Add(control, 1, wx.EXPAND)
            self.fields[name] = control
            self.controls.append(control)
        layout.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        self.role = field('Track role', choices=list(ROLES))
        self.role.SetSelection(0)
        self.minimum = field('Minimum scene duration, seconds', wx.TextCtrl, value='1')
        self.status = field('Clip editing status', wx.TextCtrl,
            value='Choose a project or create one. No audio starts automatically.',
            style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 70))
        self.controls.remove(self.status)
        self.buttons = {}
        for items in (
            (('new', '&New project', self.new), ('reload', 'Re&load projects', self.reload),
             ('add', '&Add audio file', self.add), ('recording', 'Add saved &recording', self.recording)),
            (('save', '&Save clip settings', self.save), ('remove', 'Re&move clip', self.remove),
             ('move', 'Move to another scene', self.move),
             ('duration', 'Set scene &duration', self.duration), ('preview', '&Preview saved clip', self.preview)),
            (('clip_effects', 'Clip effects...', self.clip_effects),
             ('role_effects', 'Speaking role effects...', self.role_effects),
             ('scene_effects', 'Scene effects...', self.scene_effects),
             ('master_effects', 'Master effects...', self.master_effects),
             ('dry_preview', 'Preview without clip effects', self.preview_dry)),
            (('render', 'Render saved project', self.render),
             ('stop', 'S&top playback', self.stop), ('cancel', 'Cancel operation', self.cancel),
             ('close', '&Close', self.close))):
            row = wx.BoxSizer(wx.HORIZONTAL)
            for name, label, handler in items:
                button = wx.Button(self, label=label)
                button.SetName(label.replace('&', ''))
                button.Bind(wx.EVT_BUTTON, handler)
                row.Add(button, 0, wx.ALL, 4)
                self.buttons[name] = button
            layout.Add(row, 0, wx.EXPAND)
        self.SetSizer(layout)
        self.project_choice.Bind(wx.EVT_CHOICE, self.select_project)
        self.scene_choice.Bind(wx.EVT_CHOICE, self.select_scene)
        self.clip_choice.Bind(wx.EVT_CHOICE, self.select_clip)
        self.Bind(wx.EVT_CLOSE, self.close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.Bind(wx.EVT_CHAR_HOOK, self.key)
        self._enable()

    def _library_stamp(self):
        from ..durable_state import local_path, regular
        info = regular(local_path(self.library_root, exists=True), directory=True)
        return info.st_dev, info.st_ino

    def _scope_current(self):
        if (self.closed or self.closing
                or getattr(self.frame, 'library', None) is not self.library
                or Path(self.library.root) != self.library_root
                or getattr(self.frame, '_closing', False)):
            return False
        try:
            return self._library_stamp() == self._library_identity
        except (OSError, ValueError):
            return False

    def _cancelled(self):
        return (self.cancelled.is_set() or self.frame.cancel_event.is_set()
                or not self._scope_current())

    @staticmethod
    def _discard_preview(value):
        if isinstance(value, Path) and value.name.startswith('clip-preview-'):
            value.unlink(missing_ok=True)

    def _release_service(self):
        if not getattr(self, '_owns_editor', False) or self._service_release_requested:
            return
        self._service_release_requested = True
        service = self.editor.service
        future = self._worker_future
        if future is not None and not future.done():
            # The host owns the shared executor. A completion callback closes
            # only this dialog's lazy voice registry after its worker exits.
            future.add_done_callback(lambda completed: service.close())
        else:
            service.close()

    def _destroyed(self, event):
        if event.GetEventObject() is self and not self.destroyed:
            self.destroyed = True
            self.closed = True
            self.cancelled.set()
            if self.pending:
                self.frame.cancel_event.set()
            # Stop retains uncertain output through the existing progress owner.
            # No message or other native control is touched after destruction.
            if self.stop():
                for path in self.previews:
                    self._discard_preview(path)
            self._release_service()
        event.Skip()

    def message(self, text):
        if not self.closed:
            self.status.ChangeValue(str(text))
            self.status.SetFocus()

    def _enable(self):
        if self.closed:
            return
        available = not self.pending and self._scope_current()
        for control in self.controls:
            control.Enable(available)
        for name, button in self.buttons.items():
            button.Enable(name in ('close', 'stop') or (name == 'cancel' and self.pending)
                          or (name != 'cancel' and available))

    def _run(self, title, operation, done, *, preview=False):
        if self.closed or self.pending or self.frame.working:
            return False
        if not self._scope_current():
            self.message('The active library changed or is closing. Reopen the clip editor from that library.')
            return False
        if not self.stop():
            return False
        self.cancelled.clear()
        self.pending = True
        self._enable()
        self.message(title)

        def finish(value=None, error=None):
            self.pending = False
            if preview and (self.closed or self.closing or not self._scope_current()):
                self._discard_preview(value)
            if self.closed:
                return
            self._enable()
            if self.closing:
                self.close()
                return
            if not self._scope_current():
                self.message('The active library changed or is closing. Any saved edit remains in its original library.')
            elif error is not None:
                self.message(error)
            else:
                done(value)

        def work():
            from ..audio.models import AudioCancelled
            try:
                if self._cancelled():
                    raise AudioCancelled('Clip operation cancelled before it could use the selected library.')
                value = operation()
                if preview and isinstance(value, Path):
                    self.previews.append(value)
                if preview and self._cancelled():
                    self._discard_preview(value)
                    raise AudioCancelled('Clip preview cancelled before playback.')
                return value
            except AudioCancelled as exc:
                raise InterruptedError(str(exc)) from exc
        try:
            self._worker_future = self.frame.run_job(
                title, work, lambda result: finish(result), lambda error: finish(error=error))
            if self.pending and not self.frame.working:
                finish(error=RuntimeError('The operation could not start. Try again.'))
        except Exception as exc:
            finish(error=exc)
        return True

    def _accept(self, project):
        scene_id = clip_id = None
        old_clips = set()
        try:
            current = self._scene()
            scene_id = current.id
            old_clips = {cue.id for cue in current.cues}
            clip_id = self._clip().id
        except ValueError:
            pass
        self.projects = tuple(item for item in self.projects if item.id != project.id) + (project,)
        self.project_choice.Set([f'{item.title}; revision {item.revision}' for item in self.projects])
        self.project_choice.SetSelection(len(self.projects) - 1)
        self.select_project()
        index = next((index for index, scene in enumerate(project.scenes) if scene.id == scene_id), None)
        if index is not None:
            self.scene_choice.SetSelection(index)
            self.select_scene()
            cues = project.scenes[index].cues
            added = next((cue.id for cue in cues if cue.id not in old_clips), None)
            selected = next((index for index, cue in enumerate(cues) if cue.id == (added or clip_id)), None)
            if selected is not None:
                self.clip_choice.SetSelection(selected)
                self.select_clip()
        self.message(f'Saved {project.title}, revision {project.revision}. Original audio is retained.')

    def reload(self, event=None):
        def loaded(projects):
            self.projects = projects
            self.project_choice.Set([f'{item.title}; revision {item.revision}' for item in projects])
            self.project_choice.SetSelection(0 if projects else wx.NOT_FOUND)
            self.select_project()
        return self._run('Loading Studio projects', self.editor.service.projects.list, loaded)

    def new(self, event=None):
        chooser = wx.TextEntryDialog(self, 'Title for a new clip project', 'New audio project')
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            title = chooser.GetValue()
        finally:
            chooser.Destroy()
        self._run('Creating audio project', lambda: self.editor.create(title), self._accept)

    def select_project(self, event=None):
        index = self.project_choice.GetSelection()
        self.project = self.projects[index] if 0 <= index < len(self.projects) else None
        scenes = self.project.scenes if self.project else ()
        self.scene_choice.Set([scene.title for scene in scenes])
        self.scene_choice.SetSelection(0 if scenes else wx.NOT_FOUND)
        self.select_scene()

    def _scene(self):
        index = self.scene_choice.GetSelection()
        if self.project is None or not 0 <= index < len(self.project.scenes):
            raise ValueError('Choose or create a project and select its scene.')
        return self.project.scenes[index]

    def _clip(self):
        scene = self._scene()
        index = self.clip_choice.GetSelection()
        if not 0 <= index < len(scene.cues):
            raise ValueError('Add or select a clip first.')
        return scene.cues[index]

    def select_scene(self, event=None):
        try:
            scene = self._scene()
        except ValueError:
            self.clip_choice.Set([])
            return
        self.minimum.ChangeValue(str(scene.minimum_duration))
        self.clip_choice.Set([f'{index + 1}. {cue.title or cue.id}; {cue.role}; start {cue.start:g} s; '
            f'trim {cue.trim_start:g} s; duration {cue.duration if cue.duration is not None else "remaining"}'
            for index, cue in enumerate(scene.cues)])
        self.clip_choice.SetSelection(0 if scene.cues else wx.NOT_FOUND)
        self.select_clip()

    def select_clip(self, event=None):
        try:
            cue = self._clip()
        except ValueError:
            return
        for name, control in self.fields.items():
            value = getattr(cue, name)
            control.ChangeValue('' if value is None else str(value))
        self.role.SetSelection(ROLES.index(cue.role))
        self.clip_title.ChangeValue(cue.title)

    def _settings(self):
        values = {name: (None if name == 'duration' and not control.GetValue().strip()
                        else float(control.GetValue())) for name, control in self.fields.items()}
        index = self.role.GetSelection()
        if not 0 <= index < len(ROLES):
            raise ValueError('Select a track role.')
        return dict(values, role=ROLES[index], title=self.clip_title.GetValue())

    def add(self, event=None):
        try:
            project, scene, settings = self.project, self._scene(), self._settings()
            chooser = wx.FileDialog(self, 'Choose original local audio', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                source = Path(chooser.GetPath())
            finally:
                chooser.Destroy()
            self._run('Retaining and adding audio clip', lambda: self.editor.add(project, scene.id, source,
                cancelled=self._cancelled, **dict(settings, title=source.name)), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def recording(self, event=None):
        try:
            from ..audio.recording import RecordingService
            project, scene, settings = self.project, self._scene(), self._settings()
            service = RecordingService(self.library_root)
            def choose(rows):
                if not rows:
                    self.message('No completed recording is saved. Use Record microphone in Audio Studio first.')
                    return
                chooser = wx.SingleChoiceDialog(self, 'Choose a saved recording', 'Add microphone recording',
                                               [row['title'] for row in rows])
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    selected = chooser.GetSelection()
                finally:
                    chooser.Destroy()
                if not 0 <= selected < len(rows):
                    return
                def retain():
                    record = service.get(rows[selected]['id'], cancelled=self._cancelled)
                    return self.editor.add(project, scene.id, record['path'],
                        expected_sha256=record['audio']['sha256'], cancelled=self._cancelled,
                        **dict(settings, role='microphone', title=record['title']))
                self._run('Verifying and adding saved recording', retain, self._accept)
            self._run('Loading saved recordings', lambda: service.list(cancelled=self._cancelled), choose)
        except (ValueError, OSError, ImportError) as exc:
            self.message(exc)

    def save(self, event=None):
        try:
            project, scene, cue, settings = self.project, self._scene(), self._clip(), self._settings()
            self._run('Saving clip settings', lambda: self.editor.edit(project, scene.id, cue.id,
                cancelled=self._cancelled, **settings), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def remove(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            self._run('Removing clip from scene', lambda: self.editor.remove(project, scene.id, cue.id), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def duration(self, event=None):
        try:
            project, scene, seconds = self.project, self._scene(), float(self.minimum.GetValue())
            self._run('Saving minimum scene duration',
                lambda: self.editor.scene_duration(project, scene.id, seconds), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def move(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            destinations = tuple(item for item in project.scenes if item.id != scene.id)
            if not destinations:
                raise ValueError('Add another scene in Script and audio production before moving this clip.')
            chooser = wx.SingleChoiceDialog(self, 'Choose the destination scene', 'Move audio clip',
                                           [item.title for item in destinations])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not 0 <= index < len(destinations):
                return
            self._run('Moving clip to another scene', lambda: self.editor.move(
                project, scene.id, cue.id, destinations[index].id), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def render(self, event=None):
        if self.pending or self.frame.working or not self._scope_current():
            return
        try:
            project = self.project
            if project is None:
                raise ValueError('Choose or create a saved project before rendering.')
            chooser = wx.DirDialog(self, 'Choose the parent folder for new audio outputs')
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                parent = Path(chooser.GetPath())
            finally:
                chooser.Destroy()

            def work():
                from ..audio.models import AudioCancelled
                current = self.editor.service.projects.load(project.id)
                if current.revision != project.revision or current.fingerprint != project.fingerprint:
                    raise ValueError('Project changed since it was opened. Reload before rendering.')
                if self._cancelled():
                    raise AudioCancelled('Clip rendering cancelled before creating a production job.')
                identity = self.editor.service.submit(current, parent)
                if self._cancelled():
                    raise AudioCancelled('Clip rendering cancelled; the saved job can be resumed explicitly.')
                return self.editor.service.run(identity, cancelled=self._cancelled)

            def complete(value):
                self.message('Production complete. ' + value['destination']
                             + '\nSaved audio and checksums are ready. Playback has not started.')
            self._run('Rendering saved clip project', work, complete)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def clip_effects(self, event=None):
        return self._effects('clip')

    def role_effects(self, event=None):
        return self._effects('role')

    def scene_effects(self, event=None):
        return self._effects('scene')

    def master_effects(self, event=None):
        return self._effects('master')

    def _effects(self, scope):
        if self.pending or self.frame.working or not self._scope_current():
            return False
        try:
            project, scene = self.project, self._scene()
            arguments = {}
            def guard():
                if (not self._scope_current() or self.project is None
                        or self.project.fingerprint != project.fingerprint):
                    raise ValueError('The selected Studio project or library changed. Reopen effect review.')
            guard()
            if scope == 'clip':
                cue = self._clip()
                initial, arguments = cue.effects, {'scene_id': scene.id, 'clip_id': cue.id}
            elif scope == 'scene':
                initial, arguments = scene.effects, {'scene_id': scene.id}
            elif scope == 'master':
                initial = project.effects
            elif scope == 'role':
                if not project.roles:
                    raise ValueError('This project has no speaking roles.')
                picker = wx.SingleChoiceDialog(self, 'Choose the speaking role whose effects will change.',
                    'Speaking role effects', [role.name for role in project.roles])
                try:
                    if picker.ShowModal() != wx.ID_OK:
                        return False
                    guard()
                    index = picker.GetSelection()
                    if not 0 <= index < len(project.roles):
                        raise ValueError('Choose one existing speaking role.')
                    role = project.roles[index]
                finally:
                    picker.Destroy()
                guard()
                initial, arguments = role.effects, {'role_id': role.id}
            else:
                raise ValueError('Choose an existing effect scope.')

            def inspect():
                guard()
                return self.editor.effect_capabilities(project, cancelled=self._cancelled)

            def reviewed(available):
                from .effects_dialog import show_effect_chain
                try:
                    guard()
                    if self._cancelled():
                        return
                    available = tuple(kind for kind in available if kind != 'tempo')
                    value = show_effect_chain(self, self.library_root, initial,
                        sample_rate=project.sample_rate, available=available, guard=guard)
                    guard()
                    if value is None or self._cancelled():
                        return
                    def save():
                        guard()
                        return self.editor.set_effects(project, scope, value,
                            cancelled=self._cancelled, **arguments)
                    self._run('Saving reviewed ' + scope + ' effects', save, self._accept)
                except (ValueError, OSError, RuntimeError) as error:
                    if self._scope_current():
                        self.message(error)

            return self._run('Checking installed ' + scope + ' effects', inspect, reviewed)
        except (ValueError, OSError, RuntimeError) as error:
            if self._scope_current():
                self.message(error)
            return False

    def preview_dry(self, event=None):
        return self.preview(event, dry=True)

    def preview(self, event=None, *, dry=False):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            def play(path):
                from ..speech.player import WavePlayer
                from ..speech.progress_speech import play_when_quiet
                if path not in self.previews:
                    self.previews.append(path)
                self.player = self.player or WavePlayer()
                progress = getattr(self.frame, 'progress_panel', None)
                if progress is not None:
                    progress.register_player(self.player)
                generation = self.playback_generation
                def current():
                    return self._scope_current() and generation == self.playback_generation
                def playing():
                    if current():
                        self.player.play(path)
                play_when_quiet((progress,), playing, schedule=wx.CallLater,
                    alive=current, on_error=lambda error: self.message(error) if current() else None)
                self.message(('Previewing with clip effects bypassed. ' if dry else 'Previewing saved clip effects. ')
                             + 'Timing, gain, pan and fades are retained. Stop ends playback.')
            options = {'cancelled': self._cancelled}
            if dry:
                options['dry'] = True
            self._run('Rendering saved clip preview', lambda: self.editor.preview(project, scene.id, cue.id,
                **options), play, preview=True)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def stop(self, event=None):
        self.playback_generation += 1
        if self.player is None:
            return True
        quiet = False
        try:
            if self.player.close() is False:
                raise RuntimeError('The audio device has not confirmed Stop.')
            quiet = True
        except Exception as exc:
            self.message('Playback cleanup is still pending. Retry Stop playback before continuing. ' + str(exc))
        progress = getattr(self.frame, 'progress_panel', None)
        if progress is not None:
            try:
                progress.unregister_player(self.player, silence_confirmed=quiet)
            except Exception as exc:
                self.message('Playback progress feedback could not be refreshed: ' + str(exc))
        return quiet

    def cancel(self, event=None):
        if self.pending:
            self.cancelled.set()
            self.frame.cancel_event.set()
            self.message('Cancellation requested; waiting for the owned audio operation to finish.')

    def close(self, event=None):
        quiet = self.stop()
        if self.pending:
            self.closing = True
            self.cancel()
            if event is not None and hasattr(event, 'Veto') and event.CanVeto():
                event.Veto()
            return
        if not quiet:
            if event is not None and hasattr(event, 'Veto') and event.CanVeto():
                event.Veto()
            return
        self.closed = True
        self.cancelled.set()
        for path in self.previews:
            self._discard_preview(path)
        self._release_service()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Hide()

    def key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.close()
        else:
            event.Skip()


def show_clip_editor(frame):
    if frame.working:
        frame.report('Wait for the current operation or cancel it before editing clips.')
        return
    dialog = ClipDialog(frame)
    try:
        wx.CallAfter(dialog.reload)
        dialog.ShowModal()
    finally:
        dialog.Destroy()
