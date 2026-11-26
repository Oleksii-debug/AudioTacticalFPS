"""Native keyboard editor for explicitly separate reading/narration copies.

No speech synthesis or automatic linguistic correction is implied. The testable
controller owns pending text and confirmed saves; wx is imported only to open
the dialog. Background autosave never steals keyboard or screen-reader focus.
"""
from dataclasses import asdict, replace
from hashlib import sha256

from ..reader.narration import (
    NarrationStore, PronunciationRule, comparison_text, encode_project, export_text,
    import_text, new_project, normalization_preview, pronunciation_preview, redo,
    revise, save_as, undo,
)
from .research_actions import NativeDialogs


class NarrationController:
    def __init__(self, store, project, *, expected_digest=None, draft_digest=None, guard=lambda: None):
        self.store, self.project, self.guard = store, project, guard
        self.saved_digest, self.draft_digest = expected_digest, draft_digest
        self.pending_text = project.current_text
        self.warning = ''

    def candidate(self, *, label='Edited reading text'):
        self.guard()
        return revise(self.project, self.pending_text, label=label)

    @property
    def dirty(self):
        return sha256(encode_project(self.candidate())).hexdigest() != self.saved_digest

    def set_text(self, text):
        self.guard()
        self.pending_text = text

    def autosave(self):
        project = self.candidate()
        digest = self.store.save(project, expected_digest=self.draft_digest, draft=True)
        self.draft_digest = digest
        return 'Recovery draft saved. Press Ctrl+S to save the reading project.'

    def save(self):
        project = self.candidate()
        digest = self.store.save(project, expected_digest=self.saved_digest)
        self.project, self.saved_digest, self.pending_text = project, digest, project.current_text
        self.warning = ''
        if self.draft_digest is not None:
            try:
                self.store.clear_draft(project.id, expected_digest=self.draft_digest)
                self.draft_digest = None
            except Exception as exc:
                self.warning = f' The recovery draft was retained: {exc}'
        return 'Reading project saved. Original source unchanged.' + self.warning

    def save_as(self, title):
        project = save_as(self.candidate(), title=title)
        digest = self.store.save(project)
        self.project, self.saved_digest = project, digest
        self.pending_text, self.draft_digest = project.current_text, None
        return 'A new reading project was saved. The earlier project and recovery draft were kept.'

    def apply(self, text, *, label):
        candidate = revise(self.candidate(), text, label=label)
        self.project, self.pending_text = candidate, candidate.current_text

    def undo(self):
        candidate = undo(self.candidate())
        self.project, self.pending_text = candidate, candidate.current_text

    def redo(self):
        if self.pending_text != self.project.current_text:
            raise ValueError('Save or undo the new edit before redoing an earlier revision.')
        candidate = redo(self.project)
        self.project, self.pending_text = candidate, candidate.current_text

    def set_rules(self, rules):
        candidate = replace(self.candidate(), rules=tuple(rules))
        self.project, self.pending_text = candidate, candidate.current_text

    def export(self, path, *, overwrite=False):
        candidate = self.candidate()
        export_text(candidate, path, private_root=self.store.root, overwrite=overwrite)
        return 'UTF-8 reading text exported. The project remains separate from its original source.'

    def audio_copy(self):
        """Transfer a reading derivative without changing its original or evidence."""
        from .audio_studio import StudioController
        candidate = self.candidate()
        studio = StudioController(private_root=self.store.root)
        studio.new(candidate.original_text, title=candidate.title,
            source_id='reading-copy:' + candidate.id,
            source_reference={'kind': 'versioned-reading-copy', 'project_id': candidate.id,
                              'original_sha256': candidate.original_sha256,
                              'reading_sha256': sha256(candidate.current_text.encode('utf-8')).hexdigest(),
                              'source_location': asdict(candidate.source_location) if candidate.source_location else None})
        studio.edit_script(candidate.current_text)
        self.guard()
        return studio


def _dictionary_text(rules):
    # JSON interchange makes newlines, tabs and literal arrows unambiguous.
    import json
    return json.dumps([{'written': rule.written, 'spoken': rule.spoken, 'language': rule.language}
                       for rule in rules], ensure_ascii=False, indent=2)


def _parse_rules(value):
    import json
    from ..reader.narration import MAX_RULES
    from ..reader.reading_state import _unique
    if not isinstance(value, str) or len(value.encode('utf-8')) > 1024 * 1024:
        raise ValueError('The pronunciation dictionary exceeds its editing budget.')
    try:
        items = json.loads(value, object_pairs_hook=_unique)
        if type(items) is not list or len(items) > MAX_RULES:
            raise ValueError('Expected a list of pronunciation entries.')
        rules = tuple(PronunciationRule(**item) for item in items)
        if len({item.written for item in rules}) != len(rules):
            raise ValueError('Each written phrase must occur only once.')
        return rules
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f'Invalid pronunciation dictionary: {exc}') from exc


def open_narration_editor(parent, store, project, *, expected_digest=None, draft_digest=None,
                          guard=lambda: None, dialogs=None, on_ready=None):
    """Open one reading-copy editor; source provenance is always readable.

    The caller guards the captured workspace/source, not the current global
    selection. Original and editing views are separate native text controls.
    """
    import wx

    controller = NarrationController(store, project, expected_digest=expected_digest,
                                     draft_digest=draft_digest, guard=guard)
    dialogs = dialogs or NativeDialogs()
    dialog = wx.Dialog(parent, title='Reading copy — ' + project.title, size=(960, 820),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)

    def text_control(label, value='', *, readonly=False, proportion=0, height=None):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 7)
        style = wx.TE_MULTILINE | (wx.TE_READONLY if readonly else wx.TE_PROCESS_TAB)
        control = wx.TextCtrl(dialog, value=value, style=style)
        control.SetName(label.replace('&', ''))
        if height:
            control.SetMinSize((-1, height))
        layout.Add(control, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        return control

    origin = text_control('Original source and reading-copy status', project.provenance,
                          readonly=True, height=75)
    original = text_control('Original text (read only)', project.original_text, readonly=True, height=100)
    editor = text_control('&Editable reading copy (not quotation evidence)', project.current_text,
                          proportion=1)
    # Native multiline text must allow Tab to leave the editor; no TE_PROCESS_TAB.
    editor.SetWindowStyleFlag(editor.GetWindowStyleFlag() & ~wx.TE_PROCESS_TAB)
    status = text_control('Reading-copy save and operation status',
                          'Edit the reading copy. Ctrl+S saves; Ctrl+Shift+S creates a new copy.',
                          readonly=True, height=70)
    buttons = wx.WrapSizer(wx.HORIZONTAL)
    layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 5)
    state = {'closed': False, 'refreshing': False, 'last_autosaved': None}
    timer = wx.Timer(dialog)

    def say(message, *, focus=True):
        status.ChangeValue(message)
        status.SetInsertionPoint(0)
        if focus:
            status.SetFocus()

    def refresh():
        state['refreshing'] = True
        try:
            editor.ChangeValue(controller.pending_text)
            origin.ChangeValue(controller.project.provenance)
            original.ChangeValue(controller.project.original_text)
            dialog.SetTitle('Reading copy — ' + controller.project.title)
        finally:
            state['refreshing'] = False

    def capture():
        controller.set_text(editor.GetValue())

    def run(operation, *, focus=True):
        if state['closed']:
            return
        try:
            capture()
            result = operation()
            refresh()
            if result:
                say(result, focus=focus)
            timer.StartOnce(1200)
        except Exception as exc:
            say(str(exc), focus=focus)

    def save(event=None):
        run(controller.save)

    def save_new(event=None):
        title = dialogs.text(dialog, 'Save reading project as', 'Name of the new reading project',
                             controller.project.title)
        if title is not None:
            run(lambda: controller.save_as(title))

    def preview_transform(title, text, description):
        capture()
        candidate = revise(controller.candidate(), text, label=title)
        choice = dialogs.choose(dialog, title, description + '\nThe source will remain unchanged.',
                                ('Read the proposed text', 'Apply the proposed change', 'Cancel'))
        if choice == 0:
            dialogs.show(dialog, title + ' — preview', text)
            choice = dialogs.choose(dialog, title, 'Apply this reading-only change?', ('Apply', 'Cancel'))
            choice = 1 if choice == 0 else 2
        if choice == 1:
            controller.apply(candidate.current_text, label=title)
            refresh()
            say(description + ' Applied to reading copy. Ctrl+S saves the project.')
            timer.StartOnce(1200)

    def normalize(event=None):
        try:
            capture()
            preview_transform('Normalize spacing', normalization_preview(controller.pending_text),
                              'Normalize line endings, repeated spaces and extra blank lines. '
                              'Punctuation, accents and words are not guessed.')
        except Exception as exc:
            say(str(exc))

    def pronunciation(event=None):
        try:
            capture()
            text, count = pronunciation_preview(controller.pending_text, controller.project.rules)
            preview_transform('Apply pronunciation dictionary', text,
                              f'{count} case-sensitive literal replacements; no cascading substitutions.')
        except Exception as exc:
            say(str(exc))

    def dictionary(event=None):
        try:
            value = dialogs.text(dialog, 'Pronunciation dictionary',
                                 'JSON entries: written phrase, spoken replacement, optional language label',
                                 _dictionary_text(controller.project.rules), multiline=True)
            if value is not None:
                run(lambda: (controller.set_rules(_parse_rules(value)) or
                             'Pronunciation dictionary updated. Use Preview pronunciation to apply it.'))
        except Exception as exc:
            say(str(exc))

    def add_rule(event=None):
        values = dialogs.fields(dialog, 'Add pronunciation replacement', (
            ('written', 'Written phrase (exact case)', '', False),
            ('spoken', 'Read this replacement instead (empty removes the phrase)', '', False),
            ('language', 'Language label (optional description, not automatic detection)', '', False),
        ))
        if values is not None:
            run(lambda: (controller.set_rules(controller.project.rules + (PronunciationRule(**values),))
                         or 'Literal pronunciation replacement added. Preview before applying.'))

    def compare(event=None):
        try:
            capture()
            dialogs.show(dialog, 'Original and reading-copy comparison', comparison_text(controller.candidate()))
        except Exception as exc:
            say(str(exc))

    def replace_text(event=None):
        fields = dialogs.fields(dialog, 'Find and replace literal text', (
            ('written', 'Find exact text (case sensitive)', '', False),
            ('spoken', 'Replace with', '', False),
        ))
        if fields is None:
            return
        try:
            capture()
            text, count = pronunciation_preview(controller.pending_text, (PronunciationRule(**fields),))
            preview_transform('Replace literal text', text, f'{count} literal replacements.')
        except Exception as exc:
            say(str(exc))

    def export(event=None):
        picker = wx.FileDialog(dialog, 'Export UTF-8 reading text', wildcard='Text files (*.txt)|*.txt',
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if picker.ShowModal() == wx.ID_OK:
                run(lambda: controller.export(picker.GetPath(), overwrite=True))
        finally:
            picker.Destroy()

    def audio_studio(event=None):
        try:
            from .audio_studio import AudioStudioDialog
            capture()
            controller.autosave()
            studio = controller.audio_copy()
            timer.Stop()
            AudioStudioDialog(dialog, studio).show()
            say('The reading copy is preserved. Audio Studio saves its own project and audio output.')
        except Exception as exc:
            say('Audio Studio was not opened: ' + str(exc))
        finally:
            if not state['closed']:
                timer.StartOnce(1200)

    def close(event=None):
        if state['closed']:
            return
        try:
            capture()
            if controller.dirty:
                choice = dialogs.choose(dialog, 'Unsaved reading copy',
                                        'Keep your reading edits before closing.',
                                        ('Save project and close', 'Keep recovery draft and close', 'Keep editing'))
                if choice is None or choice == 2:
                    if event is not None and hasattr(event, 'Veto'):
                        event.Veto()
                    return
                controller.save() if choice == 0 else controller.autosave()
            state['closed'] = True
            timer.Stop()
            dialog.EndModal(wx.ID_OK)
        except Exception as exc:
            say('Reading copy remains open because saving did not complete. ' + str(exc))
            if event is not None and hasattr(event, 'Veto'):
                event.Veto()

    def edited(event):
        if not state['refreshing'] and not state['closed']:
            timer.StartOnce(1200)
        event.Skip()

    def auto(event):
        if state['closed']:
            return
        try:
            capture()
            if not controller.dirty:
                return
            candidate = controller.candidate()
            signature = (candidate.current_text, candidate.rules, candidate.cursor, candidate.title)
            if signature != state['last_autosaved']:
                message = controller.autosave()
                state['last_autosaved'] = signature
                say(message, focus=False)
        except Exception as exc:
            say('Recovery draft could not be saved. Your editor remains open: ' + str(exc), focus=False)

    actions = (
        ('&Save project (Ctrl+S)', save), ('Save &as (Ctrl+Shift+S)', save_new),
        ('&Undo revision (Ctrl+Z)', lambda event: run(lambda: controller.undo() or 'Earlier revision restored.')),
        ('&Redo revision (Ctrl+Y)', lambda event: run(lambda: controller.redo() or 'Later revision restored.')),
        ('Normalize s&pacing', normalize), ('&Add pronunciation', add_rule),
        ('Pronunciation &dictionary', dictionary), ('Preview pro&nunciation', pronunciation),
        ('&Find and replace', replace_text), ('Compare &original', compare),
        ('&Export UTF-8 text', export), ('Open in Audio &Studio', audio_studio), ('C&lose', close),
    )
    ids, native_buttons = [], []
    for label, handler in actions:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        buttons.Add(button, 0, wx.ALL, 4)
        ids.append(button.GetId())
        native_buttons.append(button)
        dialog.Bind(wx.EVT_MENU, handler, id=button.GetId())
    dialog.SetAcceleratorTable(wx.AcceleratorTable([
        (wx.ACCEL_CTRL, ord('S'), ids[0]), (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), ids[1]),
        (wx.ACCEL_CTRL, ord('Z'), ids[2]), (wx.ACCEL_CTRL, ord('Y'), ids[3]),
        (wx.ACCEL_CTRL, ord('H'), ids[8]),
    ]))
    dialog.Bind(wx.EVT_TIMER, auto, timer)
    dialog.Bind(wx.EVT_CLOSE, close)
    editor.Bind(wx.EVT_TEXT, edited)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(ids[-1])
    editor.SetFocus()
    editor.SetInsertionPoint(0)
    try:
        guard()
        if on_ready is not None:
            controls = dict(editor=editor, original=original, origin=origin, status=status,
                            save=native_buttons[0], save_as=native_buttons[1],
                            normalize=native_buttons[4], pronunciation=native_buttons[7],
                            compare=native_buttons[9], close=native_buttons[-1])
            wx.CallAfter(on_ready, dialog, controller, controls)
        dialog.ShowModal()
    finally:
        state['closed'] = True
        timer.Stop()
        dialog.Destroy()
    return controller.project


def show_narration_projects(parent, store, *, workspace=None, guard=lambda: None, dialogs=None):
    """Choose a saved project or explicitly recover its autosaved draft."""
    dialogs = dialogs or NativeDialogs()
    guard()
    rows = store.list_projects(workspace)
    if workspace is None:
        rows = tuple(row for row in rows if row['project'].source_location is None)
    if not rows:
        dialogs.show(parent, 'Reading copies', 'There are no saved reading copies or recovery drafts.')
        return None
    labels = [row['project'].title + (' — recovery draft available' if row['draft'] else '')
              + ' — ' + row['project'].id for row in rows]
    index = dialogs.choose(parent, 'Open reading copy', 'Choose a reading project.', labels)
    if index is None:
        return None
    if type(index) is not int or not 0 <= index < len(rows):
        raise ValueError('Choose one reading project.')
    guard()
    row = rows[index]
    project_id = row['project'].id
    project, saved_digest = store.load(project_id) if row['saved'] else (row['project'], None)
    def authorize(loaded):
        guard()
        if (loaded.source_location is not None
                and (workspace is None or loaded.source_location.workspace != workspace)):
            raise ValueError('The reading project belongs to a different library. Open it in that library.')
    # Re-read identities after the selection dialog. A file or recovery draft
    # may have changed since the initial list was constructed.
    authorize(project)
    draft_digest = None
    if row['draft']:
        draft, draft_digest = store.load(project_id, draft=True)
        authorize(draft)
        options = ('Recover saved draft', 'Open last saved project', 'Cancel') if row['saved'] else (
            'Recover saved draft', 'Cancel')
        selection = dialogs.choose(parent, 'Recover reading draft',
                                   'A recovery draft exists. Choose explicitly which text to open.', options)
        if selection is None or selection == len(options) - 1:
            return None
        if selection == 0:
            project = draft
        # Opening the saved version still retains the draft until an explicit
        # save or subsequent autosave, protected by its original digest lease.
    authorize(project)
    return open_narration_editor(parent, store, project, expected_digest=saved_digest,
                                 draft_digest=draft_digest, guard=guard, dialogs=dialogs)


def import_reading_text(parent, store, *, guard=lambda: None):
    import wx
    picker = wx.FileDialog(parent, 'Import UTF-8 text as a reading copy',
                           wildcard='UTF-8 text files (*.txt)|*.txt|All files (*.*)|*.*',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if picker.ShowModal() != wx.ID_OK:
            return None
        guard()
        project = import_text(picker.GetPath())
    finally:
        picker.Destroy()
    return open_narration_editor(parent, store, project, guard=guard)
