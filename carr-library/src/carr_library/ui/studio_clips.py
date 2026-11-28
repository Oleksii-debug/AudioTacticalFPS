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
        self._native_id = self.GetId()
        self._destroy_requested = False
        self.cleanup_error = ''
        self.frame = frame
        self.library_root = frame.library.root
        self.navigation = None
        self._editor_closed = False
        self._editor_close_lock = threading.RLock()
        self.editor = editor or ClipEditor(frame.library.root)
        self.projects = ()
        self.project = None
        self.pending = self.closing = self.closed = False
        self.player = None
        self._player_closed = True
        self.playback_generation = 0
        self.previews = []
        self._unused_listening = set()
        self.controls = []
        body = wx.ScrolledWindow(self, style=wx.VSCROLL)
        body.SetScrollRate(0, 12)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(body, label=(
            'Add a local file or a saved microphone recording. Edits preserve the original audio.\n'
            'Save settings before Preview. Render the complete project from Script and audio production.')),
            0, wx.EXPAND | wx.ALL, 8)

        def field(label, kind=wx.Choice, **options):
            layout.Add(wx.StaticText(body, label=label), 0, wx.LEFT | wx.TOP, 6)
            control = kind(body, **options)
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
            grid.Add(wx.StaticText(body, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control = wx.TextCtrl(body, value=value)
            control.SetName(label)
            grid.Add(control, 1, wx.EXPAND)
            self.fields[name] = control
            self.controls.append(control)
        layout.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        self.role = field('Track role', choices=list(ROLES))
        self.role.SetSelection(0)
        self.minimum = field('Minimum scene duration, seconds', wx.TextCtrl, value='1')
        self.range_start = field('Range start in saved clip, seconds or HH:MM:SS', wx.TextCtrl, value='0')
        self.range_end = field('Range end in saved clip; blank uses its end', wx.TextCtrl, value='')
        self.marker_choice = field('Scene markers: authored time and label')
        self.master_rate = field('Master sample rate in Hz', wx.TextCtrl, value='24000')
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
            (('copy', 'Copy clip instructions', self.copy_clip), ('cut', 'Cut clip', self.cut_clip),
             ('paste', 'Paste copied clip...', self.paste_clip)),
            (('copy_range', 'Copy selected range', self.copy_range), ('cut_range', 'Cut selected range', self.cut_range),
             ('trim_range', 'Trim to selected range', self.trim_range), ('silence_range', 'Silence selected range', self.silence_range)),
            (('split', 'Split selected clip...', self.split_clip), ('join', 'Join adjacent source cuts...', self.join_clips),
             ('rendered_join', 'Join clips as new audio...', self.join_rendered)),
            (('crossfade', 'Crossfade into another clip...', self.crossfade),
             ('undo', 'Undo clip edit', self.undo), ('redo', 'Redo clip edit', self.redo)),
            (('marker_add', 'Add scene marker...', self.add_marker), ('marker_edit', 'Edit scene marker...', self.edit_marker),
             ('marker_remove', 'Remove scene marker', self.remove_marker)),
            (('clip_effects', 'Clip effects...', self.clip_effects), ('role_effects', 'Speaking role effects...', self.role_effects),
             ('scene_effects', 'Scene effects...', self.scene_effects), ('master_effects', 'Master effects...', self.master_effects)),
            (('bypass', 'Toggle saved clip effect bypass', self.bypass),
             ('sample_rate', 'Set master sample rate', self.set_sample_rate)),
            (('range_preview', 'Preview selected range', self.preview_range),
             ('dry_preview', 'Preview range without clip effects', self.preview_dry),
             ('loop', 'Loop selected range', self.preview_loop),
             ('navigation', 'Preview playback controls...', self.playback_controls)),
            (('stop', 'S&top playback', self.stop), ('cancel', 'Cancel operation', self.cancel),
             ('close', '&Close', self.close))):
            row = wx.BoxSizer(wx.HORIZONTAL)
            for name, label, handler in items:
                button = wx.Button(body, label=label)
                button.SetName(label.replace('&', ''))
                button.Bind(wx.EVT_BUTTON, handler)
                row.Add(button, 0, wx.ALL, 4)
                self.buttons[name] = button
            layout.Add(row, 0, wx.EXPAND)
        body.SetSizer(layout)
        body.FitInside()
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(body, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.project_choice.Bind(wx.EVT_CHOICE, self.select_project)
        self.scene_choice.Bind(wx.EVT_CHOICE, self.select_scene)
        self.clip_choice.Bind(wx.EVT_CHOICE, self.select_clip)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
        self.Bind(wx.EVT_CLOSE, self.close)
        self.Bind(wx.EVT_CHAR_HOOK, self.key)
        self._enable()

    def _same_library(self):
        return getattr(getattr(self.frame, 'library', None), 'root', None) == self.library_root

    def _cancelled(self):
        return (self.closed or self.closing or self.frame.cancel_event.is_set()
                or getattr(self.frame, '_closing', False) or not self._same_library())

    def _require_current(self):
        if self.closed or self.closing or not self._same_library():
            raise ValueError('This clip editor no longer belongs to the active library.')


    def dispose(self):
        # Pure Python latch: parent destruction can use a different wrapper.
        if self.closed:
            return
        self.closed = self.closing = True
        self.playback_generation += 1
        # The worker observes this owner's closed flag; never cancel a
        # different operation through the shared frame event on destruction.

    def on_destroy(self, event):
        if event.GetId() == self._native_id:
            self.dispose()
            wx.CallAfter(self._cleanup)
        event.Skip()

    def Destroy(self):
        if self._destroy_requested:
            return False
        self._destroy_requested = True
        self.dispose()
        self._cleanup()
        return super().Destroy() if self else False

    def _discard_preview(self, value):
        from ..speech.listening import PreparedListening
        if isinstance(value, PreparedListening):
            retained = getattr(self, '_unused_listening', None)
            if retained is None:
                retained = self._unused_listening = set()
            retained.add(value)
            try:
                if value.close() is False:
                    raise RuntimeError('Private listening cleanup is not confirmed.')
                retained.discard(value)
                return True
            except (Exception, KeyboardInterrupt) as exc:
                self.cleanup_error = str(exc)
                return False
        if isinstance(value, Path) and value.name.startswith('clip-preview-'):
            try:
                value.unlink(missing_ok=True)
                return True
            except OSError as exc:
                self.cleanup_error = str(exc)
        return False

    def _cleanup(self, *, native=True):
        # A worker may call this after deletion; native timers detach on the UI thread.
        navigation = getattr(self, 'navigation', None)
        if native and navigation is not None:
            try:
                navigation.close()
            except (Exception, KeyboardInterrupt) as exc:
                self.cleanup_error = str(exc)
        if self.player is not None:
            silence = False
            try:
                silence = getattr(self, '_player_closed', False) or self.player.close() is not False
                self._player_closed = silence
                if not silence:
                    self.cleanup_error = 'The playback device has not confirmed closure.'
            except (Exception, KeyboardInterrupt) as exc:
                self.cleanup_error = str(exc)
            progress = getattr(self.frame, 'operation_progress', None)
            if progress is not None:
                try:
                    progress.unregister_player(self.player, silence_confirmed=silence)
                except Exception as exc:
                    self.cleanup_error = str(exc)
            if silence:
                self.player = None
        if not self.pending and self.player is None:
            for value in tuple(getattr(self, '_unused_listening', ())):
                self._discard_preview(value)
            self.previews = [path for path in self.previews if not self._discard_preview(path)]
            try:
                self._close_editor()
            except (Exception, KeyboardInterrupt) as exc:
                self.cleanup_error = str(exc)

    def message(self, text):
        if not self.closed:
            self.status.ChangeValue(str(text))
            self.status.SetFocus()

    def _enable(self):
        if self.closed:
            return
        for control in self.controls:
            control.Enable(not self.pending)
        for name, button in self.buttons.items():
            button.Enable(name in ('close', 'stop') or (name == 'cancel' and self.pending)
                          or (name != 'cancel' and not self.pending))

    def _run(self, title, operation, done, *, failed=None, stop_playback=True):
        if self.closed or self.closing or self.pending or self.frame.working:
            return False
        if not self._same_library():
            self.message('The library changed. Reopen clip editing in the selected library.')
            return False
        try:
            if stop_playback:
                self.stop()
        except (Exception, KeyboardInterrupt) as exc:
            self.message(f'Playback closure is not confirmed: {exc}. Try Stop before editing.')
            return False
        self.pending = True
        self._enable()
        self.message(title)
        def finish(value=None, error=None):
            self.pending = False
            if self.closed or not self._same_library():
                self._discard_preview(value)
                self._cleanup()
                return
            self._enable()
            if error is not None:
                self.message(error)
                if failed is not None and not self.closed:
                    failed(error)
            elif self.closing:
                self._discard_preview(value)
            else:
                done(value)
            if self.closing:
                self.close()
        def work():
            from ..audio.models import AudioCancelled
            try:
                if not self._same_library():
                    raise AudioCancelled('Clip operation belongs to the previous library.')
                value = operation()
                if self.closed:
                    self._discard_preview(value)
                return value
            except AudioCancelled as exc:
                raise InterruptedError(str(exc)) from exc
            finally:
                if self.closed or getattr(self.frame, '_closing', False):
                    # The parent may suppress its UI completion callback after
                    # native destruction. Release only our finished work.
                    self.pending = False
                    self._cleanup(native=False)
        try:
            self.frame.run_job(title, work, lambda result: finish(result), lambda error: finish(error=error))
            if self.pending and not self.frame.working:
                finish(error=RuntimeError('The operation could not start. Try again.'))
        except (Exception, KeyboardInterrupt) as exc:
            finish(error=exc)
        return True

    def _accept(self, project):
        if self.closed:
            return
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
        if self.closed:
            return
        def loaded(projects):
            self.projects = projects
            self.project_choice.Set([f'{item.title}; revision {item.revision}' for item in projects])
            self.project_choice.SetSelection(0 if projects else wx.NOT_FOUND)
            self.select_project()
        return self._run('Loading Studio projects', self.editor.service.projects.list, loaded)

    def new(self, event=None):
        if self.closed:
            return
        chooser = wx.TextEntryDialog(self, 'Title for a new clip project', 'New audio project')
        try:
            if chooser.ShowModal() != wx.ID_OK or self.closed or not chooser:
                return
            title = chooser.GetValue()
        finally:
            if chooser:
                chooser.Destroy()
        if self.closed:
            return
        self._run('Creating audio project', lambda: self.editor.create(title), self._accept)

    def select_project(self, event=None):
        if self.closed:
            return
        index = self.project_choice.GetSelection()
        self.project = self.projects[index] if 0 <= index < len(self.projects) else None
        if self.project is not None:
            self.master_rate.ChangeValue(str(self.project.sample_rate))
        scenes = self.project.scenes if self.project else ()
        self.scene_choice.Set([scene.title for scene in scenes])
        self.scene_choice.SetSelection(0 if scenes else wx.NOT_FOUND)
        self.select_scene()

    def _scene(self):
        if self.closed:
            raise ValueError('The clip editor is closed.')
        index = self.scene_choice.GetSelection()
        if self.project is None or not 0 <= index < len(self.project.scenes):
            raise ValueError('Choose or create a project and select its scene.')
        return self.project.scenes[index]

    def _clip(self):
        if self.closed:
            raise ValueError('The clip editor is closed.')
        scene = self._scene()
        index = self.clip_choice.GetSelection()
        if not 0 <= index < len(scene.cues):
            raise ValueError('Add or select a clip first.')
        return scene.cues[index]

    def select_scene(self, event=None):
        if self.closed:
            return
        try:
            scene = self._scene()
        except ValueError:
            self.clip_choice.Set([])
            return
        self.minimum.ChangeValue(str(scene.minimum_duration))
        self.marker_choice.Set([f'{item.position:.6f} s; {item.label}' for item in scene.markers])
        self.marker_choice.SetSelection(0 if scene.markers else wx.NOT_FOUND)
        self.clip_choice.Set([f'{index + 1}. {cue.title or cue.id}; {cue.role}; start {cue.start:g} s; '
            f'trim {cue.trim_start:g} s; duration {cue.duration if cue.duration is not None else "remaining"}'
            for index, cue in enumerate(scene.cues)])
        self.clip_choice.SetSelection(0 if scene.cues else wx.NOT_FOUND)
        self.select_clip()

    def select_clip(self, event=None):
        if self.closed:
            return
        try:
            cue = self._clip()
        except ValueError:
            return
        for name, control in self.fields.items():
            value = getattr(cue, name)
            control.ChangeValue('' if value is None else str(value))
        self.role.SetSelection(ROLES.index(cue.role))
        self.clip_title.ChangeValue(cue.title)
        self.range_start.ChangeValue('0')
        self.range_end.ChangeValue('' if cue.duration is None else str(cue.duration))

    def _settings(self):
        if self.closed:
            raise ValueError('The clip editor is closed.')
        values = {name: (None if name == 'duration' and not control.GetValue().strip()
                        else float(control.GetValue())) for name, control in self.fields.items()}
        index = self.role.GetSelection()
        if not 0 <= index < len(ROLES):
            raise ValueError('Select a track role.')
        return dict(values, role=ROLES[index], title=self.clip_title.GetValue())

    def add(self, event=None):
        if self.closed:
            return
        try:
            project, scene, settings = self.project, self._scene(), self._settings()
            chooser = wx.FileDialog(self, 'Choose original local audio', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if chooser.ShowModal() != wx.ID_OK or self.closed or not chooser:
                    return
                source = Path(chooser.GetPath())
            finally:
                if chooser:
                    chooser.Destroy()
            if self.closed:
                return
            self._run('Retaining and adding audio clip', lambda: self.editor.add(project, scene.id, source,
                cancelled=self._cancelled, **dict(settings, title=source.name)), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def recording(self, event=None):
        if self.closed:
            return
        try:
            from ..audio.recording import RecordingService
            project, scene, settings = self.project, self._scene(), self._settings()
            service = RecordingService(self.frame.library.root)
            def choose(rows):
                if self.closed:
                    return
                if not rows:
                    self.message('No completed recording is saved. Use Record microphone in Audio Studio first.')
                    return
                chooser = wx.SingleChoiceDialog(self, 'Choose a saved recording', 'Add microphone recording',
                                               [row['title'] for row in rows])
                try:
                    if chooser.ShowModal() != wx.ID_OK or self.closed or not chooser:
                        return
                    selected = chooser.GetSelection()
                finally:
                    if chooser:
                        chooser.Destroy()
                if self.closed or not 0 <= selected < len(rows):
                    return
                def retain():
                    record = service.get(rows[selected]['id'], cancelled=self._cancelled)
                    return self.editor.add(project, scene.id, record['path'],
                        expected_sha256=record['audio']['sha256'], cancelled=self._cancelled,
                        **dict(settings, role='microphone', title=record['title']))
                self._run('Verifying and adding saved recording', retain, self._accept)
            self._run('Loading saved recordings',
                lambda: service.list(cancelled=self._cancelled), choose)
        except (ValueError, OSError, ImportError) as exc:
            self.message(exc)

    def save(self, event=None):
        if self.closed:
            return
        try:
            project, scene, cue, settings = self.project, self._scene(), self._clip(), self._settings()
            self._run('Saving clip settings', lambda: self.editor.edit(project, scene.id, cue.id,
                cancelled=self._cancelled, **settings), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def remove(self, event=None):
        if self.closed:
            return
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            self._run('Removing clip from scene', lambda: self.editor.remove(project, scene.id, cue.id), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def duration(self, event=None):
        if self.closed:
            return
        try:
            project, scene, seconds = self.project, self._scene(), float(self.minimum.GetValue())
            self._run('Saving minimum scene duration',
                lambda: self.editor.scene_duration(project, scene.id, seconds), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def move(self, event=None):
        if self.closed:
            return
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            destinations = tuple(item for item in project.scenes if item.id != scene.id)
            if not destinations:
                raise ValueError('Add another scene in Script and audio production before moving this clip.')
            chooser = wx.SingleChoiceDialog(self, 'Choose the destination scene', 'Move audio clip',
                                           [item.title for item in destinations])
            try:
                if chooser.ShowModal() != wx.ID_OK or self.closed or not chooser:
                    return
                index = chooser.GetSelection()
            finally:
                if chooser:
                    chooser.Destroy()
            if self.closed or not 0 <= index < len(destinations):
                return
            self._run('Moving clip to another scene', lambda: self.editor.move(
                project, scene.id, cue.id, destinations[index].id), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)


    def _range_values(self):
        self._require_current()
        return self.range_start.GetValue().strip(), self.range_end.GetValue().strip() or None

    def _ask_text(self, title, prompt, initial=''):
        self._require_current()
        chooser = wx.TextEntryDialog(self, prompt, title, str(initial))
        try:
            result = chooser.ShowModal()
            if result != wx.ID_OK or self.closed or not chooser or not self._same_library():
                return None
            return chooser.GetValue().strip()
        finally:
            if chooser:
                chooser.Destroy()

    def _other_clip(self, scene, cue, title):
        self._require_current()
        choices = tuple(item for item in scene.cues if item.id != cue.id)
        if not choices:
            raise ValueError('Add or select a scene with at least two clips.')
        chooser = wx.SingleChoiceDialog(self, 'Choose the second clip; the selected clip is first.',
            title, [f'{item.title or item.id}; {item.role}; start {item.start:g} seconds' for item in choices])
        try:
            result = chooser.ShowModal()
            if result != wx.ID_OK or self.closed or not chooser or not self._same_library():
                return None
            index = chooser.GetSelection()
            return choices[index] if 0 <= index < len(choices) else None
        finally:
            if chooser:
                chooser.Destroy()

    def copy_clip(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            return self._run('Verifying copied clip', lambda: self.editor.copy(
                project, scene.id, cue.id, cancelled=self._cancelled),
                lambda copied: self.message('Verified clip instructions copied within this editor. Original bytes are unchanged.'))
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def cut_clip(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            return self._run('Cutting saved clip', lambda: self.editor.cut(
                project, scene.id, cue.id, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def paste_clip(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            from ..studio.clips import timeline_seconds
            project, scene = self.project, self._scene()
            value = self._ask_text('Paste copied clip', 'Start in the scene: seconds or HH:MM:SS',
                                   self.fields['start'].GetValue())
            if value is None or self.closed:
                return
            self._require_current()
            start = timeline_seconds(value, project.sample_rate, 'Paste start')
            return self._run('Pasting verified clip', lambda: self.editor.paste(
                project, scene.id, start=start, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def _range_command(self, name):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            start, end = self._range_values()
            operation = getattr(self.editor, name)
            done = (lambda copied: self.message('Verified range copied within this editor.')) if name == 'copy_range' else self._accept
            return self._run(name.replace('_', ' ').capitalize(),
                lambda: operation(project, scene.id, cue.id, start, end, cancelled=self._cancelled), done)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def copy_range(self, event=None):
        return self._range_command('copy_range')

    def cut_range(self, event=None):
        return self._range_command('cut_range')

    def trim_range(self, event=None):
        return self._range_command('trim_range')

    def silence_range(self, event=None):
        return self._range_command('silence_range')

    def split_clip(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            value = self._ask_text('Split selected clip', 'Position from the clip start: seconds or HH:MM:SS',
                                   str(cue.duration / 2) if cue.duration is not None else '1')
            if value is None or self.closed:
                return
            self._require_current()
            return self._run('Splitting saved clip at an output sample', lambda: self.editor.split(
                project, scene.id, cue.id, value, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def _join(self, *, rendered=False):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            other = self._other_clip(scene, cue,
                'Join rendered clips into a new retained file' if rendered else 'Join adjacent cuts of the same original')
            if other is None or self.closed:
                return
            self._require_current()
            operation = self.editor.join_rendered if rendered else self.editor.join
            return self._run('Joining selected clips', lambda: operation(
                project, scene.id, cue.id, other.id, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def join_clips(self, event=None):
        return self._join()

    def join_rendered(self, event=None):
        return self._join(rendered=True)

    def crossfade(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            other = self._other_clip(scene, cue, 'Choose the clip to move into this crossfade')
            if other is None or self.closed:
                return
            value = self._ask_text('Crossfade overlap',
                'Overlap in seconds. The second clip moves to overlap the end of the first.', '0.25')
            if value is None or self.closed:
                return
            self._require_current()
            return self._run('Saving sample-aligned crossfade', lambda: self.editor.crossfade(
                project, scene.id, cue.id, other.id, value, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def _history(self, forward):
        if self.closed:
            return
        try:
            self._require_current()
            project = self.project
            return self._run('Redoing clip edit' if forward else 'Undoing clip edit',
                lambda: self.editor.history(project, forward=forward, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def undo(self, event=None):
        return self._history(False)

    def redo(self, event=None):
        return self._history(True)

    def _marker(self, edit):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene = self.project, self._scene()
            marker = None
            if edit:
                index = self.marker_choice.GetSelection()
                if not 0 <= index < len(scene.markers):
                    raise ValueError('Choose an existing scene marker.')
                marker = scene.markers[index]
            position = self._ask_text('Scene marker position', 'Scene time: seconds or HH:MM:SS',
                                      marker.position if marker else '0')
            if position is None or self.closed:
                return
            label = self._ask_text('Scene marker label', 'An authored annotation; it does not change source text.',
                                   marker.label if marker else '')
            if label is None or self.closed:
                return
            self._require_current()
            return self._run('Saving authored scene marker', lambda: self.editor.set_marker(
                project, scene.id, position, label, marker_id=marker.id if marker else None,
                cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def add_marker(self, event=None):
        return self._marker(False)

    def edit_marker(self, event=None):
        return self._marker(True)

    def remove_marker(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene = self.project, self._scene()
            index = self.marker_choice.GetSelection()
            if not 0 <= index < len(scene.markers):
                raise ValueError('Choose an existing scene marker.')
            identity = scene.markers[index].id
            return self._run('Removing authored scene marker', lambda: self.editor.remove_marker(
                project, scene.id, identity, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def bypass(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            enabled = not cue.effects_bypassed
            return self._run('Saving clip effect bypass', lambda: self.editor.bypass_effects(
                project, scene.id, cue.id, enabled, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def set_sample_rate(self, event=None):
        if self.closed:
            return
        try:
            self._require_current()
            project, value = self.project, int(self.master_rate.GetValue())
            return self._run('Saving master sample rate', lambda: self.editor.sample_rate(project, value, cancelled=self._cancelled), self._accept)
        except (ValueError, OSError, RuntimeError) as exc:
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
        if self.closed:
            return
        try:
            self._require_current()
            project, scene = self.project, self._scene()
            arguments = {}
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
                    result = picker.ShowModal()
                    if result != wx.ID_OK or self.closed or not picker or not self._same_library():
                        return
                    index = picker.GetSelection()
                    if not 0 <= index < len(project.roles):
                        return
                    role = project.roles[index]
                finally:
                    if picker:
                        picker.Destroy()
                initial, arguments = role.effects, {'role_id': role.id}
            else:
                raise ValueError('Choose an existing effect scope.')

            def inspect():
                self.editor._current(project)
                runtime = self.editor.service._runtime()
                from uuid import uuid4
                with self.editor.service._audio_admission(runtime, operation_id='clip-effects:' + uuid4().hex,
                        cancelled=self._cancelled) as stopped:
                    return runtime.capabilities(cancelled=stopped)['effects']

            def reviewed(available):
                if self.closed or not self._same_library():
                    return
                from .effects_dialog import show_effect_chain
                try:
                    available = tuple(kind for kind in available if kind != 'tempo')
                    value = show_effect_chain(self, self.library_root, initial,
                        sample_rate=project.sample_rate, available=available, guard=self._require_current)
                    if value is None or self.closed or not self._same_library():
                        return
                    self._run('Saving reviewed ' + scope + ' effects',
                        lambda: self.editor.set_effects(project, scope, value,
                            cancelled=self._cancelled, **arguments), self._accept)
                except (ValueError, OSError, RuntimeError) as exc:
                    self.message(exc)
            return self._run('Checking installed ' + scope + ' effects', inspect, reviewed)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def _preview_operation(self, *, ranged=False, dry=False, loop=False):
        if self.closed:
            return
        try:
            self._require_current()
            project, scene, cue = self.project, self._scene(), self._clip()
            start, end = self._range_values() if ranged else (0, None)
            def play(path):
                from ..speech.player import WavePlayer
                from ..speech.progress_speech import play_when_quiet
                from .playback_navigation import PlaybackNavigationHost, MIN_REPEAT_SECONDS
                if self.closed or not self._same_library():
                    self._discard_preview(path)
                    return
                self.previews.append(path)
                self.player = self.player or WavePlayer()
                progress = getattr(self.frame, 'operation_progress', None)
                if progress is not None:
                    progress.register_player(self.player)
                navigation = getattr(self, 'navigation', None)
                if navigation is None or navigation.closed:
                    self.navigation = PlaybackNavigationHost(self, self.player,
                        generation=lambda: self.playback_generation,
                        alive=lambda: not self.closed and not self.closing and self._same_library(),
                        stop_current=self.stop, progress=(progress,), report=self.message,
                        prepare=self._prepare_playback,
                        on_play=lambda: setattr(self, '_player_closed', False))
                generation = self.playback_generation
                def begin():
                    self._player_closed = False
                    self.player.play(path)
                    self.navigation.bind()
                    if loop:
                        duration = self.player.duration_seconds
                        if duration < MIN_REPEAT_SECONDS:
                            self.player.stop()
                            raise ValueError('Loop preview needs at least 0.25 seconds; use ordinary preview for a shorter range.')
                        self.navigation.controller.set_range(0, duration)
                        self.navigation.controller.enable_repeat(True)
                play_when_quiet((progress,), begin, schedule=wx.CallLater,
                    alive=lambda: not self.closed and not self.closing and self._same_library()
                        and generation == self.playback_generation,
                    on_error=lambda error: self.message(error))
                mode = 'Looping' if loop else 'Previewing'
                quality = 'without the clip effect chain' if dry else 'with saved clip processing'
                self.message(f'{mode} the selected audio {quality}. Stop ends playback.')
            operation = (lambda: self.editor.preview_range(project, scene.id, cue.id, start, end,
                dry=dry, cancelled=self._cancelled)) if ranged else (
                lambda: self.editor.preview(project, scene.id, cue.id, dry=dry, cancelled=self._cancelled))
            return self._run('Rendering saved clip preview', operation, play)
        except (ValueError, OSError, RuntimeError) as exc:
            self.message(exc)

    def preview(self, event=None):
        return self._preview_operation()

    def preview_range(self, event=None):
        return self._preview_operation(ranged=True)

    def preview_dry(self, event=None):
        return self._preview_operation(ranged=True, dry=True)

    def preview_loop(self, event=None):
        return self._preview_operation(ranged=True, loop=True)

    def _prepare_playback(self, request, completed, failed, *, cancelled):
        player = self.player
        return self._run('Preparing independent clip playback',
            lambda: player.prepare_listening(request, self.library_root,
                state_root=self.library_root / 'state',
                cancelled=lambda: cancelled() or self._cancelled()),
            completed, failed=failed, stop_playback=False)

    def playback_controls(self, event=None):
        if self.closed:
            return
        navigation = getattr(self, 'navigation', None)
        if navigation is None or navigation.closed:
            self.message('Start an explicit preview before opening playback controls.')
            return
        navigation.show()

    def stop(self, event=None):
        if self.closed:
            return
        self.playback_generation += 1
        navigation = getattr(self, 'navigation', None)
        if navigation is not None:
            navigation.invalidate()
        if self.player is not None:
            if self.player.close() is False:
                raise RuntimeError('The playback device has not confirmed closure.')
            self._player_closed = True

    def cancel(self, event=None):
        if self.closed:
            return
        if self.pending:
            self.frame.cancel_event.set()
            self.message('Cancellation requested; waiting for the owned audio operation to finish.')

    def _close_editor(self):
        # The actual worker and UI may both finish after native owner deletion.
        with self._editor_close_lock:
            if self._editor_closed:
                return
            editor = getattr(self, 'editor', None)
            cleanup = getattr(editor, 'close', None)
            if callable(cleanup) and cleanup() is False:
                raise RuntimeError('An owned voice worker has not confirmed closure.')
            self._editor_closed = True

    def close(self, event=None):
        if self.closed:
            return
        try:
            self.stop()
        except (Exception, KeyboardInterrupt) as exc:
            self.message(f'The audio device has not confirmed closure: {exc}. Try Stop or Close again.')
            if event is not None and getattr(event, 'CanVeto', lambda: False)():
                event.Veto()
            return
        if self.pending:
            self.closing = True
            self.cancel()
            if event is not None and hasattr(event, 'Veto') and event.CanVeto():
                event.Veto()
            return
        try:
            self._close_editor()
        except (Exception, KeyboardInterrupt) as exc:
            self.message(f'The editor has not completed cleanup: {exc}. Try Close again.')
            if event is not None and getattr(event, 'CanVeto', lambda: False)():
                event.Veto()
            return
        self.closed = True
        self._cleanup()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Hide()

    def key(self, event):
        if self.closed:
            event.Skip()
            return
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
        dialog.dispose()
        dialog._cleanup()
        if dialog:
            dialog.Destroy()
        if dialog.cleanup_error:
            try:
                frame.report('Clip cleanup needs attention: ' + dialog.cleanup_error)
            except (RuntimeError, AttributeError):
                pass
