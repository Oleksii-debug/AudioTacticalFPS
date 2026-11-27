"""Review pronunciation derivatives through the existing reading/dictionary APIs."""
from dataclasses import dataclass
from hashlib import sha256
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from ..durable_state import _open_json_file, atomic_bytes, local_path, regular, state_lock
from ..speech._io import check_cancelled, text_value
from ..research.export_file import (ExportReceipt, capture_export_destination,
                                    export_completion, write_binary_export)
from ..speech.reading import ReadingDocument, PronunciationRule, export_dictionary, import_dictionary
from ..speech.pronunciation_formats import ENCODINGS, parse_dictionary

MAX_DICTIONARY_BYTES = 8_000_000


@dataclass(frozen=True)
class ReadingSnapshot:
    identity: int
    signature: tuple
    text: str
    selection: tuple[int, int]
    language: str
    voice_id: str

    @classmethod
    def capture(cls, document, selection, *, language='', voice_id=''):
        if not isinstance(document, ReadingDocument):
            raise ValueError('An existing reading copy is required.')
        start, end = selection
        document._range(start, end)
        text_value(language, 'reader language', maximum=128, empty=True)
        text_value(voice_id, 'reader voice ID', maximum=2048, empty=True)
        return cls(id(document), document._signature(), document.text, (start, end), language, voice_id)


@dataclass(frozen=True)
class PronunciationPreview:
    snapshot: ReadingSnapshot
    rules: tuple[PronunciationRule, ...]
    selection_only: bool
    language: str
    start: int
    end: int
    before: str
    after: str


def preview_pronunciations(snapshot, rules, *, selection_only=False, language='', cancelled=lambda: False):
    """Run the existing bounded substitution engine on a private reading copy."""
    check_cancelled(cancelled)
    if not isinstance(snapshot, ReadingSnapshot) or type(selection_only) is not bool:
        raise ValueError('A captured reading scope is required.')
    text_value(language, 'rule match language', maximum=128, empty=True)
    start, end = snapshot.selection if selection_only else (0, len(snapshot.text))
    if start == end:
        raise ValueError('Select nonempty reading text before previewing this scope.')
    rules = tuple(rules)
    if not rules:
        raise ValueError('Add or import at least one pronunciation rule.')
    before = snapshot.text[start:end]
    copy = ReadingDocument(before)
    copy.apply_pronunciations(rules, language=language, voice_id=snapshot.voice_id, cancelled=cancelled)
    check_cancelled(cancelled)
    return PronunciationPreview(snapshot, rules, selection_only, language, start, end, before, copy.text)


def apply_pronunciation_preview(document, preview, current):
    """One undoable derivative edit, only after the current scope is rechecked."""
    if (not isinstance(preview, PronunciationPreview) or current != preview.snapshot
            or id(document) != current.identity or document._signature() != current.signature
            or document.text != current.text):
        raise ValueError('The reading text, selection or voice changed. Preview the rules again.')
    if preview.before == preview.after:
        raise ValueError('This preview makes no reading-text changes.')
    return document.set_text(current.text[:preview.start] + preview.after + current.text[preview.end:],
                             label='Reviewed pronunciation reading layer')


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_dictionary(path, *, encoding='unicode', report=None, cancelled=lambda: False):
    """Capture bounded ordinary bytes before calling the existing parser.

    The shared descriptor opener refuses a substituted FIFO or link before a
    read can block. Only this captured snapshot is parsed; source files are
    never rewritten by dictionary import.
    """
    check_cancelled(cancelled)
    path = local_path(path, exists=True)
    parent = regular(path.parent, directory=True)
    parent_identity = parent.st_dev, parent.st_ino
    before = regular(path)
    if before.st_size > MAX_DICTIONARY_BYTES:
        raise ValueError('The pronunciation dictionary exceeds its size budget.')
    chunks, size = [], 0
    with _open_json_file(path) as source:
        opened = os.fstat(source.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or _stamp(opened) != _stamp(before)):
            raise ValueError('The dictionary changed while opening it.')
        selected_parent = regular(path.parent, directory=True)
        if (selected_parent.st_dev, selected_parent.st_ino) != parent_identity:
            raise ValueError('The dictionary parent changed while opening it.')
        while True:
            check_cancelled(cancelled)
            chunk = source.read(min(64 * 1024, before.st_size - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > before.st_size:
                raise ValueError('The dictionary changed while reading it.')
            chunks.append(chunk)
        if _stamp(os.fstat(source.fileno())) != _stamp(before):
            raise ValueError('The dictionary changed while reading it.')
    selected_parent = regular(path.parent, directory=True)
    if (size != before.st_size or _stamp(regular(path)) != _stamp(before)
            or (selected_parent.st_dev, selected_parent.st_ino) != parent_identity):
        raise ValueError('The dictionary changed while reading it.')
    raw = b''.join(chunks)
    kind = path.suffix.lower().lstrip('.')
    if kind == 'json':
        with TemporaryDirectory(prefix='resonance-pronunciation-import-') as temporary:
            selected = Path(temporary) / 'dictionary.json'
            selected.write_bytes(raw)
            rules = import_dictionary(selected)
        notice = 'Canonical pronunciation JSON; existing scope and order retained.'
    else:
        imported = parse_dictionary(raw, format=kind, encoding=encoding, cancelled=cancelled)
        rules, notice = imported.rules, imported.notice
    check_cancelled(cancelled)
    if report is not None:
        report(notice)
    return rules, sha256(raw).hexdigest()


def _dictionary_bytes(rules, cancelled):
    check_cancelled(cancelled)
    with TemporaryDirectory(prefix='resonance-pronunciation-export-') as temporary:
        selected = Path(temporary) / 'dictionary.json'
        export_dictionary(rules, selected)
        if selected.stat().st_size > MAX_DICTIONARY_BYTES:
            raise ValueError('The pronunciation dictionary exceeds its size budget.')
        raw = selected.read_bytes()
    check_cancelled(cancelled)
    return raw


def export_reviewed_dictionary(rules, destination, *, private_root, approval=None,
                               cancelled=lambda: False) -> tuple[str, ExportReceipt]:
    """Publish a new dictionary through the common export policy and receipt."""
    # Capture before dictionary serialization; the native caller supplies the
    # selection captured before it queued this worker. Export stays create-only.
    check_cancelled(cancelled)
    if approval is None:
        approval = capture_export_destination(destination, private_root=private_root, cancelled=cancelled)
    if not approval.require_absent:
        raise FileExistsError('Choose a new dictionary filename; this destination already exists.')
    raw = _dictionary_bytes(rules, cancelled)
    receipt = write_binary_export(destination, raw, private_root=private_root,
                                  require_absent=True, approval=approval, cancelled=cancelled)
    return sha256(raw).hexdigest(), receipt


class DictionaryStore:
    """One existing dictionary-format file, covered by ordinary state backup."""
    def __init__(self, library_root):
        self.library_root = local_path(library_root, exists=True)
        self.root = local_path(self.library_root / 'state' / 'speech')
        self.path = self.root / 'pronunciations.json'

    def load(self, *, cancelled=lambda: False):
        check_cancelled(cancelled)
        if not os.path.lexists(self.path):
            return (), None
        return read_dictionary(self.path, cancelled=cancelled)

    def save(self, rules, *, expected, cancelled=lambda: False):
        raw = _dictionary_bytes(rules, cancelled)
        with state_lock(self.root, 'pronunciations'):
            _, actual = self.load(cancelled=cancelled)
            if actual != expected:
                raise ValueError('The saved dictionary changed. Reload it before saving your reviewed rules.')
            check_cancelled(cancelled)
            atomic_bytes(self.path, raw, replace=actual is not None)
        return sha256(raw).hexdigest()


def create_pronunciation_dialog(parent, *, library_root, capture, apply, undo, guard,
                                run, cancel, wx_module=None):
    """Use the voice reader's existing worker and cancellation event."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class PronunciationDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Pronunciation rules — review a reading copy', size=(940, 820),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self._destroy_requested = False
            guard()
            self.store = DictionaryStore(library_root)
            self.alive, self.busy, self.loaded = True, False, False
            self.rules, self.saved_rules, self.saved_token = (), (), None
            self.preview, self.preview_identity, self.applied = None, None, None
            self.close_after_save = False
            initial = capture()
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label='Rules change only the reading copy. Original sources and quotations stay unchanged. '
                'General rules run before language/voice-specific rules; list order is used within each scope. '
                'A substitution is not proof of stress, phonemes or voice quality.')
            notice.Wrap(870); box.Add(notice, 0, wx.ALL | wx.EXPAND, 5)
            def field(label, factory=wx.TextCtrl, **kwargs):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                widget = factory(panel, **kwargs); widget.SetName(label.replace('&', ''))
                box.Add(widget, 0, wx.ALL | wx.EXPAND, 3)
                return widget
            self.list = field('Rule &order', wx.ListBox, choices=[])
            self.pattern = field('Rule &pattern')
            self.replacement = field('Rule &replacement; empty removes matching text')
            self.regex = field('Use a bounded regular &expression', wx.CheckBox)
            self.ignore_case = field('Ignore letter &case', wx.CheckBox)
            self.rule_language = field('Rule language; empty applies to any language')
            self.rule_voice = field('Rule voice ID; empty applies to any voice')
            self.import_encoding = field('Dictionary import encoding', wx.Choice, choices=[
                'Unicode (UTF-8 or BOM-marked UTF-16)',
                'Windows-1251 (explicit legacy text)',
                'Windows-1252 (explicit legacy text)'])
            self.import_encoding.SetSelection(0)
            self.scope = field('Text &scope for preview', wx.Choice,
                               choices=['Whole reading copy', 'Selected text only'])
            self.scope.SetSelection(int(initial.selection[0] != initial.selection[1]))
            self.language = field('Language used to match scoped rules', value=initial.language)
            self.voice = field('Selected reader voice ID (exact scope)', value=initial.voice_id, style=wx.TE_READONLY)
            self.before = field('Current reading text in scope', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
            self.after = field('Proposed reading text — review before applying', style=wx.TE_MULTILINE | wx.TE_READONLY,
                               size=(-1, 100))
            self.status = field('Pronunciation status', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 75))
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.EXPAND)
            self.buttons = {}
            for items in ((('new', 'New rule', self.on_new), ('add', 'Add rule', self.on_add),
                           ('update', 'Update rule', self.on_update), ('remove', 'Remove rule', self.on_remove),
                           ('up', 'Move up', lambda event: self.on_move(-1)), ('down', 'Move down', lambda event: self.on_move(1))),
                          (('load', 'Reload library dictionary', self.on_load), ('save', 'Save library dictionary', self.on_save),
                           ('import', 'Import dictionary...', self.on_import), ('export', 'Export new dictionary...', self.on_export)),
                          (('preview', 'Pre&view rules', self.on_preview), ('apply', '&Apply reviewed change', self.on_apply),
                           ('undo', '&Undo last apply', self.on_undo), ('cancel', 'Ca&ncel operation', self.on_cancel),
                           ('close', 'C&lose', self.on_close))):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in items:
                    button = wx.Button(self, label=label); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 3); self.buttons[key] = button
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout); panel.FitInside()
            self.list.Bind(wx.EVT_LISTBOX, self.on_select)
            for widget in (self.pattern,self.replacement,self.rule_language,self.rule_voice,self.language):
                widget.Bind(wx.EVT_TEXT, self.on_change)
            for widget in (self.regex,self.ignore_case):
                widget.Bind(wx.EVT_CHECKBOX, self.on_change)
            self.scope.Bind(wx.EVT_CHOICE, self.on_change)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.report('Load a library dictionary or import reviewed rules. Add/update the rule entry before previewing. '
                        'Import never applies text changes automatically.')
            self.controls()

        def report(self, message):
            if self.alive:
                self.status.ChangeValue(str(message))

        def stopping(self):
            if cancel.is_set() or not self.alive:
                return True
            try:
                guard()
            except (OSError, ValueError, RuntimeError):
                return True
            return False

        def controls(self):
            if not self.alive:
                return
            for widget in (self.list,self.pattern,self.replacement,self.regex,self.ignore_case,self.rule_language,
                           self.rule_voice,self.import_encoding,self.scope,self.language):
                widget.Enable(not self.busy)
            for key, button in self.buttons.items():
                button.Enable(not self.busy if key not in ('cancel','close') else key == 'close' or self.busy)
            self.buttons['apply'].Enable(not self.busy and self.preview is not None and self.preview.before != self.preview.after)
            self.buttons['undo'].Enable(not self.busy and self.applied is not None)
            self.buttons['save'].Enable(not self.busy and self.loaded)

        def invalidate(self):
            self.preview, self.preview_identity = None, None
            self.controls()

        def on_change(self, event=None):
            if not self.alive:
                return
            self.invalidate()

        def form(self):
            return (self.pattern.GetValue(), self.replacement.GetValue(), bool(self.regex.GetValue()),
                    bool(self.ignore_case.GetValue()), self.rule_language.GetValue(), self.rule_voice.GetValue())

        def rule(self):
            return PronunciationRule(*self.form())

        def entry_saved(self):
            selected = self.list.GetSelection()
            if 0 <= selected < len(self.rules):
                if self.rule() != self.rules[selected]:
                    raise ValueError('Update the edited rule before previewing, saving or exporting.')
            elif any(self.form()):
                raise ValueError('Add the new rule before previewing, saving or exporting.')

        def refresh(self, selected=-1):
            self.list.Set([f'{i+1}. {"Regex" if rule.regex else "Literal"}: {rule.pattern[:90]} → {rule.replacement[:90]}; '
                f'language {rule.language or "any"}; voice {rule.voice_id or "any"}' for i, rule in enumerate(self.rules)])
            self.list.SetSelection(selected)
            self.on_select()
            self.invalidate()

        def on_select(self, event=None):
            if not self.alive:
                return
            index = self.list.GetSelection()
            rule = self.rules[index] if 0 <= index < len(self.rules) else None
            for widget, value in ((self.pattern, rule.pattern if rule else ''),
                (self.replacement, rule.replacement if rule else ''), (self.rule_language, rule.language if rule else ''),
                (self.rule_voice, rule.voice_id if rule else '')):
                widget.ChangeValue(value)
            self.regex.SetValue(rule.regex if rule else False)
            self.ignore_case.SetValue(rule.ignore_case if rule else False)
            self.invalidate()

        def on_new(self, event=None):
            if not self.alive:
                return
            if not self.busy:
                self.list.SetSelection(-1); self.on_select(); self.pattern.SetFocus()

        def edit(self, operation):
            if not self.alive or self.busy:
                return
            try:
                guard(); operation()
                self.report('Rule list changed. Save the library dictionary to retain it, and preview before applying.')
            except Exception as exc:
                self.report(exc)

        def on_add(self, event=None):
            def operation():
                if len(self.rules) >= 1000:
                    raise ValueError('Use at most 1000 pronunciation rules.')
                self.rules += (self.rule(),); self.refresh(len(self.rules)-1)
            self.edit(operation)

        def selected(self):
            index = self.list.GetSelection()
            if not 0 <= index < len(self.rules):
                raise ValueError('Select an existing rule first.')
            return index

        def on_update(self, event=None):
            def operation():
                index = self.selected(); rule = self.rule()
                self.rules = self.rules[:index] + (rule,) + self.rules[index+1:]; self.refresh(index)
            self.edit(operation)

        def on_remove(self, event=None):
            def operation():
                index = self.selected(); self.rules = self.rules[:index] + self.rules[index+1:]
                self.refresh(min(index, len(self.rules)-1))
            self.edit(operation)

        def on_move(self, direction):
            def operation():
                self.entry_saved(); index = self.selected(); target = index + direction
                if 0 <= target < len(self.rules):
                    rules = list(self.rules); rules[index], rules[target] = rules[target], rules[index]
                    self.rules = tuple(rules); self.refresh(target)
            self.edit(operation)

        def begin(self, work, complete, *, committed=False):
            if self.busy or not self.alive:
                return False
            try:
                guard()
            except Exception as exc:
                self.report(exc); return False
            self.busy = True; self.controls()
            def operation():
                check_cancelled(self.stopping); guard()
                result = work()
                if not committed:
                    check_cancelled(self.stopping); guard()
                return result
            def done(result):
                self.busy = False
                if not self.alive:
                    return
                try:
                    # A save/export that reached atomic publication remains
                    # committed if cancellation arrives just after the write.
                    # Closed/relocated scopes still cannot accept late UI work.
                    if not committed:
                        check_cancelled(self.stopping)
                    guard(); complete(result)
                except Exception as exc:
                    self.report(exc)
                self.controls()
            def failed(error):
                self.busy = False
                if self.alive:
                    self.close_after_save = False; self.report(error); self.controls()
            try:
                admitted = run(operation, done, on_failed=failed, committed=committed)
                if admitted is False:
                    failed('A reader operation is running. Finish or cancel it first.')
                    return False
            except Exception as exc:
                failed(exc); return False
            return True

        def on_load(self, event=None):
            def done(result):
                self.rules, self.saved_token = result
                self.saved_rules, self.loaded = self.rules, True
                self.refresh(); self.report(f'Loaded {len(self.rules)} rules. No reading text was changed.')
            self.begin(lambda: self.store.load(cancelled=self.stopping), done)

        def on_save(self, event=None):
            if not self.alive:
                return
            try:
                self.entry_saved()
                if not self.loaded:
                    raise ValueError('Reload the library dictionary before saving changes.')
                rules, expected = self.rules, self.saved_token
                def done(token):
                    self.saved_token, self.saved_rules = token, rules
                    self.report('Library dictionary saved. Rules are never automatically applied to a reading copy.')
                    if self.close_after_save:
                        self.finish_close()
                self.begin(lambda: self.store.save(rules, expected=expected, cancelled=self.stopping),
                           done, committed=True)
            except Exception as exc:
                self.close_after_save = False; self.report(exc)

        def on_import(self, event=None):
            if not self.alive:
                return
            if self.busy:
                return
            picker = wx.FileDialog(self, 'Import pronunciation rules for review',
                                   wildcard='Pronunciation rules (*.json;*.dic;*.rex;*.bxd)|*.json;*.dic;*.rex;*.bxd',
                                   style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                answer = picker.ShowModal()
                if not self.alive or not self or not picker or answer != wx.ID_OK:
                    return
                source = Path(picker.GetPath())
            finally:
                if picker:
                    picker.Destroy()
            if not self.alive or not self:
                return
            selected = self.import_encoding.GetSelection()
            if type(selected) is not int or not 0 <= selected < len(ENCODINGS):
                self.report('Choose the dictionary text encoding before importing.')
                return
            notices = []
            def done(result):
                self.rules = result[0]; self.refresh()
                self.report(f'Imported {len(self.rules)} rules for review. Save explicitly to retain them; preview before applying. '
                            + ' '.join(notices))
            self.begin(lambda: read_dictionary(source, encoding=ENCODINGS[selected],
                                                report=notices.append, cancelled=self.stopping), done)

        def on_export(self, event=None):
            if not self.alive:
                return
            try:
                if self.busy:
                    return
                guard(); self.entry_saved(); rules = self.rules
                picker = wx.FileDialog(self, 'Export rules to a new dictionary', defaultFile='pronunciations.json',
                                       wildcard='Pronunciation dictionary (*.json)|*.json', style=wx.FD_SAVE)
                try:
                    answer = picker.ShowModal()
                    if not self.alive or not self or not picker or answer != wx.ID_OK:
                        return
                    destination = Path(picker.GetPath())
                finally:
                    if picker:
                        picker.Destroy()
                if not self.alive or not self:
                    return
                guard()
                if self.rules != rules:
                    raise ValueError('The pronunciation rules changed while choosing the destination. Review them again.')
                private_root = self.store.library_root
                approval = capture_export_destination(destination, private_root=private_root,
                                                      cancelled=self.stopping)
                if not approval.require_absent:
                    raise FileExistsError('Choose a new dictionary filename; this destination already exists.')
                def saved(result):
                    digest, receipt = result
                    self.report(export_completion(f'New dictionary saved; SHA-256 {digest}.', receipt))

                self.begin(lambda: export_reviewed_dictionary(rules, destination, private_root=private_root,
                           approval=approval, cancelled=self.stopping), saved, committed=True)
            except Exception as exc:
                self.report(exc)

        def identity(self):
            return (capture(), self.rules, self.scope.GetSelection(), self.language.GetValue(), self.form())

        def on_preview(self, event=None):
            if not self.alive:
                return
            try:
                self.entry_saved(); guard(); identity = self.identity()
                snapshot, rules, scope, language, _ = identity
                if scope not in (0, 1):
                    raise ValueError('Choose whole-copy or selected-text scope.')
                self.invalidate()
                def done(result):
                    if self.identity() != identity:
                        raise ValueError('Text, selection, voice or rules changed. Preview again before applying.')
                    self.preview, self.preview_identity = result, identity
                    self.before.ChangeValue(result.before); self.after.ChangeValue(result.after)
                    self.voice.ChangeValue(snapshot.voice_id)
                    self.report(f'Review reading-copy characters {result.start + 1} to {result.end}: '
                                f'{len(result.before)} current characters and {len(result.after)} proposed characters. '
                                + ('Apply makes one undoable reading edit.' if result.before != result.after else 'No text changes for this scope.'))
                    self.after.SetFocus()
                self.begin(lambda: preview_pronunciations(snapshot, rules, selection_only=scope == 1,
                                                          language=language, cancelled=self.stopping), done)
            except Exception as exc:
                self.invalidate(); self.report(exc)

        def on_apply(self, event=None):
            if not self.alive:
                return
            try:
                if self.busy or self.preview is None:
                    raise ValueError('Preview the rules and review the proposed text before applying.')
                guard(); check_cancelled(self.stopping); self.entry_saved()
                if self.identity() != self.preview_identity:
                    raise ValueError('Text, selection, voice or rules changed. Preview again before applying.')
                self.applied, warning = apply(self.preview)
                self.invalidate(); self.report('Reviewed pronunciation changes applied to the reading copy. '
                                               'Undo last apply restores the previous reading text; original sources are unchanged. ' + warning)
            except Exception as exc:
                self.invalidate(); self.report(exc)

        def on_undo(self, event=None):
            if not self.alive:
                return
            try:
                if self.busy or self.applied is None:
                    raise ValueError('There is no pronunciation change from this dialog to undo.')
                guard(); warning = undo(self.applied)
                self.applied = None; self.invalidate()
                self.report('Last pronunciation change undone. Saved dictionary rules are unchanged. ' + warning)
            except Exception as exc:
                self.report(exc)

        def on_cancel(self, event=None):
            if not self.alive:
                return
            cancel.set(); self.close_after_save = False; self.invalidate()
            self.report('Cancellation requested; waiting for the reader worker to finish.')

        def dispose(self):
            """Detach callbacks without consulting a possibly deleted wx wrapper."""
            if not getattr(self, 'alive', False):
                return
            self.alive = False
            self.close_after_save = False
            self.preview, self.preview_identity = None, None
            if self.busy:
                cancel.set()

        def on_destroy(self, event):
            # wx can deliver the owner's native event through a new Python wrapper.
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

        def on_close(self, event=None):
            if not self.alive:
                return
            if self.busy:
                self.finish_close(); return
            pending = self.rules != self.saved_rules
            try:
                self.entry_saved()
            except ValueError:
                pending = True
            if pending:
                answer = wx.MessageBox('Save the library dictionary before closing? Reading-copy changes have their own undo/history.',
                                       'Unsaved pronunciation rules', wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, self)
                if answer == wx.CANCEL:
                    return
                if answer == wx.YES:
                    self.close_after_save = True; self.on_save(); return
            self.finish_close()

    return PronunciationDialog()
