"""Review explicit local dictionary suggestions against the existing reading snapshot."""
from dataclasses import dataclass
from hashlib import sha256

from ..speech._io import text_value
from ..speech.reading import ReadingDocument
from ..speech.spellcheck import SpellingReview, review_spelling, verify_dictionary
from .pronunciation_dialog import ReadingSnapshot


@dataclass(frozen=True)
class SpellPreview:
    snapshot: ReadingSnapshot
    review: SpellingReview
    bounds: tuple[int, int]
    issue_index: int
    replacement: str

    @property
    def start(self):
        return self.bounds[0] + self.review.issues[self.issue_index].start

    @property
    def end(self):
        return self.bounds[0] + self.review.issues[self.issue_index].end


def preview_spelling(snapshot, review, bounds, issue_index, replacement):
    if (not isinstance(snapshot, ReadingSnapshot) or not isinstance(review, SpellingReview)
            or type(bounds) is not tuple or len(bounds) != 2
            or any(type(n) is not int for n in bounds)
            or not 0 <= bounds[0] < bounds[1] <= len(snapshot.text)
            or sha256(snapshot.text[bounds[0]:bounds[1]].encode('utf-8')).hexdigest() != review.text_sha256
            or type(issue_index) is not int or not 0 <= issue_index < len(review.issues)):
        raise ValueError('Review the current reading scope before choosing a correction.')
    text_value(replacement, 'Reviewed spelling replacement', maximum=128)
    issue = review.issues[issue_index]
    if (replacement not in issue.suggestions or replacement == issue.word
            or snapshot.text[bounds[0] + issue.start:bounds[0] + issue.end] != issue.word):
        raise ValueError('Choose a suggestion from this exact spelling review.')
    return SpellPreview(snapshot, review, bounds, issue_index, replacement)


def apply_spelling_preview(document, preview, current):
    if (not isinstance(document, ReadingDocument) or not isinstance(preview, SpellPreview)
            or current != preview.snapshot or id(document) != current.identity
            or document._signature() != current.signature or document.text != current.text):
        raise ValueError('The reading copy or selection changed. Review spelling again.')
    # Revalidate the exact preview even when called outside the native dialog.
    preview_spelling(current, preview.review, preview.bounds, preview.issue_index, preview.replacement)
    return document.replace(preview.start, preview.end, preview.replacement,
                            label='Reviewed spelling correction')


def create_spellcheck_dialog(parent, *, library_root, capture, apply, undo, guard,
                             run, cancel, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class SpellcheckDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Review spelling in reading copy', size=(850, 740),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id, self._destroy_requested = self.GetId(), False
            self.alive, self.busy = True, False
            self.review = self.snapshot = self.bounds = self.preview = self.applied = None
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            guard()
            self.initial = capture()
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label='Choose your local Hunspell .aff/.dic pair. '
                'No dictionary is downloaded or selected automatically. Suggestions depend on that dictionary '
                'and the reviewed Spylls backend; this is not a grammar check. '
                'Preview and apply one correction at a time; each change is undoable.')
            notice.Wrap(780); box.Add(notice, 0, wx.ALL | wx.EXPAND, 5)

            def field(label, factory=wx.TextCtrl, **kwargs):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                control = factory(panel, **kwargs); control.SetName(label.replace('&', ''))
                box.Add(control, 0, wx.ALL | wx.EXPAND, 3)
                return control

            self.dictionary = field('Selected &dictionary file')
            self.scope = field('Review &scope', wx.Choice, choices=['Whole reading copy', 'Original selected text'])
            self.scope.SetSelection(int(self.initial.selection[0] != self.initial.selection[1]))
            self.issues = field('Words to &review', wx.Choice, choices=[])
            self.suggestions = field('Suggested &replacement', wx.Choice, choices=[])
            self.before = field('Current word', style=wx.TE_READONLY)
            self.after = field('Proposed correction', style=wx.TE_READONLY)
            self.status = field('Spelling review status', style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 140))
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.EXPAND)
            self.buttons = {}
            for row in ((('choose', 'Choose dictionary…', self.on_choose),
                         ('review', '&Review spelling', self.on_review)),
                        (('preview', '&Preview correction', self.on_preview),
                         ('apply', '&Apply reviewed correction', self.on_apply),
                         ('undo', '&Undo last correction', self.on_undo)),
                        (('cancel', 'Cancel review', self.on_cancel), ('close', 'Close', self.on_close))):
                line = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in row:
                    button = wx.Button(self, label=label); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler)
                    self.buttons[key] = button; line.Add(button, 0, wx.ALL, 3)
                layout.Add(line, 0, wx.EXPAND)
            self.SetSizer(layout); panel.FitInside()
            self.dictionary.Bind(wx.EVT_TEXT, self.on_change)
            self.scope.Bind(wx.EVT_CHOICE, self.on_change)
            self.issues.Bind(wx.EVT_CHOICE, self.on_issue)
            self.suggestions.Bind(wx.EVT_CHOICE, self.on_suggestion)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.report('Choose a same-name .aff/.dic pair that you are permitted to use. '
                'Review at most 200,000 characters and 3,000 words per operation. '
                'A partial result is identified explicitly.')
            self.controls(); self.dictionary.SetFocus()

        def report(self, value):
            if self.alive:
                self.status.ChangeValue(str(value))

        def controls(self):
            if not self.alive:
                return
            for control in (self.dictionary, self.scope, self.issues, self.suggestions):
                control.Enable(not self.busy)
            for key, button in self.buttons.items():
                button.Enable(not self.busy if key not in ('close', 'cancel') else key == 'close' or self.busy)
            self.buttons['apply'].Enable(not self.busy and self.preview is not None)
            self.buttons['undo'].Enable(not self.busy and self.applied is not None)

        def form(self):
            return self.dictionary.GetValue(), self.scope.GetSelection()

        def stopping(self):
            return not self.alive or cancel.is_set()

        def invalidate(self):
            self.review = self.preview = self.snapshot = self.bounds = None
            if self.alive:
                self.issues.Clear(); self.suggestions.Clear()
                self.before.ChangeValue(''); self.after.ChangeValue('')

        def on_change(self, event=None):
            if self.alive:
                self.invalidate(); self.controls()

        def on_choose(self, event=None):
            if self.busy or not self.alive:
                return
            chooser = None
            try:
                guard()
                chooser = wx.FileDialog(self, 'Choose Hunspell dictionary',
                    wildcard='Hunspell dictionary (*.aff;*.dic)|*.aff;*.dic',
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                result = chooser.ShowModal()
                if not self.alive or not chooser or result != wx.ID_OK:
                    return
                path = chooser.GetPath()
                guard()
                if self.alive:
                    self.dictionary.SetValue(path)
            except Exception as exc:
                self.report(exc)
            finally:
                if chooser:
                    chooser.Destroy()

        def begin(self, work, done):
            if self.busy or not self.alive:
                return False
            self.busy = True; self.controls()
            def completed(value):
                self.busy = False
                if not self.alive:
                    return
                try:
                    if self.stopping():
                        raise InterruptedError('Spelling review cancelled.')
                    guard(); done(value)
                except Exception as exc:
                    self.report(exc)
                self.controls()
            def failed(error):
                self.busy = False
                if self.alive:
                    self.report(error); self.controls()
            try:
                guard()
                if run(work, completed, on_failed=failed) is False:
                    failed('Finish or cancel the current reader operation first.')
                    return False
            except Exception as exc:
                failed(exc); return False
            return True

        def on_review(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard(); snapshot, form = capture(), self.form()
                if form[1] not in (0, 1):
                    raise ValueError('Choose the reading scope.')
                bounds = (0, len(snapshot.text))
                if form[1]:
                    if (snapshot.identity != self.initial.identity or snapshot.signature != self.initial.signature
                            or snapshot.text != self.initial.text):
                        raise ValueError('The selected text changed. Close and select the passage again.')
                    bounds = self.initial.selection
                if bounds[0] == bounds[1]:
                    raise ValueError('Choose a nonempty reading passage.')
                self.invalidate()
                def done(review):
                    if capture() != snapshot or self.form() != form:
                        raise ValueError('The text, selection or dictionary choice changed. Review again.')
                    self.review, self.snapshot, self.bounds = review, snapshot, bounds
                    self.issues.SetItems([f'{i + 1}. {item.word} — character {bounds[0] + item.start + 1}'
                                          for i, item in enumerate(review.issues)])
                    if review.issues:
                        self.issues.SetSelection(0); self.on_issue()
                    status = (f'Checked {review.checked_tokens} of {review.total_tokens} words; '
                              f'{len(review.issues)} words need review. ')
                    status += ('The chosen scope was completely checked. ' if review.complete else
                               'Partial result: the issue limit was reached. Review a smaller passage for the remainder. ')
                    status += ('Dictionary: ' + review.dictionary.aff_path.name + '. '
                               + ('Selected text. ' if form[1] else 'Whole reading copy. '))
                    self.report(status + review.notice)
                self.begin(lambda: review_spelling(snapshot.text[bounds[0]:bounds[1]], form[0],
                    library_root=library_root, cancelled=self.stopping), done)
            except Exception as exc:
                self.report(exc); self.controls()

        def on_issue(self, event=None):
            if not self.alive or self.busy:
                return
            self.preview = None
            index = self.issues.GetSelection()
            values = ()
            if self.review is not None and 0 <= index < len(self.review.issues):
                values = self.review.issues[index].suggestions
            self.suggestions.SetItems(list(values))
            if values:
                self.suggestions.SetSelection(0)
            self.before.ChangeValue(''); self.after.ChangeValue(''); self.controls()

        def on_suggestion(self, event=None):
            if self.alive and not self.busy:
                self.preview = None; self.after.ChangeValue(''); self.controls()

        def on_preview(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard()
                if self.review is None or capture() != self.snapshot:
                    raise ValueError('Review the current reading text before choosing a correction.')
                index, suggestion = self.issues.GetSelection(), self.suggestions.GetSelection()
                if not 0 <= index < len(self.review.issues) or not 0 <= suggestion < len(self.review.issues[index].suggestions):
                    raise ValueError('Choose a word and one of its suggested corrections.')
                self.preview = preview_spelling(self.snapshot, self.review, self.bounds, index,
                                                 self.review.issues[index].suggestions[suggestion])
                self.before.ChangeValue(self.review.issues[index].word)
                self.after.ChangeValue(self.preview.replacement)
                self.report('Review this one correction, then apply explicitly.')
            except Exception as exc:
                self.report(exc)
            self.controls()

        def on_apply(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard()
                preview, form = self.preview, self.form()
                if preview is None or capture() != preview.snapshot:
                    raise ValueError('Preview the current spelling correction first.')
                def done(_):
                    if self.preview is not preview or capture() != preview.snapshot or self.form() != form:
                        raise ValueError('The spelling preview changed. Review it again.')
                    self.applied, warning = apply(preview)
                    if not self.alive:
                        return
                    self.initial = self.applied
                    self.scope.SetSelection(0); self.invalidate()
                    self.report('One spelling correction applied to the reading copy. ' + warning)
                self.begin(lambda: verify_dictionary(preview.review.dictionary, cancelled=self.stopping), done)
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
                if not self.alive:
                    return
                self.applied = None; self.initial = capture()
                self.scope.SetSelection(0); self.invalidate()
                self.report('Spelling correction undone. ' + warning)
            except Exception as exc:
                self.report(exc)
            self.controls()

        def on_cancel(self, event=None):
            if self.alive and self.busy:
                cancel.set(); self.report('Cancelling spelling review. No correction is applied by cancellation.')

        def dispose(self):
            self.alive = False
            if getattr(self, 'busy', False):
                cancel.set()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True; self.dispose()
            event.Skip()

        def Destroy(self):
            self.dispose()
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

        def finish_close(self):
            self.dispose()
            if self:
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                else:
                    self.Hide()

        def on_close(self, event=None):
            if self.alive:
                self.finish_close()

    return SpellcheckDialog()
