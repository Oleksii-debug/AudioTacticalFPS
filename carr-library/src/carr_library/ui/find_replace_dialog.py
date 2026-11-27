"""Bounded find/replace on the existing reversible reading copy."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

from ..speech._io import check_cancelled, run_process, text_value
from ..speech.reading import ReadingDocument
from .pronunciation_dialog import ReadingSnapshot


@dataclass(frozen=True)
class FindQuery:
    pattern: str
    replacement: str = ''
    regex: bool = False
    ignore_case: bool = False

    def __post_init__(self):
        text_value(self.pattern, 'Find text', maximum=1024, empty=True)
        if not self.pattern:
            raise ValueError('Enter text or a regular expression to find.')
        text_value(self.replacement, 'Replacement text', maximum=4096, empty=True)
        if type(self.regex) is not bool or type(self.ignore_case) is not bool:
            raise ValueError('Find options must be booleans.')


@dataclass(frozen=True)
class FindResults:
    snapshot: ReadingSnapshot
    query: FindQuery
    bounds: tuple[int, int]
    matches: tuple[tuple[int, int, str], ...]


@dataclass(frozen=True)
class ReplacementPreview:
    results: FindResults
    indexes: tuple[int, ...]
    before: str
    after: str
    text: str


def find_matches(snapshot, query, *, bounds=None, cancelled=lambda: False, timeout=1):
    """Reuse the isolated regex worker for literal and expression searches."""
    check_cancelled(cancelled)
    if not isinstance(snapshot, ReadingSnapshot) or not isinstance(query, FindQuery):
        raise ValueError('Capture the current reading copy and search options first.')
    text_value(snapshot.text, 'Reading text', empty=True)
    bounds = (0, len(snapshot.text)) if bounds is None else bounds
    if (type(bounds) is not tuple or len(bounds) != 2
            or any(type(value) is not int for value in bounds)
            or not 0 <= bounds[0] <= bounds[1] <= len(snapshot.text)):
        raise ValueError('Choose an existing reading-text search scope.')
    start, end = bounds
    pattern = query.pattern if query.regex else re.escape(query.pattern)
    replacement = query.replacement if query.regex else query.replacement.replace('\\', '\\\\')
    payload = json.dumps({'text': snapshot.text[start:end], 'pattern': pattern,
        'replacement': replacement, 'ignore_case': query.ignore_case}, ensure_ascii=False).encode('utf-8')
    worker = Path(__file__).parents[1] / 'speech' / 'regex_worker.py'
    try:
        reply, _, _ = run_process([sys.executable, '-I', '-B', '-S', str(worker)],
            input_bytes=payload, timeout=timeout, cancelled=cancelled)
    except RuntimeError as exc:
        raise ValueError('Search could not complete. Check the expression and replacement groups, '
                         'or use a smaller text scope (at most 10,000 matches).') from exc
    check_cancelled(cancelled)
    value = json.loads(reply.decode('utf-8'))
    if type(value) is not dict or set(value) != {'matches'} or type(value['matches']) is not list:
        raise ValueError('The search worker returned an invalid result.')
    matches, previous = [], 0
    if len(value['matches']) > 10000:
        raise ValueError('Choose a smaller scope with at most 10,000 matches.')
    for row in value['matches']:
        check_cancelled(cancelled)
        if (type(row) is not list or len(row) != 3
                or type(row[0]) is not int or type(row[1]) is not int
                or not previous <= row[0] <= row[1] <= end - start):
            raise ValueError('The search worker returned invalid match positions.')
        text_value(row[2], 'Expanded replacement', maximum=1_000_000, empty=True)
        matches.append((start + row[0], start + row[1], row[2]))
        previous = row[1]
    return FindResults(snapshot, query, bounds, tuple(matches))


def next_match(results, selection, *, reverse=False, previous=None):
    """Return a match index and whether navigation wrapped, including empty matches."""
    if not isinstance(results, FindResults) or type(reverse) is not bool:
        raise ValueError('A reviewed search result is required.')
    if (type(selection) is not tuple or len(selection) != 2
            or any(type(value) is not int for value in selection)
            or not 0 <= selection[0] <= selection[1] <= len(results.snapshot.text)):
        raise ValueError('Choose an existing reading-text selection.')
    if not results.matches:
        raise ValueError('No matches in the selected search scope.')
    if previous is not None:
        if type(previous) is not int or not 0 <= previous < len(results.matches):
            raise ValueError('The previous search position is no longer available.')
        if results.matches[previous][:2] == selection:
            index = previous + (-1 if reverse else 1)
            return index % len(results.matches), not 0 <= index < len(results.matches)
    if reverse:
        for index in range(len(results.matches) - 1, -1, -1):
            if results.matches[index][1] <= selection[0]:
                return index, False
        return len(results.matches) - 1, True
    for index, match in enumerate(results.matches):
        if match[0] >= selection[1]:
            return index, False
    return 0, True


def preview_replacement(results, *, index=None, cancelled=lambda: False):
    check_cancelled(cancelled)
    if not isinstance(results, FindResults) or not results.matches:
        raise ValueError('Find a matching reading passage before replacing text.')
    if index is not None and (type(index) is not int or not 0 <= index < len(results.matches)):
        raise ValueError('Select a current search match before replacing it.')
    indexes = tuple(range(len(results.matches))) if index is None else (index,)
    start, end = results.bounds
    cursor, parts, size = start, [], 0
    for selected in indexes:
        check_cancelled(cancelled)
        first, last, replacement = results.matches[selected]
        pieces = (results.snapshot.text[cursor:first], replacement)
        size += sum(map(len, pieces))
        if size + start + len(results.snapshot.text) - end > 2_000_000:
            raise ValueError('The proposed reading edit exceeds its character budget.')
        parts.extend(pieces)
        cursor = last
    parts.append(results.snapshot.text[cursor:end])
    after = ''.join(parts)
    text = results.snapshot.text[:start] + after + results.snapshot.text[end:]
    text_value(text, 'Proposed reading copy', empty=True)
    check_cancelled(cancelled)
    return ReplacementPreview(results, indexes, results.snapshot.text[start:end], after, text)


def apply_replacement(document, preview, current):
    """Publish one undoable reading edit after rechecking the captured source."""
    if (not isinstance(document, ReadingDocument) or not isinstance(preview, ReplacementPreview)
            or current != preview.results.snapshot or id(document) != current.identity
            or document._signature() != current.signature or document.text != current.text):
        raise ValueError('The reading copy or selection changed. Preview the replacement again.')
    if preview.text == current.text:
        raise ValueError('This preview makes no reading-text changes.')
    return document.set_text(preview.text, label='Reviewed find/replace reading edit')


def create_find_replace_dialog(parent, *, capture, select, apply, undo, guard,
                               run, cancel, wx_module=None):
    """Use the reader's worker and undo history; never edit a retained original."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class FindReplaceDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Find and replace in reading copy', size=(860, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            guard()
            initial = capture()
            self.scope_snapshot = initial
            self.alive, self.busy = True, False
            self.preview, self.preview_form, self.applied, self.last_found = None, None, None, None
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label='Search this reading copy. Preview replacements before applying; '
                'each apply is one undoable edit. Original sources and quotations stay unchanged. '
                'Selected-text scope uses the selection made before opening this dialog.')
            notice.Wrap(790); box.Add(notice, 0, wx.ALL | wx.EXPAND, 5)
            def field(label, factory=wx.TextCtrl, **kwargs):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                widget = factory(panel, **kwargs); widget.SetName(label.replace('&', ''))
                box.Add(widget, 0, wx.ALL | wx.EXPAND, 3)
                return widget
            self.pattern = field('&Find text or expression')
            self.replacement = field('&Replacement text; empty removes the match')
            self.regex = field('Use a bounded regular &expression', wx.CheckBox)
            self.ignore_case = field('Ignore letter &case', wx.CheckBox)
            self.scope = field('Search &scope', wx.Choice, choices=['Whole reading copy', 'Original selected text'])
            self.scope.SetSelection(int(initial.selection[0] != initial.selection[1]))
            self.before = field('Current text in reviewed scope', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
            self.after = field('Proposed text in reviewed scope', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
            self.status = field('Find and replace status', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80))
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.EXPAND)
            self.buttons = {}
            rows = ((('previous', 'Find &previous', lambda event: self.on_find(reverse=True)),
                     ('next', 'Find &next', self.on_find), ('one', 'Preview selected replacement', self.on_preview)),
                    (('all', 'Preview replace &all', lambda event: self.on_preview(replace_all=True)),
                     ('apply', '&Apply reviewed replacement', self.on_apply),
                     ('undo', '&Undo last apply', self.on_undo)),
                    (('cancel', 'Cancel search', self.on_cancel), ('close', 'Close', self.on_close)))
            for items in rows:
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in items:
                    button = wx.Button(self, label=label); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 3)
                    self.buttons[key] = button
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout); panel.FitInside()
            for widget in (self.pattern, self.replacement):
                widget.Bind(wx.EVT_TEXT, self.on_change)
            for widget in (self.regex, self.ignore_case):
                widget.Bind(wx.EVT_CHECKBOX, self.on_change)
            self.scope.Bind(wx.EVT_CHOICE, self.on_change)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.report('Enter search text. Find next/previous changes only the selection. '
                        'Regex work is isolated, cancellable and limited to 10,000 matches per scope.')
            self.controls(); self.pattern.SetFocus()

        def report(self, value):
            if self.alive:
                self.status.ChangeValue(str(value))

        def controls(self):
            if not self.alive:
                return
            for widget in (self.pattern, self.replacement, self.regex, self.ignore_case, self.scope):
                widget.Enable(not self.busy)
            for key, button in self.buttons.items():
                button.Enable(not self.busy if key not in ('cancel', 'close') else key == 'close' or self.busy)
            self.buttons['apply'].Enable(not self.busy and self.preview is not None
                                          and self.preview.text != self.preview.results.snapshot.text)
            self.buttons['undo'].Enable(not self.busy and self.applied is not None)

        def form(self):
            return (self.pattern.GetValue(), self.replacement.GetValue(), bool(self.regex.GetValue()),
                    bool(self.ignore_case.GetValue()), self.scope.GetSelection())

        def on_change(self, event=None):
            self.preview, self.preview_form, self.last_found = None, None, None
            self.controls()

        def stopping(self):
            return cancel.is_set() or not self.alive

        def request(self, *, searching=False):
            guard(); snapshot = capture(); form = self.form()
            if form[4] not in (0, 1):
                raise ValueError('Choose a search scope.')
            bounds = (0, len(snapshot.text))
            if form[4] == 1:
                old = self.scope_snapshot
                if (snapshot.identity != old.identity or snapshot.signature != old.signature
                        or snapshot.text != old.text):
                    raise ValueError('The selected-text scope changed. Close and select the text again.')
                bounds = old.selection
                if bounds[0] == bounds[1]:
                    raise ValueError('Select reading text before opening a selected-text search.')
            query = FindQuery(form[0], '' if searching else form[1], form[2], form[3])
            return snapshot, query, bounds, form

        def begin(self, work, done):
            if self.busy or not self.alive:
                return False
            try:
                guard()
            except Exception as exc:
                self.report(exc); return False
            self.busy = True; self.controls()
            def complete(value):
                self.busy = False
                if self.alive:
                    try:
                        check_cancelled(self.stopping); guard(); done(value)
                    except Exception as exc:
                        self.report(exc)
                    self.controls()
            def failed(error):
                self.busy = False
                if self.alive:
                    self.report(error); self.controls()
            try:
                if run(work, complete, on_failed=failed) is False:
                    failed('A reader operation is running. Finish or cancel it first.'); return False
            except Exception as exc:
                failed(exc); return False
            return True

        def on_find(self, event=None, *, reverse=False):
            if self.busy or not self.alive:
                return
            try:
                snapshot, query, bounds, form = self.request(searching=True)
                identity = (snapshot.identity, snapshot.signature, snapshot.text, query, bounds)
                previous = self.last_found[1] if self.last_found and self.last_found[0] == identity else None
                self.preview, self.preview_form = None, None
                def done(result):
                    if capture() != snapshot or self.form() != form:
                        raise ValueError('The reading selection or search changed. Find again.')
                    index, wrapped = next_match(result, snapshot.selection, reverse=reverse, previous=previous)
                    first, last, _ = result.matches[index]
                    select(snapshot, (first, last))
                    self.last_found = identity, index
                    self.report(f'Match {index + 1} of {len(result.matches)}; characters {first} to {last}. '
                                + ('Wrapped within the search scope. ' if wrapped else '')
                                + 'The matching text is selected in the reading copy. Preview before replacing.')
                self.begin(lambda: find_matches(snapshot, query, bounds=bounds, cancelled=self.stopping), done)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_preview(self, event=None, *, replace_all=False):
            if self.busy or not self.alive:
                return
            try:
                snapshot, query, bounds, form = self.request()
                self.preview, self.preview_form = None, None
                def work():
                    result = find_matches(snapshot, query, bounds=bounds, cancelled=self.stopping)
                    index = None
                    if not replace_all:
                        index = next((index for index, match in enumerate(result.matches)
                                      if match[:2] == snapshot.selection), None)
                        if index is None:
                            raise ValueError('Use Find next or previous to select a matching passage first.')
                    return preview_replacement(result, index=index, cancelled=self.stopping)
                def done(preview):
                    if capture() != snapshot or self.form() != form:
                        raise ValueError('Text, selection or search options changed. Preview again.')
                    self.preview, self.preview_form = preview, form
                    self.before.ChangeValue(preview.before); self.after.ChangeValue(preview.after)
                    self.report(f'Previewed {len(preview.indexes)} replacements. '
                        + ('No text would change.' if preview.text == snapshot.text
                           else 'Review the proposed text, then apply explicitly.'))
                self.begin(work, done)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_apply(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard()
                if self.preview is None or self.preview_form != self.form():
                    raise ValueError('Preview the current replacement before applying.')
                self.applied, warning = apply(self.preview)
                self.scope_snapshot = self.applied
                self.scope.SetSelection(0)
                self.on_change()
                self.report('Replacement applied as one reading edit. ' + warning)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_undo(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard()
                if self.applied is None:
                    raise ValueError('No find/replace edit is available to undo here.')
                warning = undo(self.applied)
                self.applied = None; self.scope_snapshot = capture(); self.scope.SetSelection(0)
                self.on_change(); self.report('Find/replace edit undone. ' + warning)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_cancel(self, event=None):
            if self.busy:
                cancel.set(); self.report('Cancelling search. No replacement is applied by cancellation.')

        def finish_close(self):
            self.alive = False
            if self.busy:
                cancel.set()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Hide()

        def on_close(self, event=None):
            if self.alive:
                self.finish_close()

    return FindReplaceDialog()
