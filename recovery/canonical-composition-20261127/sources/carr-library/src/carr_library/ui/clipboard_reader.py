"""Explicit clipboard snapshots and window-owned, opt-in change notices.

Clipboard text is never written to a recovery store, persisted preference or
history by this module. Monitoring announces changes; only an explicit load
action can offer a new snapshot to the existing reader. No speech starts here.
"""
from hashlib import sha256

from ..speech._io import text_value


def read_clipboard_text(wx):
    """Take one explicit Unicode snapshot without modifying the clipboard."""
    clipboard = wx.TheClipboard
    if not clipboard.Open():
        raise ValueError('The clipboard is busy. Try again when the other application releases it.')
    try:
        if not clipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
            raise ValueError('The clipboard does not contain Unicode text to read.')
        data = wx.TextDataObject()
        if not clipboard.GetData(data):
            raise ValueError('The clipboard text could not be read. Copy the text again and retry.')
        value = data.GetText()
    finally:
        clipboard.Close()
    return text_value(value, 'Clipboard reading text')


def _digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


class ClipboardSession:
    """Keep one change fingerprint, with no clipboard text queue or history."""
    def __init__(self, reader, initial):
        if not callable(reader):
            raise TypeError('Clipboard reading requires an explicit snapshot callback.')
        self._reader = reader
        self._last = _digest(text_value(initial, 'Clipboard reading text'))
        self.enabled = self.closed = False

    def enable(self, value):
        if type(value) is not bool:
            raise ValueError('Clipboard monitoring must be explicitly enabled or disabled.')
        if self.closed:
            return False
        self.enabled = value
        return True

    def capture(self):
        if self.closed:
            raise ValueError('The clipboard reading window is closed.')
        value = text_value(self._reader(), 'Clipboard reading text')
        if self.closed:
            raise ValueError('The clipboard reading window closed during capture.')
        self._last = _digest(value)
        return value

    def poll(self):
        if self.closed or not self.enabled:
            return None
        value = text_value(self._reader(), 'Clipboard reading text')
        if self.closed or not self.enabled:
            return None
        digest = _digest(value)
        if digest == self._last:
            return None
        self._last = digest
        return len(value)

    def close(self):
        self.closed, self.enabled, self._last = True, False, None
        self._reader = None


class ClipboardControls:
    """Native controls attached to one existing editable reader window."""
    def __init__(self, wx, dialog, layout, *, reader, initial, apply, alive):
        self.wx, self.apply, self.alive = wx, apply, alive
        self.session = ClipboardSession(reader, initial)
        self.closed = self.loading = False
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.watch = wx.CheckBox(dialog, label='Watch &clipboard in this window')
        self.watch.SetName('Watch clipboard in this reading window; off by default')
        self.watch.SetValue(False)
        self.load = wx.Button(dialog, label='Load current clip&board')
        self.load.SetName('Load current clipboard into this reading copy; confirm discarding edits')
        row.Add(self.watch, 0, wx.ALL, 4)
        row.Add(self.load, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
        self.status = wx.TextCtrl(dialog, value=(
            'Clipboard watch is off. This copy is held in this window. '
            'Save As exports only when you request it.'),
            style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 55))
        self.status.SetName('Clipboard monitoring status; notices never replace reading text')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
        self.timer = wx.Timer(dialog)
        dialog.Bind(wx.EVT_TIMER, self.on_tick, self.timer)
        self.watch.Bind(wx.EVT_CHECKBOX, self.on_toggle)
        self.load.Bind(wx.EVT_BUTTON, self.on_load)
        # Creating the controls does not start the timer or read the clipboard.

    def _active(self):
        if self.closed:
            return False
        if not self.alive():
            self.close()
            return False
        return True

    def _message(self, value):
        if self._active():
            self.status.ChangeValue(value)

    def on_toggle(self, event=None):
        if not self._active():
            return
        enabled = bool(self.watch.GetValue())
        self.session.enable(enabled)
        if enabled:
            self.timer.Start(1000)
            self._message('Watching clipboard until this window closes. Changes are announced only; '
                          'use Load current clipboard to replace this reading copy explicitly.')
        else:
            self.timer.Stop()
            self._message('Clipboard watch is off. The reading copy and its edits are unchanged.')

    def on_tick(self, event=None):
        if not self._active() or self.loading:
            return
        try:
            count = self.session.poll()
            if count is not None:
                self._message(f'Clipboard text changed ({count} characters). '
                              'Your reading copy is unchanged. Use Load current clipboard when ready.')
        except (OSError, ValueError, RuntimeError) as exc:
            self._message(f'Clipboard watch could not read a snapshot. The reading copy is unchanged. {exc}')

    def on_load(self, event=None):
        if not self._active() or self.loading:
            return False
        self.loading = True
        try:
            value = self.session.capture()
            if not self._active():
                return False
            accepted = self.apply(value)
            if accepted:
                self._message('The selected clipboard snapshot is loaded in memory. '
                              'Choose Read explicitly to start speech; Save As explicitly exports a copy.')
            return bool(accepted)
        except (OSError, ValueError, RuntimeError) as exc:
            self._message(f'The clipboard snapshot was not loaded. {exc}')
            return False
        finally:
            self.loading = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.session.close()
        self.apply = self.alive = None
        self.timer.Stop()


def show_clipboard_reader(parent):
    """Open the existing reader with an explicit transient snapshot."""
    import wx
    from .speech_dialog import show_speech_reader

    try:
        if not parent or getattr(parent, '_closing', False):
            return
        value = read_clipboard_text(wx)
        if not parent or getattr(parent, '_closing', False):
            return
        show_speech_reader(parent, text=value, title='Clipboard reading copy',
            recovery_slot=None, clipboard_reader=lambda: read_clipboard_text(wx))
    except Exception as exc:
        if parent and not getattr(parent, '_closing', False):
            parent.report(f'The clipboard reading copy could not be opened. {exc}')
