"""Native, keyboard-operated entrypoints for local voices, studio and tools.

The editor is a narration derivative. It never replaces a library source or an
approved quotation. Rendering uses the same durable services as the CLI.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from ..studio import Project, ProjectStore, Role, load_project, narration_project
from ..studio.script import dialogue_project


class StudioDialog:
    def __init__(self, parent, voices, *, initial_text='', store=None, configuration_root=None):
        import wx
        self.wx, self.parent, self.voices = wx, parent, tuple(voices)
        self.store = store or ProjectStore(parent.library.root / 'audio-projects' / uuid4().hex)
        self.configuration_root = configuration_root
        if self.configuration_root is None and getattr(parent, 'library', None) is not None:
            self.configuration_root = parent.library.root
        self.dialog = wx.Dialog(parent, title='Audio Studio — local narration and dialogue',
                                size=(850, 740), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        display = wx.Display.GetFromWindow(parent)
        work_area = (wx.Display(display).GetClientArea() if display != wx.NOT_FOUND
                     else wx.GetClientDisplayRect())
        size = min(850, work_area.width), min(740, work_area.height)
        self.dialog.SetSize(size)
        self.dialog.SetMinSize((min(620, size[0]), min(460, size[1])))
        self.dialog.SetPosition((work_area.x + (work_area.width - size[0]) // 2,
                                 work_area.y + (work_area.height - size[1]) // 2))
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-studio')
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.busy = self.closed = False
        self.saved_revision = None
        self.player = None
        self.playback_timer = None
        self._playback_pending = False
        self._playback_generation = 0
        self.result = None
        outer = wx.BoxSizer(wx.VERTICAL)
        self.form = wx.ScrolledWindow(self.dialog, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
        self.form.SetScrollRate(0, 16)
        layout = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(self.form, label=(
            'Local voices and a separately installed FFmpeg runtime produce audio in this project folder. '
            'The original library text remains unchanged. Ctrl+S saves; Render resumes verified completed chunks.'))
        intro.Wrap(790)
        layout.Add(intro, 0, wx.EXPAND | wx.ALL, 8)
        self.title = self._text(layout, 'Project title', 'Narration')
        self.folder = self._text(layout, 'Project folder', str(self.store.root), readonly=True)
        layout.Add(wx.StaticText(self.form, label='Editor mode'), 0, wx.LEFT, 8)
        self.mode = wx.Choice(self.form, choices=['Faithful narration text', 'Speaker-labelled dialogue', 'Versioned JSON scenario'])
        self.mode.SetName('Narration editor mode')
        self.mode.SetSelection(0)
        layout.Add(self.mode, 0, wx.EXPAND | wx.ALL, 8)
        self.cast = []
        grid = wx.FlexGridSizer(cols=3, hgap=6, vgap=6)
        grid.AddGrowableCol(1)
        choices = [f'{v.name}; {v.language or "language not declared"}; {v.engine}; {v.id}' for v in voices]
        for index, name in enumerate(('Narrator', 'Speaker 1', 'Speaker 2')):
            role = wx.TextCtrl(self.form, value=name)
            role.SetName(f'Role {index + 1} name')
            voice = wx.Choice(self.form, choices=choices)
            voice.SetName(f'Voice for role {index + 1}')
            if choices:
                voice.SetSelection(min(index, len(choices) - 1))
            pan = wx.Choice(self.form, choices=['Center', 'Left', 'Right'])
            pan.SetName(f'Audio position for role {index + 1}')
            pan.SetSelection(index if index < 3 else 0)
            self.cast.append((role, voice, pan))
            for control in (role, voice, pan):
                grid.Add(control, 0, wx.EXPAND)
        layout.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
        help_text = wx.StaticText(self.form, label=(
            'Narration uses the first voice. Dialogue uses role labels, for example Narrator: Hello. '
            'Start a chapter with ## Chapter title. JSON supports additional roles, prepared music and exact pauses.'))
        help_text.Wrap(790)
        layout.Add(help_text, 0, wx.EXPAND | wx.ALL, 8)
        self.editor = self._text(layout, 'Narration derivative or authored script', initial_text, multiline=True, proportion=1)
        self.editor.SetMinSize((-1, 140))
        self.form.SetSizer(layout)
        self.form.FitInside()
        outer.Add(self.form, 1, wx.EXPAND)
        from .progress_panel import NativeProgressPanel
        self.dialog.working = False
        self.dialog.progress_media_busy = self._media_busy
        self.progress_panel = NativeProgressPanel(self.dialog, host=self.dialog, layout=outer,
            root=self.configuration_root if self.configuration_root is not None else self.store.root,
            insert_at=None)
        self.status = self.progress_panel.text
        self.status.SetName('Audio Studio status')
        self.status.SetMinSize((-1, 75))
        self.say('Ready. Choose a voice, enter text and save the project. Ctrl+Shift+Q queries render progress.')
        buttons = wx.GridSizer(cols=4, hgap=4, vgap=4)
        self.actions = []
        for label, handler in (('&Save project', self.save), ('Save &JSON as', self.save_as), ('&Render or resume', self.render),
                               ('&Pause', self.pause_render), ('&Cancel render', self.cancel_render),
                               ('&Play output', self.play), ('Pause p&layback', self.pause_playback), ('&Stop playback', self.stop_playback)):
            button = wx.Button(self.dialog, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.EXPAND | wx.ALL, 3)
            self.actions.append(button)
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        close = wx.Button(self.dialog, wx.ID_CANCEL, '&Close')
        close.Bind(wx.EVT_BUTTON, self.close)
        outer.Add(close, 0, wx.ALL, 8)
        self.dialog.SetSizer(outer)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_CHAR_HOOK, self.key)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        if store is not None:
            project = store.load()
            self.title.ChangeValue(project.title)
            self.editor.ChangeValue(json.dumps(project.to_dict(), ensure_ascii=False, indent=2))
            self.mode.SetSelection(2)
            self.saved_revision = project.revision
            self.say('Saved project loaded. Render resumes its verified chunks.')
        self.saved_form = None if store is None and initial_text else self._form_state()
        self.editor.SetFocus()

    def _text(self, layout, label, value='', *, readonly=False, multiline=False, proportion=0):
        wx = self.wx
        layout.Add(wx.StaticText(self.form, label=label), 0, wx.LEFT | wx.TOP, 8)
        style = (wx.TE_READONLY if readonly else 0) | (wx.TE_MULTILINE if multiline else 0)
        control = wx.TextCtrl(self.form, value=value, style=style)
        control.SetName(label)
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 8)
        return control

    def say(self, text, focus=False):
        if self.closed:
            return
        try:
            self.progress_panel.set_message(text)
        except Exception:
            self.status.ChangeValue(text)
        if focus:
            self.status.SetInsertionPoint(0)
            self.status.SetFocus()

    def key(self, event):
        if event.ControlDown() and event.ShiftDown() and event.GetKeyCode() in (ord('Q'), ord('q')):
            self.progress_panel.query()
            return
        if event.ControlDown() and event.GetKeyCode() in (ord('S'), ord('s')):
            self.save_as() if event.ShiftDown() else self.save()
            return
        event.Skip()

    def _form_state(self):
        # TextCtrl.IsModified does not track title, mode, role, voice or pan
        # controls. Compare raw values so invalid JSON drafts are also retained.
        return (self.title.GetValue(), self.mode.GetSelection(), self.editor.GetValue(),
                tuple((name.GetValue(), voice.GetSelection(), pan.GetSelection())
                      for name, voice, pan in self.cast))

    def _editing(self, enabled):
        for control in (self.title, self.editor, self.mode, *(c for row in self.cast for c in row)):
            control.Enable(enabled)

    def _cleanup(self):
        self._stop_pending_playback()
        self.progress_panel.close()
        player, self.player = self.player, None
        try:
            if player is not None:
                try:
                    player.close()
                except Exception as exc:
                    # Cleanup errors must not trap the user inside the modal or
                    # prevent its native controls from being destroyed.
                    self.cleanup_error = str(exc)
        finally:
            self.executor.shutdown(wait=False, cancel_futures=True)

    def project(self):
        mode = self.mode.GetSelection()
        text = self.editor.GetValue()
        if mode not in (0, 1, 2):
            raise ValueError('Choose a narration editor mode.')
        if mode == 2:
            from ..studio.models import unique_object
            return Project.from_dict(json.loads(text, object_pairs_hook=unique_object))
        roles = {}
        for index, (name, voice, pan) in enumerate(self.cast if mode == 1 else self.cast[:1]):
            selected = voice.GetSelection()
            if not 0 <= selected < len(self.voices):
                raise ValueError('No selected local voice. Install a supported local engine and reopen the voice list.')
            label = name.GetValue().strip()
            if label in roles:
                raise ValueError('Role names must be distinct.')
            if pan.GetSelection() not in (0, 1, 2):
                raise ValueError('Choose Center, Left or Right for each active role.')
            roles[label] = Role(f'role-{index + 1}', self.voices[selected].id,
                               pan=(0.0, -1.0, 1.0)[pan.GetSelection()])
        if mode == 1:
            return dialogue_project(text, roles, title=self.title.GetValue())
        role = next(iter(roles.values()))
        return narration_project(text, role.voice_id, title=self.title.GetValue(), pan=role.pan)

    def save(self, event=None):
        if self.busy:
            self.say('Rendering uses the saved revision. Pause before editing or saving another revision.', True)
            return None
        try:
            project = self.project()
            self.saved_revision = self.store.save(project, expected_revision=self.saved_revision)
            self.editor.DiscardEdits()
            self.saved_form = self._form_state()
            self.say('Project saved. Original library sources remain unchanged.', True)
            return project
        except Exception as exc:
            # Retain invalid draft text for user correction; never false success.
            self.say(f'Could not save: {exc}. Your draft is still in the editor.', True)
            return None

    def save_as(self, event=None):
        project = self.save()
        if project is None:
            return
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, 'Export project JSON to a new filename', wildcard='JSON (*.json)|*.json',
                               defaultFile='narration-project.json', style=wx.FD_SAVE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            from ..studio.store import atomic_text
            atomic_text(Path(dialog.GetPath()), json.dumps(project.to_dict(), ensure_ascii=False, indent=2) + '\n')
            self.say('Project JSON exported.', True)
        except Exception as exc:
            self.say(f'Could not export: {exc}', True)
        finally:
            dialog.Destroy()

    def render(self, event=None):
        if self.busy:
            self.say('A render is already running. Use Pause or Cancel render.', True)
            return
        project = self.save()
        if project is None:
            return
        self.cancel.clear()
        self.pause.clear()
        panel = self.progress_panel
        try:
            if panel.active_task is not None and panel.active_task.snapshot().state == 'paused':
                # The same script can now select different engine/model bytes.
                # Count the new validation pass from zero; keep the earlier
                # paused job intact in its durable Studio history.
                from ..progress import ProgressHub
                panel.hub = ProgressHub()
            task = panel.begin('Rendering Audio Studio project', unit='speech chunks')
        except Exception as exc:
            self.say(f'Could not start render progress: {exc}. Your saved project remains available.', True)
            return
        self.busy = self.dialog.working = True
        self._editing(False)
        panel.set_busy(True)
        self.say('Rendering locally. Pause or cancel is available; accepted chunks remain recoverable.')
        def operation():
            from ..studio.director import Director
            from ..studio.progress import report_render_progress
            from ..progress import bind_progress
            with bind_progress(task):
                return Director(self.store, configuration_root=self.configuration_root).render(
                    project, cancelled=self.cancel.is_set, paused=self.pause.is_set,
                    progress=report_render_progress)
        try:
            future = self.executor.submit(operation)
        except Exception as exc:
            self.busy = self.dialog.working = False
            self._editing(True)
            panel.set_busy(False)
            self._finish_render_progress(task, 'failed', str(exc))
            self.say(f'Could not start rendering: {exc}. Your saved project is available to resume.', True)
            return
        def finished(done):
            def apply():
                if self.closed:
                    return
                self.busy = self.dialog.working = False
                self._editing(True)
                panel.set_busy(False)
                try:
                    result = done.result()
                except Exception as exc:
                    from ..studio.director import RenderCancelled, RenderPaused
                    state = 'paused' if isinstance(exc, RenderPaused) else (
                        'cancelled' if isinstance(exc, (RenderCancelled, InterruptedError)) else 'failed')
                    self._finish_render_progress(task, state, str(exc))
                    self.say(str(exc) + ' Choose Render or resume to continue the saved project.', True)
                else:
                    self.result = result
                    self._finish_render_progress(task, 'succeeded',
                        'Master audio, production manifest and captions are saved.'
                        + (' Completion occurred before cancellation could stop it.' if self.cancel.is_set() else ''))
                    self.say(f'Render complete. {self.result["master"]["duration_seconds"]:.2f} seconds. '
                             f'Output: {self.result["master"]["path"]}. Play output is available.', True)
            self.wx.CallAfter(apply)
        future.add_done_callback(finished)

    def _finish_render_progress(self, task, state, message):
        try:
            if task.snapshot().state == 'unavailable':
                self.progress_panel.reporting_unavailable(task, state, task.snapshot().message)
            else:
                self.progress_panel.finish(task, state, message)
        except Exception as exc:
            try:
                self.progress_panel.reporting_unavailable(task, state,
                    f'Render result: {state}. Progress reporting is unavailable: {exc}')
            except Exception:
                pass

    def pause_render(self, event=None):
        if self.busy:
            self.pause.set()
            self.say('Pause requested. Waiting for the render to retain its current checkpoint.', True)

    def cancel_render(self, event=None):
        if self.busy:
            self.cancel.set()
            try:
                self.progress_panel.request_cancel()
            except Exception:
                pass  # A lost observer must not prevent actual cancellation.
            self.say('Cancellation requested. Waiting for the render to stop; accepted chunks remain saved.', True)

    def _media_busy(self):
        if self._playback_pending:
            return True
        return self.player is not None and self.player.status().get('state') in ('playing', 'paused', 'recording')

    def _stop_pending_playback(self):
        self._playback_generation += 1
        self._playback_pending = False
        if self.playback_timer is not None:
            self.playback_timer.Stop()
            self.playback_timer = None

    def _start_playback(self, action):
        """Yield to the native loop until a cancelled progress cue has ended."""
        self._stop_pending_playback()
        self._playback_pending = True
        generation = self._playback_generation
        started = time.monotonic()
        quiet = self.progress_panel.preferences.quiet_during_media
        if quiet:
            self.progress_panel.feedback.suspend()

        def ready():
            if self.closed or generation != self._playback_generation:
                return
            if quiet and not self.progress_panel.feedback.quiescent:
                if time.monotonic() - started >= 5:
                    self._playback_pending = False
                    self.say('Playback did not start because a progress notification has not stopped. Try again.', True)
                    return
                self.playback_timer = self.wx.CallLater(20, ready)
                return
            try:
                action()
                self.say('Playing the rendered local output. Pause playback and Stop playback are available.', True)
            except Exception as exc:
                self.say(f'Playback unavailable: {exc}', True)
            finally:
                self._playback_pending = False
                self.playback_timer = None
        ready()

    def play(self, event=None):
        try:
            if self.result is None:
                jobs = self.store.jobs()
                result = next((j['result'] for j in jobs if j['state'] == 'complete'), None)
                if result is None:
                    raise ValueError('Render the project first.')
                self.result = result
            from ..speech.player import LocalPlayer
            if self.player is None:
                self.player = LocalPlayer()
            path = Path(self.result['master']['path'])
            def begin():
                self.player.open(path)
                self.player.play()
            self._start_playback(begin)
        except Exception as exc:
            self.say(f'Playback unavailable: {exc}', True)

    def pause_playback(self, event=None):
        try:
            if self.player is None:
                raise ValueError('Start playback first.')
            state = self.player.status()
            if state.get('state') == 'paused':
                self._start_playback(self.player.play)
            else:
                self.player.pause()
                self.say('Playback ' + str(self.player.status().get('state', 'updated')), True)
        except Exception as exc:
            self.say(str(exc), True)

    def stop_playback(self, event=None):
        self._stop_pending_playback()
        if self.player is not None:
            try:
                self.player.stop()
                self.say('Playback stopped.', True)
            except Exception as exc:
                self.say(str(exc), True)

    def close(self, event=None):
        if self.closed:
            return
        if self.busy:
            self.pause_render()
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        if self._form_state() != self.saved_form:
            answer = self.wx.MessageBox('Save your narration draft before closing?', 'Unsaved narration',
                self.wx.YES_NO | self.wx.CANCEL | self.wx.ICON_QUESTION, self.dialog)
            if answer == self.wx.CANCEL or answer == self.wx.YES and self.save() is None:
                return
        self.closed = True
        try:
            self._cleanup()
        finally:
            self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        holds, ancestor = [], self.parent
        while ancestor is not None:
            panel = getattr(ancestor, 'progress_panel', None)
            if panel is not None and hasattr(panel, 'suspend_feedback'):
                holds.append((panel, panel.suspend_feedback()))
            parent = getattr(ancestor, 'GetParent', None)
            ancestor = parent() if callable(parent) else None
        try:
            self.dialog.ShowModal()
        finally:
            self.closed = True
            self.cancel.set()
            try:
                self._cleanup()
            finally:
                self.dialog.Destroy()
                for panel, token in holds:
                    panel.release_feedback(token)


def install_studio_actions(frame):
    import wx
    from .desktop_app import show_text, text_dialog
    from ..speech import discover_voices

    def open_studio(event=None, *, store=None, initial_text=''):
        library = frame.library
        def show(voices):
            if getattr(frame, '_closing', False):
                return
            if frame.library is not library:
                frame.report('The library folder changed. Reopen Audio Studio to review its local voices.')
                return
            StudioDialog(frame, voices, initial_text=initial_text, store=store,
                         configuration_root=library.root).show()
        # Let the host finish the discovery task before entering a nested modal
        # event loop. Studio owns its separate measured render task.
        frame.run_job('Discovering installed local voices',
            lambda: discover_voices(configuration_root=library.root, cancelled=frame.cancel_event.is_set),
            lambda voices: wx.CallAfter(show, voices))

    def open_existing(event=None):
        dialog = wx.DirDialog(frame, 'Open a saved Audio Studio project folder')
        try:
            if dialog.ShowModal() == wx.ID_OK:
                store = ProjectStore(dialog.GetPath())
                store.load()
                open_studio(store=store)
        except Exception as exc:
            frame.report(str(exc))
        finally:
            dialog.Destroy()

    def list_voices(event=None):
        frame.run_job('Inspecting local voices',
            lambda: discover_voices(configuration_root=frame.library.root, cancelled=frame.cancel_event.is_set),
            lambda voices: wx.CallAfter(show_text, frame, 'Installed local voices', '\n\n'.join(
                f'{v.name}\nID: {v.id}\nLanguage: {v.language or "not declared"}\nEngine: {v.engine}\nControls: {", ".join(v.capabilities)}'
                for v in voices) or 'No supported local voices were found. Install a local SAPI5 or eSpeak NG engine, then scan again.'))

    def dordiai_settings(event=None):
        from .dordiai_settings import show_dordiai_settings
        show_dordiai_settings(frame)

    def current_text(event=None):
        selected = wx.Window.FindFocus()
        if isinstance(selected, wx.TextCtrl):
            start, end = selected.GetSelection()
            text = selected.GetRange(start, end) if start != end else selected.GetValue()
            if text.strip():
                open_studio(initial_text=text)
                return
        frame.report('Select text in a native text field, then use Narrate selected text. You can also paste text into a new studio project.')

    def convert(event=None):
        source_dialog = wx.FileDialog(frame, 'Choose local audio to convert', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if source_dialog.ShowModal() != wx.ID_OK:
                return
            source = Path(source_dialog.GetPath())
        finally:
            source_dialog.Destroy()
        target_dialog = wx.FileDialog(frame, 'Save converted audio to a new file', defaultFile=source.stem + '-converted.wav',
            wildcard='WAV (*.wav)|*.wav|FLAC (*.flac)|*.flac|MP3 (*.mp3)|*.mp3|Opus (*.opus)|*.opus|M4A (*.m4a)|*.m4a', style=wx.FD_SAVE)
        try:
            if target_dialog.ShowModal() != wx.ID_OK:
                return
            target = Path(target_dialog.GetPath())
        finally:
            target_dialog.Destroy()
        def operation():
            from ..audio import FFmpegAudio
            return FFmpegAudio(allowed_roots=(source.parent, target.parent)).convert(source, target, cancelled=frame.cancel_event.is_set)
        frame.run_job('Converting local audio', operation, lambda result: frame.report(f'Converted audio saved: {result.path}'))

    def backup_project(event=None):
        project_dialog = wx.DirDialog(frame, 'Choose a saved Audio Studio project to back up')
        try:
            if project_dialog.ShowModal() != wx.ID_OK:
                return
            store = ProjectStore(project_dialog.GetPath())
            store.load()
        except Exception as exc:
            frame.report(str(exc))
            return
        finally:
            project_dialog.Destroy()
        folder = wx.DirDialog(frame, 'Choose where to create a new Audio Studio backup folder')
        try:
            if folder.ShowModal() != wx.ID_OK:
                return
            destination = Path(folder.GetPath()) / ('audio-backup-' + uuid4().hex)
        finally:
            folder.Destroy()
        from ..studio.backup import backup_project as snapshot
        frame.run_job('Backing up the audio project',
            lambda: snapshot(store, destination, cancelled=frame.cancel_event.is_set),
            lambda digest: show_text(frame, 'Audio Studio backup saved',
                f'Backup folder:\n{destination}\n\nManifest SHA256:\n{digest}\n\n'
                'Keep this digest separately from the backup. Restoration checks it before accepting the snapshot. '
                'The backup includes saved script revisions, durable jobs and verified completed audio. '
                'External voice engines, model files and source music assets remain separate.'))

    def restore_project(event=None):
        source_dialog = wx.DirDialog(frame, 'Choose the Audio Studio backup folder')
        try:
            if source_dialog.ShowModal() != wx.ID_OK:
                return
            source = Path(source_dialog.GetPath())
        finally:
            source_dialog.Destroy()
        digest = text_dialog(frame, 'Verify Audio Studio backup', 'Enter the independent manifest SHA256 retained when this backup was created')
        if digest is None:
            return
        digest = digest.strip().lower()
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            frame.report('A complete 64-character SHA256 digest is required. The saved backup remains unchanged.')
            return
        folder = wx.DirDialog(frame, 'Choose where to create the restored audio project')
        try:
            if folder.ShowModal() != wx.ID_OK:
                return
            destination = Path(folder.GetPath()) / ('restored-audio-' + uuid4().hex)
        finally:
            folder.Destroy()
        from ..studio.backup import restore_project as restore
        frame.run_job('Verifying and restoring the audio project',
            lambda: restore(source, destination, expected_manifest_sha256=digest,
                            cancelled=frame.cancel_event.is_set),
            lambda store: open_studio(store=store))

    def assistant(event=None):
        from ..extract.document_formats import file_dialog_wildcard
        name = text_dialog(frame, 'Local assistant', 'Name for a new library to create and populate')
        if name is None:
            return
        files = wx.FileDialog(frame, 'Choose documents for the local assistant',
            wildcard=file_dialog_wildcard(), style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        try:
            if files.ShowModal() != wx.ID_OK:
                return
            paths = [Path(p) for p in files.GetPaths()]
        finally:
            files.Destroy()
        from ..agent import LocalAgent, import_plan
        plan = import_plan(name, paths)
        preview = wx.Dialog(frame, title='Review local assistant plan', size=(700, 520))
        layout = wx.BoxSizer(wx.VERTICAL)
        view = wx.TextCtrl(preview, value=f'Create library: {name}\n\nImport the following selected files:\n' + '\n'.join(str(p) for p in paths)
            + '\n\nOriginals are retained. No network, shell, deletion or publishing tools will run.', style=wx.TE_MULTILINE | wx.TE_READONLY)
        view.SetName('Local assistant plan and selected files')
        layout.Add(view, 1, wx.EXPAND | wx.ALL, 8)
        layout.Add(preview.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 8)
        preview.SetSizer(layout)
        view.SetFocus()
        try:
            if preview.ShowModal() != wx.ID_OK:
                return
        finally:
            preview.Destroy()
        agent = LocalAgent(frame.library, frame.library.root / 'agent-jobs',
                           read_roots=tuple({p.parent for p in paths}), allow_create=True)
        frame.run_job('Running the approved local assistant plan',
            lambda: agent.run(plan, approval_sha256=plan.digest, cancelled=frame.cancel_event.is_set),
            lambda result: (frame.refresh_scopes(), frame.report(f'Local plan complete. {len(result["results"])} verified tool results.')))

    def save_all(event=None):
        if frame.working:
            frame.report('The current operation is checkpointing its own progress. Save after it finishes or pauses.')
            return
        if frame._dirty() and frame.evidence is not None:
            frame.evidence.save_note()
        else:
            frame.report('Library changes are already committed. Audio Studio uses Ctrl+S inside its editor.')

    file_index = frame.menu_bar.FindMenu('File')
    if file_index == wx.NOT_FOUND:
        menu = wx.Menu()
        item = menu.Append(wx.ID_ANY, 'Save current changes\tCtrl+S')
        frame.Bind(wx.EVT_MENU, save_all, item)
        frame.menu_bar.Insert(0, menu, '&File')
    for label, commands in (
        ('&Voice', [('Installed local voices', list_voices), ('Narrate selected text\tCtrl+Alt+V', current_text),
                    ('Connect or disable dordiai-voices', dordiai_settings)]),
        ('Audio St&udio', [('New narration or dialogue\tCtrl+Alt+U', open_studio), ('Open saved audio project', open_existing),
                         ('Back up audio project', backup_project), ('Restore audio project', restore_project), ('Convert local audio', convert)]),
        ('&Tools', [('Local assistant: create and import\tCtrl+Alt+J', assistant)])):
        menu = wx.Menu()
        for title, handler in commands:
            item = menu.Append(wx.ID_ANY, title)
            frame.Bind(wx.EVT_MENU, handler, item)
        frame.menu_bar.Append(menu, label)
    frame.studio_actions = {'new': open_studio, 'open': open_existing, 'voices': list_voices,
                            'convert': convert, 'assistant': assistant, 'save': save_all,
                            'backup': backup_project, 'restore': restore_project,
                            'dordiai_settings': dordiai_settings}
