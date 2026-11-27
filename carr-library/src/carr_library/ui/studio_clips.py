"""Native scene clip controls; every operation is available from the keyboard."""
from pathlib import Path

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
        self.editor = editor or ClipEditor(frame.library.root)
        self.library_root = frame.library.root
        self._destroy_when_closed = False
        self._editor_closed = False
        self.projects = ()
        self.project = None
        self.pending = self.closing = self.closed = False
        self.player = None
        self.playback_generation = 0
        self.previews = []
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
        self.master_rate = field('Master sample rate in Hz: 16000, 22050, 24000, 44100 or 48000',
                                 wx.TextCtrl, value='24000')
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
            (('split', 'Split selected clip...', self.split_clip), ('join', 'Join adjacent clips...', self.join_clips),
             ('undo', 'Undo clip edit', self.undo), ('redo', 'Redo clip edit', self.redo)),
            (('clip_effects', 'Clip effects...', self.clip_effects),
             ('role_effects', 'Speaking role effects...', self.role_effects),
             ('scene_effects', 'Scene effects...', self.scene_effects),
             ('master_effects', 'Master effects...', self.master_effects)),
            (('dry_preview', 'Preview without clip effects', self.preview_dry),
             ('sample_rate', 'Set master sample rate', self.set_sample_rate)),
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
        self.Bind(wx.EVT_CLOSE, self.close)
        self.Bind(wx.EVT_CHAR_HOOK, self.key)
        self._enable()

    def message(self, text):
        if not self.closed:
            self.status.ChangeValue(str(text))
            self.status.SetFocus()

    def _enable(self):
        for control in self.controls:
            control.Enable(not self.pending)
        for name, button in self.buttons.items():
            button.Enable(name in ('close', 'stop') or (name == 'cancel' and self.pending)
                          or (name != 'cancel' and not self.pending))

    def cancelled(self):
        return (self.frame.cancel_event.is_set() or self.closed or self.closing
                or getattr(self.frame, '_closing', False)
                or self.frame.library.root != self.library_root)

    def _run(self, title, operation, done):
        if self.closed or self.closing or self.pending or self.frame.working:
            return False
        if self.frame.library.root != self.library_root:
            self.message('The library changed. Reopen clip editing in the selected library.')
            return False
        try:
            self.stop()
        except Exception as exc:
            self.message(f'Playback closure is not confirmed: {exc}. Try Stop before editing.')
            return False
        self.pending = True
        self._enable()
        self.message(title)
        def finish(value=None, error=None):
            self.pending = False
            if self.closed:
                return
            self._enable()
            if error is not None:
                self.message(error)
            elif self.closing:
                if isinstance(value, Path) and value.name.startswith('clip-preview-'):
                    self.previews.append(value)
            elif self.frame.library.root != self.library_root:
                if isinstance(value, Path) and value.name.startswith('clip-preview-'):
                    self.previews.append(value)
                self.message('The operation belongs to the original library. Reopen its clip editor to review the result.')
            else:
                done(value)
            if self.closing:
                self.close()
        def work():
            from ..audio.models import AudioCancelled
            try:
                if self.cancelled():
                    raise AudioCancelled('Clip operation cancelled before it started.')
                return operation()
            except AudioCancelled as exc:
                raise InterruptedError(str(exc)) from exc
            finally:
                # Desktop shutdown suppresses UI completion callbacks. The
                # worker still owns teardown after its operation has stopped.
                if self.closing or self.closed or getattr(self.frame, '_closing', False):
                    self._close_editor()
        try:
            self.frame.run_job(title, work, lambda result: finish(result), lambda error: finish(error=error))
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
        self.master_rate.ChangeValue(str(self.project.sample_rate if self.project else 24000))
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
                cancelled=self.cancelled, **dict(settings, title=source.name)), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def recording(self, event=None):
        try:
            from ..audio.recording import RecordingService
            project, scene, settings = self.project, self._scene(), self._settings()
            service = RecordingService(self.frame.library.root)
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
                    record = service.get(rows[selected]['id'])
                    return self.editor.add(project, scene.id, record['path'],
                        expected_sha256=record['audio']['sha256'], cancelled=self.cancelled,
                        **dict(settings, role='microphone', title=record['title']))
                self._run('Verifying and adding saved recording', retain, self._accept)
            self._run('Loading saved recordings', service.list, choose)
        except (ValueError, OSError, ImportError) as exc:
            self.message(exc)

    def save(self, event=None):
        try:
            project, scene, cue, settings = self.project, self._scene(), self._clip(), self._settings()
            self._run('Saving clip settings', lambda: self.editor.edit(project, scene.id, cue.id,
                cancelled=self.cancelled, **settings), self._accept)
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


    def copy_clip(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            self._run('Verifying copied clip', lambda: self.editor.copy(
                project, scene.id, cue.id, cancelled=self.cancelled),
                lambda copied: self.message('Clip instructions copied in this editor. Original audio is unchanged.'))
        except (ValueError, OSError) as exc:
            self.message(exc)

    def cut_clip(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            self._run('Cutting clip from scene', lambda: self.editor.cut(
                project, scene.id, cue.id, cancelled=self.cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def paste_clip(self, event=None):
        try:
            project, scene = self.project, self._scene()
            chooser = wx.TextEntryDialog(self, 'Start in this scene, in seconds', 'Paste copied clip',
                                         self.fields['start'].GetValue())
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                start = float(chooser.GetValue())
            finally:
                chooser.Destroy()
            self._run('Pasting verified clip instructions', lambda: self.editor.paste(
                project, scene.id, start=start, cancelled=self.cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def split_clip(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            default = str(cue.duration / 2) if cue.duration is not None else '1'
            chooser = wx.TextEntryDialog(self, 'Seconds from the beginning of the selected clip',
                                         'Split at an output sample', default)
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                position = float(chooser.GetValue())
            finally:
                chooser.Destroy()
            self._run('Splitting saved clip instructions', lambda: self.editor.split(
                project, scene.id, cue.id, position, cancelled=self.cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def join_clips(self, event=None):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            choices = tuple(item for item in scene.cues if item.id != cue.id)
            if not choices:
                raise ValueError('Choose a scene with two adjacent clips from the same original.')
            chooser = wx.SingleChoiceDialog(self,
                'Choose the other cut. Both must be contiguous in the scene and original, with matching track controls.',
                'Join adjacent clips', [f'{item.title or item.id}; start {item.start:g} seconds' for item in choices])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                index = chooser.GetSelection()
            finally:
                chooser.Destroy()
            if not 0 <= index < len(choices):
                return
            other_id = choices[index].id
            self._run('Joining adjacent saved cuts', lambda: self.editor.join(
                project, scene.id, cue.id, other_id, cancelled=self.cancelled), self._accept)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def undo(self, event=None):
        project = self.project
        self._run('Undoing the last clip edit', lambda: self.editor.history(
            project, cancelled=self.cancelled), self._accept)

    def redo(self, event=None):
        project = self.project
        self._run('Redoing the last clip edit', lambda: self.editor.history(
            project, forward=True, cancelled=self.cancelled), self._accept)


    def set_sample_rate(self, event=None):
        try:
            project = self.project
            self._scene()
            value = int(self.master_rate.GetValue())
            return self._run('Saving master sample rate',
                lambda: self.editor.sample_rate(project, value), self._accept)
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
        try:
            project, scene = self.project, self._scene()
            arguments = {}
            if scope == 'clip':
                cue = self._clip()
                initial = cue.effects
                arguments = {'scene_id': scene.id, 'clip_id': cue.id}
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
                        return
                    role = project.roles[picker.GetSelection()]
                finally:
                    picker.Destroy()
                initial, arguments = role.effects, {'role_id': role.id}
            else:
                raise ValueError('Choose an existing effect scope.')

            def inspect():
                self.editor._current(project)
                return self.editor.service._runtime().capabilities()['effects']

            def reviewed(available):
                from .effects_dialog import show_effect_chain
                try:
                    # Tempo would change the saved subtitle/clip timeline.
                    # It remains available in the separate conversion workflow.
                    available = tuple(kind for kind in available if kind != 'tempo')
                    value = show_effect_chain(self, self.library_root, initial,
                                              sample_rate=project.sample_rate, available=available)
                    if value is None or self.cancelled():
                        return
                    self._run('Saving reviewed ' + scope + ' effects',
                        lambda: self.editor.set_effects(project, scope, value,
                            cancelled=self.cancelled, **arguments), self._accept)
                except (ValueError, OSError, RuntimeError) as exc:
                    self.message(exc)

            return self._run('Checking installed ' + scope + ' effects', inspect, reviewed)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def preview_dry(self, event=None):
        return self.preview(event, dry=True)


    def preview(self, event=None, *, dry=False):
        try:
            project, scene, cue = self.project, self._scene(), self._clip()
            def play(path):
                from ..speech.player import WavePlayer
                from ..speech.progress_speech import play_when_quiet
                self.previews.append(path)
                self.player = self.player or WavePlayer()
                progress = getattr(self.frame, 'operation_progress', None)
                if progress is not None:
                    progress.register_player(self.player)
                generation = self.playback_generation
                play_when_quiet((progress,), lambda: self.player.play(path), schedule=wx.CallLater,
                    alive=lambda: not self.closed and not self.closing and generation == self.playback_generation,
                    on_error=lambda error: self.message(error))
                self.message(('Previewing with clip effects bypassed. ' if dry else 'Previewing saved clip effects. ')
                             + 'Timing, gain, pan and fades are retained. Stop ends playback.')
            options = {'cancelled': self.cancelled}
            if dry:
                options['dry'] = True
            self._run('Rendering saved clip preview', lambda: self.editor.preview(project, scene.id, cue.id,
                **options), play)
        except (ValueError, OSError) as exc:
            self.message(exc)

    def stop(self, event=None):
        self.playback_generation += 1
        if self.player is not None:
            self.player.close()

    def cancel(self, event=None):
        if self.pending:
            self.frame.cancel_event.set()
            self.message('Cancellation requested; waiting for the owned audio operation to finish.')

    def _close_editor(self):
        if not getattr(self, '_editor_closed', False):
            self.editor.close()
            self._editor_closed = True

    def close(self, event=None):
        if self.closed:
            return
        try:
            self.stop()
        except Exception as exc:
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
        except Exception as exc:
            self.message(f'The editor has not completed cleanup: {exc}. Try Close again.')
            if event is not None and getattr(event, 'CanVeto', lambda: False)():
                event.Veto()
            return
        progress = getattr(self.frame, 'operation_progress', None)
        if progress is not None and self.player is not None:
            try:
                progress.unregister_player(self.player, silence_confirmed=True)
            except Exception as exc:
                self.message(f'Playback is closed, but its progress observer could not detach: {exc}')
        self.closed = True
        for path in self.previews:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # Closed preview caches may be cleaned later; do not trap the dialog.
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Hide()
        if self._destroy_when_closed:
            wx.CallAfter(self.Destroy)

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
        dialog.close()
        if dialog.closed:
            dialog.Destroy()
        else:
            dialog._destroy_when_closed = True
