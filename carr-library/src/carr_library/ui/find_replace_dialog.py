"""Precise find/replace review through the existing bounded substitution worker."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import sys

from ..speech._io import check_cancelled, run_process, text_value
from ..speech.reading import ReadingDocument, PronunciationRule
from .pronunciation_dialog import ReadingSnapshot


@dataclass(frozen=True)
class FindMatch:
    start: int
    end: int
    before: str
    replacement: str


@dataclass(frozen=True)
class FindPreview:
    snapshot: ReadingSnapshot
    rule: PronunciationRule
    selection_only: bool
    start: int
    end: int
    before: str
    after: str
    matches: tuple[FindMatch, ...]


def preview_find(snapshot, rule, *, selection_only=False, cancelled=lambda: False, timeout=1):
    """Capture exact match offsets in a cancellable child, never edit the document."""
    check_cancelled(cancelled)
    if (not isinstance(snapshot, ReadingSnapshot) or not isinstance(rule, PronunciationRule)
            or rule.language or rule.voice_id or type(selection_only) is not bool):
        raise ValueError('Use one reviewed text pattern and a captured reading scope.')
    if type(timeout) not in (int, float) or not .01 <= timeout <= 10:
        raise ValueError('The search worker time allowance must be between 0.01 and 10 seconds.')
    start, end = snapshot.selection if selection_only else (0, len(snapshot.text))
    if start == end:
        raise ValueError('Choose nonempty reading text to search.')
    before = snapshot.text[start:end]
    payload = json.dumps({'text': before,
        'pattern': rule.pattern if rule.regex else re.escape(rule.pattern),
        'replacement': rule.replacement if rule.regex else rule.replacement.replace('\\', '\\\\'),
        'ignore_case': rule.ignore_case}, ensure_ascii=False).encode('utf-8')
    from ..speech import reading
    reply, _, _ = run_process([sys.executable, '-I', '-B', '-S',
        str(Path(reading.__file__).with_name('regex_worker.py'))],
        input_bytes=payload, timeout=timeout, cancelled=cancelled)
    result = json.loads(reply.decode('utf-8'))
    if (not isinstance(result, dict) or set(result) != {'matches'}
            or not isinstance(result['matches'], list) or len(result['matches']) > 10000):
        raise ValueError('The bounded search worker returned an invalid match list.')
    matches, pieces, cursor = [], [], 0
    for row in result['matches']:
        check_cancelled(cancelled)
        if (not isinstance(row, list) or len(row) != 3 or type(row[0]) is not int
                or type(row[1]) is not int or not cursor <= row[0] <= row[1] <= len(before)):
            raise ValueError('The search worker returned an invalid text interval.')
        first, last, replacement = row
        text_value(replacement, 'expanded replacement', maximum=1_000_000, empty=True)
        matches.append(FindMatch(start + first, start + last, before[first:last], replacement))
        pieces.extend((before[cursor:first], replacement))
        cursor = last
    pieces.append(before[cursor:])
    after = ''.join(pieces)
    text_value(snapshot.text[:start] + after + snapshot.text[end:], 'proposed reading copy', empty=True)
    check_cancelled(cancelled)
    return FindPreview(snapshot, rule, selection_only, start, end, before, after, tuple(matches))


def check_find_preview(document, preview, current):
    if (not isinstance(document, ReadingDocument) or not isinstance(preview, FindPreview)
            or not isinstance(current, ReadingSnapshot) or current != preview.snapshot
            or id(document) != current.identity or document._signature() != current.signature
            or document.text != current.text
            or preview.before != document.text[preview.start:preview.end]):
        raise ValueError('The reading text or selection changed. Preview the search again.')


def apply_find_preview(document, preview, current, *, index=None):
    """Apply exactly one selected match or the whole reviewed proposal as one edit."""
    check_find_preview(document, preview, current)
    if index is None:
        if not preview.matches or preview.before == preview.after:
            raise ValueError('The reviewed search contains no text changes.')
        return document.set_text(current.text[:preview.start] + preview.after + current.text[preview.end:],
                                 label='Reviewed find/replace')
    if type(index) is not int or not 0 <= index < len(preview.matches):
        raise ValueError('Choose a match from the current reviewed search.')
    item = preview.matches[index]
    if current.selection != (item.start, item.end):
        raise ValueError('Select this exact reviewed match before replacing it.')
    if document.text[item.start:item.end] != item.before or item.before == item.replacement:
        raise ValueError('The reviewed match changed or its replacement makes no change.')
    return document.replace(item.start, item.end, item.replacement, label='Reviewed single replacement')


def create_find_dialog(parent, *, capture, select, apply, undo, guard, run, cancel, wx_module=None):
    """Use the existing reader executor; no additional worker pool or registry."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class FindDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Find and replace — review a reading copy', size=(940, 800),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            guard()
            initial = capture()
            selected = initial.text[slice(*initial.selection)]
            self.alive, self.busy = True, False
            self.preview, self.preview_form, self.applied, self.index = None, None, None, -1
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label='Find selects exact reading-copy characters. Preview before replacing. '
                'Original source bytes and quotation authority are unchanged. '
                'Search uses the existing isolated regex worker with a time and match budget.')
            notice.Wrap(870); box.Add(notice, 0, wx.ALL | wx.EXPAND, 5)
            def field(label, factory=wx.TextCtrl, **kwargs):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                widget = factory(panel, **kwargs); widget.SetName(label.replace('&', ''))
                box.Add(widget, 0, wx.ALL | wx.EXPAND, 3)
                return widget
            self.pattern = field('Find &text or pattern', value=selected if 0 < len(selected) <= 1024 else '')
            self.replacement = field('Replacement text; empty deletes a match')
            self.regex = field('Use a bounded regular expression', wx.CheckBox)
            self.ignore_case = field('Ignore letter case', wx.CheckBox)
            self.scope = field('Search scope', wx.Choice, choices=['Whole reading copy', 'Selected text only'])
            self.scope.SetSelection(0)
            self.list = field('Reviewed exact matches', wx.ListBox, choices=[])
            self.before = field('Current text in the reviewed scope', style=wx.TE_MULTILINE | wx.TE_READONLY,
                                size=(-1, 90))
            self.after = field('Proposed text for Replace all reviewed', style=wx.TE_MULTILINE | wx.TE_READONLY,
                               size=(-1, 90))
            self.status = field('Find and replace status', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.EXPAND)
            self.buttons = {}
            for items in ((('preview', 'Preview matches', self.on_preview),
                           ('previous', 'Previous match', lambda event: self.move(-1)),
                           ('next', 'Next match', lambda event: self.move(1))),
                          (('one', 'Replace selected match', self.on_one),
                           ('all', 'Replace all reviewed', self.on_all),
                           ('undo', 'Undo last replacement', self.on_undo),
                           ('cancel', 'Cancel search', self.on_cancel),
                           ('close', 'Close', self.on_close))):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in items:
                    button = wx.Button(self, label=label); button.SetName(label)
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 4)
                    self.buttons[key] = button
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout)
            for widget, event in ((self.pattern, wx.EVT_TEXT), (self.replacement, wx.EVT_TEXT),
                                  (self.regex, wx.EVT_CHECKBOX), (self.ignore_case, wx.EVT_CHECKBOX),
                                  (self.scope, wx.EVT_CHOICE)):
                widget.Bind(event, self.on_change)
            self.list.Bind(wx.EVT_LISTBOX, self.on_select)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_CHAR_HOOK, self.key)
            self.controls(); self.pattern.SetFocus()

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
            for widget in (self.pattern, self.replacement, self.regex, self.ignore_case, self.scope, self.list):
                widget.Enable(not self.busy)
            for key, button in self.buttons.items():
                button.Enable(not self.busy if key not in ('cancel', 'close') else key == 'close' or self.busy)
            valid = not self.busy and self.preview is not None
            self.buttons['previous'].Enable(valid and self.index > 0)
            self.buttons['next'].Enable(valid and self.index + 1 < len(self.preview.matches))
            self.buttons['one'].Enable(valid and 0 <= self.index < len(self.preview.matches)
                and self.preview.matches[self.index].before != self.preview.matches[self.index].replacement)
            self.buttons['all'].Enable(valid and bool(self.preview.matches) and self.preview.before != self.preview.after)
            self.buttons['undo'].Enable(not self.busy and self.applied is not None)

        def invalidate(self):
            self.preview, self.preview_form, self.index = None, None, -1
            if self.alive:
                self.list.Set([]); self.before.ChangeValue(''); self.after.ChangeValue('')
                self.controls()

        def on_change(self, event=None):
            if not self.busy:
                self.invalidate()

        def form(self):
            return (self.pattern.GetValue(), self.replacement.GetValue(), bool(self.regex.GetValue()),
                    bool(self.ignore_case.GetValue()), self.scope.GetSelection())

        def current(self):
            guard(); check_cancelled(self.stopping)
            if self.preview is None or self.form() != self.preview_form or capture() != self.preview.snapshot:
                raise ValueError('The reading text, selected range or search form changed. Preview again.')
            return self.preview

        def begin(self, work, complete):
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
                check_cancelled(self.stopping); guard()
                return result
            def done(result):
                self.busy = False
                if not self.alive:
                    return
                try:
                    check_cancelled(self.stopping); guard(); complete(result)
                except Exception as exc:
                    self.invalidate(); self.report(exc)
                self.controls()
            def failed(error):
                self.busy = False
                if self.alive:
                    self.invalidate(); self.report(error); self.controls()
            try:
                if run(operation, done, on_error=failed) is False:
                    failed('A reader operation is running. Finish or cancel it first.')
                    return False
            except Exception as exc:
                failed(exc); return False
            return True

        def on_preview(self, event=None):
            if self.busy:
                return
            try:
                guard(); form, snapshot = self.form(), capture()
                pattern, replacement, regex, ignore_case, scope = form
                if scope not in (0, 1):
                    raise ValueError('Choose whole-copy or selected-text scope.')
                rule = PronunciationRule(pattern, replacement, regex=regex, ignore_case=ignore_case)
                self.invalidate()
                def done(result):
                    if self.form() != form or capture() != snapshot:
                        raise ValueError('Text, selection or search form changed. Preview again.')
                    self.preview, self.preview_form = result, form
                    self.list.Set([f'{i + 1}. Characters {item.start + 1}–{item.end}: '
                                   + (item.before[:90] if item.before else '[insertion point]')
                                   for i, item in enumerate(result.matches)])
                    self.before.ChangeValue(result.before); self.after.ChangeValue(result.after)
                    self.report(f'{len(result.matches)} exact matches in characters {result.start + 1} to {result.end}. '
                                'Review the proposal, or select a match before replacing only that occurrence.')
                    if result.matches:
                        self.choose(0)
                    self.after.SetFocus()
                self.begin(lambda: preview_find(snapshot, rule, selection_only=scope == 1,
                                                cancelled=self.stopping), done)
            except Exception as exc:
                self.invalidate(); self.report(exc)

        def choose(self, index):
            preview = self.current()
            if type(index) is not int or not 0 <= index < len(preview.matches):
                raise ValueError('There is no further match in this direction.')
            current = select(preview, index)
            self.preview = replace(preview, snapshot=current)
            self.index = index
            self.list.SetSelection(index)
            self.report(f'Match {index + 1} of {len(preview.matches)} selected. No text was changed.')
            self.controls()

        def move(self, direction):
            if self.busy:
                return
            try:
                self.choose(self.index + direction)
            except Exception as exc:
                self.report(exc)

        def on_select(self, event=None):
            if self.busy:
                return
            try:
                self.choose(self.list.GetSelection())
            except Exception as exc:
                self.invalidate(); self.report(exc)

        def replace_reviewed(self, index):
            if self.busy:
                return
            try:
                preview = self.current()
                self.applied, warning = apply(preview, index)
                self.invalidate()
                self.report('Reviewed replacement applied as one undoable reading-copy edit. ' + warning)
            except Exception as exc:
                self.invalidate(); self.report(exc)

        def on_one(self, event=None):
            if self.index < 0:
                self.report('Select a reviewed match first.'); return
            self.replace_reviewed(self.index)

        def on_all(self, event=None):
            self.replace_reviewed(None)

        def on_undo(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                if self.applied is None:
                    raise ValueError('There is no replacement from this dialog to undo.')
                guard(); warning = undo(self.applied)
                self.applied = None; self.invalidate()
                self.report('Last replacement undone. Original source bytes are unchanged. ' + warning)
            except Exception as exc:
                self.report(exc)

        def on_cancel(self, event=None):
            if not self.alive:
                return
            cancel.set(); self.invalidate()
            self.report('Search cancellation requested. No replacement is applied by a search.')

        def on_close(self, event=None):
            if not self.alive:
                return
            self.alive = False; cancel.set()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Hide()

        def key(self, event):
            if event.GetKeyCode() == wx.WXK_ESCAPE:
                self.on_close()
            else:
                event.Skip()

    return FindDialog()
