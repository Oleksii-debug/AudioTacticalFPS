"""Review, accept and undo pronunciation changes without editing source text.

The dialog returns one new project revision. Its caller owns optimistic saving;
Cancel returns None and cannot modify the project or the evidence database.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
import threading

from ..audio.narration import cue_derivative
from ..audio.pronunciation import (NarrationDerivative, NarrationEdit, PronunciationRule,
                                    dictionary_from_json, dictionary_to_json)
from ..audio.projects import plain
from ..audio.voices import _local_path


@dataclass(frozen=True)
class NarrationProposal:
    cue_id: str
    edit: NarrationEdit


class NarrationEditorModel:
    """A private draft with explicit proposal acceptance and original mappings."""
    def __init__(self, project):
        self.project = project
        self.cues = tuple(cue for scene in project.scenes for cue in scene.cues if cue.kind == 'speech')
        if not self.cues:
            raise ValueError('This audio project has no speech segments to edit.')
        self.roles = {role.id: role for role in project.roles}
        self._cue = {cue.id: cue for cue in self.cues}
        self.snapshots = dict(project.narration)
        self.selected = self.cues[0].id
        self.proposals = ()
        self.generation = 0
        self.proposal_generation = None

    @property
    def dirty(self):
        return self.snapshots != self.project.narration

    def derivative(self, cue_id=None):
        cue_id = cue_id or self.selected
        if cue_id not in self._cue:
            raise ValueError('Select a speech segment from this project.')
        if cue_id in self.snapshots:
            return NarrationDerivative.from_dict(self.snapshots[cue_id])
        return cue_derivative(self.project, cue_id)

    def select(self, cue_id):
        if cue_id not in self._cue:
            raise ValueError('Select a speech segment from this project.')
        self.selected = cue_id
        self.clear_preview()

    def clear_preview(self):
        self.proposals = ()
        self.proposal_generation = None

    def preview(self, rules, *, all_cues=False, cancelled=None):
        self.clear_preview()
        proposals = []
        for cue in (self.cues if all_cues else (self._cue[self.selected],)):
            if cancelled is not None and cancelled():
                raise InterruptedError('Reading-copy preview cancelled; no proposed edits were accepted.')
            role = self.roles[cue.role]
            derivative = self.derivative(cue.id)
            changes = derivative.preview_rules(rules, language=role.language, voice_id=role.voice_id,
                                               project_id=self.project.id, cancelled=cancelled)
            if len(proposals) + len(changes) > 5000:
                raise ValueError('Preview at most 5000 changes at a time. Choose a smaller segment scope.')
            proposals.extend(NarrationProposal(cue.id, edit) for edit in changes)
        self.proposals = tuple(proposals)
        self.proposal_generation = self.generation
        return self.proposals

    def _set(self, cue_id, derivative):
        self.snapshots[cue_id] = derivative.to_dict()
        self.generation += 1
        self.clear_preview()

    def accept(self):
        if not self.proposals or self.proposal_generation != self.generation:
            raise ValueError('Preview the current rules and their exact changes before applying them.')
        grouped = {}
        for proposal in self.proposals:
            grouped.setdefault(proposal.cue_id, []).append(proposal.edit)
        # Validate the complete proposal before changing any draft segment.
        changes = {cue_id: self.derivative(cue_id).apply_edits(edits) for cue_id, edits in grouped.items()}
        for cue_id, derivative in changes.items():
            self.snapshots[cue_id] = derivative.to_dict()
        self.generation += 1
        self.clear_preview()

    def reject(self, edit_id):
        self._set(self.selected, self.derivative().reject(edit_id))

    def undo(self):
        current = self.derivative()
        updated = current.undo()
        if updated != current:
            self._set(self.selected, updated)

    def redo(self):
        current = self.derivative()
        updated = current.redo()
        if updated != current:
            self._set(self.selected, updated)

    def result(self):
        if not self.dirty:
            return None
        return replace(self.project, narration=dict(self.snapshots), revision=self.project.revision + 1)

    def proposal_label(self, proposal):
        cue = self._cue[proposal.cue_id]
        edit = proposal.edit
        original = cue.text[edit.source_start:edit.source_end]
        return (f'Segment {self.cues.index(cue) + 1}; original offsets '
                f'{cue.source_start + edit.source_start}–{cue.source_start + edit.source_end}; '
                f'{original[:70]!r} to {edit.replacement[:70]!r}')


class NarrationEditorDialog:
    def __init__(self, parent, project):
        import wx
        self.wx = wx
        self.model = NarrationEditorModel(project)
        self.model.derivative()  # Fail before allocating native resources if the segment is too large.
        self.dialog = wx.Dialog(parent, title='Pronunciation and reading copy', size=(980, 840),
                                 style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='narration-preview')
        self.cancelled = threading.Event()
        self.busy = self.closed = False
        self.result = None
        self.rules = ()
        layout = wx.BoxSizer(wx.VERTICAL)
        intro = wx.TextCtrl(self.dialog, value='The original script and imported source stay unchanged. '
            'Dictionary rules use literal text, not executable regular expressions. Preview exact original offsets, '
            'then Apply reviewed changes. The resulting reading copy is used for speech and is not a source quotation. '
            'Undo, reject and Cancel remain available before saving.', style=wx.TE_MULTILINE | wx.TE_READONLY)
        intro.SetName('Reading-copy purpose and original-source boundary')
        intro.SetMinSize((-1, 75))
        layout.Add(intro, 0, wx.EXPAND | wx.ALL, 6)
        layout.Add(wx.StaticText(self.dialog, label='Original speech segment'), 0, wx.LEFT, 8)
        self.cues = wx.Choice(self.dialog, choices=[
            f'{index}. {self.model.roles[cue.role].name}; offsets {cue.source_start}–{cue.source_end}; {cue.text[:85]}'
            for index, cue in enumerate(self.model.cues, 1)])
        self.cues.SetName('Original speech segment and source offsets')
        self.cues.SetSelection(0)
        self.cues.Bind(wx.EVT_CHOICE, self.select)
        layout.Add(self.cues, 0, wx.EXPAND | wx.ALL, 6)
        self.notebook = wx.Notebook(self.dialog)
        self.original = self._page('Original source', 'Original source text; read only')
        self.reading = self._page('Reading copy', 'Current reading derivative; read only')
        self.diff = self._page('Accepted changes', 'Accepted reading changes with original offsets; read only')
        layout.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 6)
        form = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        form.AddGrowableCol(1, 1)
        self.find = wx.TextCtrl(self.dialog)
        self.replacement = wx.TextCtrl(self.dialog)
        for label, control in (('Literal text to find', self.find), ('Spoken replacement; empty deletes a match', self.replacement)):
            form.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control.SetName(label)
            form.Add(control, 1, wx.EXPAND)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 6)
        options = wx.WrapSizer(wx.HORIZONTAL)
        self.match_case = wx.CheckBox(self.dialog, label='Match &case')
        self.match_case.SetValue(True)
        self.whole_word = wx.CheckBox(self.dialog, label='Whole &words only')
        self.whole_word.SetValue(True)
        self.language_scope = wx.CheckBox(self.dialog, label='Current &language only')
        self.language_scope.SetValue(True)
        self.voice_scope = wx.CheckBox(self.dialog, label='Current &voice only')
        self.project_scope = wx.CheckBox(self.dialog, label='This &project only')
        self.project_scope.SetValue(True)
        self.all_cues = wx.CheckBox(self.dialog, label='Preview &all project segments')
        self.all_cues.Bind(wx.EVT_CHECKBOX, self.invalidate)
        for control in (self.match_case, self.whole_word, self.language_scope, self.voice_scope,
                        self.project_scope, self.all_cues):
            options.Add(control, 0, wx.ALL, 3)
        layout.Add(options, 0, wx.EXPAND | wx.ALL, 3)
        self.rules_list = wx.ListBox(self.dialog)
        self.rules_list.SetName('Literal dictionary rules and their language, voice and project scopes')
        self.rules_list.SetMinSize((-1, 60))
        layout.Add(self.rules_list, 0, wx.EXPAND | wx.ALL, 6)
        rule_buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.add_button = self._button('Add literal &rule', self.add_rule, rule_buttons)
        self.remove_rule_button = self._button('Remove selected rule', self.remove_rule, rule_buttons)
        self.import_button = self._button('&Import dictionary...', self.import_rules, rule_buttons)
        self.export_button = self._button('&Export dictionary...', self.export_rules, rule_buttons)
        self.preview_button = self._button('&Preview exact changes', self.preview, rule_buttons)
        self.apply_button = self._button('&Apply reviewed changes', self.accept, rule_buttons)
        layout.Add(rule_buttons, 0, wx.EXPAND | wx.ALL, 3)
        self.changes = wx.ListBox(self.dialog)
        self.changes.SetName('Proposed changes before acceptance; original offsets and replacement text')
        self.changes.Bind(wx.EVT_LISTBOX, self.show_proposal)
        self.changes.SetMinSize((-1, 80))
        layout.Add(self.changes, 1, wx.EXPAND | wx.ALL, 6)
        self.accepted = wx.Choice(self.dialog)
        self.accepted.SetName('Accepted changes in selected segment; choose one to reject')
        layout.Add(self.accepted, 0, wx.EXPAND | wx.ALL, 6)
        action_buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.reject_button = self._button('Reject selected change', self.reject, action_buttons)
        self.undo_button = self._button('&Undo segment edit', self.undo, action_buttons)
        self.redo_button = self._button('Re&do segment edit', self.redo, action_buttons)
        self.save_button = self._button('&Save reading copy', self.save, action_buttons)
        self.close_button = self._button('Cancel and close', self.close, action_buttons, wx.ID_CANCEL)
        layout.Add(action_buttons, 0, wx.EXPAND | wx.ALL, 3)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Narration editor status and errors')
        self.status.SetMinSize((-1, 55))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.controls = (self.cues, self.find, self.replacement, self.match_case, self.whole_word,
            self.language_scope, self.voice_scope, self.project_scope, self.all_cues, self.rules_list,
            self.add_button, self.remove_rule_button, self.import_button, self.export_button,
            self.preview_button, self.apply_button, self.changes, self.accepted, self.reject_button,
            self.undo_button, self.redo_button, self.save_button)
        self.refresh()
        self.original.SetFocus()

    def _page(self, title, name):
        wx = self.wx
        panel = wx.Panel(self.notebook)
        layout = wx.BoxSizer(wx.VERTICAL)
        control = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        control.SetName(name)
        layout.Add(control, 1, wx.EXPAND | wx.ALL, 4)
        panel.SetSizer(layout)
        self.notebook.AddPage(panel, title)
        return control

    def _button(self, title, action, layout, identity=None):
        wx = self.wx
        control = wx.Button(self.dialog, wx.ID_ANY if identity is None else identity, label=title)
        control.Bind(wx.EVT_BUTTON, action)
        layout.Add(control, 0, wx.ALL, 3)
        return control

    def say(self, message, focus=True):
        if not self.closed:
            self.status.ChangeValue(message)
            if focus:
                self.status.SetFocus()

    def refresh(self):
        derivative = self.model.derivative()
        cue = self.model._cue[self.model.selected]
        self.original.ChangeValue(derivative.source_text)
        self.reading.ChangeValue(derivative.text)
        descriptions = []
        labels = []
        for row in derivative.diff():
            positions = f'{cue.source_start + row["source_start"]}–{cue.source_start + row["source_end"]}'
            labels.append(f'Offsets {positions}: {row["original"][:60]!r} to {row["reading"][:60]!r}')
            descriptions.append(f'Original offsets: {positions}\nOriginal: {row["original"]}\n'
                                f'Reading: {row["reading"]}\nReason: {row["reason"]}')
        self.diff.ChangeValue('\n\n'.join(descriptions) or 'No accepted pronunciation changes in this segment.')
        self.accepted.Set(labels)
        if labels:
            self.accepted.SetSelection(0)
        self.rules_list.Set([f'{rule.source!r} to {rule.replacement!r}; '
            f'language={rule.language or "any"}; voice={rule.voice_id or "any"}; '
            f'project={rule.project_id or "any"}' for rule in self.rules])
        if self.rules:
            self.rules_list.SetSelection(len(self.rules) - 1)
        self.changes.Set([self.model.proposal_label(proposal) for proposal in self.model.proposals])
        for control in self.controls:
            control.Enable(not self.busy)
        if not self.busy:
            self.apply_button.Enable(bool(self.model.proposals))
            self.preview_button.Enable(bool(self.rules))
            self.export_button.Enable(bool(self.rules))
            self.remove_rule_button.Enable(bool(self.rules))
            self.reject_button.Enable(bool(derivative.edits))
            self.undo_button.Enable(bool(derivative.undo_stack))
            self.redo_button.Enable(bool(derivative.redo_stack))
            self.save_button.Enable(self.model.dirty)

    def select(self, event=None):
        try:
            index = self.cues.GetSelection()
            if not 0 <= index < len(self.model.cues):
                return
            self.model.select(self.model.cues[index].id)
            self.refresh()
            self.original.SetFocus()
        except Exception as exc:
            self.say('Segment unavailable: ' + str(exc))

    def invalidate(self, event=None):
        self.model.clear_preview()
        self.refresh()

    def add_rule(self, event=None):
        try:
            if len(self.rules) >= 1000:
                raise ValueError('A dictionary supports at most 1000 literal rules.')
            cue = self.model._cue[self.model.selected]
            role = self.model.roles[cue.role]
            rule = PronunciationRule(self.find.GetValue(), self.replacement.GetValue(),
                language=role.language if self.language_scope.GetValue() else None,
                voice_id=role.voice_id if self.voice_scope.GetValue() else None,
                project_id=self.model.project.id if self.project_scope.GetValue() else None,
                match_case=self.match_case.GetValue(), whole_word=self.whole_word.GetValue())
            self.rules = (*self.rules, rule)
            self.invalidate()
            self.say('Literal rule added. Preview exact changes before applying it.')
        except Exception as exc:
            self.say('Rule not added: ' + str(exc))

    def remove_rule(self, event=None):
        index = self.rules_list.GetSelection()
        if 0 <= index < len(self.rules):
            self.rules = self.rules[:index] + self.rules[index + 1:]
            self.invalidate()
            self.say('Rule removed from this dictionary. Already accepted edits can be rejected or undone separately.')

    def import_rules(self, event=None):
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, 'Import literal pronunciation dictionary',
                               wildcard='JSON dictionary (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = _local_path(dialog.GetPath())
            plain(path)
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('Dictionary exceeds the four MiB input limit.')
            with path.open('rb') as stream:
                data = stream.read(4 * 1024 * 1024 + 1)
            rules = dictionary_from_json(data.decode('utf-8-sig'))
            self.rules = rules
            self.invalidate()
            self.say(f'Imported {len(rules)} literal rules. Their saved scopes still apply. Preview before accepting any change.')
        except Exception as exc:
            self.say('Dictionary not imported: ' + str(exc))
        finally:
            dialog.Destroy()

    def export_rules(self, event=None):
        wx = self.wx
        dialog = wx.FileDialog(self.dialog, 'Export a new pronunciation dictionary',
                               wildcard='JSON dictionary (*.json)|*.json', style=wx.FD_SAVE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = _local_path(dialog.GetPath())
            with path.open('x', encoding='utf-8') as stream:
                stream.write(dictionary_to_json(self.rules))
            self.say('Literal dictionary exported. Existing files were not replaced.')
        except Exception as exc:
            self.say('Dictionary not exported: ' + str(exc))
        finally:
            dialog.Destroy()

    def preview(self, event=None):
        if self.busy:
            return
        self.busy = True
        self.cancelled.clear()
        rules, all_cues = self.rules, self.all_cues.GetValue()
        self.refresh()
        self.say('Preparing exact literal matches. Cancel and close requests cancellation; no proposal is accepted automatically.')
        future = self.executor.submit(lambda: self.model.preview(rules, all_cues=all_cues,
                                                                 cancelled=self.cancelled.is_set))
        def finish(future):
            if self.closed:
                return
            self.busy = False
            try:
                changes = future.result()
                self.refresh()
                self.say(f'{len(changes)} proposed changes. Read their original offsets and replacement text, then choose Apply reviewed changes.')
                if changes:
                    self.changes.SetSelection(0)
                    self.show_proposal()
                    self.changes.SetFocus()
            except Exception as exc:
                self.model.clear_preview()
                self.refresh()
                self.say('Preview not completed: ' + str(exc))
        future.add_done_callback(lambda done: self.wx.CallAfter(finish, done) if not self.closed else None)

    def show_proposal(self, event=None):
        index = self.changes.GetSelection()
        if not 0 <= index < len(self.model.proposals):
            return
        proposal = self.model.proposals[index]
        cue = self.model._cue[proposal.cue_id]
        edit = proposal.edit
        self.diff.ChangeValue('PROPOSAL ONLY — not accepted.\n'
            f'Original segment: {self.model.cues.index(cue) + 1}\n'
            f'Original script offsets: {cue.source_start + edit.source_start}–{cue.source_start + edit.source_end}\n\n'
            f'Exact original text:\n{cue.text[edit.source_start:edit.source_end]}\n\n'
            f'Exact spoken replacement:\n{edit.replacement}\n\n'
            'Use the proposed-changes list to inspect each match, then Apply reviewed changes.')
        self.notebook.SetSelection(2)

    def accept(self, event=None):
        try:
            count = len(self.model.proposals)
            self.model.accept()
            self.refresh()
            self.notebook.SetSelection(1)
            self.say(f'Accepted {count} changes in the draft reading copy. Original text is unchanged. Save to use this copy for speech.')
        except Exception as exc:
            self.say('Changes not accepted: ' + str(exc))

    def reject(self, event=None):
        try:
            edits = self.model.derivative().edits
            index = self.accepted.GetSelection()
            if not 0 <= index < len(edits):
                raise ValueError('Select an accepted change to reject.')
            self.model.reject(edits[index].id)
            self.refresh()
            self.say('Selected reading change rejected. The original words are restored in this reading copy.')
        except Exception as exc:
            self.say('Change not rejected: ' + str(exc))

    def undo(self, event=None):
        self.model.undo()
        self.refresh()
        self.say('Undid the last accepted edit in this segment.')

    def redo(self, event=None):
        self.model.redo()
        self.refresh()
        self.say('Redid the last undone edit in this segment.')

    def save(self, event=None):
        try:
            if self.busy:
                return
            self.result = self.model.result()
            self.closed = True
            self.dialog.EndModal(self.wx.ID_OK)
        except Exception as exc:
            self.say('Reading copy not saved: ' + str(exc))

    def close(self, event=None):
        if self.busy:
            self.cancelled.set()
            self.say('Cancelling the preview. Close again when it stops. Accepted draft changes have not been saved.')
            if event and hasattr(event, 'Veto'):
                event.Veto()
            return
        self.result = None
        self.closed = True
        self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        try:
            self.dialog.ShowModal()
            return self.result
        finally:
            self.closed = True
            self.cancelled.set()
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.dialog.Destroy()


def show_narration_editor(parent, project):
    return NarrationEditorDialog(parent, project).show()
