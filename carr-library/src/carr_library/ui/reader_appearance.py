"""Native appearance choices for the existing reader text controls.

wx reset APIs: https://docs.wxpython.org/wx.Window.html
Windows contrast policy: https://learn.microsoft.com/en-us/windows/win32/winauto/high-contrast-parameter
No font files are loaded and no document text or selection is changed here.
"""
from dataclasses import replace
import sys

from ..backup_files import local_path, regular
from ..reader.appearance import (AppearanceSnapshot, ReaderAppearancePreferences,
                                 ReaderFont, load_appearance, save_appearance)


def high_contrast_enabled():
    """Read Windows' accessibility setting; an unknown result keeps OS colours."""
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        from ctypes import wintypes
        class HighContrast(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('dwFlags', wintypes.DWORD),
                        ('lpszDefaultScheme', wintypes.LPWSTR)]
        value = HighContrast()
        value.cbSize = ctypes.sizeof(value)
        query = ctypes.WinDLL('user32', use_last_error=True).SystemParametersInfoW
        query.argtypes = (wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT)
        query.restype = wintypes.BOOL
        if not query(0x0042, value.cbSize, ctypes.byref(value), 0):  # SPI_GETHIGHCONTRAST
            return None
        return bool(value.dwFlags & 0x00000001)  # HCF_HIGHCONTRASTON
    except (OSError, AttributeError, ValueError):
        return None


def _style_names(wx):
    return {'normal': wx.FONTSTYLE_NORMAL, 'italic': wx.FONTSTYLE_ITALIC,
            'slant': wx.FONTSTYLE_SLANT}


def selected_font(font, wx):
    if not font.IsOk():
        raise ValueError('Choose a valid installed font.')
    styles = {value: name for name, value in _style_names(wx).items()}
    choice = ReaderFont(font.GetFaceName(), font.GetFractionalPointSize(),
        font.GetNumericWeight(), styles.get(font.GetStyle(), ''),
        bool(font.GetUnderlined()), bool(font.GetStrikethrough()))
    if not wx.FontEnumerator.IsValidFacename(choice.face):
        raise ValueError('The selected font is not installed on this computer.')
    return choice


def _font(control, preferences, wx):
    font = wx.Font(control.GetDefaultAttributes().font)
    notice = ''
    choice = preferences.font
    if choice is not None:
        if not wx.FontEnumerator.IsValidFacename(choice.face):
            notice = f'The saved font {choice.face} is unavailable; the system font is being used.'
        else:
            font.SetFaceName(choice.face)
            font.SetFractionalPointSize(choice.points)
            font.SetNumericWeight(choice.weight)
            font.SetStyle(_style_names(wx)[choice.style])
            font.SetUnderlined(choice.underlined)
            font.SetStrikethrough(choice.strikethrough)
    font.SetFractionalPointSize(font.GetFractionalPointSize() * preferences.zoom_percent / 100)
    return font, notice


def apply_appearance(control, preferences, wx, *, high_contrast):
    """Apply native style only; use OS defaults and respect Windows contrast."""
    font, notice = _font(control, preferences, wx)
    control.SetFont(wx.NullFont if preferences.font is None and preferences.zoom_percent == 100 else font)
    if high_contrast is not False:
        control.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        control.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
        message = ('Windows high contrast is active; system text colours take priority.'
                   if high_contrast is True else
                   'The Windows contrast setting could not be read; system text colours are retained.')
        notice = ' '.join(value for value in (notice, message) if value)
    else:
        control.SetForegroundColour(wx.Colour(preferences.foreground) if preferences.foreground else wx.NullColour)
        control.SetBackgroundColour(wx.Colour(preferences.background) if preferences.background else wx.NullColour)
    control.Refresh()
    return notice


class ReaderAppearance:
    """One view bound to the current root, source guard and native owner."""
    def __init__(self, window, text, root, *, guard=lambda: None, alive=lambda: True,
                 report=lambda value: None, additional_controls=(), wx_module=None,
                 contrast=high_contrast_enabled):
        if wx_module is None:
            import wx as wx_module
        if not callable(guard) or not callable(alive) or not callable(report):
            raise ValueError('Reader appearance needs the current reader owner.')
        self.wx, self.window, self.text = wx_module, window, text
        self.root = local_path(root, exists=True)
        info = regular(self.root, directory=True)
        self._root_identity = info.st_dev, info.st_ino
        self.guard = guard
        self.controls = (text, *tuple(additional_controls))
        self.alive, self.report, self.contrast = alive, report, contrast
        self.closed, self._applying, self._styled, self.active_dialog = False, False, False, None
        self._native_id = window.GetId()
        self.snapshot = AppearanceSnapshot(ReaderAppearancePreferences(), None)
        self.notice = ''
        window.Bind(self.wx.EVT_WINDOW_DESTROY, self.on_destroy)
        window.Bind(self.wx.EVT_SYS_COLOUR_CHANGED, self.on_colours)
        try:
            self.snapshot = load_appearance(self.root)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice = f'Saved appearance is unavailable; system defaults remain in use. {exc}'
        self.notice = ' '.join(value for value in (self.notice, self._apply()) if value)

    def check(self):
        if self.closed or not self.alive() or not self.window or any(not control for control in self.controls):
            raise ValueError('The reader has closed.')
        self.guard()
        current = local_path(self.root, exists=True)
        info = regular(current, directory=True)
        if current != self.root or (info.st_dev, info.st_ino) != self._root_identity:
            raise ValueError('The library changed. Reopen its reader appearance settings.')

    def _apply(self):
        self.check()
        # Fresh native controls already use the OS defaults, including high
        # contrast. A reset after a custom style must actively clear that style.
        if not self._styled and self.snapshot.preferences == ReaderAppearancePreferences():
            return ''
        self._applying = True
        try:
            notices = [apply_appearance(control, self.snapshot.preferences, self.wx,
                                        high_contrast=self.contrast()) for control in self.controls]
            self._styled = True
            return ' '.join(dict.fromkeys(value for value in notices if value))
        finally:
            self._applying = False

    def save(self, preferences, *, expected_token):
        self.check()
        self.snapshot = result = save_appearance(self.root, preferences, expected_token=expected_token)
        try:
            notice = self._apply()
        except (OSError, ValueError, RuntimeError) as exc:
            notice = f'Appearance was saved; this reader could not apply it: {exc}'
        self.notice = ' '.join(value for value in (result.warning, notice) if value)
        return result, self.notice

    def on_colours(self, event):
        try:
            if not self.closed and not self._applying:
                self.notice = self._apply()
                if self.active_dialog is not None and self.active_dialog.alive:
                    self.active_dialog.refresh_preview()
        except (OSError, ValueError, RuntimeError):
            # A changed library or destroyed native parent has no view to update.
            pass
        finally:
            event.Skip()

    def on_destroy(self, event):
        if event.GetId() == self._native_id:
            self.close()
        event.Skip()

    def close(self):
        self.closed = True
        if self.active_dialog is not None:
            self.active_dialog.dispose()

    def show(self, event=None):
        if self.closed or self.active_dialog is not None:
            return
        dialog = None
        try:
            self.check()
            snapshot = load_appearance(self.root)
            dialog = create_appearance_dialog(self, snapshot)
            self.active_dialog = dialog
            dialog.ShowModal()
        except (OSError, ValueError, RuntimeError) as exc:
            if not self.closed and self.alive() and self.window:
                self.report(str(exc))
        finally:
            if dialog is not None:
                dialog.dispose()
                if dialog:
                    dialog.Destroy()
            self.active_dialog = None
        if not self.closed and self.alive() and self.window and self.text:
            self.text.SetFocus()


def create_appearance_dialog(binding, snapshot):
    wx = binding.wx

    class AppearanceDialog(wx.Dialog):
        def __init__(self):
            binding.check()
            super().__init__(binding.window, title='Reader appearance', size=(750, 720),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self.alive, self._destroy_requested = True, False
            self.candidate, self.expected_token = snapshot.preferences, snapshot.token
            try:
                self._build()
            except BaseException:
                self.dispose()
                super().Destroy()
                raise

        def _build(self):
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label='Save applies to this reader and future readers in this library. '
                'Text and reading positions stay unchanged. System defaults follow your computer’s theme. '
                'Windows high contrast always takes priority over custom colours.')
            notice.Wrap(690)
            box.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
            def field(label, factory=wx.TextCtrl, **kwargs):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
                control = factory(panel, **kwargs)
                control.SetName(label.replace('&', ''))
                box.Add(control, 0, wx.ALL | wx.EXPAND, 4)
                return control
            self.font_summary = field('Selected font', style=wx.TE_READONLY)
            self.colour_summary = field('Selected text and background colours', style=wx.TE_READONLY)
            self.zoom = field('&Zoom percent (50 to 400)', wx.SpinCtrl,
                              min=50, max=400, initial=self.candidate.zoom_percent)
            self.preview = field('Appearance preview', style=wx.TE_READONLY | wx.TE_MULTILINE,
                value='Reader appearance\nУкраїнський текст — English text — 0123456789\n'
                      'This preview does not change the book or reading copy.', size=(-1, 150))
            self.status = field('Appearance status', style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 85))
            self.buttons = {}
            rows = ((('font', 'Choose &font...', self.on_font),
                     ('system_font', 'Use s&ystem font', self.on_system_font)),
                    (('foreground', '&Text colour...', lambda event: self.on_colour('foreground')),
                     ('background', '&Background colour...', lambda event: self.on_colour('background')),
                     ('system_colours', 'Use system &colours', self.on_system_colours)),
                    (('save', '&Save appearance', self.on_save),
                     ('reset', '&Reset to system defaults', self.on_reset),
                     ('close', 'Close', self.on_close)))
            for items in rows:
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in items:
                    button = wx.Button(panel, wx.ID_CANCEL if key == 'close' else wx.ID_ANY, label)
                    button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, handler)
                    row.Add(button, 0, wx.ALL, 4)
                    self.buttons[key] = button
                box.Add(row, 0, wx.EXPAND)
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(panel, 1, wx.EXPAND)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.zoom.Bind(wx.EVT_SPINCTRL, self.on_zoom)
            self.zoom.Bind(wx.EVT_TEXT, self.on_zoom)
            panel.FitInside()
            self.refresh_preview()
            self.zoom.SetFocus()

        def report(self, message):
            if self.alive and self and not binding.closed and binding.alive():
                self.status.ChangeValue(str(message))

        def refresh_preview(self, message='Review the preview, then Save appearance. Close leaves unsaved choices behind.'):
            if not self.alive:
                return
            binding.check()
            contrast = binding.contrast()
            notice = apply_appearance(self.preview, self.candidate, wx, high_contrast=contrast)
            font = self.candidate.font
            self.font_summary.ChangeValue('System font' if font is None else
                f'{font.face}, {font.points:g} points, {font.style}, weight {font.weight}')
            self.colour_summary.ChangeValue(f'Text: {self.candidate.foreground or "System"}; '
                                           f'background: {self.candidate.background or "System"}')
            for key in ('foreground', 'background'):
                self.buttons[key].Enable(contrast is False)
            self.report(' '.join(value for value in (message, notice) if value))

        def on_zoom(self, event=None):
            if not self.alive:
                return
            try:
                binding.check()
                self.candidate = replace(self.candidate, zoom_percent=self.zoom.GetValue())
                self.refresh_preview()
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc)

        def on_font(self, event=None):
            if not self.alive:
                return
            picker, choice = None, None
            try:
                binding.check()
                data = wx.FontData()
                font, _ = _font(self.preview, replace(self.candidate, zoom_percent=100), wx)
                data.SetInitialFont(font)
                data.SetAllowSymbols(False)
                data.EnableEffects(False)
                data.SetRange(4, 144)
                picker = wx.FontDialog(self, data)
                try:
                    accepted = picker.ShowModal() == wx.ID_OK
                    if not self.alive or not self or not picker:
                        return
                    binding.check()
                    if accepted:
                        choice = selected_font(picker.GetFontData().GetChosenFont(), wx)
                finally:
                    if picker:
                        picker.Destroy()
                if not self.alive:
                    return
                binding.check()
                if choice is not None:
                    self.candidate = replace(self.candidate, font=choice)
                    self.refresh_preview()
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc)

        def on_colour(self, field):
            if not self.alive:
                return
            try:
                binding.check()
                if binding.contrast() is not False:
                    self.refresh_preview()
                    return
                data = wx.ColourData()
                colour = getattr(self.candidate, field)
                system = wx.SYS_COLOUR_WINDOWTEXT if field == 'foreground' else wx.SYS_COLOUR_WINDOW
                data.SetColour(wx.Colour(colour) if colour else wx.SystemSettings.GetColour(system))
                picker, selected = wx.ColourDialog(self, data), None
                try:
                    accepted = picker.ShowModal() == wx.ID_OK
                    if not self.alive or not self or not picker:
                        return
                    binding.check()
                    if accepted:
                        selected_colour = picker.GetColourData().GetColour()
                        if not selected_colour.IsOk():
                            raise ValueError('Choose a valid text colour.')
                        selected = '#{:02X}{:02X}{:02X}'.format(
                            selected_colour.Red(), selected_colour.Green(), selected_colour.Blue())
                finally:
                    if picker:
                        picker.Destroy()
                if not self.alive:
                    return
                binding.check()
                if selected is not None:
                    self.candidate = replace(self.candidate, **{field: selected})
                    self.refresh_preview()
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc)

        def on_system_font(self, event=None):
            if not self.alive:
                return
            try:
                binding.check()
                self.candidate = replace(self.candidate, font=None)
                self.refresh_preview()
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc)

        def on_system_colours(self, event=None):
            if not self.alive:
                return
            try:
                binding.check()
                self.candidate = replace(self.candidate, foreground=None, background=None)
                self.refresh_preview()
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc)

        def on_reset(self, event=None):
            if not self.alive:
                return
            try:
                binding.check()
                self.candidate = ReaderAppearancePreferences()
                self.zoom.SetValue(100)
                self.refresh_preview('System defaults previewed. Save appearance to retain them.')
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(exc)

        def on_save(self, event=None):
            if not self.alive:
                return
            try:
                binding.check()
                self.candidate = replace(self.candidate, zoom_percent=self.zoom.GetValue())
                saved, notice = binding.save(self.candidate, expected_token=self.expected_token)
                self.expected_token = saved.token
                if self.alive and not binding.closed:
                    self.report(' '.join(value for value in ('Reader appearance saved.', notice) if value))
            except (OSError, ValueError, RuntimeError) as exc:
                self.report(f'Appearance save needs attention: {exc}')

        def dispose(self):
            self.alive = False

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self.dispose()
            event.Skip()

        def Destroy(self):
            self.dispose()
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

        def on_close(self, event=None):
            self.dispose()
            if self and self.IsModal():
                self.EndModal(wx.ID_CANCEL)

    return AppearanceDialog()
