"""Review explicit Hunspell results as reversible reading-copy edits."""
from dataclasses import dataclass, replace
from hashlib import sha256

from ..speech._io import check_cancelled, text_value
from ..speech.reading import ReadingDocument
from ..speech.spelling import SpellingResult, check_spelling, review_hunspell
from .pronunciation_dialog import ReadingSnapshot


@dataclass(frozen=True)
class SpellingReview:
    snapshot: ReadingSnapshot
    result: SpellingResult


@dataclass(frozen=True)
class SpellingCorrection:
    review: SpellingReview
    index: int
    replacement: str

    @property
    def issue(self):
        return self.review.result.issues[self.index]


def _same_text(first, second):
    return (first.identity, first.signature, first.text) == (second.identity, second.signature, second.text)


def preview_correction(review, index, replacement, current):
    if (not isinstance(review, SpellingReview) or not isinstance(review.result, SpellingResult)
            or not isinstance(current, ReadingSnapshot) or review.snapshot != current
            or review.result.text_sha256 != sha256(current.text.encode('utf-8')).hexdigest()
            or type(index) is not int or not 0 <= index < len(review.result.issues)):
        raise ValueError('Check the current reading scope and select a spelling issue first.')
    text_value(replacement, 'Reviewed spelling replacement', maximum=4096, empty=True)
    issue = review.result.issues[index]
    if (not review.result.bounds[0] <= issue.start < issue.end <= review.result.bounds[1]
            or current.text[issue.start:issue.end] != issue.word
            or current.selection != (issue.start, issue.end)):
        raise ValueError('Select the exact current spelling issue before previewing its correction.')
    if replacement == issue.word:
        raise ValueError('The proposed spelling correction makes no change.')
    text_value(current.text[:issue.start] + replacement + current.text[issue.end:],
               'Corrected reading copy', empty=True)
    return SpellingCorrection(review, index, replacement)


def apply_correction(document, preview, current):
    if (not isinstance(document, ReadingDocument) or not isinstance(preview, SpellingCorrection)
            or not isinstance(current, ReadingSnapshot) or preview.review.snapshot != current
            or id(document) != current.identity or document._signature() != current.signature
            or document.text != current.text):
        raise ValueError('The reading copy or selected issue changed. Review the correction again.')
    checked = preview_correction(preview.review, preview.index, preview.replacement, current)
    return document.replace(checked.issue.start, checked.issue.end, checked.replacement,
                            label='Reviewed Hunspell spelling correction')


def create_spelling_dialog(parent, *, capture, select, apply, undo, guard, run, cancel, wx_module=None):
    """Use the reader's existing worker, source authority and cancellation event."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class SpellingDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Spelling review — Hunspell', size=(930, 850),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            guard()
            self.alive, self.busy = True, False
            self.profile = self.review = self.preview = self.applied = None
            self.preview_form = None
            self.scope_snapshot = capture()
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label='Select a trusted local Hunspell library and a matching licensed .aff/.dic pair. '
                'Review their identities before checking. Corrections are optional edits to this reading copy. '
                'This profile lasts only while this window is open.')
            notice.Wrap(850); box.Add(notice, 0, wx.ALL | wx.EXPAND, 4)
            def field(label, factory=wx.TextCtrl, **kwargs):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                widget = factory(panel, **kwargs); widget.SetName(label.replace('&', ''))
                box.Add(widget, 0, wx.ALL | wx.EXPAND, 3)
                return widget
            self.library = field('Hunspell native &library path')
            self.affix = field('Dictionary &affix file (.aff)')
            self.dictionary = field('Dictionary &word file (.dic)')
            self.language = field('Dictionary &language', value=self.scope_snapshot.language)
            self.provenance = field('Engine and dictionary origin / license notice')
            self.identity = field('Reviewed engine and dictionary identities',
                                  style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80))
            self.scope = field('Spelling &scope', wx.Choice, choices=['Whole reading copy', 'Original selected text'])
            self.scope.SetSelection(int(self.scope_snapshot.selection[0] != self.scope_snapshot.selection[1]))
            self.issues = field('Spelling &issues', wx.ListBox, choices=[])
            self.suggestions = field('Dictionary &suggestions', wx.ListBox, choices=[])
            self.replacement = field('Reviewed replacement; empty removes the selected word')
            self.preview_text = field('Spelling correction preview', style=wx.TE_MULTILINE | wx.TE_READONLY,
                                      size=(-1, 70))
            self.status = field('Spelling review status', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80))
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.EXPAND)
            self.buttons = {}
            rows = ((('library', 'Choose library...', lambda event: self.choose('library')),
                     ('affix', 'Choose affix...', lambda event: self.choose('affix')),
                     ('dictionary', 'Choose dictionary...', lambda event: self.choose('dictionary'))),
                    (('review', 'Review selected &profile', self.on_profile),
                     ('check', '&Check spelling', self.on_check),
                     ('preview', 'Preview &correction', self.on_preview)),
                    (('apply', '&Apply reviewed correction', self.on_apply), ('undo', '&Undo last apply', self.on_undo),
                     ('cancel', 'Cancel check', self.on_cancel), ('close', 'Close', self.on_close)))
            for entries in rows:
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in entries:
                    button = wx.Button(self, label=label); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 3)
                    self.buttons[key] = button
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout); panel.FitInside()
            for widget in (self.library, self.affix, self.dictionary, self.language, self.provenance):
                widget.Bind(wx.EVT_TEXT, self.on_profile_change)
            self.scope.Bind(wx.EVT_CHOICE, self.on_scope_change)
            self.issues.Bind(wx.EVT_LISTBOX, self.on_issue)
            self.suggestions.Bind(wx.EVT_LISTBOX, self.on_suggestion)
            self.replacement.Bind(wx.EVT_TEXT, self.on_replacement)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.report('No dictionary is selected and no native library has been loaded. '
                        'Use a compatible standalone library; companion DLLs are not discovered or downloaded.')
            self.controls(); self.library.SetFocus()

        def report(self, message):
            if self.alive:
                self.status.ChangeValue(str(message))

        def controls(self):
            if not self.alive:
                return
            for widget in (self.library, self.affix, self.dictionary, self.language, self.provenance,
                           self.scope, self.issues, self.suggestions, self.replacement):
                widget.Enable(not self.busy)
            for key, button in self.buttons.items():
                button.Enable(not self.busy if key not in ('cancel', 'close') else key == 'close' or self.busy)
            self.buttons['check'].Enable(not self.busy and self.profile is not None)
            self.buttons['preview'].Enable(not self.busy and self.review is not None
                                            and 0 <= self.issues.GetSelection() < len(self.review.result.issues))
            self.buttons['apply'].Enable(not self.busy and self.preview is not None)
            self.buttons['undo'].Enable(not self.busy and self.applied is not None)

        def profile_form(self):
            return tuple(widget.GetValue() for widget in
                         (self.library, self.affix, self.dictionary, self.language, self.provenance))

        def correction_form(self):
            return self.profile_form(), self.scope.GetSelection(), self.issues.GetSelection(), self.replacement.GetValue()

        def clear_review(self):
            self.review = self.preview = self.preview_form = None
            self.issues.Set([]); self.suggestions.Set([])
            self.preview_text.ChangeValue(''); self.controls()

        def on_profile_change(self, event=None):
            self.profile = None; self.identity.ChangeValue('')
            self.clear_review()

        def on_scope_change(self, event=None):
            self.clear_review()

        def on_replacement(self, event=None):
            self.preview = self.preview_form = None
            self.preview_text.ChangeValue(''); self.controls()

        def stopping(self):
            return cancel.is_set() or not self.alive

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

        def choose(self, role):
            if self.busy or not self.alive:
                return
            try:
                guard()
                wildcard = {'library': 'Native libraries (*.dll;*.so;*.dylib)|*.dll;*.so;*.dylib|All files|*.*',
                            'affix': 'Hunspell affix (*.aff)|*.aff', 'dictionary': 'Hunspell dictionary (*.dic)|*.dic'}[role]
                picker = wx.FileDialog(self, 'Choose local Hunspell ' + role, wildcard=wildcard,
                                       style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                try:
                    if picker.ShowModal() == wx.ID_OK:
                        getattr(self, role).ChangeValue(picker.GetPath()); self.on_profile_change()
                finally:
                    picker.Destroy()
            except Exception as exc:
                self.report(exc)

        def on_profile(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard(); form = self.profile_form()
                self.profile = None; self.identity.ChangeValue(''); self.clear_review()
                def done(profile):
                    if self.profile_form() != form:
                        raise ValueError('The selected files or provenance changed. Review the profile again.')
                    self.profile = profile
                    rows = [f'{profile.language}; {profile.provenance}', 'Profile SHA256: ' + profile.identity]
                    for role in ('library', 'affix', 'dictionary'):
                        item = getattr(profile, role)
                        rows.append(f'{role}: {item.size} bytes; SHA256 {item.sha256}')
                    self.identity.ChangeValue('\n'.join(rows))
                    self.report('File identities reviewed. Check spelling explicitly to load this native library '
                                'in a cancellable child process. License text is your supplied provenance, not an automatic rights approval.')
                self.begin(lambda: review_hunspell(*form[:3], language=form[3], provenance=form[4],
                                                   cancelled=self.stopping), done)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_check(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard(); snapshot = capture(); profile = self.profile
                form, scope = self.profile_form(), self.scope.GetSelection()
                if profile is None or scope not in (0, 1):
                    raise ValueError('Review a profile and choose a spelling scope first.')
                bounds = (0, len(snapshot.text))
                if scope:
                    if not _same_text(snapshot, self.scope_snapshot):
                        raise ValueError('The selected-text scope changed. Close and select text again.')
                    bounds = self.scope_snapshot.selection
                    if bounds[0] == bounds[1]:
                        raise ValueError('Select nonempty reading text before opening a selected-text review.')
                self.clear_review()
                def done(result):
                    if (capture() != snapshot or self.profile is not profile or self.profile_form() != form
                            or self.scope.GetSelection() != scope):
                        raise ValueError('The reading scope or dictionary selection changed. Check again.')
                    self.review = SpellingReview(snapshot, result)
                    self.issues.Set([f'{index + 1}. {issue.word[:80]} — {issue.reason}; characters {issue.start} to {issue.end}'
                                     for index, issue in enumerate(result.issues)])
                    self.report(f'Checked {result.words} letter-based words with {profile.language} / {result.encoding}. '
                                f'{len(result.issues)} issues for review. Select an issue; suggestions never apply automatically. '
                                'Names, unsupported encodings and language-specific word boundaries require your judgment.')
                self.begin(lambda: check_spelling(snapshot.text, profile, bounds=bounds, cancelled=self.stopping), done)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_issue(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard(); current = capture(); index = self.issues.GetSelection()
                if self.review is None or not _same_text(current, self.review.snapshot):
                    raise ValueError('The reading copy changed. Check spelling again.')
                if not 0 <= index < len(self.review.result.issues):
                    raise ValueError('Select a spelling issue.')
                issue = self.review.result.issues[index]
                select(current, (issue.start, issue.end))
                self.review = replace(self.review, snapshot=capture())
                self.suggestions.Set(list(issue.suggestions))
                self.replacement.ChangeValue(issue.word)
                self.on_replacement()
                self.report(f'Selected {issue.word[:128]}. {len(issue.suggestions)} suggestions. '
                            'Choose a suggestion or enter a correction, then preview it.')
            except Exception as exc:
                self.preview = None; self.report(exc); self.controls()

        def on_suggestion(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                index, choice = self.issues.GetSelection(), self.suggestions.GetSelection()
                if self.review is None or not 0 <= index < len(self.review.result.issues):
                    raise ValueError('Select a current spelling issue.')
                suggestions = self.review.result.issues[index].suggestions
                if not 0 <= choice < len(suggestions):
                    raise ValueError('Select a dictionary suggestion.')
                self.replacement.ChangeValue(suggestions[choice]); self.on_replacement()
            except Exception as exc:
                self.report(exc)

        def on_preview(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard(); self.preview = None
                self.preview = preview_correction(self.review, self.issues.GetSelection(),
                                                    self.replacement.GetValue(), capture())
                self.preview_form = self.correction_form()
                self.preview_text.ChangeValue('Before: ' + self.preview.issue.word + '\nAfter: ' + self.preview.replacement)
                self.report('Correction previewed. Apply creates one undoable reading edit.')
            except Exception as exc:
                self.report(exc)
            self.controls()

        def on_apply(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard()
                if self.preview is None or self.preview_form != self.correction_form():
                    raise ValueError('Preview the current correction before applying.')
                self.applied, warning = apply(self.preview)
                self.scope_snapshot = self.applied; self.scope.SetSelection(0)
                self.clear_review()
                self.report('Spelling correction applied as one reading edit. Check again for the updated text. ' + warning)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_undo(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard()
                if self.applied is None:
                    raise ValueError('No spelling correction is available to undo here.')
                warning = undo(self.applied)
                self.applied = None; self.scope_snapshot = capture(); self.scope.SetSelection(0)
                self.clear_review(); self.report('Spelling correction undone. ' + warning)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_cancel(self, event=None):
            if self.busy:
                cancel.set(); self.report('Cancelling the spelling operation. No correction is applied by cancellation.')

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

    return SpellingDialog()
