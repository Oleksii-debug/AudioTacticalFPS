"""Explicit Windows spelling review on the existing reader edit transaction."""
from ..speech.reading_tools import ReadingPreview
from ..speech.spelling import (MAX_TEXT, SpellingPreview, WindowsSpelling, _language)
from .reading_tools import ReaderToolSession


class ReaderSpellingController:
    """Borrow the reader's executor, source guard, undo history and cancellation."""

    def __init__(self, host, *, service=None, report=None, changed=lambda: None):
        self.host = host
        self.service = service or WindowsSpelling()
        self.languages = ()
        self.result = None
        self.snapshot = None
        self.scope = None
        self.session = ReaderToolSession(host, report=report or host.report, changed=changed)

    def invalidate(self):
        self.result = self.snapshot = self.scope = None
        self.session.invalidate()

    def load_languages(self):
        self.invalidate()

        def finished(snapshot, languages):
            self.languages = languages
            self.session.report('Choose an installed language explicitly, then check the passage.'
                if languages else 'No installed Windows spelling dictionaries are available.')
            self.session.changed()

        return self.session.start(
            lambda snapshot, cancelled: self.service.languages(cancelled=cancelled), finished)

    def check(self, language):
        if self.session.closed or not self.host.alive() or self.host.busy():
            raise ValueError('Wait for the current reader operation before checking spelling.')
        _language(language)
        if language not in self.languages:
            raise ValueError('Choose an exact language from the installed dictionary list.')
        selection = self.host.selection()
        if (type(selection) is not tuple or len(selection) != 2
                or any(type(value) is not int for value in selection)
                or not 0 <= selection[0] <= selection[1]):
            raise ValueError('Select a valid passage in the reading copy.')
        self.invalidate()

        def work(snapshot, cancelled):
            start, end = selection if selection[0] != selection[1] else (0, len(snapshot.text))
            if not 0 <= start < end <= len(snapshot.text) or end - start > MAX_TEXT:
                raise ValueError(f'Select between 1 and {MAX_TEXT:,} characters for spelling review.')
            return (start, end), self.service.check(snapshot.text[start:end], language,
                                                    cancelled=cancelled)

        def finished(snapshot, value):
            from hashlib import sha256
            scope, result = value
            if (not isinstance(result, SpellingPreview) or result.language != language
                    or result.text_sha256 != sha256(
                        snapshot.text[scope[0]:scope[1]].encode('utf-8')).hexdigest()):
                raise ValueError('The spelling preview does not match the captured passage.')
            self.scope, self.snapshot, self.result = scope, snapshot, result
            self.session.report(f'{len(result.issues)} spelling issue(s) in the reviewed passage. '
                                'Select an issue and preview one suggested correction.'
                                if result.issues else 'No spelling issues were reported for this passage.')
            self.session.changed()

        return self.session.start(work, finished)

    def preview(self, index, replacement):
        if (self.session.closed or self.session.busy or self.host.busy()
                or self.result is None or self.snapshot is None
                or type(index) is not int or not 0 <= index < len(self.result.issues)):
            raise ValueError('Check the current passage before previewing a correction.')
        if self.session.cancelled.is_set() or self.host.cancelled():
            raise InterruptedError('Spelling review cancelled. Check the passage again.')
        self.host.verify(self.snapshot)
        issue = self.result.issues[index]
        if type(replacement) is not str or replacement not in issue.replacements:
            raise ValueError('Choose one of the suggestions returned by the selected dictionary.')
        start, end = self.scope[0] + issue.start, self.scope[0] + issue.end
        if self.snapshot.text[start:end] != issue.word:
            raise ValueError('The spelling issue no longer matches the captured reading text.')
        self.host.select(start, end)
        self.session.preview = ReadingPreview(self.snapshot, start, end, replacement,
            f'Spelling correction ({self.result.language})', 1, 'spelling correction')
        self.session.changed()
        return self.session.preview

    def apply(self):
        if not self.host.alive() or self.host.busy():
            raise ValueError('The reading copy is unavailable or busy.')
        changed = self.session.accept()
        self.invalidate()
        self.session.report('Correction applied to the reading copy. Undo is available in the reader. '
                            'Check again before another correction.')
        return changed

    def cancel(self):
        self.invalidate()
        self.session.cancel()

    def close(self):
        self.result = self.snapshot = self.scope = None
        self.session.close()


class ReaderSpellingDialog:
    def __init__(self, parent, host, *, service=None):
        import wx
        self.wx, self.host = wx, host
        self.dialog = wx.Dialog(parent, title='Review spelling in reading copy',
            size=(840, 700), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.closed = self.destroyed = False
        layout = wx.BoxSizer(wx.VERTICAL)
        info = wx.TextCtrl(self.dialog, value=(
            'Uses only installed Windows spelling dictionaries. Choose a language explicitly. '
            'A selected passage is checked; with no selection the current reading copy is checked '
            f'(up to {MAX_TEXT:,} characters). Preview each correction before applying. '
            'Original text, source quotations and dictionaries remain unchanged.'),
            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
        info.SetName('Spelling scope, installed dictionaries and original-source boundary')
        layout.Add(info, 0, wx.EXPAND | wx.ALL, 6)
        language_row = wx.BoxSizer(wx.HORIZONTAL)
        language_row.Add(wx.StaticText(self.dialog, label='Installed spelling &language'),
                         0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        self.language = wx.Choice(self.dialog)
        self.language.SetName('Explicit installed spelling language; no fallback')
        language_row.Add(self.language, 1, wx.EXPAND | wx.ALL, 4)
        self.reload = wx.Button(self.dialog, label='&Reload languages')
        self.reload.SetName('Reload installed Windows spelling languages')
        language_row.Add(self.reload, 0, wx.ALL, 4)
        self.check = wx.Button(self.dialog, label='&Check passage')
        self.check.SetName('Check selected passage or current reading copy')
        language_row.Add(self.check, 0, wx.ALL, 4)
        layout.Add(language_row, 0, wx.EXPAND)
        self.issues = wx.ListBox(self.dialog)
        self.issues.SetName('Spelling issues and exact reading offsets')
        layout.Add(self.issues, 1, wx.EXPAND | wx.ALL, 6)
        self.suggestions = wx.Choice(self.dialog)
        self.suggestions.SetName('Suggestions from the explicitly selected spelling dictionary')
        layout.Add(self.suggestions, 0, wx.EXPAND | wx.ALL, 6)
        self.preview_text = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY,
                                        size=(-1, 150))
        self.preview_text.SetName('Spelling correction preview; original and proposed reading fragment')
        layout.Add(self.preview_text, 1, wx.EXPAND | wx.ALL, 6)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.preview_button = wx.Button(self.dialog, label='&Preview correction')
        self.preview_button.SetName('Preview selected spelling correction without applying')
        self.apply_button = wx.Button(self.dialog, label='&Apply reviewed correction')
        self.apply_button.SetName('Apply previewed spelling correction to reading copy')
        self.cancel_button = wx.Button(self.dialog, label='Cancel &check')
        self.cancel_button.SetName('Cancel spelling check and discard unaccepted preview')
        self.close_button = wx.Button(self.dialog, wx.ID_CLOSE, label='&Close')
        for button in (self.preview_button, self.apply_button, self.cancel_button, self.close_button):
            buttons.Add(button, 0, wx.ALL, 4)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 2)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        self.status.SetName('Spelling review status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
        self.dialog.SetSizer(layout)
        self.controller = ReaderSpellingController(host, service=service,
            report=self.report, changed=self.refresh)
        self.shown_result = None
        self.shown_languages = ()
        self.preview_button.Bind(wx.EVT_BUTTON, lambda event: self._attempt(self.preview))
        self.apply_button.Bind(wx.EVT_BUTTON, lambda event: self._attempt(self.apply))
        self.check.Bind(wx.EVT_BUTTON, lambda event: self._attempt(self.start_check))
        self.reload.Bind(wx.EVT_BUTTON, lambda event: self._attempt(self.controller.load_languages))
        self.cancel_button.Bind(wx.EVT_BUTTON, lambda event: self.controller.cancel())
        self.close_button.Bind(wx.EVT_BUTTON, lambda event: self.dialog.Close())
        self.language.Bind(wx.EVT_CHOICE, lambda event: self.controller.invalidate())
        self.issues.Bind(wx.EVT_LISTBOX, self.issue_selected)
        self.suggestions.Bind(wx.EVT_CHOICE, self.suggestion_selected)
        self.dialog.Bind(wx.EVT_CLOSE, self.on_close)
        owner_id = self.dialog.GetId()

        def destroyed(event):
            if event.GetId() == owner_id:
                self.closed = self.destroyed = True
                self.controller.close()
            event.Skip()

        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        self.refresh()

    def report(self, message):
        if not self.closed and self.host.alive():
            self.status.SetValue(message)

    def _attempt(self, operation):
        if self.closed or not self.host.alive():
            return
        try:
            operation()
        except Exception as exc:
            self.report(str(exc))
        self.refresh()

    def refresh(self):
        if self.closed or not self.host.alive():
            return
        controller, wx = self.controller, self.wx
        busy = controller.session.busy or self.host.busy()
        if controller.languages != self.shown_languages:
            self.shown_languages = controller.languages
            self.language.SetItems(list(controller.languages))
            self.language.SetSelection(wx.NOT_FOUND)
        if controller.result is not self.shown_result:
            self.shown_result = controller.result
            rows = [] if controller.result is None else [
                f'{controller.scope[0] + issue.start}–{controller.scope[0] + issue.end}: {issue.word}'
                for issue in controller.result.issues]
            self.issues.Set(rows)
            self.suggestions.SetItems([])
            self.suggestions.SetSelection(wx.NOT_FOUND)
            self.preview_text.SetValue('')
        self.language.Enable(not busy)
        self.reload.Enable(not busy)
        self.check.Enable(not busy and self.language.GetSelection() != wx.NOT_FOUND)
        self.issues.Enable(not busy)
        self.suggestions.Enable(not busy)
        self.preview_button.Enable(not busy and self.suggestions.GetSelection() != wx.NOT_FOUND)
        self.apply_button.Enable(not busy and controller.session.preview is not None)
        self.cancel_button.Enable(busy or controller.result is not None)

    def issue_selected(self, event):
        if self.closed or not self.host.alive():
            return
        self.controller.session.preview = None
        self.preview_text.SetValue('')
        index = self.issues.GetSelection()
        result = self.controller.result
        if result is not None and 0 <= index < len(result.issues):
            issue = result.issues[index]
            self.suggestions.SetItems([text if text else '(Remove this fragment)'
                                       for text in issue.replacements])
            self.suggestions.SetSelection(self.wx.NOT_FOUND)
            self.report('Only the first 12 suggestions are shown.' if issue.suggestions_limited else
                        'Choose a suggestion and preview it before applying.')
        self.refresh()

    def suggestion_selected(self, event):
        if self.closed or not self.host.alive():
            return
        self.controller.session.preview = None
        self.preview_text.SetValue('')
        self.refresh()

    def start_check(self):
        self.controller.check(self.language.GetStringSelection())

    def preview(self):
        index, suggestion = self.issues.GetSelection(), self.suggestions.GetSelection()
        result = self.controller.result
        if (result is None or not 0 <= index < len(result.issues)
                or not 0 <= suggestion < len(result.issues[index].replacements)):
            raise ValueError('Select a spelling issue and one proposed correction.')
        preview = self.controller.preview(index, result.issues[index].replacements[suggestion])
        self.preview_text.SetValue(preview.description())

    def apply(self):
        self.controller.apply()
        self.language.SetFocus()

    def on_close(self, event):
        self.closed = True
        self.controller.close()
        event.Skip()

    def show(self):
        self.dialog.Show()
        self.language.SetFocus()
        self._attempt(self.controller.load_languages)
        return self


def show_reader_spelling(parent, *, host):
    """One native child, borrowing the current reader's job and edit contracts."""
    if not host.alive() or host.busy():
        host.report('Wait for the current reader operation or cancel it first.')
        return None
    host.guard()
    review = ReaderSpellingDialog(parent, host)
    # Match the reader's other edit tools: initial work runs inside the modal
    # event loop, and focus returns to the reader only after this review closes.
    review.wx.CallAfter(review._attempt, review.controller.load_languages)
    try:
        review.language.SetFocus()
        review.dialog.ShowModal()
    finally:
        review.closed = True
        review.controller.close()
        if not review.destroyed:
            review.dialog.Destroy()
    return review
