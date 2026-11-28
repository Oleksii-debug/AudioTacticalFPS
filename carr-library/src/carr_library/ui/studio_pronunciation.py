"""Review one canonical Studio spoken line through the shared pronunciation UI."""
from dataclasses import replace

from ..durable_state import local_path
from ..speech import ReadingDocument
from ..speech._io import check_cancelled
from ..studio.pronunciation import StudioPronunciations, SpokenLineDraft
from .pronunciation_dialog import ReadingSnapshot, apply_pronunciation_preview, create_pronunciation_dialog


def native_reading_range(original, displayed, prefix, selection):
    """Map wx's normalized GetRange text without rewriting retained CR/LF bytes."""
    first, last = len(prefix), len(prefix) + len(selection)
    if not displayed.startswith(prefix) or displayed[first:last] != selection:
        raise ValueError('The native spoken-line selection changed. Select it again.')
    if displayed == original:
        return first, last
    characters, boundaries, offset = [], [0], 0
    while offset < len(original):
        character = original[offset]
        if character == '\r':
            offset += 2 if original[offset:offset + 2] == '\r\n' else 1
            characters.append('\n')
        else:
            offset += 1; characters.append(character)
        boundaries.append(offset)
    if ''.join(characters) != displayed or last >= len(boundaries):
        raise ValueError('The displayed spoken-line text changed. Open the line again.')
    return boundaries[first], boundaries[last]


def create_studio_pronunciation_dialog(frame, *, service=None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class StudioPronunciationDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Studio spoken-line pronunciation', size=(950, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self._destroy_requested = False
            self.library = frame.library
            self.root = local_path(self.library.root, exists=True)
            self.service = service or StudioPronunciations(self.root)
            self.projects, self.draft, self.last_saved = (), None, None
            self.alive, self.pending, self.closing = True, False, False
            layout = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(self, label=(
                'Choose a saved project, scene and spoken line. Review rules on a local reading draft, then Save spoken line.\n'
                'Saving creates a project revision. The original script, source references, other lines and audio clips stay unchanged.'))
            notice.Wrap(900); layout.Add(notice, 0, wx.ALL | wx.EXPAND, 7)
            def field(label, kind=wx.Choice, proportion=0, **kwargs):
                layout.Add(wx.StaticText(self, label=label), 0, wx.ALL, 4)
                control = kind(self, **kwargs); control.SetName(label.replace('&', ''))
                layout.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
                return control
            self.project_choice = field('&Project')
            self.scene_choice = field('Sc&ene')
            self.line_choice = field('Spoken &line and role')
            self.text = field('Reading text for the opened spoken line; select a range before reviewing', wx.TextCtrl,
                              proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status = field('Spoken-line pronunciation status', wx.TextCtrl,
                                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
            self.buttons = {}
            for items in (
                (('reload', 'Reload projects', self.reload), ('open', '&Open selected line', self.open_line),
                 ('review', '&Review pronunciation rules', self.review)),
                (('prepare', 'Prepare speech text...', lambda event: self.review(event, preparation=True)),),
                (('save', '&Save spoken line', self.save), ('undo', '&Undo last saved change', self.undo_saved),
                 ('discard', '&Discard unsaved line changes', self.discard)),
                (('cancel', 'Ca&ncel operation', self.cancel), ('close', '&Close', self.close))):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, callback in items:
                    options = {'id': wx.ID_CANCEL} if key == 'close' else {}
                    control = wx.Button(self, label=label, **options)
                    control.SetName(label.replace('&', '')); control.Bind(wx.EVT_BUTTON, callback)
                    self.buttons[key] = control; row.Add(control, 0, wx.ALL, 4)
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout); self.SetEscapeId(wx.ID_CANCEL)
            self.project_choice.Bind(wx.EVT_CHOICE, self.select_project)
            self.scene_choice.Bind(wx.EVT_CHOICE, self.select_scene)
            self.line_choice.Bind(wx.EVT_CHOICE, self.select_line)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.report('Load saved projects. No synthesis starts automatically.')
            self.enable()

        def guard(self):
            if (not self.alive or self.closing or getattr(frame, '_closing', False)
                    or frame.library is not self.library
                    or local_path(frame.library.root, exists=True) != self.root):
                raise ValueError('The library changed or this editor closed. Reopen the spoken-line editor.')

        def stopping(self):
            if frame.cancel_event.is_set():
                return True
            try:
                self.guard()
            except (OSError, ValueError, RuntimeError):
                return True
            return False

        def report(self, message):
            if self.alive:
                self.status.ChangeValue(str(message))

        def enable(self):
            if not self.alive:
                return
            dirty = self.draft is not None and self.draft.dirty
            idle = not self.pending and not self.closing
            for choice in (self.project_choice, self.scene_choice, self.line_choice):
                choice.Enable(idle and not dirty)
            for key, button in self.buttons.items():
                allowed = idle
                if key in ('reload', 'open'):
                    allowed = idle and not dirty
                elif key in ('review', 'prepare'):
                    allowed = idle and self.draft is not None
                elif key in ('save', 'discard'):
                    allowed = idle and dirty
                elif key == 'undo':
                    allowed = idle and not dirty and self.last_saved is not None
                elif key == 'cancel':
                    allowed = self.pending and not self.closing
                elif key == 'close':
                    allowed = True
                button.Enable(allowed)

        def run(self, label, operation, done, *, failed=None, committed=False):
            if self.pending or not self.alive or frame.working:
                return False
            try:
                self.guard()
            except Exception as exc:
                self.report(exc); return False
            self.pending = True; self.enable()
            def work():
                self.guard(); check_cancelled(self.stopping)
                result = operation()
                if not committed:
                    check_cancelled(self.stopping); self.guard()
                return result
            def failure(error):
                self.pending = False
                if not self.alive:
                    return
                if self.closing:
                    self.finish_close(); return
                try:
                    if failed is not None:
                        failed(error)
                    else:
                        self.report(error)
                finally:
                    self.enable()
            def complete(result):
                self.pending = False
                if not self.alive:
                    return
                if self.closing:
                    self.finish_close(); return
                try:
                    self.guard()
                    if not committed:
                        check_cancelled(self.stopping)
                    done(result)
                except Exception as exc:
                    failure(exc)
                finally:
                    self.enable()
            try:
                frame.run_job(label, work, complete, failure)
                if self.pending and not frame.working:
                    failure(RuntimeError('The shared operation could not start. Try again.'))
                    return False
            except Exception as exc:
                failure(exc); return False
            return True

        def selected(self):
            self.guard()
            pi, si, li = (control.GetSelection() for control in
                          (self.project_choice, self.scene_choice, self.line_choice))
            if not 0 <= pi < len(self.projects):
                raise ValueError('Choose a saved project.')
            project = self.projects[pi]
            if not 0 <= si < len(project.scenes):
                raise ValueError('Choose a scene.')
            scene = project.scenes[si]
            if not 0 <= li < len(scene.lines):
                raise ValueError('Choose a spoken line. Audio clips have no pronunciation text.')
            return project, scene.id, scene.lines[li].id

        def same_selection(self, draft):
            project, scene_id, line_id = self.selected()
            if (project.fingerprint, scene_id, line_id) != (draft.project.fingerprint, draft.scene_id, draft.line_id):
                raise ValueError('The selected project, scene or spoken line changed. Open the line again.')

        def clear_draft(self):
            if self.draft is not None and self.draft.dirty:
                self.restore_selection(self.draft.project, self.draft.scene_id, self.draft.line_id)
                raise ValueError('Save or discard the unsaved spoken-line draft before changing selection.')
            self.draft, self.last_saved = None, None
            self.text.ChangeValue(''); self.enable()

        def select_project(self, event=None):
            if not self.alive:
                return
            try:
                self.clear_draft()
                index = self.project_choice.GetSelection()
                scenes = self.projects[index].scenes if 0 <= index < len(self.projects) else ()
                self.scene_choice.Set([scene.title for scene in scenes])
                self.scene_choice.SetSelection(0 if scenes else -1)
                self.select_scene()
            except Exception as exc:
                self.report(exc)

        def select_scene(self, event=None):
            if not self.alive:
                return
            try:
                self.clear_draft()
                pi, si = self.project_choice.GetSelection(), self.scene_choice.GetSelection()
                project = self.projects[pi] if 0 <= pi < len(self.projects) else None
                scene = project.scenes[si] if project and 0 <= si < len(project.scenes) else None
                roles = {role.id: role.name for role in project.roles} if project else {}
                lines = scene.lines if scene else ()
                self.line_choice.Set([f'{index+1}. {roles[line.role]}: {line.text[:120] or "[empty line]"}'
                                      for index, line in enumerate(lines)])
                self.line_choice.SetSelection(0 if lines else -1)
                self.report('Open a spoken line to review. A scene containing only audio clips has no spoken lines.')
            except Exception as exc:
                self.report(exc)

        def select_line(self, event=None):
            if not self.alive:
                return
            try:
                self.clear_draft(); self.report('Open the selected spoken line to review its pronunciation.')
            except Exception as exc:
                self.report(exc)

        def restore_selection(self, project, scene_id, line_id):
            self.projects = tuple(project if item.id == project.id else item for item in self.projects)
            self.project_choice.Set([f'{item.title}; revision {item.revision}' for item in self.projects])
            self.project_choice.SetSelection(next(i for i, item in enumerate(self.projects) if item.id == project.id))
            self.scene_choice.Set([scene.title for scene in project.scenes])
            si = next(i for i, scene in enumerate(project.scenes) if scene.id == scene_id)
            self.scene_choice.SetSelection(si)
            scene = project.scenes[si]; roles = {role.id: role.name for role in project.roles}
            self.line_choice.Set([f'{index+1}. {roles[line.role]}: {line.text[:120] or "[empty line]"}'
                                  for index, line in enumerate(scene.lines)])
            self.line_choice.SetSelection(next(i for i, line in enumerate(scene.lines) if line.id == line_id))

        def reload(self, event=None):
            if not self.alive:
                return False
            try:
                self.clear_draft()
                def loaded(projects):
                    self.projects = projects
                    self.project_choice.Set([f'{item.title}; revision {item.revision}' for item in projects])
                    self.project_choice.SetSelection(0 if projects else -1)
                    self.select_project()
                return self.run('Loading Studio spoken-line projects',
                                lambda: self.service.list_projects(cancelled=self.stopping), loaded)
            except Exception as exc:
                self.report(exc); return False

        def open_line(self, event=None):
            if not self.alive:
                return False
            try:
                self.clear_draft(); selected = self.selected()
                def loaded(draft):
                    self.same_selection(draft)
                    self.draft = draft; self.text.ChangeValue(draft.document.text)
                    self.report(f'Opened {draft.role.name}; language {draft.role.language or "unspecified"}; '
                        f'voice {draft.role.voice_id or "unassigned"}. Select a text range if needed, then Review pronunciation rules.')
                    self.enable(); self.text.SetFocus()
                return self.run('Opening a Studio spoken-line draft',
                    lambda: self.service.open_line(*selected, cancelled=self.stopping), loaded)
            except Exception as exc:
                self.report(exc); return False

        def draft_guard(self, draft, signature=None):
            self.guard()
            if self.draft is not draft or (signature is not None and draft.document._signature() != signature):
                raise ValueError('The spoken-line reading draft changed. Review it again.')

        def review(self, event=None, *, preparation=False):
            if not self.alive:
                return
            try:
                self.guard()
                if self.pending or self.draft is None:
                    raise ValueError('Open a spoken line and finish its current operation first.')
                draft = self.draft; self.same_selection(draft)
                if preparation:
                    from .speech_preparation import capture_speech, apply_speech, create_speech_preparation
                def guard():
                    self.draft_guard(draft)
                def capture():
                    guard(); self.same_selection(draft)
                    first, last = self.text.GetSelection()
                    prefix, selection = self.text.GetRange(0, first), self.text.GetRange(first, last)
                    start, end = native_reading_range(draft.document.text, self.text.GetValue(), prefix, selection)
                    capture_current = capture_speech if preparation else ReadingSnapshot.capture
                    return capture_current(draft.document, (start, end),
                        language=draft.role.language, voice_id=draft.role.voice_id)
                def apply(preview):
                    if preparation:
                        apply_speech(draft.document, preview, capture())
                    else:
                        apply_pronunciation_preview(draft.document, preview, capture())
                    self.text.ChangeValue(draft.document.text)
                    return capture(), 'This is a local Studio line draft. Close this dialog and Save spoken line to create a project revision.'
                def undo(expected):
                    if capture() != expected or not draft.document.undo():
                        raise ValueError('The spoken-line draft changed after applying. Reopen its review.')
                    self.text.ChangeValue(draft.document.text)
                    return 'The saved Studio project has not been changed.'
                def run(operation, done, *, on_failed, committed=True):
                    # The shared dialog owns cancellation/commit classification
                    # for its dictionary saves and preview results. Older shared
                    # dialogs omit this keyword and classify their own results.
                    return self.run('Reviewing Studio pronunciation rules', operation, done,
                                    failed=on_failed, committed=committed)
                if preparation:
                    child = create_speech_preparation(self, capture=capture, apply=apply, undo=undo,
                        guard=guard, run=run, cancel=frame.cancel_event, wx_module=wx)
                else:
                    child = create_pronunciation_dialog(self, library_root=self.root, capture=capture,
                        apply=apply, undo=undo, guard=guard, run=run, cancel=frame.cancel_event, wx_module=wx)
                try:
                    if not preparation:
                        child.on_load()
                    child.ShowModal()
                finally:
                    if child and child.alive:
                        child.finish_close()
                    if child:
                        child.Destroy()
                if self.alive:
                    self.enable()
                    self.report('Unsaved spoken-line draft. Save spoken line creates a project revision; Discard restores the opened line.'
                                if draft.dirty else 'Spoken line unchanged. Original script and saved project are preserved.')
            except Exception as exc:
                self.report(exc)

        def save(self, event=None):
            if not self.alive:
                return False
            try:
                if self.pending or self.draft is None or not self.draft.dirty:
                    raise ValueError('Apply a reviewed pronunciation change before saving the spoken line.')
                draft = self.draft; self.same_selection(draft)
                signature = draft.document._signature()
                copied = ReadingDocument(draft.document.source_text, title=draft.document.title)
                copied.set_text(draft.document.text, label='Reviewed Studio pronunciation')
                frozen = replace(draft, document=copied)
                def guard():
                    self.draft_guard(draft, signature)
                def saved(change):
                    self.same_selection(draft)
                    self.accept_saved(change); self.last_saved = change
                    self.report(f'Spoken line saved as project revision {change.after.revision}. '
                                'Original script and all other performance settings are preserved.')
                return self.run('Saving a reviewed Studio spoken line',
                    lambda: self.service.save_line(frozen, cancelled=self.stopping, guard=guard), saved, committed=True)
            except Exception as exc:
                self.report(exc); return False

        def accept_saved(self, change):
            self.restore_selection(change.after, change.scene_id, change.line_id)
            scene = next(item for item in change.after.scenes if item.id == change.scene_id)
            line = next(item for item in scene.lines if item.id == change.line_id)
            self.draft = SpokenLineDraft(change.after, scene.id, line.id, ReadingDocument(line.text))
            self.text.ChangeValue(line.text); self.enable()

        def undo_saved(self, event=None):
            if not self.alive:
                return False
            try:
                if self.pending or self.draft is None or self.draft.dirty or self.last_saved is None:
                    raise ValueError('Save or discard the current draft before undoing its last saved pronunciation.')
                draft, change = self.draft, self.last_saved
                self.same_selection(draft)
                if draft.project.fingerprint != change.after.fingerprint:
                    raise ValueError('Open the exact saved spoken line before undoing it.')
                def undone(result):
                    self.same_selection(draft); self.accept_saved(result); self.last_saved = None
                    self.report(f'Saved pronunciation undone in project revision {result.after.revision}. Earlier revisions remain in history.')
                return self.run('Undoing a saved Studio pronunciation',
                    lambda: self.service.undo_line(change, cancelled=self.stopping,
                                                  guard=lambda: self.draft_guard(draft)), undone, committed=True)
            except Exception as exc:
                self.report(exc); return False

        def discard(self, event=None):
            if not self.alive:
                return
            if not self.pending and self.draft is not None:
                self.draft = replace(self.draft, document=ReadingDocument(self.draft.line.text))
                self.text.ChangeValue(self.draft.document.text); self.enable()
                self.report('Unsaved line changes discarded. The saved project is unchanged.')

        def cancel(self, event=None):
            if not self.alive or not self.pending:
                return
            frame.cancel_event.set(); self.report('Cancellation requested; waiting for the shared worker.'); self.enable()

        def dispose(self):
            """Detach callbacks without consulting destroyed native controls."""
            if not getattr(self, 'alive', False):
                return
            self.alive, self.closing = False, True
            if self.pending:
                frame.cancel_event.set()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self.dispose()
            event.Skip()

        def Destroy(self):
            self.dispose()
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

        def finish_close(self):
            if not self.alive:
                return
            self.dispose()
            if self:
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                else:
                    self.Hide()

        def close(self, event=None):
            if not self.alive:
                return
            if self.pending:
                self.closing = True; frame.cancel_event.set(); self.enable()
                self.report('Waiting for the shared worker to finish before closing.'); return
            if self.draft is not None and self.draft.dirty:
                answer = wx.MessageBox('Discard the unsaved spoken-line draft? Saved project revisions and dictionaries remain intact.',
                    'Unsaved spoken-line changes', wx.YES_NO | wx.ICON_QUESTION, self)
                if not self.alive or not self or answer != wx.YES:
                    return
            self.finish_close()

    return StudioPronunciationDialog()


def show_studio_pronunciations(frame):
    dialog = create_studio_pronunciation_dialog(frame)
    try:
        dialog.reload(); dialog.ShowModal()
    finally:
        if dialog.alive:
            dialog.close()
        if dialog and not dialog.pending:
            dialog.Destroy()

