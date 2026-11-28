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
        self._undo = []
        self._redo = []
        self._copied_clip = None
        self.pending = self.closing = self.closed = False
        self.player = None
        self.playback_generation = 0
        self.previews = []
        self._preview_binding = None
        self._preview_repeat_timer = None
        self._preview_repeat_pending = None
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
             ('duration', 'Set scene &duration', self.duration), ('preview', '&Preview saved clip', self.preview),
             ('preview_repeat', 'Preview repeat...', self.preview_repeat)),
            (('clip_effects', 'Clip effects...', self.clip_effects),
             ('role_effects', 'Speaking role effects...', self.role_effects),
             ('scene_effects', 'Scene effects...', self.scene_effects),
             ('master_effects', 'Master effects...', self.master_effects),
             ('dry_preview', 'Preview without clip effects', self.preview_dry)),
            (('timeline', 'Timeline edits and &history...', self.timeline),
             ('render', 'Render saved project', self.render),
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
        self.buttons['preview_repeat'].Enable(available and self._preview_binding is not None)

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

    def _accept(self, project, *, history_mode=None):
        self._record_history(project, history_mode)
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
        self.select_project(preserve_history=True)
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

    def select_project(self, event=None, *, preserve_history=False):
        if not preserve_history:
            self._undo, self._redo = [], []
        index = self.project_choice.GetSelection()
        previous = getattr(self, 'project', None)
        self.project = self.projects[index] if 0 <= index < len(self.projects) else None
        if (previous is None or self.project is None or previous.id != self.project.id):
            self._copied_clip = None
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

    def _record_history(self, project, mode):
        from ..studio.project_history import RevisionRef
        previous = getattr(self, 'project', None)
        if previous is None or previous.id != project.id:
            self._undo, self._redo = [], []
            return
        if previous.fingerprint == project.fingerprint:
            return
        undo, redo = getattr(self, '_undo', []), getattr(self, '_redo', [])
        reference = RevisionRef(previous.revision, previous.fingerprint)
        if mode == 'undo':
            if undo:
                undo.pop()
            redo.append(reference)
        elif mode == 'redo':
            if redo:
                redo.pop()
            undo.append(reference)
        else:
            undo.append(reference)
            redo.clear()
        self._undo, self._redo = undo[-100:], redo[-100:]

    def timeline(self, event=None):
        if self.pending or self.frame.working or not self._scope_current():
            return
        choices = (
            'Split selected clip...',
            'Cut an interval and leave its time as silence...',
            'Rejoin the next section of the same original...',
            'Undo the last saved project edit in this session',
            'Redo the last undone project edit in this session',
            'Browse and restore an earlier saved project revision...',
            'Copy selected saved clip',
            'Cut selected saved clip for Paste',
            'Paste copied clip at a scene position...',
            'Crossfade two adjacent saved clips...',
            'Named scene markers...',
        )
        chooser = wx.SingleChoiceDialog(self,
            'Choose a saved-project operation. Original audio is always retained.',
            'Timeline editing and project history', choices)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            index = chooser.GetSelection()
        finally:
            chooser.Destroy()
        if self.pending or self.frame.working or not self._scope_current():
            return
        actions = (self.split, self.cut, self.join, self.undo, self.redo, self.history,
                   self.copy_clip, self.cut_for_paste, self.paste_clip, self.crossfade, self.markers)
        if 0 <= index < len(actions):
            actions[index]()

    def _time_entry(self, title, message, *, value=None):
        options = {} if value is None else {'value': value}
        chooser = wx.TextEntryDialog(self, message, title, **options)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return None
            return chooser.GetValue()
        finally:
            chooser.Destroy()

    def split(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            value = self._time_entry('Split saved clip',
                'Position in seconds from the selected clip start. The original and scene timing remain unchanged.')
            if value is None:
                return
            seconds = float(value)
            self._run('Splitting saved clip', lambda: self.editor.split(
                project, scene.id, cue.id, seconds, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def cut(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            value = self._time_entry('Cut saved clip interval',
                'Enter start and end seconds from the clip start, separated by a space. '
                'Removed time stays silent; other clips do not move. Example: 1.25 2.5')
            if value is None:
                return
            parts = value.split()
            if len(parts) != 2:
                raise ValueError('Enter exactly two numbers: the interval start and end.')
            start, end = map(float, parts)
            self._run('Cutting interval from saved clip', lambda: self.editor.cut(
                project, scene.id, cue.id, start, end, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def join(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            candidates = tuple(item for item in scene.cues
                               if item.id != cue.id and item.path == cue.path and item.start >= cue.start)
            if not candidates:
                raise ValueError('Select the first of two adjacent sections of the same retained original.')
            chooser = wx.SingleChoiceDialog(self,
                'Choose the following section. Source and scene positions must be contiguous; '
                'internal fades or changed curves cannot be discarded.',
                'Rejoin saved clips',
                [f'{item.title or item.id}; scene start {item.start:g} seconds' for item in candidates])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not 0 <= index < len(candidates):
                return
            second = candidates[index]
            self._run('Rejoining saved clip sections', lambda: self.editor.join(
                project, scene.id, cue.id, second.id, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)


    def _timeline_review_current(self, project):
        if (self.closed or self.closing or self.pending or self.frame.working
                or not self._scope_current() or project is None
                or self.project is None or self.project.fingerprint != project.fingerprint):
            self.message('The selected project or library changed or is busy. Review this operation again.')
            return False
        return True

    def crossfade(self, event=None):
        try:
            from ..studio.clip_operations import sample_index
            project, scene, first = self.project, self._scene(), self._clip()
            if not self._timeline_review_current(project):
                return
            candidates = tuple(item for item in scene.cues
                               if item.id != first.id and item.start > first.start)
            if not candidates:
                raise ValueError('Select the first of two adjacent saved clips.')
            chooser = wx.SingleChoiceDialog(self,
                'Choose the following adjacent clip. The overlap moves only that clip earlier; '
                'the previous scene length and other clips stay in place. Existing internal fades are refused.',
                'Crossfade saved clips',
                [f'{item.title or item.id}; scene start {item.start:g} seconds' for item in candidates])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not self._timeline_review_current(project) or not 0 <= index < len(candidates):
                return
            second = candidates[index]
            value = self._time_entry('Crossfade overlap',
                'Overlap in seconds, rounded to the nearest output sample. '
                'Both retained selections must fit the overlap without conflicting fades. '
                'Original audio is retained; the edit is saved as one project revision.')
            if value is None or not self._timeline_review_current(project):
                return
            frames = sample_index(float(value), project.sample_rate, 'Crossfade overlap')
            if frames < 1:
                raise ValueError('Crossfade overlap must contain at least one output sample.')
            seconds = frames / project.sample_rate
            self._run('Crossfading saved clips', lambda: self.editor.crossfade(
                project, scene.id, first.id, second.id, seconds, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def _marker_fields(self, project, marker=None):
        from ..studio.clip_operations import sample_index
        value = self._time_entry('Scene marker position',
            'Seconds from this scene start, rounded to the nearest output sample. '
            'A marker does not move clips, change audio, or extend the scene.',
            value=str(marker.position) if marker is not None else '0')
        if value is None or not self._timeline_review_current(project):
            return None
        position = sample_index(float(value), project.sample_rate, 'Marker position') / project.sample_rate
        label = self._time_entry('Scene marker name',
            'Enter a name for this saved time position. At most 256 characters.',
            value=marker.label if marker is not None else '')
        if label is None or not self._timeline_review_current(project):
            return None
        return position, label

    def markers(self, event=None):
        try:
            project, scene = self.project, self._scene()
            if not self._timeline_review_current(project):
                return
            markers = tuple(item for item in project.markers if item.scene_id == scene.id)
            chooser = wx.SingleChoiceDialog(self,
                'Markers belong to this saved scene. Times are scene-relative seconds; '
                'original audio and scene length stay unchanged. Select a marker to edit or remove it.',
                'Named scene markers',
                ['Add a named marker...'] + [
                    f'{item.label}; {item.position:g} seconds' for item in markers])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not self._timeline_review_current(project):
                return
            if index == 0:
                values = self._marker_fields(project)
                if values is None:
                    return
                position, label = values
                self._run('Adding a saved scene marker',
                    lambda: self.editor.service.projects.add_marker(project, scene_id=scene.id,
                        position=position, label=label, cancelled=self._cancelled), self._accept)
                return
            if not 1 <= index <= len(markers):
                return
            marker = markers[index - 1]
            chooser = wx.SingleChoiceDialog(self,
                f'{marker.label}; {marker.position:g} seconds from the scene start. '
                'Saved changes can be restored through project history.',
                'Edit named scene marker',
                ['Edit marker name and time...', 'Remove this marker'])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                action = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not self._timeline_review_current(project):
                return
            if action == 0:
                values = self._marker_fields(project, marker)
                if values is None:
                    return
                position, label = values
                self._run('Editing a saved scene marker',
                    lambda: self.editor.service.projects.update_marker(project, marker.id,
                        position=position, label=label, cancelled=self._cancelled), self._accept)
            elif action == 1:
                self._run('Removing a saved scene marker',
                    lambda: self.editor.service.projects.remove_marker(
                        project, marker.id, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def copy_clip(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            def copied(reference):
                if self.project is not None and self.project.id == project.id:
                    self._copied_clip = reference
                    self.message('Copied the saved clip. Choose Paste and its new scene position. '
                                 'No audio or project content was changed.')
            self._run('Copying saved clip selection', lambda: self.editor.copy(
                project, scene.id, cue.id, cancelled=self._cancelled), copied)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def cut_for_paste(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            def completed(result):
                self._copied_clip = result['reference']
                self._accept(result['project'])
                self.message('Cut the saved clip. Paste can place its exact selection in this project. '
                             'Original audio is retained; Undo can restore the previous project.')
            self._run('Cutting saved clip for Paste', lambda: self.editor.cut_for_paste(
                project, scene.id, cue.id, cancelled=self._cancelled), completed)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def paste_clip(self, event=None):
        try:
            project, scene = self.project, self._scene()
            reference = getattr(self, '_copied_clip', None)
            if reference is None:
                raise ValueError('Copy or cut a saved clip from this project first.')
            value = self._time_entry('Paste saved clip',
                'Position in seconds from the destination scene start. '
                'The copied original selection and effects are preserved; other clips stay in place.')
            if value is None:
                return
            seconds = float(value)
            self._run('Pasting saved clip', lambda: self.editor.paste(
                project, scene.id, reference, seconds, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def _restore_history(self, reference, *, mode=None):
        project = self.project
        if project is None:
            self.message('Choose a saved project first.')
            return
        self._run('Restoring reviewed project content as a new revision',
            lambda: self.editor.restore(project, reference, cancelled=self._cancelled),
            lambda result: self._accept(result, history_mode=mode))

    def undo(self, event=None):
        if not getattr(self, '_undo', []):
            self.message('There is no earlier saved edit in this session. Use project history for older revisions.')
            return
        self._restore_history(self._undo[-1], mode='undo')

    def redo(self, event=None):
        if not getattr(self, '_redo', []):
            self.message('There is no undone saved edit to redo in this session.')
            return
        self._restore_history(self._redo[-1], mode='redo')

    def history(self, event=None, *, before_revision=None):
        project = self.project
        if project is None:
            self.message('Choose a saved project first.')
            return

        def reviewed(references):
            if not references:
                self.message('There are no earlier saved revisions on this page.')
                return
            labels = [f'Revision {item.revision}; identity {item.fingerprint[:12]}' for item in references]
            if len(references) == 100:
                labels.append('Show earlier saved revisions...')
            chooser = wx.SingleChoiceDialog(self,
                'Restore the whole saved project, including timing, roles and effects. '
                'This creates a new revision; originals and completed outputs remain available.',
                'Saved project history', labels)
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not self._scope_current() or self.project is None or self.project.fingerprint != project.fingerprint:
                self.message('The selected project or library changed. Review history again.')
                return
            if index == len(references) and len(references) == 100:
                self.history(before_revision=references[-1].revision)
            elif 0 <= index < len(references):
                self._restore_history(references[index])

        self._run('Reviewing saved project history', lambda: self.editor.history(
            project, before_revision=before_revision, cancelled=self._cancelled), reviewed)

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
                    excluded = {'tempo', 'pan_envelope'} if scope in ('scene', 'master') else {'tempo', 'width_envelope'}
                    available = tuple(kind for kind in available if kind not in excluded)
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
                        self._preview_binding = (self.player, self.player.path, project, scene.id, cue.id)
                        self._enable()
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

    def _preview_current(self, binding, generation):
        if (binding is None or self.destroyed or self._cancelled() or self.pending
                or self.frame.working or generation != self.playback_generation
                or getattr(self, '_preview_binding', None) is not binding
                or self.player is not binding[0] or self.player.path != binding[1]
                or self.project is not binding[2]):
            return False
        try:
            return self._scene().id == binding[3] and self._clip().id == binding[4]
        except (ValueError, RuntimeError):
            return False

    def _stop_preview_repeat_timer(self):
        self._preview_repeat_pending = None
        timer = getattr(self, '_preview_repeat_timer', None)
        if timer is not None:
            timer.Stop()

    def _start_preview_repeat_timer(self):
        if getattr(self, '_preview_repeat_timer', None) is None:
            self._preview_repeat_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._poll_preview_repeat, self._preview_repeat_timer)
        if not self._preview_repeat_timer.Start(100):
            raise RuntimeError('The clip preview timer could not start. Repeat remains off.')

    def _preview_repeat_failed(self, error, generation):
        if generation != self.playback_generation:
            return
        quiet = self.stop()
        if not self.closed and self._scope_current():
            self.message('Preview repeat stopped: ' + str(error)
                         + ('' if quiet else ' Playback cleanup is still pending. Retry Stop playback.'))

    def _admit_preview_repeat(self, binding, generation, action):
        from ..speech.progress_speech import play_when_quiet
        self._preview_repeat_pending = generation

        def current():
            return self._preview_current(binding, generation)

        def perform():
            try:
                if current():
                    action()
            finally:
                if self._preview_repeat_pending == generation:
                    self._preview_repeat_pending = None

        try:
            play_when_quiet((getattr(self.frame, 'progress_panel', None),), perform,
                schedule=wx.CallLater, alive=current,
                on_error=lambda error: self._preview_repeat_failed(error, generation))
        except Exception as error:
            self._preview_repeat_failed(error, generation)

    def _poll_preview_repeat(self, event=None):
        if self.closed or self.destroyed:
            return
        binding = getattr(self, '_preview_binding', None)
        generation = self.playback_generation
        try:
            if not self._preview_current(binding, generation):
                self.stop()
                return
            outcome = self.player.poll()
            if outcome == 'repeat-pending' and self._preview_repeat_pending is None:
                self._admit_preview_repeat(binding, generation, self.player.repeat_if_ready)
            elif outcome == 'sleep':
                self.stop()
            elif self.player.ab_loop is None and self._preview_repeat_pending is None:
                self._stop_preview_repeat_timer()
        except Exception as error:
            self._preview_repeat_failed(error, generation)

    def _apply_preview_repeat(self, binding, generation, first=None, last=None):
        if not self._preview_current(binding, generation):
            return False
        clear = first is None and last is None
        if not clear:
            if (any(type(value) not in (float, int) for value in (first, last))
                    or not 0 <= first < last <= self.player.duration_seconds
                    or last - first + 1e-9 < .05):
                raise ValueError('Choose A and B inside this preview, at least 0.05 seconds apart.')
            first, last = float(first), float(last)
        # Supersede a pending repeat before a modal or quiet callback can start it.
        self.playback_generation += 1
        generation = self.playback_generation

        def apply():
            if clear:
                self.player.clear_ab_loop()
                self._stop_preview_repeat_timer()
                self.message('Preview repeat is off. The saved project is unchanged.')
            else:
                self.player.set_ab_loop(first, last)
                # This explicit Play action also handles EOF during the range dialogs.
                self.player.play_current()
                self.message(f'Preview repeats from {first:g} to {last:g} seconds. '
                             'Stop playback ends the repeat. The saved project is unchanged.')

        try:
            self._start_preview_repeat_timer()
            self._admit_preview_repeat(binding, generation, apply)
        except Exception as error:
            self._preview_repeat_failed(error, generation)
            return False
        return True

    def preview_repeat(self, event=None):
        binding = getattr(self, '_preview_binding', None)
        generation = self.playback_generation
        if not self._preview_current(binding, generation):
            if not self.closed and self._scope_current():
                self.message('Preview the selected saved clip before choosing A–B repeat.')
            return False
        try:
            chooser = wx.SingleChoiceDialog(self,
                'Repeat uses only the current rendered preview. It never changes the saved clip.',
                'Clip preview repeat', ('Play A–B repeat...', 'Turn preview repeat off'))
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return False
                choice = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not self._preview_current(binding, generation):
                return False
            if choice == 1:
                return self._apply_preview_repeat(binding, generation)
            if choice != 0:
                return False
            first, last = self.player.ab_loop or (0, self.player.duration_seconds)
            values = []
            for name, initial in (('A', first), ('B', last)):
                chooser = wx.TextEntryDialog(self,
                    f'{name} in seconds within this preview (0 to {self.player.duration_seconds:g}). '
                    'A and B must be at least 0.05 seconds apart.',
                    'Preview repeat ' + name, value=format(initial, '.12g'))
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        return False
                    value = chooser.GetValue()
                finally:
                    chooser.Destroy()
                if not self._preview_current(binding, generation):
                    return False
                values.append(float(value))
            return self._apply_preview_repeat(binding, generation, *values)
        except (ValueError, OSError, RuntimeError) as error:
            if not self.closed and self._scope_current():
                self.message(error)
            return False

    def stop(self, event=None):
        self.playback_generation += 1
        self._preview_binding = None
        repeat_stopped = True
        try:
            self._stop_preview_repeat_timer()
        except Exception as error:
            repeat_stopped = False
            self.message('The preview timer could not confirm Stop: ' + str(error))
        if self.player is None:
            return repeat_stopped
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
        return quiet and repeat_stopped

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
