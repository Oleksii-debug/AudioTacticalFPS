"""Native reading appearance controls for the existing editor and edit host."""
from ..speech.presentation import ReadingAppearance, ReadingAppearanceStore, ReadingFont


class ReaderPresentationControls:
    def __init__(self, parent, editor, host, *, library_root=None):
        import wx
        self.parent, self.editor, self.host = parent, editor, host
        self.baseline = (wx.Font(editor.GetFont()), editor.GetForegroundColour(), editor.GetBackgroundColour())
        self.current = ReadingAppearance()
        self.store = None
        self.error = None
        self.view = None
        try:
            host.guard()
            if library_root is not None:
                self.store = ReadingAppearanceStore(library_root)
                self.current = self.store.load()
            self._draw(editor, self.current)
        except (ValueError, OSError, RuntimeError) as exc:
            self.error = str(exc)
            host.report('Reading appearance could not be loaded: ' + self.error)

    def _resolved(self, value):
        import wx
        if type(value) is not ReadingAppearance:
            raise ValueError('Validated reading appearance is required.')
        font = wx.Font(self.baseline[0])
        if value.font is not None:
            if not font.SetFaceName(value.font.face):
                raise ValueError('The saved font is unavailable. Choose an installed font or the system font.')
            font.SetPointSize(value.font.points)
            font.SetNumericWeight(value.font.weight)
            font.SetStyle({'normal': wx.FONTSTYLE_NORMAL, 'italic': wx.FONTSTYLE_ITALIC,
                           'slant': wx.FONTSTYLE_SLANT}[value.font.style])
            font.SetUnderlined(value.font.underlined)
        font.SetFractionalPointSize(max(3.0, font.GetFractionalPointSize() * value.zoom / 100))
        if not font.IsOk():
            raise ValueError('The selected reading font is unavailable.')
        return (font, wx.Colour(value.foreground) if value.foreground else self.baseline[1],
                wx.Colour(value.background) if value.background else self.baseline[2])

    def _draw(self, editor, value):
        # No SetValue, Replace, SetSelection or document mutation: native text
        # positions, source binding and the user's editing history stay intact.
        self._draw_resolved(editor, self._resolved(value))

    @staticmethod
    def _draw_resolved(editor, resolved):
        font, foreground, background = resolved
        editor.SetFont(font)
        editor.SetForegroundColour(foreground)
        editor.SetBackgroundColour(background)
        editor.Refresh()

    def apply(self, value):
        if not self.host.alive() or self.host.busy():
            raise ValueError('Finish or cancel the current reading operation before changing its appearance.')
        self.host.guard()
        resolved = self._resolved(value)
        import wx
        previous = (wx.Font(self.editor.GetFont()), self.editor.GetForegroundColour(),
                    self.editor.GetBackgroundColour())
        saving = False
        try:
            # Resolve only once. Native failure precedes durable publication;
            # a rejected concurrent save restores the actual previous style.
            self._draw_resolved(self.editor, resolved)
            if self.store is not None:
                saving = True
                self.store.save(value, expected=self.current, guard=self.host.guard)
        except (ValueError, OSError, RuntimeError) as exc:
            committed = False
            if saving:
                try:
                    committed = self.store.load() == value
                except (ValueError, OSError, RuntimeError):
                    pass
            if committed:
                # An atomic replacement can finish before its flush reports an
                # error. Retain the matching visible/durable result explicitly.
                self.current = value
                self.error = None
                self.host.report('Reading appearance was saved. A completion check reported: ' + str(exc))
                return
            try:
                self._draw_resolved(self.editor, previous)
            except (ValueError, OSError, RuntimeError) as rollback:
                raise RuntimeError('Reading appearance could not be restored after an error. '
                                   'Reopen the reader. ' + str(rollback)) from exc
            raise
        self.current = value
        self.error = None
        self.host.report(f'Reading appearance applied. Zoom {value.zoom} percent.'
                         + (' Saved for this library.' if self.store is not None else ' For this reader session.'))

    def show(self, event=None):
        import wx
        try:
            if not self.host.alive() or self.host.busy():
                raise ValueError('Finish or cancel the current reading operation before opening appearance settings.')
            self.host.guard()
            if self.store is not None:
                # Reload accepted cosmetic preferences from another reader. An
                # invalid file is retained and reported, never overwritten.
                self.current = self.store.load()
        except (ValueError, OSError, RuntimeError) as exc:
            self.host.report(str(exc), focus=True)
            return
        dialog = wx.Dialog(self.parent, title='Reading appearance', size=(660, 570),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.view = dialog
        draft_font = [self.current.font]
        outer = wx.BoxSizer(wx.VERTICAL)
        notice = wx.StaticText(dialog, label='Choose how the reading copy looks. Text, bookmarks and exported content stay the same.\n'
                               'Leave a colour empty to follow the system colour. Preview before applying.')
        outer.Add(notice, 0, wx.ALL | wx.EXPAND, 10)
        font_label = wx.StaticText(dialog, label='')
        font_label.SetName('Selected reading font')
        outer.Add(font_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        choose = wx.Button(dialog, label='Choose &font…')
        choose.SetName('Choose reading font')
        system = wx.Button(dialog, label='Use &system font')
        system.SetName('Use system reading font')
        row.Add(choose, 0, wx.RIGHT, 8)
        row.Add(system)
        outer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        grid = wx.FlexGridSizer(cols=2, hgap=10, vgap=10)
        grid.AddGrowableCol(1, 1)
        def field(label, control):
            grid.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control.SetName(label.replace('&', ''))
            grid.Add(control, 1, wx.EXPAND)
            return control
        zoom = field('&Zoom percent (50–400)', wx.SpinCtrl(dialog, min=50, max=400, initial=self.current.zoom))
        foreground = field('&Text colour (#RRGGBB or empty)', wx.TextCtrl(dialog, value=self.current.foreground or ''))
        background = field('&Background colour (#RRGGBB or empty)', wx.TextCtrl(dialog, value=self.current.background or ''))
        outer.Add(grid, 0, wx.ALL | wx.EXPAND, 10)
        preview = wx.TextCtrl(dialog, value=self.editor.GetValue()[:1200] or 'Reading preview — Українська, English, 123.',
                              style=wx.TE_MULTILINE | wx.TE_READONLY)
        preview.SetName('Reading appearance preview')
        outer.Add(preview, 1, wx.ALL | wx.EXPAND, 10)
        status = wx.StaticText(dialog, label='')
        status.SetName('Reading appearance status')
        outer.Add(status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        reset = wx.Button(dialog, label='&Reset to system appearance')
        reset.SetName('Reset to system appearance')
        apply_button = wx.Button(dialog, wx.ID_APPLY, label='&Apply appearance')
        apply_button.SetName('Apply reading appearance')
        cancel = wx.Button(dialog, wx.ID_CANCEL, label='Cancel')
        for button in (reset, apply_button, cancel):
            buttons.Add(button, 0, wx.RIGHT, 8)
        outer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        dialog.SetSizer(outer)
        dialog.SetMinSize((580, 510))
        # Stable handles support native keyboard acceptance without label walks.
        dialog.zoom, dialog.foreground, dialog.background = zoom, foreground, background
        dialog.preview, dialog.apply_button, dialog.cancel_button = preview, apply_button, cancel
        dialog.reset_button, dialog.font_button, dialog.system_font_button = reset, choose, system
        dialog.appearance_status = status

        def candidate():
            return ReadingAppearance(draft_font[0], foreground.GetValue().strip().upper() or None,
                                     background.GetValue().strip().upper() or None, zoom.GetValue())

        def refresh(event=None):
            chosen = draft_font[0]
            font_label.SetLabel('System font' if chosen is None else f'{chosen.face}, {chosen.points} points')
            try:
                value = candidate()
                self._draw(preview, value)
                status.SetLabel('Text and background colours are identical.' if value.foreground is not None
                                and value.foreground == value.background else 'Preview ready. Apply saves these choices.')
                apply_button.Enable(True)
            except (ValueError, OSError, RuntimeError) as exc:
                status.SetLabel(str(exc))
                apply_button.Enable(False)

        def select_font(event):
            data = wx.FontData()
            base = ReadingAppearance(draft_font[0])
            try:
                data.SetInitialFont(self._resolved(base)[0])
            except ValueError:
                data.SetInitialFont(self.baseline[0])
            data.SetRange(6, 72)
            data.EnableEffects(False)
            chooser = wx.FontDialog(dialog, data)
            try:
                if chooser.ShowModal() == wx.ID_OK:
                    font = chooser.GetFontData().GetChosenFont()
                    style = {wx.FONTSTYLE_NORMAL: 'normal', wx.FONTSTYLE_ITALIC: 'italic',
                             wx.FONTSTYLE_SLANT: 'slant'}[font.GetStyle()]
                    draft_font[0] = ReadingFont(font.GetFaceName(), font.GetPointSize(),
                                               font.GetNumericWeight(), style, font.GetUnderlined())
            except (ValueError, KeyError) as exc:
                status.SetLabel(str(exc))
            finally:
                chooser.Destroy()
            refresh()

        def system_font(event):
            draft_font[0] = None
            refresh()

        def defaults(event):
            draft_font[0] = None
            foreground.ChangeValue('')
            background.ChangeValue('')
            zoom.SetValue(100)
            refresh()

        def accept(event):
            try:
                self.apply(candidate())
                dialog.EndModal(wx.ID_OK)
            except (ValueError, OSError, RuntimeError) as exc:
                status.SetLabel(str(exc))
                self.host.report(str(exc), focus=False)

        choose.Bind(wx.EVT_BUTTON, select_font)
        system.Bind(wx.EVT_BUTTON, system_font)
        reset.Bind(wx.EVT_BUTTON, defaults)
        apply_button.Bind(wx.EVT_BUTTON, accept)
        zoom.Bind(wx.EVT_SPINCTRL, refresh)
        zoom.Bind(wx.EVT_TEXT, refresh)
        foreground.Bind(wx.EVT_TEXT, refresh)
        background.Bind(wx.EVT_TEXT, refresh)
        refresh()
        try:
            zoom.SetFocus()
            dialog.ShowModal()
        finally:
            self.view = None
            dialog.Destroy()
