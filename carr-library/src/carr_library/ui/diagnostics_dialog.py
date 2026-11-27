"""Native redacted preview and explicit export through the existing safe writer."""
from hashlib import sha256
from pathlib import Path

from ..diagnostics import collect_diagnostics
from ..research.export_file import write_binary_export
from .productivity import _plain


class DiagnosticsPreview:
    def __init__(self, frame, payload):
        if type(payload) is not bytes or not 0 < len(payload) <= 128 * 1024:
            raise ValueError('A bounded diagnostics preview is required.')
        self.text = payload.decode('utf-8', errors='strict')
        self.payload = payload
        self.frame, self.library = frame, frame.library
        self.root = Path(self.library.root)
        self.closed = False

    def guard(self):
        if (self.closed or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library
                or Path(self.frame.library.root) != self.root):
            raise ValueError('The library or preview changed. Open diagnostics again.')

    def export(self, path, *, cancelled=lambda: False):
        self.guard()
        def stopped():
            if cancelled():
                return True
            self.guard()
            return False
        write_binary_export(path, self.payload, private_root=self.root,
                            cancelled=stopped, require_absent=True)
        return {'bytes': len(self.payload), 'sha256': sha256(self.payload).hexdigest()}

    def close(self):
        self.closed = True


def show_diagnostics_preview(frame, payload, *, on_ready=None):
    import wx
    controller = DiagnosticsPreview(frame, payload)
    controller.guard()
    dialog = wx.Dialog(frame, title='Redacted diagnostics — preview before export', size=(830, 650),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.controller = controller
    layout = wx.BoxSizer(wx.VERTICAL)
    explanation = wx.StaticText(dialog, label=
        'This is the exact report that will be saved. It contains allowlisted package and system versions '
        'and an isolated synthetic installation check. User documents, paths, accounts, credentials and raw logs '
        'are omitted. Choose a new output file; existing files are never replaced.')
    explanation.Wrap(770)
    layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
    dialog.preview = wx.TextCtrl(dialog, value=controller.text, style=wx.TE_MULTILINE | wx.TE_READONLY)
    dialog.preview.SetName('Exact redacted diagnostics report to export')
    layout.Add(dialog.preview, 1, wx.EXPAND | wx.ALL, 8)
    dialog.status = wx.TextCtrl(dialog, value='Review the report before choosing Export.',
                               style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 65))
    dialog.status.SetName('Diagnostics export status')
    layout.Add(dialog.status, 0, wx.EXPAND | wx.ALL, 8)
    row = wx.BoxSizer(wx.HORIZONTAL)
    dialog.export_button = wx.Button(dialog, label='&Export exact preview...')
    dialog.export_button.SetName('Export the exact redacted diagnostics preview')
    row.Add(dialog.export_button, 0, wx.ALL, 8)
    row.Add(wx.Button(dialog, wx.ID_CANCEL, '&Close'), 0, wx.ALL, 8)
    layout.Add(row, 0, wx.ALIGN_RIGHT)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    def export(event=None):
        try:
            controller.guard()
            picker = wx.FileDialog(dialog, 'Save diagnostics to a new file',
                defaultFile='Resonance-diagnostics.json', wildcard='Diagnostics JSON (*.json)|*.json',
                style=wx.FD_SAVE)
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                selected = Path(picker.GetPath())
            finally:
                picker.Destroy()
            controller.guard()
            evidence = controller.export(selected)
            dialog.status.ChangeValue(
                f'Exact preview saved: {evidence["bytes"]} bytes. SHA-256 {evidence["sha256"]}')
        except Exception as exc:
            dialog.status.ChangeValue('Diagnostics were not exported: ' + str(exc))
        dialog.status.SetFocus()
    dialog.export_button.Bind(wx.EVT_BUTTON, export)
    dialog.preview.SetInsertionPoint(0)
    dialog.preview.SetFocus()
    try:
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog)
        dialog.ShowModal()
    finally:
        controller.close()
        dialog.Destroy()


class DiagnosticsActions:
    def __init__(self, frame):
        self.frame = frame

    def show(self, event=None, *, on_ready=None):
        frame = self.frame
        if getattr(frame, '_closing', False):
            return False
        if frame.working or getattr(frame, 'busy', False):
            frame.report('Wait for the current operation before running diagnostics.')
            return False
        library, root = frame.library, Path(frame.library.root)
        def done(payload):
            if (getattr(frame, '_closing', False) or frame.library is not library
                    or Path(frame.library.root) != root):
                raise ValueError('The library changed. Open diagnostics again.')
            show_diagnostics_preview(frame, payload, on_ready=on_ready)
        frame.run_job('Preparing redacted diagnostics',
            lambda: collect_diagnostics(cancelled=frame.cancel_event.is_set), done)
        return True


def install_diagnostics(frame):
    import wx
    current = getattr(frame, 'diagnostics_actions', None)
    if current is not None:
        return current
    if getattr(frame, 'keyboard_actions', None) is not None:
        raise ValueError('Install diagnostics before capturing keyboard shortcuts.')
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if _plain(frame.menu_bar.GetMenuLabel(index)).casefold() == 'help'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Help')
    actions = DiagnosticsActions(frame)
    item = menu.Append(wx.ID_ANY, 'Diagnostics preview and export...')
    frame.Bind(wx.EVT_MENU, actions.show, item)
    frame.diagnostics_actions = actions
    return actions
