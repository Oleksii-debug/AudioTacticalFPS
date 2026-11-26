"""Keyboard-first authoring, local production, save/resume and playback."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
import uuid

from ..audio.contracts import AudioProject, Line, MusicCue, Role, Scene, parse_script
from ..audio.drafts import DraftStore, new_session_id
from ..audio.project import ProjectStore

_UNCHANGED = object()


class StudioSession:
    """The same persisted project is used by the desktop and headless renderer."""
    def __init__(self):
        self.store = None
        self.project = None
        self.file_sha256 = None
        self.voices = ()
        self.overrides = {}
        self.dirty = False
        self.session_id = new_session_id()
        self.draft_store = None
        self.draft_cleanup_error = ""

    def open(self, root):
        store = ProjectStore(root)
        drafts = DraftStore(root)
        if store.path.exists() or store.path.is_symlink():
            loaded = store.load()
            self.store, self.project, self.file_sha256 = store, loaded.project, loaded.file_sha256
        else:
            if not drafts.list():
                raise ValueError("This folder contains neither a saved audio project nor recovery drafts.")
            self.store = self.project = self.file_sha256 = None
        self.draft_store = drafts
        self.overrides = {r.name.casefold(): r for r in self.project.roles} if self.project else {}
        self.dirty = False
        return self.project

    def reserve_draft_folder(self, root):
        if self.draft_store is not None:
            raise ValueError("This editor already has a project folder. Use Save As to change it.")
        self.draft_store = DraftStore.reserve(root)
        return self.draft_store.root

    def autosave(self, *, text, title, settings):
        if self.draft_store is None:
            return None
        return self.draft_store.save(self.session_id, text=text, title=title,
            settings=settings, roles=tuple(self.overrides.values()), base_project_sha256=self.file_sha256)

    def recover(self, draft):
        """Restore editor data only; callers must explicitly choose a draft."""
        draft.payload()
        self.file_sha256 = draft.base_project_sha256
        self.overrides = {role.name.casefold(): role for role in draft.roles}
        self.dirty = True
        # Imported executable paths remain archival data, never configuration.
        return {key: value for key, value in draft.settings.items() if key not in ("ffmpeg", "ffprobe")}

    def discard_draft(self):
        if self.draft_store is not None:
            self.draft_store.discard(self.session_id)

    def prepare(self, script, *, title="Audio project", music=_UNCHANGED, export_format="wav",
                stems=True, interpret_speakers=True):
        previous = self.project
        if previous is not None and script == previous.source_text and interpret_speakers == previous.interpret_speaker_syntax:
            scenes = previous.scenes if music is _UNCHANGED else tuple(replace(scene, music=music) for scene in previous.scenes)
            roles = tuple(replace(self.overrides.get(role.name.casefold(), role), id=role.id, name=role.name)
                          for role in previous.roles)
            self.project = replace(previous, title=title, export_format=export_format, stems=stems, scenes=scenes, roles=roles).validate()
            self.dirty = self.dirty or self.project != previous
            return self.project
        voices = self.voices or tuple(SimpleNamespace(provider=r.provider, id=r.voice)
                                      for r in (previous.roles if previous else self.overrides.values()))
        identity = self.project.id if self.project else uuid.uuid4().hex
        rate = self.project.sample_rate if self.project else 24000
        if interpret_speakers:
            project = parse_script(script, voices, title=title, project_id=identity,
                music=None if music is _UNCHANGED else music,
                sample_rate=rate, export_format=export_format, stems=stems)
        else:
            if not voices:
                raise ValueError("Choose an installed local voice first.")
            voice = voices[0]
            role = Role("role-1", "Narrator", voice.provider, voice.id)
            project = AudioProject(identity, title, script, (role,),
                (Scene("scene-1", title, (Line("line-1", role.id, script,
                    source_start=0, source_end=len(script)),), music=None if music is _UNCHANGED else music),),
                sample_rate=rate, export_format=export_format, stems=stems,
                interpret_speaker_syntax=False).validate()
        assigned = []
        for role in project.roles:
            old = self.overrides.get(role.name.casefold())
            assigned.append(replace(old, id=role.id, name=role.name) if old else role)
        if previous is not None and music is _UNCHANGED:
            old_cues = {s.title: s.music for s in previous.scenes}
            project = replace(project, scenes=tuple(replace(s, music=old_cues.get(s.title)) for s in project.scenes))
        self.project = replace(project, roles=tuple(assigned)).validate()
        self.overrides.update({r.name.casefold(): r for r in self.project.roles})
        self.dirty = True
        return self.project

    def assign(self, role_name, *, provider, voice, pan=0.0, rate=1.0, volume=1.0):
        if self.project is None:
            raise ValueError("Prepare the script's speakers first.")
        role = next((r for r in self.project.roles if r.name == role_name), None)
        if role is None:
            raise ValueError("This speaker is no longer present. Prepare the script again.")
        changed = replace(role, provider=provider, voice=voice, pan=pan, rate=rate, volume=volume)
        changed.validate()
        self.overrides[role.name.casefold()] = changed
        self.project = replace(self.project, roles=tuple(changed if r.id == role.id else r for r in self.project.roles))
        self.dirty = True

    def save(self, *, new_directory=None):
        if self.project is None:
            raise ValueError("Enter and prepare a script before saving.")
        old_drafts = self.draft_store
        if new_directory is not None:
            store = ProjectStore.create(new_directory, self.project)
            loaded = store.load()
            self.store, self.file_sha256 = store, loaded.file_sha256
        elif self.store is None:
            if self.draft_store is None:
                raise ValueError("Choose a new audio project folder.")
            store = ProjectStore(self.draft_store.root)
            checksum = store.save(self.project, expected_sha256=self.file_sha256)
            self.store, self.file_sha256 = store, checksum
        else:
            self.file_sha256 = self.store.save(self.project, expected_sha256=self.file_sha256)
        self.dirty = False
        self.draft_store = DraftStore(self.store.root)
        self.draft_cleanup_error = ""
        if old_drafts is not None:
            try:
                old_drafts.discard(self.session_id)
            except (ValueError, OSError, RuntimeError) as exc:
                self.draft_cleanup_error = f"The project was committed, but this window's recovery draft could not be removed: {exc}"
        return self.store.path


def build_audio_studio(parent, *, base_directory=None, initial_text="", auto_discover=True):
    """Construct real native controls; importing this module needs no wx runtime."""
    import wx

    class Studio(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title="Local Audio Studio", size=(940, 780),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.session = StudioSession()
            self.session.dirty = bool(initial_text)
            self.running = False
            self.alive = True
            self.updating = False
            self.music_changed = False
            self.cancel_event = threading.Event()
            self.backend = self.registry = self.player = self.result = None
            self.base_directory = Path(base_directory) if base_directory else Path.home()
            layout = wx.BoxSizer(wx.VERTICAL)
            heading = wx.StaticText(self, label="Write or open a script, save the project, then render locally. "
                "Use Speaker: text, # Chapter, and [pause 2] for explicit performance directions.")
            layout.Add(heading, 0, wx.EXPAND | wx.ALL, 8)
            form = wx.FlexGridSizer(cols=2, hgap=8, vgap=5)
            form.AddGrowableCol(1)

            def field(label, control):
                control.SetName(label)
                form.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                form.Add(control, 1, wx.EXPAND)
                return control

            self.title_text = field("Project title", wx.TextCtrl(self, value="Audio project"))
            self.ffmpeg = field("Local FFmpeg executable", wx.TextCtrl(self, value=shutil.which("ffmpeg") or ""))
            self.ffprobe = field("Local ffprobe executable", wx.TextCtrl(self, value=shutil.which("ffprobe") or ""))
            layout.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            setup = wx.BoxSizer(wx.HORIZONTAL)
            self.discover_button = wx.Button(self, label="Scan installed &voices and audio tools")
            self.discover_button.Bind(wx.EVT_BUTTON, self.discover)
            setup.Add(self.discover_button, 0, wx.ALL, 4)
            self.syntax = wx.CheckBox(self, label="Interpret explicit speaker and chapter labels")
            self.syntax.SetName("Interpret explicit speaker and chapter labels")
            self.syntax.SetValue(not bool(initial_text))
            setup.Add(self.syntax, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
            self.folder_button = wx.Button(self, label="Choose project folder")
            self.folder_button.Bind(wx.EVT_BUTTON, self.choose_draft_folder)
            setup.Add(self.folder_button, 0, wx.ALL, 4)
            layout.Add(setup, 0, wx.EXPAND | wx.LEFT, 4)
            layout.Add(wx.StaticText(self, label="Author script"), 0, wx.LEFT | wx.TOP, 8)
            self.script = wx.TextCtrl(self, value=initial_text, style=wx.TE_MULTILINE | wx.TE_DONTWRAP)
            self.script.SetName("Author script")
            layout.Add(self.script, 2, wx.EXPAND | wx.ALL, 8)
            role_form = wx.FlexGridSizer(cols=6, hgap=6, vgap=4)
            for label in ("Speaker", "Local voice", "Pan: -1 left, 1 right", "Speech rate", "Voice volume", ""):
                role_form.Add(wx.StaticText(self, label=label))
            self.speaker = wx.Choice(self)
            self.speaker.SetName("Speaker to configure")
            self.voice = wx.Choice(self)
            self.voice.SetName("Installed voice for this speaker")
            self.pan = wx.SpinCtrlDouble(self, min=-1, max=1, initial=0, inc=0.1)
            self.pan.SetDigits(2)
            self.pan.SetName("Speaker pan, minus one left to one right")
            self.rate = wx.SpinCtrlDouble(self, min=0.5, max=2, initial=1, inc=0.05)
            self.rate.SetName("Speech rate multiplier")
            self.volume = wx.SpinCtrlDouble(self, min=0, max=1, initial=1, inc=0.05)
            self.volume.SetName("Speaker volume, zero to one")
            self.apply_voice = wx.Button(self, label="Apply voice se&lection")
            for control in (self.speaker, self.voice, self.pan, self.rate, self.volume, self.apply_voice):
                role_form.Add(control, 0, wx.EXPAND)
            role_form.AddGrowableCol(1)
            layout.Add(role_form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            self.speaker.Bind(wx.EVT_CHOICE, self.choose_speaker)
            self.apply_voice.Bind(wx.EVT_BUTTON, self.assign_voice)
            options = wx.BoxSizer(wx.HORIZONTAL)
            prepare = wx.Button(self, label="&Prepare speakers")
            prepare.Bind(wx.EVT_BUTTON, self.prepare)
            options.Add(prepare, 0, wx.ALL, 4)
            options.Add(wx.StaticText(self, label="Music:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
            self.music = wx.Choice(self, choices=["None", "Calm procedural", "Tense procedural", "Bright procedural"])
            self.music.SetName("Local procedural music mood")
            self.music.SetSelection(0)
            options.Add(self.music, 0, wx.ALL, 4)
            self.format = wx.Choice(self, choices=["wav", "flac", "mp3", "ogg", "m4a"])
            self.format.SetName("Additional export format; WAV master is always retained")
            self.format.SetSelection(0)
            options.Add(self.format, 0, wx.ALL, 4)
            self.stems = wx.CheckBox(self, label="Separate speaker and music tracks")
            self.stems.SetName("Export separate speaker and music tracks")
            self.stems.SetValue(True)
            options.Add(self.stems, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
            layout.Add(options, 0, wx.EXPAND | wx.LEFT, 4)
            self.timeline = wx.ListBox(self, size=(-1, 85))
            self.timeline.SetName("Chapter and speaker timeline")
            layout.Add(self.timeline, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            commands = wx.BoxSizer(wx.HORIZONTAL)
            self.save_button = wx.Button(self, label="&Save, Ctrl+S")
            self.open_button = wx.Button(self, label="&Open project")
            self.save_as_button = wx.Button(self, label="Save &as")
            self.render_button = wx.Button(self, label="&Render or resume")
            self.cancel_button = wx.Button(self, label="&Interrupt rendering")
            self.cancel_button.Disable()
            self.backup_button = wx.Button(self, label="&Back up project")
            for control, handler in ((self.open_button, self.open_project), (self.save_button, self.save),
                    (self.save_as_button, self.save_as), (self.render_button, self.render),
                    (self.cancel_button, self.cancel), (self.backup_button, self.backup)):
                commands.Add(control, 0, wx.ALL, 4)
                control.Bind(wx.EVT_BUTTON, handler)
            layout.Add(commands, 0, wx.EXPAND | wx.LEFT, 4)
            playback = wx.BoxSizer(wx.HORIZONTAL)
            for label, method in (("Play &master", "play"), ("Pause playback", "pause"),
                                  ("Resume playback", "resume"), ("Stop playback", "stop")):
                control = wx.Button(self, label=label)
                control.SetName(label.replace("&", ""))
                control.Bind(wx.EVT_BUTTON, lambda event, method=method: self.playback(method))
                playback.Add(control, 0, wx.ALL, 4)
            self.seek_seconds = wx.TextCtrl(self, value="0", size=(75, -1))
            self.seek_seconds.SetName("Playback position in seconds")
            playback.Add(self.seek_seconds, 0, wx.ALL, 4)
            seek = wx.Button(self, label="Seek to seconds")
            seek.Bind(wx.EVT_BUTTON, self.seek)
            playback.Add(seek, 0, wx.ALL, 4)
            effects = wx.Button(self, label="Effects and conversion")
            effects.Bind(wx.EVT_BUTTON, self.effects)
            playback.Add(effects, 0, wx.ALL, 4)
            layout.Add(playback, 0, wx.EXPAND | wx.LEFT, 4)
            self.status = wx.TextCtrl(self, value="Local only. Choose installed tools; optional models are not downloaded.",
                                     style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 85))
            self.status.SetName("Audio studio status and output location")
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            close = wx.Button(self, wx.ID_CANCEL, "&Close")
            close.Bind(wx.EVT_BUTTON, self.close)
            layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
            self.SetSizer(layout)
            self.SetMinSize((800, 650))
            self.Bind(wx.EVT_CLOSE, self.close)
            save_id, open_id, save_as_id = wx.NewIdRef(), wx.NewIdRef(), wx.NewIdRef()
            self.Bind(wx.EVT_MENU, self.save, id=save_id)
            self.Bind(wx.EVT_MENU, self.open_project, id=open_id)
            self.Bind(wx.EVT_MENU, self.save_as, id=save_as_id)
            self.SetAcceleratorTable(wx.AcceleratorTable([
                (wx.ACCEL_CTRL, ord("S"), save_id), (wx.ACCEL_CTRL, ord("O"), open_id),
                (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("S"), save_as_id)]))
            self.autosave_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._save_draft, self.autosave_timer)
            for control, event_type in ((self.script, wx.EVT_TEXT), (self.title_text, wx.EVT_TEXT),
                    (self.syntax, wx.EVT_CHECKBOX), (self.music, wx.EVT_CHOICE),
                    (self.format, wx.EVT_CHOICE), (self.stems, wx.EVT_CHECKBOX),
                    (self.ffmpeg, wx.EVT_TEXT), (self.ffprobe, wx.EVT_TEXT),
                    (self.voice, wx.EVT_CHOICE), (self.pan, wx.EVT_SPINCTRLDOUBLE),
                    (self.rate, wx.EVT_SPINCTRLDOUBLE), (self.volume, wx.EVT_SPINCTRLDOUBLE)):
                control.Bind(event_type, self.changed)
            self.script.SetFocus()
            if auto_discover:
                wx.CallAfter(self.discover)

        def report(self, message, *, focus=False):
            if self.alive:
                self.status.ChangeValue(str(message))
                if focus:
                    self.status.SetInsertionPoint(0)
                    self.status.SetFocus()

        def changed(self, event):
            if not self.updating:
                self.session.dirty = True
                if event.GetEventObject() is self.music:
                    self.music_changed = True
                if event.GetEventObject() in (self.ffmpeg, self.ffprobe):
                    self.backend = self.registry = None
                self.schedule_draft()
            event.Skip()

        def schedule_draft(self):
            if self.alive and self.session.draft_store is not None:
                self.autosave_timer.StartOnce(1000)

        def _draft_settings(self):
            index = self.voice.GetSelection()
            voice = self.session.voices[index] if 0 <= index < len(self.session.voices) else None
            return {"interpret_speakers": self.syntax.GetValue(), "stems": self.stems.GetValue(),
                    "export_format": self.format.GetStringSelection(),
                    "music_mood": (None, "calm", "tense", "bright")[self.music.GetSelection()],
                    "music_changed": self.music_changed, "ffmpeg": self.ffmpeg.GetValue(),
                    "ffprobe": self.ffprobe.GetValue(), "speaker_name": self.speaker.GetStringSelection(),
                    "voice_provider": voice.provider if voice else "", "voice_id": voice.id if voice else "",
                    "pan": self.pan.GetValue(), "rate": self.rate.GetValue(), "volume": self.volume.GetValue()}

        def _save_draft(self, event=None):
            if not self.alive or self.session.draft_store is None or not self.session.dirty:
                return True
            try:
                self.session.autosave(text=self.script.GetValue(), title=self.title_text.GetValue(),
                                      settings=self._draft_settings())
                return True
            except (ValueError, OSError, RuntimeError) as exc:
                self.report(f"Recovery draft was not saved. Your editor remains open. {exc}")
                return False

        def choose_draft_folder(self, event=None):
            if self.session.draft_store is not None:
                self.report(f"Recovery drafts are stored in: {self.session.draft_store.root}. Use Save As to change projects.", focus=True)
                return True
            directory = self.choose_new_folder()
            if directory is None:
                return False
            try:
                self.session.reserve_draft_folder(directory)
                self.session.dirty = True
                if not self._save_draft():
                    return False
                self.report(f"Recovery folder selected: {directory}. Drafts save after edits; Ctrl+S commits a valid project.", focus=True)
                return True
            except (ValueError, OSError, RuntimeError) as exc:
                self.report(exc, focus=True)
                return False

        def worker(self, title, operation, success):
            if self.running:
                self.report("An audio operation is running. Cancel it or wait for completion.", focus=True)
                return
            self.running = True
            self.cancel_event.clear()
            for button in (self.discover_button, self.open_button, self.save_as_button,
                           self.render_button, self.backup_button):
                button.Disable()
            self.cancel_button.Enable()
            self.report(title, focus=True)

            def finish(result, error):
                if not self.alive:
                    return
                self.running = False
                self.cancel_button.Disable()
                for button in (self.discover_button, self.open_button, self.save_as_button,
                               self.render_button, self.backup_button):
                    button.Enable()
                if error is not None:
                    self.report(str(error), focus=True)
                else:
                    success(result)

            def run():
                try:
                    result = operation()
                except Exception as exc:
                    wx.CallAfter(finish, None, exc)
                else:
                    wx.CallAfter(finish, result, None)
            threading.Thread(target=run, daemon=True, name="carr-audio-operation").start()

        def discover(self, event=None):
            ffmpeg = self.ffmpeg.GetValue().strip() or None
            ffprobe = self.ffprobe.GetValue().strip() or None

            def operation():
                from ..audio.ffmpeg import AudioBackend
                from ..audio.voices import discover_local_providers
                backend = AudioBackend(ffmpeg, ffprobe,
                    sample_rate=self.session.project.sample_rate if self.session.project else 24000)
                registry = discover_local_providers(ffmpeg=backend.ffmpeg)
                return backend, registry, registry.list_voices(), registry.availability()

            def done(result):
                if ffmpeg != (self.ffmpeg.GetValue().strip() or None) or ffprobe != (self.ffprobe.GetValue().strip() or None):
                    self.backend = self.registry = None
                    self.report("Tool paths changed while scanning. Scan the selected tools again.", focus=True)
                    return
                self.backend, self.registry, self.session.voices, availability = result
                self.voice.SetItems([f"{v.name}; {', '.join(v.languages)}; {v.provider}" for v in self.session.voices])
                if self.session.voices:
                    self.voice.SetSelection(0)
                self.report("\n".join(f"{a.provider}: {a.voices} voices. {a.reason}" for a in availability), focus=True)
            self.worker("Checking installed local audio tools and voices…", operation, done)

        def prepare(self, event=None):
            try:
                mood = (None, "calm", "tense", "bright")[self.music.GetSelection()]
                project = self.session.prepare(self.script.GetValue(), title=self.title_text.GetValue(),
                    music=(MusicCue(mood=mood) if mood else None) if self.music_changed or self.session.project is None else _UNCHANGED,
                    export_format=self.format.GetStringSelection(), stems=self.stems.GetValue(),
                    interpret_speakers=self.syntax.GetValue())
                self.speaker.SetItems([r.name for r in project.roles])
                if project.roles:
                    self.speaker.SetSelection(0)
                    self.choose_speaker()
                self.timeline.Set([f"{scene.title}: {len(scene.lines)} spoken lines" for scene in project.scenes])
                if event is not None:
                    self.report(f"Prepared {len(project.scenes)} chapters and {len(project.roles)} speakers. "
                                "Choose a speaker to adjust its voice, position, speed or volume.", focus=True)
                return True
            except (ValueError, OSError) as exc:
                self.report(exc, focus=True)
                return False

        def choose_speaker(self, event=None):
            if self.speaker.GetSelection() == wx.NOT_FOUND:
                return
            role = self.session.overrides.get(self.speaker.GetStringSelection().casefold())
            if role is None:
                return
            matching = next((i for i, v in enumerate(self.session.voices)
                             if v.provider == role.provider and v.id == role.voice), wx.NOT_FOUND)
            self.voice.SetSelection(matching)
            self.pan.SetValue(role.pan)
            self.rate.SetValue(role.rate)
            self.volume.SetValue(role.volume)

        def assign_voice(self, event=None):
            try:
                index = self.voice.GetSelection()
                if not 0 <= index < len(self.session.voices):
                    raise ValueError("Choose an available installed voice.")
                voice = self.session.voices[index]
                self.session.assign(self.speaker.GetStringSelection(), provider=voice.provider,
                    voice=voice.id, pan=self.pan.GetValue(), rate=self.rate.GetValue(), volume=self.volume.GetValue())
                self.schedule_draft()
                self.report("Speaker settings applied. Save or Render to retain them.", focus=True)
            except ValueError as exc:
                self.report(exc, focus=True)

        def choose_new_folder(self):
            chooser = wx.DirDialog(self, "Choose a parent folder for the new audio project",
                                   defaultPath=str(self.base_directory))
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return None
                return Path(chooser.GetPath()) / f"AudioProject-{uuid.uuid4().hex[:10]}"
            finally:
                chooser.Destroy()

        def save(self, event=None, *, save_as=False):
            if self.session.draft_store is None and self.session.store is None and not self.choose_draft_folder():
                return False
            self._save_draft()
            if not self.prepare():
                return False
            try:
                directory = self.choose_new_folder() if save_as else None
                if save_as and directory is None:
                    return False
                path = self.session.save(new_directory=directory)
                self.autosave_timer.Stop()
                self.music_changed = False
                warning = f"\n{self.session.draft_cleanup_error}" if self.session.draft_cleanup_error else ""
                self.report(f"Project saved: {path}{warning}", focus=True)
                return True
            except (ValueError, OSError, RuntimeError) as exc:
                self.report(f"Project was not saved. Your script is still in the editor. {exc}", focus=True)
                return False

        def save_as(self, event=None):
            self.save(event, save_as=True)

        def _discard_allowed(self):
            if not self.session.dirty:
                return True
            self._save_draft()
            question = wx.MessageDialog(self, "Save the current script before continuing? No discards only this window's draft; Cancel keeps the editor open.",
                "Unsaved audio project", wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION)
            try:
                answer = question.ShowModal()
            finally:
                question.Destroy()
            if answer == wx.ID_YES:
                return self.save()
            if answer == wx.ID_NO:
                try:
                    self.session.discard_draft()
                except (ValueError, OSError, RuntimeError) as exc:
                    self.report(f"The recovery draft could not be discarded. {exc}", focus=True)
                    return False
                self.autosave_timer.Stop()
                return True
            return False

        def open_project(self, event=None):
            if self.running or not self._discard_allowed():
                return
            chooser = wx.DirDialog(self, "Open an audio project or recovery-draft folder",
                                   defaultPath=str(self.base_directory))
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                project = self.session.open(chooser.GetPath())
                if self.player is not None:
                    self.player.close()
                    self.player = None
                self.updating = True
                self.script.ChangeValue(project.source_text if project else "")
                self.title_text.ChangeValue(project.title if project else "Audio project")
                self.syntax.SetValue(project.interpret_speaker_syntax if project else True)
                moods = {s.music.mood for s in project.scenes if s.music} if project else set()
                self.music.SetSelection((None, "calm", "tense", "bright").index(next(iter(moods))) if len(moods) == 1 else 0)
                self.format.SetStringSelection(project.export_format if project else "wav")
                self.stems.SetValue(project.stems if project else True)
                self.speaker.SetItems([r.name for r in project.roles] if project else [])
                if project and project.roles:
                    self.speaker.SetSelection(0)
                self.choose_speaker()
                self.timeline.Set([f"{s.title}: {len(s.lines)} spoken lines" for s in project.scenes] if project else [])
                self.result = None
                self.music_changed = False
                self.report(f"Project folder opened: {self.session.draft_store.root}.", focus=True)
                if project and self.backend is not None and self.backend.sample_rate != project.sample_rate:
                    self.backend = self.registry = None
                    self.report("Project opened. Scan local tools again for this project's sample rate.", focus=True)
                self._offer_recovery()
            except (ValueError, OSError, RuntimeError) as exc:
                self.report(exc, focus=True)
            finally:
                self.updating = False
                chooser.Destroy()
                if self.session.dirty:
                    self.schedule_draft()

        def _offer_recovery(self):
            drafts = self.session.draft_store.list()
            if not drafts:
                return
            from datetime import datetime
            labels = ["Keep the saved project; recover nothing"] + [
                f"{draft.title or 'Untitled'} — {datetime.fromtimestamp(draft.saved_at_ns / 1_000_000_000).isoformat(sep=' ', timespec='seconds')} — window {draft.session_id[:8]} — revision {draft.revision}"
                for draft in drafts]
            chooser = wx.SingleChoiceDialog(self, "Choose a recovery draft to load into the editor. Nothing will be rendered or executed.",
                                           "Recover unsaved audio edits", labels)
            chooser.SetSelection(0)
            try:
                if chooser.ShowModal() != wx.ID_OK or chooser.GetSelection() < 1:
                    return
                draft = drafts[chooser.GetSelection() - 1]
                settings = self.session.recover(draft)
                self.script.ChangeValue(draft.text)
                self.title_text.ChangeValue(draft.title)
                self.syntax.SetValue(settings.get("interpret_speakers", True))
                self.stems.SetValue(settings.get("stems", True))
                self.format.SetStringSelection(settings.get("export_format", "wav"))
                self.music.SetSelection((None, "calm", "tense", "bright").index(settings.get("music_mood")))
                self.music_changed = settings.get("music_changed", False)
                self.speaker.SetItems([role.name for role in draft.roles])
                self.speaker.SetStringSelection(settings.get("speaker_name", ""))
                selected = next((i for i, voice in enumerate(self.session.voices)
                    if voice.provider == settings.get("voice_provider") and voice.id == settings.get("voice_id")), wx.NOT_FOUND)
                self.voice.SetSelection(selected)
                self.pan.SetValue(settings.get("pan", 0))
                self.rate.SetValue(settings.get("rate", 1))
                self.volume.SetValue(settings.get("volume", 1))
                self.timeline.Set(["Recovered editor text; prepare speakers to validate the current script."])
                # Never restore tool paths or discover engines from imported data.
                self._save_draft()
                self.report("Recovery draft loaded into this window. Ctrl+S validates and commits it. Other windows' drafts are retained; local tool settings were kept.", focus=True)
            finally:
                chooser.Destroy()

        def render(self, event=None):
            if self.running:
                return
            if self.backend is None or self.registry is None:
                self.report("Scan the installed voices and audio tools first.", focus=True)
                return
            if not self.save():
                return
            from ..audio.director import render_project
            store, backend, registry = self.session.store, self.backend, self.registry
            rendered_revision = self.session.project.sha256
            if self.player is not None:
                self.player.stop()

            def progress(row):
                wx.CallAfter(self.report, row["message"])

            def done(result):
                self.result = result
                changed = "\nThe editor has changed since this render started; render again for the newer revision." if self.session.project.sha256 != rendered_revision else ""
                self.report(f"Audio completed and verified. Duration {result.duration_seconds:.2f} seconds. "
                    f"New checkpoints: {result.created}; reused: {result.reused}.\nMaster: {result.master}\n"
                    f"Report, chapters, subtitles and selected stems: {result.manifest.parent}{changed}", focus=True)
            self.worker("Rendering the saved local production…",
                lambda: render_project(store, backend=backend, registry=registry,
                                       cancel=self.cancel_event, progress=progress), done)

        def cancel(self, event=None):
            self.cancel_event.set()
            self.report("Cancellation requested. Completed audio checkpoints are retained for Resume.", focus=True)

        def backup(self, event=None):
            if self.session.store is None:
                self.report("Commit one valid project with Ctrl+S before making a portable backup. Recovery drafts remain in the chosen local folder.", focus=True)
                return
            if not self._save_draft():
                return
            target = self.choose_new_folder()
            if target is None:
                return
            self.worker("Copying and verifying the audio project backup…",
                lambda: self.session.store.backup(target, cancel=self.cancel_event),
                lambda checksum: self.report(f"Backup complete: {target}\nManifest SHA256: {checksum}", focus=True))

        def playback(self, method):
            try:
                if method == "play":
                    if self.result is None:
                        raise ValueError("Render or resume this project first to select a verified master.")
                    from ..audio.player import NativePlayer
                    if self.player is not None:
                        self.player.close()
                    self.player = NativePlayer()
                    self.player.open(self.result.master)
                    self.player.play()
                elif self.player is None:
                    raise ValueError("Play the master first.")
                else:
                    getattr(self.player, method)()
                self.report(f"Playback: {method}.")
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc, focus=True)

        def seek(self, event=None):
            try:
                import math
                seconds = float(self.seek_seconds.GetValue())
                if not math.isfinite(seconds) or seconds < 0 or self.player is None:
                    raise ValueError("Play the master and enter a nonnegative time in seconds.")
                self.player.seek(round(seconds * 1000))
                self.report(f"Playback position: {seconds:g} seconds.", focus=True)
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc, focus=True)

        def effects(self, event=None):
            if self.running:
                self.report("Finish or cancel rendering before opening effects.", focus=True)
                return
            from .audio_effects import show_audio_effects
            show_audio_effects(self, source=self.result.master if self.result else None,
                              ffmpeg=self.ffmpeg.GetValue().strip() or None,
                              ffprobe=self.ffprobe.GetValue().strip() or None)

        def close(self, event=None):
            if self.running:
                self.cancel()
                self.report("Stopping the current audio operation. Close again after it stops; checkpoints remain saved.", focus=True)
                if hasattr(event, "Veto"):
                    event.Veto()
                return
            if not self._discard_allowed():
                if hasattr(event, "Veto"):
                    event.Veto()
                return
            if self.player is not None:
                self.player.close()
            self.autosave_timer.Stop()
            self.alive = False
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Destroy()

    return Studio()


def show_audio_studio(parent, *, base_directory=None, initial_text=""):
    dialog = build_audio_studio(parent, base_directory=base_directory, initial_text=initial_text)
    try:
        return dialog.ShowModal()
    finally:
        dialog.autosave_timer.Stop()
        if dialog.alive:
            dialog.alive = False
        dialog.Destroy()


def install_audio_studio_actions(frame):
    import wx
    menu = wx.Menu()
    studio = menu.Append(wx.ID_ANY, "Open &studio…\tCtrl+Alt+A")
    effects = menu.Append(wx.ID_ANY, "Audio effects and &conversion…")

    def open_studio(event):
        if getattr(frame, "working", False):
            frame.report("Finish or cancel the current library operation before opening the studio.")
            return
        # Audio projects are selected explicitly and never become library source
        # documents or citations merely because they were generated here.
        show_audio_studio(frame, base_directory=frame.library.root.parent)

    def open_effects(event):
        from .audio_effects import show_audio_effects
        show_audio_effects(frame)

    frame.Bind(wx.EVT_MENU, open_studio, studio)
    frame.Bind(wx.EVT_MENU, open_effects, effects)
    frame.GetMenuBar().Append(menu, "A&udio Studio")
