"""Accessible reviewed-content controls on the reader's existing worker."""
from ..speech.narration import CATEGORIES, LABELS


def create_reading_narration(parent, *, capture, execute, guard, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class ReadingNarration(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Read or skip reviewed reading content', size=(780, 690),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive, self.working, self.cancel_pending = True, False, None
            self.reviewed, self.rows, self.policy, self.buttons = None, (), None, {}
            self.preview_selection = None
            layout = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(self, label=(
                'Only your explicit text selections are classified. Unmarked content is always read; '
                'automatic table, note, formula and code recognition is unavailable. '
                'Read/Skip changes narration, never the original or the editable text. '
                'Close this window to select another region in the reader.'))
            notice.Wrap(730)
            layout.Add(notice, 0, wx.ALL | wx.EXPAND, 8)
            self.selection_label = wx.StaticText(self, label='')
            self.selection_label.SetName('Captured reading content selection')
            layout.Add(self.selection_label, 0, wx.ALL | wx.EXPAND, 6)
            layout.Add(wx.StaticText(self, label='Category for the explicit selection'), 0, wx.LEFT, 8)
            self.category = wx.Choice(self, choices=[LABELS[item] for item in CATEGORIES])
            self.category.SetName('Reading content category')
            self.category.SetSelection(0)
            self.category.Bind(wx.EVT_CHOICE, self.category_changed)
            layout.Add(self.category, 0, wx.ALL | wx.EXPAND, 6)
            layout.Add(wx.StaticText(self, label='Region name'), 0, wx.LEFT, 8)
            self.name = wx.TextCtrl(self, value='Reviewed content')
            self.name.SetName('Reading content region name')
            layout.Add(self.name, 0, wx.ALL | wx.EXPAND, 6)
            self.list = wx.ListBox(self)
            self.list.SetName('Reviewed reading content regions')
            layout.Add(self.list, 1, wx.ALL | wx.EXPAND, 6)
            layout.Add(wx.StaticText(self, label='Narration for the selected category'), 0, wx.LEFT, 8)
            self.mode = wx.Choice(self, choices=['Read', 'Skip'])
            self.mode.SetName('Read or skip selected category')
            self.mode.SetSelection(0)
            layout.Add(self.mode, 0, wx.ALL | wx.EXPAND, 6)
            self.scope = wx.Choice(self, choices=[
                'Whole reading copy', 'Selection captured at opening', 'From cursor captured at opening'])
            self.scope.SetName('Narration preview range')
            self.scope.SetSelection(0)
            layout.Add(self.scope, 0, wx.ALL | wx.EXPAND, 6)
            self.preview = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 110))
            self.preview.SetName('Exact narration preview')
            layout.Add(self.preview, 1, wx.ALL | wx.EXPAND, 6)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 75))
            self.status.SetName('Reading content policy status')
            layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 6)
            for actions in (
                (('mark', 'Mark selected reading content', self.mark),
                 ('remove', 'Remove selected content region', self.remove),
                 ('apply', 'Apply category Read or Skip', self.apply)),
                (('preview', 'Preview exact narration', self.show_preview),
                 ('refresh', 'Refresh narration review', self.refresh),
                 ('close', 'Close reading content policy', self.close)),
            ):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in actions:
                    button = wx.Button(self, wx.ID_CANCEL, label) if key == 'close' else wx.Button(self, label=label)
                    button.SetName(label)
                    button.Bind(wx.EVT_BUTTON, handler)
                    self.buttons[key] = button
                    row.Add(button, 0, wx.ALL, 4)
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            try:
                self.refresh()
                self.category.SetFocus()
            except BaseException:
                try:
                    self.invalidate()
                finally:
                    self.Destroy()
                raise

        def controls(self):
            if not self.alive:
                return
            enabled = not self.working
            for control in (self.category, self.name, self.list, self.mode, self.scope):
                control.Enable(enabled)
            for key, button in self.buttons.items():
                button.Enable(key == 'close' or enabled)
            category = self.selected_category()
            available = self.policy is not None and any(row['category'] == category for row in self.rows)
            self.buttons['apply'].Enable(enabled and available)
            self.buttons['remove'].Enable(enabled and bool(self.rows))
            self.buttons['mark'].Enable(enabled and self.reviewed is not None
                                        and self.reviewed.first < self.reviewed.last)

        def selected_category(self):
            index = self.category.GetSelection()
            if not 0 <= index < len(CATEGORIES):
                raise ValueError('Choose an explicit reading-content category.')
            return CATEGORIES[index]

        def category_changed(self, event=None):
            if self.alive:
                self.mode.SetSelection(1 if self.policy and self.selected_category() in self.policy['skip'] else 0)
                self.controls()

        def completed(self, value, snapshot):
            if not self.alive:
                return
            self.working, self.cancel_pending, self.reviewed = False, None, snapshot
            self.policy = value['policy']
            self.rows = tuple(self.policy['regions']) if self.policy else ()
            skipped = self.policy['skip'] if self.policy else ()
            self.list.Set([
                row['label'] + '; ' + LABELS[row['category']] + '; characters '
                + str(row['start'] + 1) + '–' + str(row['end']) + '; '
                + ('Skip' if row['category'] in skipped else 'Read')
                + ('; text changed — re-mark or choose Read' if row['text_sha256'] != value['text_sha256'] else '')
                for row in self.rows])
            self.selection_label.SetLabel(
                'Captured selection: characters ' + str(snapshot.first + 1) + '–' + str(snapshot.last)
                if snapshot.first < snapshot.last else 'No selected region. Close and select text to classify it.')
            self.preview_selection = value['preview']
            if self.preview_selection is not None:
                selected = self.preview_selection
                self.preview.ChangeValue(selected.text)
                message = ('Empty narration: the selected range has no readable content after Read/Skip. '
                           'No engine or output was started.' if selected.empty else
                           'Preview only; no speech started. ' + str(len(selected.slices)) + ' retained or derived slices; '
                           + str(len(selected.skipped)) + ' skipped regions.')
                if selected.filtered:
                    message += ' Inserted gap separators are derived. Filtered audio has no text-follow or saved timing.'
                self.status.ChangeValue(message)
            else:
                self.preview.ChangeValue('')
                self.status.ChangeValue(
                    'Reviewed content updated. Unmarked content is Read. Ctrl+S in the reader retains these settings. '
                    'Preview before narration; Read re-enables a category without changing text.')
            self.category_changed()

        def failed(self, exc):
            if self.alive:
                self.working, self.cancel_pending = False, None
                self.status.ChangeValue(str(exc))
                self.controls()

        def action(self, kind, arguments):
            if not self.alive or self.working:
                return
            try:
                guard()
                self.working = True
                self.controls()
                self.status.ChangeValue('Checking the exact reading copy and content policy...')
                self.cancel_pending = execute(kind, dict(arguments), self.reviewed, self.completed, self.failed,
                                              active=lambda: self.alive)
            except Exception as exc:
                self.failed(exc)

        def refresh(self, event=None):
            if not self.alive or self.working:
                return
            try:
                guard()
                self.reviewed = capture()
                self.action('inspect', {})
            except Exception as exc:
                self.failed(exc)

        def mark(self, event=None):
            if self.alive and not self.working:
                self.action('mark', {'category': self.selected_category(), 'label': self.name.GetValue()})

        def remove(self, event=None):
            if self.alive and not self.working:
                index = self.list.GetSelection()
                if not 0 <= index < len(self.rows):
                    self.failed(ValueError('Choose a reviewed content region first.'))
                    return
                self.action('remove', {'id': self.rows[index]['id']})

        def apply(self, event=None):
            if self.alive and not self.working:
                index = self.mode.GetSelection()
                if index not in (0, 1):
                    self.failed(ValueError('Choose Read or Skip.'))
                    return
                self.action('read', {'category': self.selected_category(), 'read': index == 0})

        def show_preview(self, event=None):
            if self.alive and not self.working:
                self.action('preview', {'scope': self.scope.GetSelection()})

        def invalidate(self):
            self.alive = False
            cancel_pending, self.cancel_pending = self.cancel_pending, None
            if cancel_pending is not None:
                cancel_pending()

        def destroyed(self, event):
            try:
                if event.GetEventObject() is self:
                    self.invalidate()
            finally:
                event.Skip()

        def close(self, event=None):
            if self.alive:
                try:
                    self.invalidate()
                finally:
                    self.EndModal(wx.ID_CANCEL)

    return ReadingNarration()


def show_reading_narration(parent, **options):
    dialog = create_reading_narration(parent, **options)
    try:
        dialog.ShowModal()
    finally:
        try:
            if dialog.alive:
                dialog.close()
        finally:
            if dialog:
                dialog.Destroy()
