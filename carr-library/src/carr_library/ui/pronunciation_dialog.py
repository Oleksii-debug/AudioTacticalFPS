"""Native rule editing on the existing reader's worker and pronunciation APIs."""
from dataclasses import dataclass
from pathlib import Path

from ..speech import ReadingDocument, PronunciationRule, import_dictionary, export_dictionary
from ..speech._io import text_value
from ..speech.navigation import native_cursor_position, native_insertion_offset
from ..research.export_file import capture_export_destination


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


def text_at_caret(editor, *, paragraph=False):
    """Logical lines and blank-line paragraphs, using wx positions only in wx.

    Native multiline offsets are not Python indices on Windows. GetRange also
    avoids treating a UTF-16 caret offset as a Unicode code-point offset.
    """
    position = editor.GetInsertionPoint()
    before = editor.GetRange(0, position).replace('\r\n', '\n').replace('\r', '\n')
    after = editor.GetRange(position, editor.GetLastPosition()).replace('\r\n', '\n').replace('\r', '\n')
    left, right = before.split('\n'), after.split('\n')
    current = left[-1] + right[0]
    if not paragraph or not current.strip():
        return current
    previous, following = [], []
    for line in reversed(left[:-1]):
        if not line.strip():
            break
        previous.append(line)
    for line in right[1:]:
        if not line.strip():
            break
        following.append(line)
    return '\n'.join([*reversed(previous), current, *following])


def caret_offset(editor):
    """Persist the existing navigation contract's Unicode text offset."""
    return native_insertion_offset(editor, editor.GetValue())


def native_caret_position(editor, offset):
    """Reuse the observed-prefix native mapping, including UTF-16 and CRLF."""
    value = editor.GetValue()
    return native_cursor_position(editor, value, min(max(0, offset), len(value)))


class PronunciationDialog:
    """No engine, executor or persistent format of its own; rules are explicit."""

    def __init__(self, parent, rules, *, run, cancelled, cancel, guard, private_root,
                 changed, prepare_apply, active_voice, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.wx, self.run, self.cancelled, self.cancel = wx_module, run, cancelled, cancel
        self.guard, self.private_root = guard, private_root
        self.changed, self.prepare_apply = changed, prepare_apply
        self.rules, self.closed, self.busy = list(rules), False, False
        wx = self.wx
        self.dialog = wx.Dialog(parent, title='Pronunciation dictionary', size=(820, 800),
                               style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        box = wx.BoxSizer(wx.VERTICAL)
        notice = wx.StaticText(self.dialog, label=(
            'Rules change only the reading copy after Apply. Undo restores the text.\n'
            'Exact language and voice IDs filter rules; empty fields apply to any voice.\n'
            'Regular expressions run in the existing isolated, cancellable worker.\n'
            'Export saves this session dictionary to a new JSON file. ' + active_voice))
        notice.Wrap(770)
        box.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
        self.controls = []

        def field(label, cls=wx.TextCtrl, **kwargs):
            box.Add(wx.StaticText(self.dialog, label=label), 0, wx.LEFT | wx.TOP, 6)
            control = cls(self.dialog, **kwargs)
            control.SetName(label.replace('&', ''))
            box.Add(control, 0, wx.ALL | wx.EXPAND, 6)
            self.controls.append(control)
            return control

        self.choice = field('Pronunciation rules', wx.Choice)
        self.pattern = field('Pattern to pronounce')
        self.replacement = field('Spoken replacement')
        self.regex = field('Use regular expression', wx.CheckBox)
        self.ignore_case = field('Ignore letter case', wx.CheckBox)
        self.language = field('Exact language code; empty means any')
        self.voice_id = field('Exact voice ID; empty means any')
        self.status = field('Dictionary operation status', style=wx.TE_READONLY | wx.TE_MULTILINE,
                            size=(-1, 75))
        self.controls.remove(self.status)
        self.buttons = {}
        for entries in (
            (('add', '&Add rule', self.add), ('edit', 'Save rule &changes', self.edit),
             ('remove', '&Remove selected rule', self.remove)),
            (('import', '&Import dictionary JSON', self.import_rules),
             ('export', '&Export dictionary JSON', self.export_rules),
             ('apply', 'Apply to &reading copy', self.apply)),
            (('cancel', 'Cancel dictionary &operation', lambda event: self.cancel()),
             ('close', '&Close dictionary', self.close))):
            row = wx.WrapSizer(wx.HORIZONTAL)
            for identity, label, callback in entries:
                button = (wx.Button(self.dialog, wx.ID_CANCEL, label) if identity == 'close'
                          else wx.Button(self.dialog, label=label))
                button.SetName(label.replace('&', ''))
                button.Bind(wx.EVT_BUTTON, callback)
                self.buttons[identity] = button
                row.Add(button, 0, wx.ALL, 4)
            box.Add(row, 0, wx.EXPAND)
        self.dialog.SetSizer(box)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.choice.Bind(wx.EVT_CHOICE, self.select)
        self.refresh()

    def report(self, message):
        if not self.closed:
            self.status.ChangeValue(str(message))
            self.status.SetFocus()

    def admitted(self):
        if self.closed or self.busy:
            return False
        self.guard()
        return True

    def refresh(self, selected=None):
        if selected is None:
            selected = self.choice.GetSelection()
        self.choice.Set([f'{index + 1}. {rule.pattern} → {rule.replacement}; '
                         f'{"regex" if rule.regex else "literal"}; '
                         f'{rule.language or "any language"}; {rule.voice_id or "any voice"}'
                         for index, rule in enumerate(self.rules)])
        self.choice.SetSelection(selected if 0 <= selected < len(self.rules) else self.wx.NOT_FOUND)
        rule = self.rules[selected] if 0 <= selected < len(self.rules) else None
        for name in ('pattern', 'replacement', 'language', 'voice_id'):
            getattr(self, name).SetValue(getattr(rule, name) if rule is not None else '')
        for name in ('regex', 'ignore_case'):
            getattr(self, name).SetValue(getattr(rule, name) if rule is not None else False)
        for control in self.controls:
            control.Enable(not self.busy)
        for identity, button in self.buttons.items():
            button.Enable(identity == 'close' or (self.busy if identity == 'cancel' else not self.busy))

    def _index(self):
        index = self.choice.GetSelection()
        if not 0 <= index < len(self.rules):
            raise ValueError('Choose an existing rule first.')
        return index

    def _rule(self):
        return PronunciationRule(self.pattern.GetValue(), self.replacement.GetValue(),
            regex=self.regex.GetValue(), ignore_case=self.ignore_case.GetValue(),
            language=self.language.GetValue(), voice_id=self.voice_id.GetValue())

    def _entry_saved(self):
        """Do not apply/export old rules while a changed form is still visible."""
        selected = self.choice.GetSelection()
        if 0 <= selected < len(self.rules):
            if self._rule() != self.rules[selected]:
                raise ValueError('Save the edited rule changes before applying or exporting.')
        elif any(getattr(self, name).GetValue() for name in
                 ('pattern', 'replacement', 'language', 'voice_id', 'regex', 'ignore_case')):
            raise ValueError('Add the new rule before applying or exporting.')

    def select(self, event=None):
        try:
            if not self.admitted(): return
            rule = self.rules[self._index()]
            for name in ('pattern', 'replacement', 'regex', 'ignore_case', 'language', 'voice_id'):
                getattr(self, name).SetValue(getattr(rule, name))
        except Exception as exc:
            self.report(exc)

    def _change(self, rules, selected=-1):
        self.guard()
        self.changed(tuple(rules))
        self.rules = list(rules)
        self.refresh(selected)

    def add(self, event=None):
        try:
            if not self.admitted(): return
            if len(self.rules) >= 1000:
                raise ValueError('Use at most 1000 pronunciation rules.')
            self._change([*self.rules, self._rule()], len(self.rules))
            self.report('Rule added. Apply changes the reading copy; Export saves the dictionary.')
        except Exception as exc: self.report(exc)

    def edit(self, event=None):
        try:
            if not self.admitted(): return
            index, rules = self._index(), list(self.rules)
            rules[index] = self._rule()
            self._change(rules, index)
            self.report('Rule updated; the reading copy is unchanged until Apply.')
        except Exception as exc: self.report(exc)

    def remove(self, event=None):
        try:
            if not self.admitted(): return
            index, rules = self._index(), list(self.rules)
            del rules[index]
            self._change(rules, min(index, len(rules) - 1))
            self.report('Rule removed from this session dictionary.')
        except Exception as exc: self.report(exc)

    def _run(self, operation, done):
        self.busy = True
        self.refresh()
        def work():
            self.guard()
            if self.cancelled(): raise InterruptedError('Dictionary operation cancelled.')
            return operation()
        def accept(result):
            if not self.closed:
                self.guard()
                done(result)
        def finish():
            self.busy = False
            if not self.closed: self.refresh()
        if not self.run(work, accept, on_error=self.report, on_finished=finish):
            finish()

    def import_rules(self, event=None):
        try:
            if not self.admitted(): return
            with self.wx.FileDialog(self.dialog, 'Import pronunciation dictionary',
                    wildcard='Dictionary JSON (*.json)|*.json',
                    style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST) as picker:
                if picker.ShowModal() != self.wx.ID_OK: return
                path = Path(picker.GetPath())
            self.guard()
            def loaded(rules):
                if self.cancelled(): raise InterruptedError('Dictionary import cancelled.')
                self._change(rules)
                self.report(f'Imported {len(rules)} rules; they replace this session list, not the reading text.')
            self._run(lambda: import_dictionary(path, cancelled=self.cancelled), loaded)
        except Exception as exc: self.report(exc)

    def export_rules(self, event=None):
        try:
            if not self.admitted(): return
            self._entry_saved()
            with self.wx.FileDialog(self.dialog, 'Export dictionary to a new JSON file',
                    wildcard='Dictionary JSON (*.json)|*.json', style=self.wx.FD_SAVE) as picker:
                if picker.ShowModal() != self.wx.ID_OK: return
                path = Path(picker.GetPath())
            self.guard()
            approval = capture_export_destination(path, private_root=self.private_root)
            if not approval.require_absent:
                raise FileExistsError('Choose a new dictionary filename; existing files are not replaced.')
            rules = tuple(self.rules)
            def stopped():
                self.guard()
                current = capture_export_destination(path, private_root=self.private_root)
                if current != approval:
                    raise ValueError('Dictionary destination changed. Choose a new file again.')
                return self.cancelled()
            self._run(lambda: export_dictionary(rules, path, cancelled=stopped),
                      lambda _: self.report('Dictionary exported to ' + str(path)))
        except Exception as exc: self.report(exc)

    def apply(self, event=None):
        try:
            if not self.admitted(): return
            self._entry_saved()
            operation, accept = self.prepare_apply(tuple(self.rules))
            def applied(value):
                accept(value)
                self.report('Dictionary applied to the reading copy. Undo in the reader restores it; the source is unchanged.')
            self._run(operation, applied)
        except Exception as exc: self.report(exc)

    def close(self, event=None):
        if self.closed: return
        if self.busy: self.cancel()
        self.closed = True
        self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        try:
            self.dialog.ShowModal()
        finally:
            if self.busy: self.cancel()
            self.closed = True
            self.dialog.Destroy()
