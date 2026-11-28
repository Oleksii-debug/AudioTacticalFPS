"""Native find/replace and pronunciation review on the existing speech reader."""
from dataclasses import dataclass
from pathlib import Path
import sys
import threading

from ..speech.reading import PronunciationRule, export_dictionary, import_dictionary
from ..speech.reading_tools import (ReadingSnapshot, find_matches, next_match,
                                    preview_matches, preview_pronunciations)


@dataclass
class ReaderEditHost:
    """Borrowed callbacks; the speech reader owns its document and executor."""
    capture: object
    selection: object
    select: object
    verify: object
    apply: object
    submit: object
    guard: object
    report: object
    alive: object
    busy: object
    cancelled: object
    voice: object


def reading_offset(control, position):
    """Native multiline positions are not Python indices on Windows."""
    value = control.GetValue()
    prefix = control.GetRange(0, position)
    if not value.startswith(prefix):
        raise ValueError('The native reading selection changed. Select the text again.')
    return len(prefix)


def reading_selection(control):
    first, last = control.GetSelection()
    return reading_offset(control, first), reading_offset(control, last)


def native_position(control, offset, *, windows=None):
    value = control.GetValue()
    if type(offset) is not int or not 0 <= offset <= len(value):
        raise ValueError('The requested reading position is outside the current copy.')
    prefix = value[:offset]
    line = prefix.count('\n')
    column = prefix.rsplit('\n', 1)[-1]
    windows = sys.platform == 'win32' if windows is None else windows
    width = len(column.encode('utf-16-le')) // 2 if windows else len(column)
    position = control.XYToPosition(width, line)
    # Check the actual native API result, including CRLF and non-BMP characters.
    # Never guess or apply a replacement using an unverified offset conversion.
    if position < 0 or control.GetRange(0, position) != prefix:
        raise ValueError('The native control could not locate this Unicode reading position.')
    return position


def select_reading_range(control, start, end):
    first, last = native_position(control, start), native_position(control, end)
    control.SetSelection(first, last)
    control.ShowPosition(first)


class ReaderToolSession:
    """Guard one review transaction while borrowing the reader's worker."""
    def __init__(self, host, *, report, changed=lambda: None):
        self.host, self.report, self.changed = host, report, changed
        self.closed = self.busy = False
        self.cancelled = threading.Event()
        self.generation = 0
        self.preview = None

    def invalidate(self):
        self.preview = None
        self.generation += 1
        self.changed()

    def start(self, work, on_done, *, durable=False):
        if self.closed or not self.host.alive():
            return False
        if self.busy or self.host.busy():
            self.report('Wait for the current reader operation or cancel it first.')
            return False
        try:
            self.host.guard()
            snapshot = self.host.capture()
        except Exception as exc:
            self.report(str(exc))
            return False
        self.invalidate()
        generation = self.generation
        self.cancelled = threading.Event()
        cancel = self.cancelled
        self.busy = True
        self.changed()

        def stopped():
            return cancel.is_set() or self.host.cancelled() or not self.host.alive()

        def perform():
            if stopped():
                raise InterruptedError('Reading operation cancelled before execution.')
            return work(snapshot, stopped)

        def failed(exc):
            self.busy = False
            if self.closed or not self.host.alive():
                return
            self.changed()
            self.report(f'Reading operation did not complete. {exc}')

        def finished(value):
            self.busy = False
            if self.closed or not self.host.alive():
                return
            self.changed()
            if not durable and (stopped() or generation != self.generation):
                self.report('Reading operation cancelled; no proposed edit was applied.')
                return
            try:
                if not durable:
                    self.host.verify(snapshot)
                on_done(snapshot, value)
            except Exception as exc:
                failed(exc)

        try:
            submitted = self.host.submit(perform, finished, on_error=failed)
        except Exception as exc:
            failed(exc)
            return False
        if submitted is False:
            self.busy = False
            self.changed()
            return False
        return True

    def accept(self):
        if self.closed or self.busy or self.preview is None:
            raise ValueError('Prepare a current preview before applying an edit.')
        preview = self.preview
        if self.cancelled.is_set() or self.host.cancelled():
            raise InterruptedError('The preview was cancelled. Prepare it again.')
        self.host.verify(preview.snapshot)
        changed = self.host.apply(preview.snapshot, preview.start, preview.end,
                                  preview.replacement, label=preview.label)
        self.invalidate()
        return changed

    def cancel(self):
        self.cancelled.set()
        self.invalidate()
        if not self.closed:
            self.report('Cancelling the reading operation; accepted edits and original text are retained.')

    def close(self):
        self.closed = True
        self.cancelled.set()
        self.preview = None
        self.generation += 1


class ReadingToolsDialog:
    def __init__(self, parent, host, *, pronunciation=False, rules=()):
        import wx
        self.wx, self.host = wx, host
        self.pronunciation = pronunciation
        self.rules = list(rules)
        self.scope = host.selection()
        self.last_find = None
        self.dialog = wx.Dialog(parent,
            title='Pronunciation dictionary for this reading copy' if pronunciation else 'Find and replace in reading copy',
            size=(920, 760), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.reading_tools = self
        self._window_id = self.dialog.GetId()
        self.destroyed = False
        layout = wx.BoxSizer(wx.VERTICAL)
        info = wx.TextCtrl(self.dialog, value='Only this reading copy can change. The original text and source quotations '
            'remain unchanged. Regular expressions run in the existing isolated, bounded worker. '
            'Preview before applying; Undo in the reader restores the complete accepted operation.',
            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        info.SetName('Reading edit scope and original-source boundary')
        layout.Add(info, 0, wx.EXPAND | wx.ALL, 6)
        form = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        form.AddGrowableCol(1, 1)
        self.pattern = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE, size=(-1, 48))
        self.replacement = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE, size=(-1, 48))
        for label, field in (('Find text or regular expression', self.pattern),
                             ('Replacement; empty removes the match', self.replacement)):
            form.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            field.SetName(label)
            form.Add(field, 1, wx.EXPAND)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 6)
        options = wx.WrapSizer(wx.HORIZONTAL)
        self.regex = wx.CheckBox(self.dialog, label='Regular &expression')
        self.match_case = wx.CheckBox(self.dialog, label='Match &case')
        self.match_case.SetValue(True)
        self.whole_word = wx.CheckBox(self.dialog, label='Whole &words')
        self.selected_only = wx.CheckBox(self.dialog, label='Captured reading &selection only')
        self.wrap = wx.CheckBox(self.dialog, label='&Wrap search')
        self.wrap.SetValue(True)
        for field in (self.regex, self.match_case, self.whole_word, self.selected_only, self.wrap):
            options.Add(field, 0, wx.ALL, 3)
        self.whole_word.Show(not pronunciation)
        self.wrap.Show(not pronunciation)
        layout.Add(options, 0, wx.EXPAND | wx.ALL, 3)
        self.rules_list = None
        self.language = self.voice_id = None
        self.mutators = [self.pattern, self.replacement, self.regex, self.match_case,
                         self.whole_word, self.selected_only, self.wrap]
        if pronunciation:
            scopes = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
            scopes.AddGrowableCol(1, 1)
            language, identity, name = host.voice()
            self.language = wx.TextCtrl(self.dialog, value=language)
            self.voice_id = wx.TextCtrl(self.dialog, value=identity)
            for label, field in (('Rule language; empty applies to all', self.language),
                                 ('Rule voice ID; empty applies to all', self.voice_id)):
                scopes.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                field.SetName(label)
                scopes.Add(field, 1, wx.EXPAND)
            layout.Add(scopes, 0, wx.EXPAND | wx.ALL, 6)
            self.mutators.extend((self.language, self.voice_id))
            self.rules_list = wx.ListBox(self.dialog)
            self.rules_list.SetName('Pronunciation rules; general scopes first, list order within equal scopes')
            self.rules_list.SetMinSize((-1, 90))
            layout.Add(self.rules_list, 0, wx.EXPAND | wx.ALL, 6)
            row = wx.WrapSizer(wx.HORIZONTAL)
            for label, handler in (('&Add rule', self.add_rule), ('&Update selected rule', self.update_rule),
                ('&Remove selected rule', self.remove_rule), ('Move rule up', lambda e: self.move_rule(-1)),
                ('Move rule down', lambda e: self.move_rule(1)), ('&Import dictionary...', self.import_rules),
                ('&Export dictionary...', self.export_rules)):
                self.mutators.append(self._button(row, label, handler))
            layout.Add(row, 0, wx.EXPAND | wx.ALL, 3)
            self.rules_list.Bind(wx.EVT_LISTBOX, self.select_rule)
        row = wx.WrapSizer(wx.HORIZONTAL)
        if not pronunciation:
            for label, handler in (('Find &next', lambda e: self.find(False)),
                ('Find &previous', lambda e: self.find(True)),
                ('Preview replace &one', self.replace_one)):
                self.mutators.append(self._button(row, label, handler))
        self.preview_button = self._button(row,
            '&Preview pronunciation changes' if pronunciation else 'Preview replace &all', self.preview)
        self.mutators.append(self.preview_button)
        self.apply_button = self._button(row, '&Apply reviewed changes', self.accept)
        self.reject_button = self._button(row, 'Reject preview', self.reject)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 3)
        self.preview_text = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.preview_text.SetName('Reviewed reading-copy changes or exact search result')
        layout.Add(self.preview_text, 1, wx.EXPAND | wx.ALL, 6)
        self.status = wx.TextCtrl(self.dialog, value='Ready. No change is applied automatically.',
                                  style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 60))
        self.status.SetName('Reading tools operation status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.cancel_button = self._button(row, 'Cancel operation', self.cancel)
        self.close_button = self._button(row, 'Close reading tools', self.close, identifier=wx.ID_CANCEL)
        layout.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.session = ReaderToolSession(host, report=self.report, changed=self.sync)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        for field in self.mutators:
            if isinstance(field, wx.TextCtrl):
                field.Bind(wx.EVT_TEXT, self.changed)
            elif isinstance(field, wx.CheckBox):
                field.Bind(wx.EVT_CHECKBOX, self.changed)
        self.refresh_rules()
        self.sync()

    def _button(self, row, label, handler, *, identifier=None):
        wx = self.wx
        button = wx.Button(self.dialog, wx.ID_ANY if identifier is None else identifier, label)
        button.SetName(label.replace('&', ''))
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.ALL, 3)
        return button

    def report(self, message):
        if not self.session.closed:
            self.status.ChangeValue(str(message))

    def sync(self):
        if self.session.closed:
            return
        for field in self.mutators:
            field.Enable(not self.session.busy)
        if self.rules_list is not None:
            self.rules_list.Enable(not self.session.busy)
        self.apply_button.Enable(not self.session.busy and self.session.preview is not None
                                 and self.session.preview.changed)
        self.reject_button.Enable(not self.session.busy and self.session.preview is not None)
        self.cancel_button.Enable(self.session.busy)

    def changed(self, event=None):
        if self.session.busy:
            return
        self.last_find = None
        self.session.invalidate()
        self.preview_text.ChangeValue('')
        if event is not None:
            event.Skip()

    def _options(self):
        return dict(regex=self.regex.GetValue(), ignore_case=not self.match_case.GetValue(),
                    whole_word=self.whole_word.GetValue(), scope=self.scope if self.selected_only.GetValue() else None)

    def find(self, backwards=False):
        pattern, options = self.pattern.GetValue(), self._options()
        selection, wrap = self.host.selection(), self.wrap.GetValue()
        previous = self.last_find

        def done(snapshot, matches):
            skip = selection[0] != selection[1] or previous == (snapshot.signature, pattern, options, selection)
            match = next_match(matches, selection, backwards=backwards, wrap=wrap, skip_current=skip)
            if match is None:
                self.report('No matching reading text in this direction and scope.')
                return
            self.host.select(match.start, match.end)
            self.last_find = (snapshot.signature, pattern, options, (match.start, match.end))
            self.preview_text.ChangeValue(f'{len(matches)} match(es). Current reading offsets '
                f'{match.start}–{match.end}.\n\n' + snapshot.text[max(0, match.start - 120):match.end + 120])
            self.report(f'Match selected at reading offsets {match.start}–{match.end}. '
                        'Close this dialog to read or edit the selection.')
        self.session.start(lambda snapshot, cancelled: find_matches(snapshot, pattern,
            cancelled=cancelled, **options), done)

    def _show_preview(self, snapshot, preview):
        self.session.preview = preview
        self.preview_text.ChangeValue(preview.description())
        self.report('Review the exact changes, then Apply or Reject.' if preview.changed else 'No reading text would change.')
        self.sync()

    def preview(self, event=None):
        if self.pronunciation:
            rules = tuple(self.rules)
            language, voice_id, _ = self.host.voice()
            scope = self.scope if self.selected_only.GetValue() else None
            work = lambda snapshot, cancelled: preview_pronunciations(snapshot, rules,
                language=language, voice_id=voice_id, scope=scope, cancelled=cancelled)
        else:
            pattern, replacement, options = self.pattern.GetValue(), self.replacement.GetValue(), self._options()
            work = lambda snapshot, cancelled: preview_matches(snapshot,
                find_matches(snapshot, pattern, replacement, cancelled=cancelled, **options))
        self.session.start(work, self._show_preview)

    def replace_one(self, event=None):
        selection = self.host.selection()
        pattern, replacement, options = self.pattern.GetValue(), self.replacement.GetValue(), self._options()

        def work(snapshot, cancelled):
            matches = find_matches(snapshot, pattern, replacement, cancelled=cancelled, **options)
            match = next((m for m in matches if (m.start, m.end) == selection), None)
            if match is None:
                raise ValueError('Use Find Next or Previous to select an exact current match before replacing one.')
            return preview_matches(snapshot, (match,))
        self.session.start(work, self._show_preview)

    def accept(self, event=None):
        try:
            preview = self.session.preview
            changed = self.session.accept()
            if changed:
                self.scope = preview.after_selection
                self.last_find = None
                self.report('Reviewed change applied to the reading copy. Undo in the reader restores this operation.')
            else:
                self.report('No reading text changed.')
        except Exception as exc:
            self.report(str(exc))

    def reject(self, event=None):
        self.session.invalidate()
        self.preview_text.ChangeValue('')
        self.report('Preview rejected. No reading edit was applied.')

    def _rule(self):
        return PronunciationRule(self.pattern.GetValue(), self.replacement.GetValue(),
            regex=self.regex.GetValue(), ignore_case=not self.match_case.GetValue(),
            language=self.language.GetValue().strip(), voice_id=self.voice_id.GetValue().strip())

    def refresh_rules(self, selected=None):
        if self.rules_list is None:
            return
        self.rules_list.Set([f'{i + 1}. {"Regex" if rule.regex else "Literal"}: {rule.pattern[:80]!r} '
            f'to {rule.replacement[:80]!r}; language {rule.language or "all"}; voice {rule.voice_id or "all"}'
            for i, rule in enumerate(self.rules)])
        if selected is not None and 0 <= selected < len(self.rules):
            self.rules_list.SetSelection(selected)

    def add_rule(self, event=None):
        try:
            if len(self.rules) >= 1000:
                raise ValueError('The dictionary supports at most 1000 rules.')
            self.rules.append(self._rule())
            self.refresh_rules(len(self.rules) - 1)
            self.changed()
            self.report('Rule added to this manager. Export dictionary preserves rules for another session.')
        except Exception as exc:
            self.report(str(exc))

    def update_rule(self, event=None):
        try:
            index = self.rules_list.GetSelection()
            if not 0 <= index < len(self.rules):
                raise ValueError('Select a dictionary rule to update.')
            self.rules[index] = self._rule()
            self.refresh_rules(index)
            self.changed()
        except Exception as exc:
            self.report(str(exc))

    def select_rule(self, event=None):
        index = self.rules_list.GetSelection()
        if 0 <= index < len(self.rules):
            rule = self.rules[index]
            for field, value in ((self.pattern, rule.pattern), (self.replacement, rule.replacement),
                                 (self.language, rule.language), (self.voice_id, rule.voice_id)):
                field.ChangeValue(value)
            self.regex.SetValue(rule.regex)
            self.match_case.SetValue(not rule.ignore_case)
            self.changed()

    def remove_rule(self, event=None):
        index = self.rules_list.GetSelection()
        if 0 <= index < len(self.rules):
            self.rules.pop(index)
            self.refresh_rules(min(index, len(self.rules) - 1))
            self.changed()

    def move_rule(self, direction):
        index = self.rules_list.GetSelection()
        target = index + direction
        if 0 <= index < len(self.rules) and 0 <= target < len(self.rules):
            self.rules[index], self.rules[target] = self.rules[target], self.rules[index]
            self.refresh_rules(target)
            self.changed()

    def import_rules(self, event=None):
        wx = self.wx
        try:
            self.host.guard()
            with wx.FileDialog(self.dialog, 'Import reading pronunciation dictionary',
                wildcard='Reading pronunciation dictionary (*.json)|*.json',
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as choose:
                if choose.ShowModal() != wx.ID_OK:
                    return
                path = Path(choose.GetPath())
            self.host.guard()
            if self.session.closed:
                return

            def done(snapshot, rules):
                self.rules = list(rules)
                self.refresh_rules(0)
                self.report(f'{len(self.rules)} rules imported. Preview is required before applying any pronunciation.')
            self.session.start(lambda snapshot, cancelled: import_dictionary(path), done)
        except Exception as exc:
            self.report(str(exc))

    def export_rules(self, event=None):
        wx = self.wx
        try:
            self.host.guard()
            rules = tuple(self.rules)
            with wx.FileDialog(self.dialog, 'Export reading pronunciation dictionary to a new file',
                wildcard='Reading pronunciation dictionary (*.json)|*.json', style=wx.FD_SAVE) as choose:
                if choose.ShowModal() != wx.ID_OK:
                    return
                path = Path(choose.GetPath())
            self.host.guard()
            if self.session.closed:
                return

            def work(snapshot, cancelled):
                export_dictionary(rules, path, cancelled=cancelled)
                return path
            self.session.start(work, lambda snapshot, path:
                self.report('Pronunciation dictionary exported to the selected new file.'), durable=True)
        except Exception as exc:
            self.report(str(exc))

    def cancel(self, event=None):
        self.session.cancel()

    def _destroyed(self, event):
        # SIP may invalidate the original wrapper before EVT_WINDOW_DESTROY.
        # Use its captured live ID; never inspect or call the dying window.
        if event.GetId() == self._window_id:
            self.destroyed = True
            self.session.close()
        event.Skip()

    def close(self, event=None):
        if self.destroyed:
            return
        self.session.close()
        self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        try:
            self.pattern.SetFocus()
            self.dialog.ShowModal()
            return tuple(self.rules)
        finally:
            self.session.close()
            if not self.destroyed:
                self.dialog.Destroy()


def show_find_replace(parent, host):
    return ReadingToolsDialog(parent, host).show()


def show_pronunciations(parent, host, rules=()):
    return ReadingToolsDialog(parent, host, pronunciation=True, rules=rules).show()
