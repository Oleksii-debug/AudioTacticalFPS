"""Modal preview-only language preparation on the existing reader worker."""
from ..speech.preparation import LANGUAGES, PreparationOptions, preview_preparation
from .reading_tools import ReaderToolSession


class ReaderPreparationDialog:
    def __init__(self, parent, host):
        import wx
        self.wx, self.host = wx, host
        self.open_snapshot = host.capture()
        self.scope = host.selection()
        self.destroyed = False
        self.dialog = wx.Dialog(parent, title='Prepare reading copy for speech',
            size=(880, 740), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.reader_preparation = self
        self._window_id = self.dialog.GetId()
        self.session = ReaderToolSession(host, report=self.report, changed=self.sync)
        layout = wx.BoxSizer(wx.VERTICAL)
        notice = wx.TextCtrl(self.dialog, value=(
            'Choose a language and only the transformations you want. Preview changes before Apply. '
            'Numbers use explicit digit names; ISO dates use year, month and day labels. '
            'Stress, natural grammar, punctuation restoration and voice switching are not inferred. '
            'Only the reading copy may change; the original source and quotation status remain unchanged.'),
            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
        notice.SetName('Reading preparation scope and supported behavior')
        layout.Add(notice, 0, wx.EXPAND | wx.ALL, 6)
        layout.Add(wx.StaticText(self.dialog, label='Explicit preparation language'), 0,
                   wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.language = wx.Choice(self.dialog,
            choices=['Choose a language'] + [label + ' (' + code + ')' for code, label in LANGUAGES])
        self.language.SetName('Reading preparation language; explicit choice required')
        self.language.SetSelection(0)
        self.language.Bind(wx.EVT_CHOICE, self.changed)
        layout.Add(self.language, 0, wx.EXPAND | wx.ALL, 6)
        choices = wx.WrapSizer(wx.HORIZONTAL)
        self.options = {}
        for name, label in (
                ('numbers', 'Read bare integers digit by digit'),
                ('iso_dates', 'Read ISO dates as year month day fields'),
                ('symbols', 'Read supported symbols by name'),
                ('normalize_spaces', 'Normalize reading whitespace'),
                ('join_hyphenated', 'Join hyphenated line breaks'),
                ('remove_soft_hyphens', 'Remove soft hyphens from reading copy')):
            field = wx.CheckBox(self.dialog, label=label)
            field.SetName(label)
            field.SetValue(False)
            field.Bind(wx.EVT_CHECKBOX, self.changed)
            self.options[name] = field
            choices.Add(field, 0, wx.ALL, 5)
        layout.Add(choices, 0, wx.EXPAND | wx.ALL, 3)
        self.selected_only = wx.CheckBox(self.dialog, label='Captured reading selection only')
        self.selected_only.SetName('Prepare only the captured reading selection')
        self.selected_only.SetValue(self.scope[0] < self.scope[1])
        self.selected_only.Bind(wx.EVT_CHECKBOX, self.changed)
        layout.Add(self.selected_only, 0, wx.ALL, 6)
        self.preview_text = wx.TextCtrl(self.dialog,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(-1, 270))
        self.preview_text.SetName('Current and proposed reading preparation')
        layout.Add(self.preview_text, 1, wx.EXPAND | wx.ALL, 6)
        self.status = wx.TextCtrl(self.dialog, value='No reading changes have been proposed.',
                                  style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 60))
        self.status.SetName('Reading preparation status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.preview_button = self._button(row, 'Preview speech preparation', self.preview)
        self.apply_button = self._button(row, 'Apply reading preparation', self.accept)
        self.reject_button = self._button(row, 'Reject preparation preview', self.reject)
        self.cancel_button = self._button(row, 'Cancel preparation work', self.cancel)
        self.close_button = self._button(row, 'Close speech preparation', self.close,
                                         identifier=wx.ID_CANCEL)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.sync()

    def _button(self, row, label, handler, *, identifier=None):
        wx = self.wx
        button = wx.Button(self.dialog, wx.ID_ANY if identifier is None else identifier, label=label)
        button.SetName(label)
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.ALL, 4)
        return button

    def _alive(self):
        return not self.destroyed and not self.session.closed and self.host.alive()

    def report(self, message):
        if self._alive():
            self.status.ChangeValue(message)
            self.status.SetFocus()

    def sync(self):
        if not self._alive() or not hasattr(self, 'preview_button'):
            return
        busy = self.session.busy
        for field in (self.language, self.selected_only, *self.options.values()):
            field.Enable(not busy)
        self.preview_button.Enable(not busy and 1 <= self.language.GetSelection() <= len(LANGUAGES))
        self.apply_button.Enable(not busy and self.session.preview is not None
                                 and self.session.preview.changed)
        self.reject_button.Enable(not busy and self.session.preview is not None)
        self.cancel_button.Enable(busy)

    def changed(self, event=None):
        if self._alive():
            self.session.invalidate()
            self.preview_text.ChangeValue('')
            self.report('Settings changed. Prepare a fresh preview before applying.')

    def preview(self, event=None):
        if not self._alive():
            return False
        try:
            self.host.verify(self.open_snapshot)
            selected = self.language.GetSelection()
            if not 1 <= selected <= len(LANGUAGES):
                raise ValueError('Choose the preparation language explicitly.')
            options = PreparationOptions(LANGUAGES[selected - 1][0],
                **{name: field.GetValue() for name, field in self.options.items()})
            scope = self.scope if self.selected_only.GetValue() else None
        except Exception as exc:
            self.report(str(exc))
            return False

        def prepared(snapshot, result):
            self.session.preview = result.preview
            self.preview_text.ChangeValue(result.description())
            self.report('Review the exact derivative, then Apply or Reject.'
                        if result.preview.changed else 'No reading text would change.')
            self.sync()
        return self.session.start(
            lambda snapshot, cancelled: preview_preparation(
                snapshot, options, scope=scope, cancelled=cancelled), prepared)

    def accept(self, event=None):
        if not self._alive():
            return
        try:
            self.host.verify(self.open_snapshot)
            preview = self.session.preview
            changed = self.session.accept()
            if not self._alive():
                return
            if changed:
                self.scope = preview.after_selection
                self.open_snapshot = self.host.capture()
                self.host.select(*self.scope)
                self.report('Applied only to the reading copy. One Undo restores this accepted preparation.')
            else:
                self.report('No reading text changed.')
            self.sync()
        except Exception as exc:
            self.report(str(exc))

    def reject(self, event=None):
        if self._alive():
            self.session.invalidate()
            self.preview_text.ChangeValue('')
            self.report('Preview rejected. Previously accepted edits and original source are retained.')

    def cancel(self, event=None):
        if self._alive():
            self.session.cancel()

    def _destroyed(self, event):
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
            self.language.SetFocus()
            self.dialog.ShowModal()
        finally:
            self.session.close()
            if not self.destroyed:
                self.dialog.Destroy()


def show_reader_preparation(parent, *, host):
    return ReaderPreparationDialog(parent, host).show()
