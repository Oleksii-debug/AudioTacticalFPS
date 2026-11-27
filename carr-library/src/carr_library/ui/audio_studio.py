"""Native keyboard-first script editor, voice assignment and resumable production.

The controller is UI-independent. Workers never read wx controls and callbacks
check dialog lifetime. No waveform, mouse gesture or visual-only state is needed.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from ..audio.projects import ProjectStore, Settings, parse_script, read_json
from ..audio.director import ProductionDirector
from ..audio.voices import VoiceRegistry


def prepare_reading_studio(root,page,registry=None):
    """Create a detached reading copy from an already authenticated source page."""
    from ..reader.reading_desk import ReadingPage
    from hashlib import sha256
    if type(page) is not ReadingPage or sha256(page.raw_text.encode('utf-8')).hexdigest()!=page.location.text_sha256:
        raise ValueError('Reopen the exact source page before preparing narration.')
    controller=StudioController(root,registry=registry)
    controller.source={key:getattr(page.location,key) for key in
                       ('workspace','edition','revision','kind','index','text_sha256')}
    controller.original_script=page.raw_text
    controller.directives=False
    return controller


class StudioController:
    def __init__(self, root, registry=None, project_id=None, draft_id=None):
        self.store = ProjectStore(root)
        self.registry = registry or VoiceRegistry.for_library(root)
        self.voices = self.registry.list_voices()
        self.project = self.store.load(project_id) if project_id else None
        self.source = self.project.source if self.project else {}
        self.original_script=self.project.script if self.source else None
        self.directives = not bool(self.source)
        self.recovery_draft=None
        self.recovery_metadata={}
        self.recovery_candidates=()
        self.recovery_selected=False
        self.cleanup_warning=''
        self.draft_id=uuid4().hex
        self.base_revision=self.project.revision if self.project else 0
        self.base_fingerprint=self.project.fingerprint if self.project else None
        if self.project:
            self.recovery_candidates=self.store.project_drafts(self.project.id)
            if self.recovery_candidates:
                # Discovery is data only. The native dialog requires a choice
                # before putting any candidate into the current editor.
                self.recovery_metadata=self.recovery_candidates[0]
                self.recovery_draft=self.recovery_metadata['script']
            if draft_id:
                draft=next((row for row in self.recovery_candidates if row.get('id')==draft_id),None)
                if draft is None:
                    raise ValueError('The chosen recovery draft is no longer available.')
                self.recover_draft(draft)
        elif draft_id:
            draft=next((row for row in self.store.new_drafts() if row['id']==draft_id),None)
            if draft is None:
                raise ValueError('The recovered draft is no longer available.')
            self.recover_draft(draft)

    def recover_draft(self, draft):
        """Select plain editor data; never run an engine or alter saved source."""
        self.store.validate_draft(draft, standalone='id' in draft)
        if draft.get('project_id') not in (None, self.project.id if self.project else None):
            raise ValueError('This draft belongs to another saved project.')
        source=draft.get('source', self.project.source if self.project else {})
        if self.project and source != self.project.source:
            raise ValueError('Recovery cannot change a saved project source identity.')
        if source.get('text_sha256') is not None:
            from hashlib import sha256
            if source['text_sha256'] != sha256(draft['script'].encode('utf-8')).hexdigest():
                raise ValueError('Recovery must retain the exact original source text.')
        self.source=source
        self.directives=draft.get('directives', not bool(source))
        self.original_script=draft['script'] if source else None
        self.base_revision=draft.get('base_revision') or (self.project.revision if self.project else 0)
        self.base_fingerprint=draft.get('base_fingerprint',
            self.project.fingerprint if self.project and self.base_revision==self.project.revision else None)
        self.recovery_metadata=draft
        self.recovery_draft=draft['script']
        self.recovery_selected=True
        return draft

    def save(self, title, script, language='en', *, settings=None):
        if self.project and self.base_revision != self.project.revision:
            raise ValueError('This recovery draft is based on an older project revision. Its text is retained; reopen the saved project before committing changes.')
        if self.project is not None and script == self.project.script:
            # Exact source preservation includes imported/custom cues, scene
            # music and accepted pronunciation history, not just role names.
            project=replace(self.project,title=title,settings=settings or self.project.settings,
                            revision=self.project.revision+1)
        else:
            if not self.voices:
                raise ValueError('No supported local voice was found. Install a Windows SAPI voice or configure a local voice runtime.')
            project = parse_script(script, self.voices, title=title, language=language,
                previous=self.project, settings=settings, directives=self.directives, source=self.source)
        if self.project and language!=self.project.roles[0].language:
            previous_default=self.project.roles[0].language
            project=replace(project,roles=tuple(replace(role,language=language)
                if role.language==previous_default else role for role in project.roles))
        self.project = self.store.save(project,expected_fingerprint=self.base_fingerprint) if self.project else self.store.create(project)
        self.base_revision,self.base_fingerprint=self.project.revision,self.project.fingerprint
        self.cleanup_warning=''
        try:
            self.store.discard_new_draft(self.draft_id)
        except (ValueError, OSError) as exc:
            self.cleanup_warning='Project committed, but this window\'s recovery draft could not be removed: '+str(exc)
            # Preserve the old artifact for recovery. A new identity prevents
            # its old project binding or corruption from blocking later edits.
            self.draft_id=uuid4().hex
        self.recovery_draft=None
        self.recovery_metadata={}
        self.recovery_selected=False
        return self.project

    def save_draft(self,title,script,language,format):
        if self.project:
            self.store.save_draft(self.project.id,script,title=title,language=language,format=format,
                base_revision=self.base_revision,base_fingerprint=self.base_fingerprint,
                session_id=self.draft_id,source=self.source,directives=self.directives)
        else:
            self.store.save_new_draft(self.draft_id,script,title=title,language=language,format=format,
                source=self.source,directives=self.directives)

    def set_role(self, role_id, *, voice_id, language, rate, volume, pan):
        if self.project is None:
            raise ValueError('Save the script before assigning voices.')
        if voice_id not in {voice.id for voice in self.voices}:
            raise ValueError('Select an installed local voice.')
        if role_id not in {role.id for role in self.project.roles}:
            raise ValueError('Select a role in the current script.')
        roles = tuple(replace(role, voice_id=voice_id, language=language,
            rate=rate, volume=volume, pan=pan) if role.id == role_id else role for role in self.project.roles)
        updated = replace(self.project, roles=roles, revision=self.project.revision + 1)
        self.project = self.store.save(updated,expected_fingerprint=self.base_fingerprint)
        self.base_revision,self.base_fingerprint=self.project.revision,self.project.fingerprint
        return self.project

    def plan_text(self):
        if self.project is None:
            return 'Save the script to preview its local production plan.'
        project = self.project
        roles = {role.id:role for role in project.roles}
        rows = [f'{project.title}. Revision {project.revision}. Local processing only.',
                f'{len(project.scenes)} chapters, {len(project.roles)} roles. Format {project.settings.format.upper()}.']
        for index, scene in enumerate(project.scenes, 1):
            rows.append(f'Chapter {index}: {scene.title}.')
            if scene.music:
                rows.append(f'Music: {scene.music.engine}, {scene.music.mood}, {scene.music.tempo_bpm} BPM; ducked under speech.')
            for cue in scene.cues:
                if cue.kind == 'speech':
                    role = roles[cue.role]
                    position = 'left' if role.pan < 0 else 'right' if role.pan > 0 else 'center'
                    rows.append(f'{role.name}; {role.voice_id}; {role.language}; {position}; {len(cue.text)} characters.')
                elif cue.kind == 'pause':
                    rows.append(f'Pause: {cue.seconds:g} seconds.')
                else:
                    rows.append('Imported local sound effect: ' + cue.asset[:12])
        rows.append('Generate runs synthesis, local music when requested, mixing, chapters, subtitles and voice stems. Original script is preserved.')
        return '\n'.join(rows)


class StudioDialog:
    def __init__(self, parent, controller, *, script=None, title=None):
        import wx
        self.wx, self.parent, self.controller = wx, parent, controller
        self.dialog = wx.Dialog(parent, title='Audio Studio — local production', size=(1000, 850),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-studio')
        self.cancelled = threading.Event()
        self.closed = self.busy = self.dirty = False
        self._save_library = getattr(parent, 'library', None)
        self._save_root = getattr(self._save_library, 'root', None)
        self._save_unregister = lambda: None
        self.result, self.player = None, None
        project = controller.project
        recovery=controller.recovery_metadata if controller.recovery_selected else {}
        layout = wx.BoxSizer(wx.VERTICAL)
        details = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        details.AddGrowableCol(1)
        self.title = wx.TextCtrl(self.dialog, value=title or recovery.get('title',project.title if project else 'Audio project'))
        self.language = wx.TextCtrl(self.dialog, value=recovery.get('language',project.roles[0].language if project else 'en'))
        self.format = wx.Choice(self.dialog, choices=['wav','flac','mp3','ogg','opus','m4a','m4b','aiff'])
        self.format.SetStringSelection(recovery.get('format',project.settings.format if project else 'wav'))
        for label, control in (('Project title',self.title),('Default language code (en, uk, sk)',self.language),('Output format',self.format)):
            control.SetName(label)
            details.Add(wx.StaticText(self.dialog,label=label))
            details.Add(control,0,wx.EXPAND)
        layout.Add(details,0,wx.EXPAND|wx.ALL,8)
        instructions = ('Write plain narration or Role: spoken words. Start a chapter with # Title. '
                        'Use @pause 2 for a pause and @music calm 80 for local instrumental music. '
                        'Save with Ctrl+S, inspect the plan, then Generate. No network service is used.')
        if not controller.directives:
            instructions = 'This is a reading copy of imported source text. Script directives are disabled; source and saved quotations stay unchanged.'
        help_text = wx.TextCtrl(self.dialog,value=instructions,style=wx.TE_READONLY|wx.TE_MULTILINE)
        help_text.SetName('Audio production instructions')
        help_text.SetMinSize((-1,65))
        layout.Add(help_text,0,wx.EXPAND|wx.LEFT|wx.RIGHT,8)
        self.script = wx.TextCtrl(self.dialog,value=script if script is not None else (controller.recovery_draft if controller.recovery_selected else (project.script if project else '')),
                                  style=wx.TE_MULTILINE|wx.TE_RICH2)
        self.script.SetName('Original script or narration text')
        if controller.source:
            self.script.SetEditable(False)
        layout.Add(self.script,1,wx.EXPAND|wx.ALL,8)
        roles = wx.BoxSizer(wx.HORIZONTAL)
        self.role_list = wx.ListBox(self.dialog)
        self.role_list.SetName('Speaker roles in this saved project')
        roles.Add(self.role_list,1,wx.EXPAND|wx.RIGHT,6)
        self.voice = wx.Choice(self.dialog,choices=[f'{voice.name}; {voice.engine}; {", ".join(voice.languages)}' for voice in controller.voices])
        self.voice.SetName('Installed local voice for selected role')
        roles.Add(self.voice,2,wx.EXPAND|wx.RIGHT,6)
        self.pan = wx.Choice(self.dialog,choices=['Left','Center','Right'])
        self.pan.SetName('Selected role stereo placement')
        self.pan.SetSelection(1)
        roles.Add(self.pan,0,wx.RIGHT,6)
        self.rate = wx.TextCtrl(self.dialog,value='1.0',size=(65,-1))
        self.rate.SetName('Selected role speech rate multiplier')
        roles.Add(self.rate,0,wx.RIGHT,6)
        apply_voice = wx.Button(self.dialog,label='Apply &voice')
        apply_voice.Bind(wx.EVT_BUTTON,self.apply_voice)
        roles.Add(apply_voice)
        layout.Add(roles,0,wx.EXPAND|wx.ALL,8)
        self.plan = wx.TextCtrl(self.dialog,style=wx.TE_MULTILINE|wx.TE_READONLY)
        self.plan.SetName('Production plan and voice assignments')
        self.plan.SetMinSize((-1,130))
        layout.Add(self.plan,0,wx.EXPAND|wx.LEFT|wx.RIGHT,8)
        commands = wx.WrapSizer(wx.HORIZONTAL)
        self.buttons = []
        for label, handler in [('&Save project',self.save),('Save project &as JSON',self.export),
                ('Save all open drafts',self.save_all),('Cancel Save All',self.cancel_save_all),
                ('Pronunciation and reading copy',self.edit_narration),
                ('&Generate audio',self.generate),('&Resume a saved job',self.resume),
                ('&Preview completed audio',self.preview),('Pause playback',self.pause_playback),
                ('Resume playback',self.resume_playback),('Stop playback',self.stop_playback)]:
            button = wx.Button(self.dialog,label=label)
            button.Bind(wx.EVT_BUTTON,handler)
            commands.Add(button,0,wx.ALL,3)
            self.buttons.append(button)
        self.stop = wx.Button(self.dialog,label='Pause production; keep completed chunks')
        self.stop.Bind(wx.EVT_BUTTON,lambda event:self.cancelled.set())
        self.stop.Disable()
        commands.Add(self.stop,0,wx.ALL,3)
        layout.Add(commands,0,wx.EXPAND|wx.ALL,5)
        self.status = wx.TextCtrl(self.dialog,style=wx.TE_READONLY|wx.TE_MULTILINE)
        self.status.SetName('Audio production status and errors')
        self.status.SetMinSize((-1,65))
        layout.Add(self.status,0,wx.EXPAND|wx.ALL,8)
        close = wx.Button(self.dialog,wx.ID_CLOSE,label='Close')
        close.Bind(wx.EVT_BUTTON,self.close)
        layout.Add(close,0,wx.ALIGN_RIGHT|wx.ALL,8)
        self.dialog.SetSizer(layout)
        save_id, render_id = wx.NewIdRef(), wx.NewIdRef()
        self._ids = [save_id,render_id]
        self.dialog.Bind(wx.EVT_MENU,self.save,id=int(save_id))
        self.dialog.Bind(wx.EVT_MENU,self.generate,id=int(render_id))
        self.dialog.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_CTRL,ord('S'),int(save_id)),(wx.ACCEL_CTRL,wx.WXK_RETURN,int(render_id))]))
        self.dialog.Bind(wx.EVT_CLOSE,self.close)
        self.role_list.Bind(wx.EVT_LISTBOX,self.select_role)
        self.timer = wx.Timer(self.dialog)
        self.dialog.Bind(wx.EVT_TIMER,self.autosave,self.timer)
        for control in (self.script,self.title,self.language):
            control.Bind(wx.EVT_TEXT,self.changed)
        self.format.Bind(wx.EVT_CHOICE,self.changed)
        self.apply_button = apply_voice
        self.refresh()
        if controller.recovery_selected:
            self.dirty=True
            self.say('An autosaved draft was recovered. Ctrl+S validates it and saves a new revision.')
        elif controller.recovery_candidates:
            wx.CallAfter(self.offer_recovery)
        self._saved_signature = ((project.title, project.script, project.roles[0].language, project.settings.format)
                                 if project is not None else None)
        from .save_all import coordinator_for
        coordinator = coordinator_for(parent)
        if coordinator is not None:
            self._save_unregister = coordinator.register('audio-studio.' + str(id(self)), 'Audio Studio project',
                dirty=self.unsaved, save=self.save_registered, focus=self.script.SetFocus)
        self.script.SetFocus()

    def editor_signature(self):
        return (self.title.GetValue(), self.script_text(), self.language.GetValue(),
                self.format.GetStringSelection())

    def unsaved(self):
        if self.closed:
            return False
        signature = self.editor_signature()
        return (self.dirty or bool(signature[1]) or self._saved_signature is not None) and signature != self._saved_signature

    def save_registered(self, completed):
        from .save_all import SaveResult
        if (self.closed or self.busy or getattr(self.parent, 'library', None) is not self._save_library
                or getattr(self._save_library, 'root', None) != self._save_root):
            completed(SaveResult(False, 'Pause production and keep this project in its original library before saving.'))
            return
        saved = self.save()
        completed(SaveResult(saved, '' if saved else 'Project validation or saving did not complete.'))

    def save_all(self, event=None):
        from .save_actions import save_all_open
        return save_all_open(self.parent, event, report=lambda message: self.say(message, True))

    def cancel_save_all(self, event=None):
        from .save_actions import cancel_save_all
        return cancel_save_all(self.parent, event, report=lambda message: self.say(message, True))

    def offer_recovery(self):
        if self.closed:
            return
        wx=self.wx
        rows=self.controller.recovery_candidates
        chooser=wx.SingleChoiceDialog(self.dialog,
            'Choose editor text to recover. This does not save a project or start production.',
            'Recover audio draft', ['Keep the saved project; recover nothing'] + [
                f'{row.get("title") or "Untitled"}; {row["saved_at"]}; base revision {row.get("base_revision", "unknown")}; window {row.get("id", "legacy")[:8]}'
                for row in rows])
        chooser.SetSelection(0)
        try:
            if chooser.ShowModal()!=wx.ID_OK or chooser.GetSelection()<1:
                return
            draft=self.controller.recover_draft(rows[chooser.GetSelection()-1])
            self.script.ChangeValue(draft['script'])
            self.title.ChangeValue(draft.get('title', self.title.GetValue()))
            self.language.ChangeValue(draft.get('language', self.language.GetValue()))
            self.format.SetStringSelection(draft.get('format', self.format.GetStringSelection()))
            self.dirty=True
            self.autosave()
            self.say('Draft loaded into this window. Other windows\' drafts remain unchanged. Ctrl+S validates and commits the selected revision.',True)
        except Exception as exc:
            self.say('Draft could not be recovered: '+str(exc),True)
        finally:
            chooser.Destroy()

    def say(self,message,focus=False):
        if self.closed:
            return
        self.status.ChangeValue(message)
        if focus:
            self.status.SetInsertionPoint(0)
            self.status.SetFocus()

    def changed(self,event):
        self.dirty = True
        self.timer.StartOnce(1500)
        event.Skip()

    def autosave(self,event=None):
        if not self.busy and self.dirty:
            try:
                self.controller.save_draft(self.title.GetValue(),self.script_text(),
                    self.language.GetValue(),self.format.GetStringSelection())
                self.say('Draft autosaved. Ctrl+S validates and saves a project revision.')
            except Exception as exc:
                self.say('Draft could not be saved: '+str(exc))

    def save(self,event=None):
        if self.busy:
            self.say('Pause production before changing its saved project.',True)
            return False
        try:
            signature = self.editor_signature()
            settings = replace(self.controller.project.settings if self.controller.project else Settings(),
                               format=signature[3])
            project = self.controller.save(signature[0], signature[1], signature[2], settings=settings)
            self._saved_signature = signature
            self.dirty = self.editor_signature() != signature
            self.refresh()
            warning='\n'+self.controller.cleanup_warning if self.controller.cleanup_warning else ''
            newer = ' Newer unsaved edits are retained.' if self.dirty else ''
            self.say(f'Project saved. Revision {project.revision}; {len(project.scenes)} chapters.{newer}{warning}',True)
            return True
        except Exception as exc:
            try:
                self.controller.save_draft(self.title.GetValue(),self.script_text(),
                    self.language.GetValue(),self.format.GetStringSelection())
            except Exception:
                pass
            self.say('Project not saved: '+str(exc),True)
            return False

    def script_text(self):
        # Native Windows text controls may normalize CRLF for display. Exact
        # imported source comes from its immutable authenticated snapshot.
        return self.controller.original_script if self.controller.source else self.script.GetValue()

    def refresh(self):
        project = self.controller.project
        self.role_list.Set([role.name for role in project.roles] if project else [])
        if project:
            self.role_list.SetSelection(0)
            self.select_role()
        self.plan.ChangeValue(self.controller.plan_text())

    def select_role(self,event=None):
        project = self.controller.project
        selection = self.role_list.GetSelection()
        if not project or not 0 <= selection < len(project.roles):
            return
        role = project.roles[selection]
        ids = [voice.id for voice in self.controller.voices]
        self.voice.SetSelection(ids.index(role.voice_id) if role.voice_id in ids else -1)
        self.pan.SetSelection(0 if role.pan<0 else 2 if role.pan>0 else 1)
        self.rate.ChangeValue(str(role.rate))

    def apply_voice(self,event=None):
        if self.busy or (self.dirty and not self.save()):
            return
        try:
            project = self.controller.project
            ri, vi = self.role_list.GetSelection(), self.voice.GetSelection()
            if not project or not 0<=ri<len(project.roles) or not 0<=vi<len(self.controller.voices):
                raise ValueError('Save a script and select a role and a local voice.')
            role = project.roles[ri]
            self.controller.set_role(role.id, voice_id=self.controller.voices[vi].id,
                language=self.language.GetValue(), rate=float(self.rate.GetValue()),
                volume=role.volume, pan=(-1,0,1)[self.pan.GetSelection()])
            self.refresh()
            self.say('Voice assignment saved.',True)
        except Exception as exc:
            self.say('Voice assignment not saved: '+str(exc),True)

    def edit_narration(self,event=None):
        if self.busy or ((self.dirty or not self.controller.project) and not self.save()):
            return
        try:
            from .narration_editor import show_narration_editor
            updated=show_narration_editor(self.dialog,self.controller.project)
            if updated is not None:
                self.controller.project=self.controller.store.save(updated,expected_fingerprint=self.controller.base_fingerprint)
                self.controller.base_revision=self.controller.project.revision
                self.controller.base_fingerprint=self.controller.project.fingerprint
                self.refresh()
                self.say('Reading copy saved. Generate uses these pronunciation edits; original text stays unchanged.',True)
        except Exception as exc:
            self.say('Reading copy not saved: '+str(exc),True)

    def export(self,event=None):
        wx = self.wx
        if (self.dirty or not self.controller.project) and not self.save():
            return
        dialog = wx.FileDialog(self.dialog,'Export script and voice settings',wildcard='Audio project JSON (*.json)|*.json',style=wx.FD_SAVE)
        try:
            if dialog.ShowModal()!=wx.ID_OK:
                return
            self.controller.store.export(self.controller.project.id,dialog.GetPath())
            self.say('Project JSON exported. Generated audio remains in its output folder.',True)
        except Exception as exc:
            self.say('Project export failed: '+str(exc),True)
        finally:
            dialog.Destroy()

    def generate(self,event=None):
        if self.busy or ((self.dirty or not self.controller.project) and not self.save()):
            return
        wx = self.wx
        choose = wx.DirDialog(self.dialog,'Choose the parent folder for a new audio output folder')
        try:
            if choose.ShowModal()!=wx.ID_OK:
                return
            stem = re.sub(r'[^A-Za-z0-9_-]+','-',self.controller.project.title).strip('-')[:60] or 'audio'
            destination = Path(choose.GetPath())/(stem+'-'+uuid4().hex[:8])
        finally:
            choose.Destroy()
        director = ProductionDirector(self.controller.store.root,registry=self.controller.registry)
        project_id = self.controller.project.id
        self.start(lambda:director.render(project_id,destination,cancelled=self.cancelled.is_set,progress=self.progress))

    def resume(self,event=None):
        wx = self.wx
        if self.busy or not self.controller.project:
            self.say('Open a saved project before resuming a job.',True)
            return
        director = ProductionDirector(self.controller.store.root,registry=self.controller.registry)
        rows = director.jobs(self.controller.project.id)
        if not rows:
            self.say('This project has no saved production jobs.',True)
            return
        choose=wx.SingleChoiceDialog(self.dialog,'Choose the exact saved production. It keeps its saved script revision.',
            'Resume production',[f'{i+1}. {row["state"]}; {row["created_at"]}; {row["id"]}' for i,row in enumerate(rows)])
        try:
            if choose.ShowModal()!=wx.ID_OK:
                return
            index=choose.GetSelection()
            if not 0<=index<len(rows):
                raise ValueError('Choose a production job.')
            job_id=rows[index]['id']
        finally:
            choose.Destroy()
        project_id=self.controller.project.id
        self.start(lambda:director.render(project_id,job_id=job_id,cancelled=self.cancelled.is_set,progress=self.progress))

    def start(self,operation):
        self.busy=True
        self.cancelled.clear()
        for control in (self.script,self.title,self.language,self.format,self.role_list,self.voice,self.pan,self.rate,self.apply_button,*self.buttons):
            control.Disable()
        self.stop.Enable()
        self.say('Local production started. Pause keeps completed chunks.',True)
        future=self.executor.submit(operation)
        def completed(future):
            if self.closed:
                return
            self.wx.CallAfter(self.finish,future)
        future.add_done_callback(completed)

    def progress(self,value):
        if not self.closed:
            self.wx.CallAfter(self.say,f'{value["progress"]}% — {value["message"]}')

    def finish(self,future):
        if self.closed:
            return
        self.busy=False
        for control in (self.script,self.title,self.language,self.format,self.role_list,self.voice,self.pan,self.rate,self.apply_button,*self.buttons):
            control.Enable()
        self.stop.Disable()
        try:
            self.result=future.result()
            self.say(f'Audio completed. {self.result["duration_seconds"]:.2f} seconds. Output: {self.result["output_dir"]}. '
                f'Reused {self.result["reused_steps"]} completed steps.',True)
        except Exception as exc:
            self.say('Production stopped: '+str(exc)+' Use Resume to continue the saved job.',True)

    def preview(self,event=None):
        try:
            if self.result is None:
                raise ValueError('Render or resume a completed production first.')
            from ..audio.playback import LocalPlayer
            if self.player:
                self.player.close()
            self.player=LocalPlayer()
            self.player.open(self.result['master'])
            self.player.play()
            self.say('Playback started. Use Pause or Stop playback; NVDA volume is unchanged.',True)
        except Exception as exc:
            self.say('Preview unavailable: '+str(exc),True)

    def pause_playback(self,event=None):
        self._playback('pause')

    def resume_playback(self,event=None):
        self._playback('resume')

    def stop_playback(self,event=None):
        self._playback('stop')

    def _playback(self,action):
        try:
            if self.player is None:
                raise ValueError('Open an audio preview first.')
            getattr(self.player,action)()
            self.say('Playback: '+action+'.',True)
        except Exception as exc:
            self.say(str(exc),True)

    def close(self,event=None):
        if self.busy:
            self.cancelled.set()
            self.say('Pausing production before closing. Completed chunks will be kept.',True)
            if event and hasattr(event,'Veto'):
                event.Veto()
            return
        if self.dirty:
            self.autosave()
            choice=self.wx.MessageBox('Save your edited script before closing?', 'Unsaved audio script',
                self.wx.YES_NO|self.wx.CANCEL|self.wx.ICON_QUESTION,parent=self.dialog)
            if choice==self.wx.CANCEL or (choice==self.wx.YES and not self.save()):
                if event and hasattr(event,'Veto'):
                    event.Veto()
                return
            if choice==self.wx.NO:
                try:
                    self.controller.store.discard_new_draft(self.controller.draft_id)
                except (ValueError, OSError) as exc:
                    self.say('This window\'s draft could not be discarded: '+str(exc),True)
                    if event and hasattr(event,'Veto'):
                        event.Veto()
                    return
        self._save_unregister()
        self.closed=True
        self.timer.Stop()
        if self.player:
            self.player.close()
        self.executor.shutdown(wait=False,cancel_futures=True)
        self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self):
        try:
            return self.dialog.ShowModal()
        finally:
            self._save_unregister()
            self.closed=True
            self.timer.Stop()
            self.cancelled.set()
            self.executor.shutdown(wait=False,cancel_futures=True)
            if self.player:
                self.player.close()
            self.dialog.Destroy()


def show_studio(frame,project_id=None,draft_id=None):
    root=frame.library.root
    def prepare():
        return StudioController(root,project_id=project_id,draft_id=draft_id)
    def ready(controller):
        if frame.library.root!=root:
            frame.report('The library folder changed. Open Audio Studio again.')
            return
        StudioDialog(frame,controller).show()
    frame.run_job('Discovering installed local voices',prepare,ready)


def choose_project(frame,event=None):
    import wx
    try:
        rows=ProjectStore(frame.library.root).list_projects()
        if not rows:
            frame.report('There are no saved audio projects. Choose Audio Studio, New production.')
            return
        dialog=wx.SingleChoiceDialog(frame,'Choose a saved project','Audio projects',
            [f'{i+1}. {row["title"]}; revision {row["revision"]}; {row["id"]}' for i,row in enumerate(rows)])
        try:
            if dialog.ShowModal()!=wx.ID_OK:
                return
            index=dialog.GetSelection()
            if not 0<=index<len(rows):
                raise ValueError('Choose a project.')
            project_id=rows[index]['id']
        finally:
            dialog.Destroy()
        show_studio(frame,project_id)
    except Exception as exc:
        frame.report('Audio project could not be opened: '+str(exc))


def install_audio_studio(frame):
    import wx
    menu=wx.Menu()
    new=menu.Append(wx.ID_ANY,'New &production...\tCtrl+Alt+P')
    frame.Bind(wx.EVT_MENU,lambda event:show_studio(frame),new)
    opened=menu.Append(wx.ID_ANY,'&Open saved project...')
    frame.Bind(wx.EVT_MENU,lambda event:choose_project(frame,event),opened)
    drafts=menu.Append(wx.ID_ANY,'Recover an unsaved &draft...')
    frame.Bind(wx.EVT_MENU,lambda event:choose_draft(frame),drafts)
    voices=menu.Append(wx.ID_ANY,'Local &voice engines...')
    def configure_voices(event):
        from .voice_settings import show_voice_settings
        try:
            show_voice_settings(frame,frame.library.root)
        except Exception as exc:
            frame.report('Local voice settings could not be opened: '+str(exc))
    frame.Bind(wx.EVT_MENU,configure_voices,voices)
    music=menu.Append(wx.ID_ANY,'Local &music engines and sample...')
    def configure_music(event):
        from .music_settings import show_music_settings
        try:
            show_music_settings(frame,frame.library.root)
        except Exception as exc:
            frame.report('Local music settings could not be opened: '+str(exc))
    frame.Bind(wx.EVT_MENU,configure_music,music)
    scenes = menu.Append(wx.ID_ANY, 'Scene projects and saved scene jobs...')
    def open_scene_projects(event):
        from .studio_dialog import show_studio as show_scene_studio
        show_scene_studio(frame)
    frame.Bind(wx.EVT_MENU, open_scene_projects, scenes)
    registrations = menu.Append(wx.ID_ANY, 'Scene voice and music engines...')
    def configure_scene_engines(event):
        from .engine_settings import show_engine_settings
        try:
            show_engine_settings(frame)
        except Exception as exc:
            frame.report('Scene engine settings could not open: ' + str(exc))
    frame.Bind(wx.EVT_MENU, configure_scene_engines, registrations)
    publication = menu.Append(wx.ID_ANY, 'Synchronized &EPUB3 publication...')
    def open_publication(event):
        from .publishing_dialog import show_publishing
        try:
            show_publishing(frame)
        except Exception as exc:
            frame.report('EPUB publication could not be opened: ' + str(exc))
    frame.Bind(wx.EVT_MENU, open_publication, publication)
    synchronized_reader = menu.Append(wx.ID_ANY, 'Read synchronized EPUB...')
    def open_synchronized_reader(event):
        from .media_overlay_dialog import show_media_overlay
        try:
            show_media_overlay(frame)
        except Exception as exc:
            frame.report('Synchronized EPUB reading could not open: ' + str(exc))
    frame.Bind(wx.EVT_MENU, open_synchronized_reader, synchronized_reader)
    frame.menu_bar.Insert(max(0,frame.menu_bar.GetMenuCount()-1),menu,'Audio &Studio')


def choose_draft(frame):
    import wx
    try:
        rows=ProjectStore(frame.library.root).new_drafts()
        if not rows:
            frame.report('There are no unsaved audio drafts. Drafts of existing projects reopen with their project.')
            return
        dialog=wx.SingleChoiceDialog(frame,'Choose an autosaved script','Recover audio draft',
            [f'{row["title"] or "Untitled"}; {row["saved_at"]}; {len(row["script"])} characters' for row in rows])
        try:
            if dialog.ShowModal()==wx.ID_OK:
                selection=dialog.GetSelection()
                if not 0<=selection<len(rows):
                    raise ValueError('Choose an autosaved draft.')
                show_studio(frame,draft_id=rows[selection]['id'])
        finally:
            dialog.Destroy()
    except Exception as exc:
        frame.report('Draft could not be recovered: '+str(exc))
