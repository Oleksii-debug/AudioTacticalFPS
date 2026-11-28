"""Native reader font/color/zoom preview; never changes reading-copy content."""
from dataclasses import replace

from ..speech.presentation import PresentationStore, ReadingFont, ReadingPresentation


def native_font(wx, control, profile):
    font = wx.Font(control.GetClassDefaultAttributes().font)
    if profile.font is not None:
        selected = profile.font
        if not wx.FontEnumerator.IsValidFacename(selected.face):
            raise ValueError(f'The saved font is not installed: {selected.face}. Choose a font or reset to OS defaults.')
        if not font.SetFaceName(selected.face):
            raise ValueError('The selected native font could not be created.')
        font.SetFractionalPointSize(selected.points)
        font.SetNumericWeight(selected.weight)
        font.SetStyle({'normal': wx.FONTSTYLE_NORMAL, 'italic': wx.FONTSTYLE_ITALIC,
                       'slant': wx.FONTSTYLE_SLANT}[selected.style])
        font.SetUnderlined(False); font.SetStrikethrough(False)
    font.SetFractionalPointSize(font.GetFractionalPointSize() * profile.zoom / 100)
    if not font.IsOk():
        raise ValueError('The native font is unavailable. Choose a font or reset to OS defaults.')
    return font


class NativeReadingPresentation:
    """Applies global native control attributes without rewriting its text."""
    def __init__(self, control, wx_module):
        self.control, self.wx = control, wx_module
        self.current = ReadingPresentation()

    def apply(self, profile):
        if not isinstance(profile, ReadingPresentation):
            raise ValueError('Use a validated reading appearance profile.')
        wx, control = self.wx, self.control
        font = native_font(wx, control, profile)
        foreground = wx.Colour(*profile.foreground) if profile.foreground is not None else wx.NullColour
        background = wx.Colour(*profile.background) if profile.background is not None else wx.NullColour
        text, selection, cursor = control.GetValue(), control.GetSelection(), control.GetInsertionPoint()
        old = wx.Font(control.GetFont()), control.GetForegroundColour(), control.GetBackgroundColour()
        try:
            # False from a native setter can mean the attribute was unchanged.
            # No SetValue/ChangeValue/SetStyle or editing-history call is used.
            control.SetFont(font)
            control.SetForegroundColour(foreground)
            control.SetBackgroundColour(background)
            if control.GetValue() != text:
                raise ValueError('The native reader changed text while applying appearance.')
            if control.GetSelection() != selection or control.GetInsertionPoint() != cursor:
                if selection[0] == selection[1]:
                    control.SetInsertionPoint(cursor)
                elif cursor == selection[0]:
                    control.SetSelection(selection[1], selection[0])
                else:
                    control.SetSelection(*selection)
            if control.GetSelection() != selection or control.GetInsertionPoint() != cursor:
                raise ValueError('The native reader could not preserve its exact selection.')
            control.Refresh()
        except Exception:
            control.SetFont(old[0]); control.SetForegroundColour(old[1]); control.SetBackgroundColour(old[2])
            raise
        self.current = profile


def load_reading_presentation(control, library_root, wx_module):
    """Restore only non-executable display preferences; preserve invalid state."""
    presenter = NativeReadingPresentation(control, wx_module)
    store = None
    try:
        if library_root is not None:
            store = PresentationStore(library_root)
            profile, token = store.load()
            if token is not None:
                presenter.apply(profile)
    except (OSError, ValueError, RuntimeError) as exc:
        return presenter, store, f'Reading appearance was not applied: {exc}. '
    return presenter, store, ''


def _description(profile):
    font = ('OS default font' if profile.font is None else
            f'{profile.font.face}; {profile.font.points:g} points; {profile.font.style}; weight {profile.font.weight}')
    color = lambda value: 'OS theme default' if value is None else '#%02X%02X%02X' % value
    return '\n'.join((font, f'Zoom: {profile.zoom} percent',
                      f'Text color: {color(profile.foreground)}', f'Background: {color(profile.background)}'))


def create_reading_presentation(parent, *, library_root, presenter, guard=lambda: None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    store = PresentationStore(library_root)
    profile, token = store.load()

    class ReadingAppearanceDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Reading appearance: font, colors and zoom', size=(780, 680),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive, self.applied = True, False
            self.profile, self.token, self.reviewed = profile, token, None
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            layout = wx.BoxSizer(wx.VERTICAL)
            note = wx.StaticText(panel, label='Preview the reading-copy display, then apply it explicitly. '
                'Text, original source, editing history and speech settings stay unchanged. '
                'The profile belongs to this library and is included in its normal backup.')
            note.Wrap(720); layout.Add(note, 0, wx.ALL | wx.EXPAND, 6)
            def field(label, factory, **kwargs):
                layout.Add(wx.StaticText(panel, label=label), 0, wx.LEFT | wx.TOP, 6)
                control = factory(panel, **kwargs); control.SetName(label)
                layout.Add(control, 0, wx.ALL | wx.EXPAND, 6)
                return control
            self.zoom = field('Reading zoom, 50 to 300 percent', wx.SpinCtrl,
                              min=50, max=300, initial=profile.zoom)
            self.details = field('Selected reading appearance', wx.TextCtrl, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                 size=(-1, 85))
            self.sample = field('Reading appearance preview', wx.TextCtrl,
                value=presenter.control.GetValue()[:512] or 'Reading sample: Україна. Clear text, 123. 😀',
                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 140))
            self.sample_presenter = NativeReadingPresentation(self.sample, wx)
            self.status = field('Reading appearance status', wx.TextCtrl,
                                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 70))
            self.buttons = {}
            groups = (
                (('font', 'Choose native font...', self.on_font),
                 ('foreground', 'Choose text color...', lambda event: self.on_color('foreground')),
                 ('background', 'Choose background color...', lambda event: self.on_color('background'))),
                (('default_font', 'Use OS font', self.on_default_font),
                 ('default_colors', 'Use OS colors', self.on_default_colors),
                 ('reset', 'Reset to OS theme and 100 percent', self.on_reset)),
                (('preview', 'Preview appearance', self.on_preview),
                 ('apply', 'Apply to reader and save', self.on_apply),
                 ('reload', 'Reload saved profile', self.on_reload), ('close', 'Close', self.on_close)),
            )
            for group in groups:
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in group:
                    button = wx.Button(panel, label=label); button.SetName(label)
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 4)
                    self.buttons[key] = button
                layout.Add(row, 0, wx.EXPAND)
            panel.SetSizer(layout)
            outer = wx.BoxSizer(wx.VERTICAL); outer.Add(panel, 1, wx.EXPAND)
            self.SetSizer(outer); panel.FitInside()
            self.SetEscapeId(self.buttons['close'].GetId())
            self.zoom.Bind(wx.EVT_SPINCTRL, self.on_changed)
            self.zoom.Bind(wx.EVT_TEXT, self.on_changed)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.update('Choose a font, colors or zoom, then preview. The reader has not changed.')
            self.zoom.SetFocus()

        def check(self):
            if not self.alive:
                raise ValueError('Reading appearance is closed.')
            guard(); store.guard()

        def current(self):
            return replace(self.profile, zoom=self.zoom.GetValue())

        def update(self, message):
            if self.alive:
                try:
                    current = self.current()
                    details = _description(current)
                except ValueError as exc:
                    current, details = None, str(exc)
                self.details.ChangeValue(details)
                self.status.ChangeValue(str(message))
                self.buttons['apply'].Enable(current is not None and self.reviewed is not None and self.reviewed == current)

        def on_changed(self, event=None):
            self.reviewed = None
            self.update('Preview the current choices before applying them.')

        def on_font(self, event=None):
            chooser = None
            try:
                self.check()
                data = wx.FontData(); data.SetAllowSymbols(False); data.EnableEffects(False); data.SetRange(6, 72)
                try:
                    initial = native_font(wx, self.sample, replace(self.current(), zoom=100))
                except ValueError:
                    initial = wx.Font(self.sample.GetClassDefaultAttributes().font)
                data.SetInitialFont(initial)
                chooser = wx.FontDialog(self, data)
                if chooser.ShowModal() != wx.ID_OK:
                    return
                selected = chooser.GetFontData().GetChosenFont()
                self.check()
                styles = {wx.FONTSTYLE_NORMAL: 'normal', wx.FONTSTYLE_ITALIC: 'italic', wx.FONTSTYLE_SLANT: 'slant'}
                chosen = ReadingFont(selected.GetFaceName(), selected.GetFractionalPointSize(),
                                     selected.GetNumericWeight(), styles[selected.GetStyle()])
                self.profile = replace(self.current(), font=chosen)
                self.on_changed()
            except Exception as exc:
                self.update(exc)
            finally:
                if chooser is not None: chooser.Destroy()

        def on_color(self, target):
            chooser = None
            try:
                self.check()
                data = wx.ColourData(); data.SetChooseFull(True); data.SetChooseAlpha(False)
                current = getattr(self.current(), target)
                default = (self.sample.GetForegroundColour() if target == 'foreground'
                           else self.sample.GetBackgroundColour())
                data.SetColour(wx.Colour(*current) if current is not None else default)
                chooser = wx.ColourDialog(self, data)
                if chooser.ShowModal() != wx.ID_OK:
                    return
                selected = chooser.GetColourData().GetColour()
                self.check()
                if not selected.IsOk() or selected.Alpha() != 255:
                    raise ValueError('Choose an opaque native RGB color.')
                self.profile = replace(self.current(), **{target: (selected.Red(), selected.Green(), selected.Blue())})
                self.on_changed()
            except Exception as exc:
                self.update(exc)
            finally:
                if chooser is not None: chooser.Destroy()

        def on_default_font(self, event=None):
            try:
                self.check()
                self.profile = replace(self.current(), font=None); self.on_changed()
            except Exception as exc:
                self.update(exc)

        def on_default_colors(self, event=None):
            try:
                self.check()
                self.profile = replace(self.current(), foreground=None, background=None); self.on_changed()
            except Exception as exc:
                self.update(exc)

        def on_reset(self, event=None):
            try:
                self.check()
                self.profile = ReadingPresentation(); self.zoom.SetValue(100)
                self.on_preview()
            except Exception as exc:
                self.update(exc)

        def on_preview(self, event=None):
            self.reviewed = None
            try:
                self.check()
                profile = self.current()
                self.sample_presenter.apply(profile)
                self.reviewed = profile
                self.update('Preview ready. Apply to reader and save commits these exact choices.'); self.sample.SetFocus()
            except Exception as exc:
                self.update(exc)

        def on_apply(self, event=None):
            try:
                self.check()
                selected = self.current()
                if self.reviewed is None or selected != self.reviewed:
                    raise ValueError('Preview the current font, colors and zoom before applying.')
                previous = presenter.current
                presenter.apply(selected)
                try:
                    self.profile, self.token = store.save(selected, expected_token=self.token)
                except Exception:
                    presenter.apply(previous)
                    raise
                self.applied = True
                self.update('Reading appearance applied and saved. Text and editing history were not changed.')
            except Exception as exc:
                self.update(exc)

        def on_reload(self, event=None):
            try:
                self.check()
                self.profile, self.token = store.load()
                self.zoom.SetValue(self.profile.zoom); self.reviewed = None
                self.update('Saved profile loaded. Preview and apply it explicitly to this reader.')
            except Exception as exc:
                self.update(exc)

        def on_close(self, event=None):
            self.alive = False
            if self.IsModal(): self.EndModal(wx.ID_CANCEL)

    return ReadingAppearanceDialog()


def show_reading_presentation(parent, **kwargs):
    dialog = create_reading_presentation(parent, **kwargs)
    try:
        dialog.ShowModal()
        return dialog.applied
    finally:
        dialog.alive = False
        dialog.Destroy()
