"""Native read-only diagnostic preview and explicit protected text export."""
from ..diagnostics import collect_diagnostics, save_diagnostic_preview
from .command_registry import CommandSpec


DIAGNOSTICS_COMMAND = CommandSpec(
    'help.diagnostics', 'Preview and export diagnostics', 'main', '')


def show_diagnostics(frame):
    import wx
    library = frame.library
    preview = collect_diagnostics(library.root / 'state')
    dialog = wx.Dialog(frame, title='Diagnostics preview and export', size=(760, 650),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    explanation = wx.StaticText(dialog, label=(
        'Review the exact report below before saving. Saving creates a local text file; '
        'you decide whether to share it.'))
    explanation.Wrap(700)
    layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
    layout.Add(wx.StaticText(dialog, label='Diagnostic &preview'), 0, wx.LEFT | wx.RIGHT, 8)
    content = wx.TextCtrl(dialog, value=preview.text, style=wx.TE_MULTILINE | wx.TE_READONLY)
    content.SetName('Diagnostics preview')
    layout.Add(content, 1, wx.EXPAND | wx.ALL, 8)
    status = wx.TextCtrl(dialog, value='Ready for review. No file has been saved.',
                         style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 64))
    status.SetName('Diagnostic export status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 8)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    save = wx.Button(dialog, label='&Save as...')
    save.SetName('Save diagnostics as')
    close = wx.Button(dialog, id=wx.ID_CANCEL, label='&Close')
    close.SetName('Close diagnostics')
    buttons.Add(save, 0, wx.ALL, 4)
    buttons.Add(close, 0, wx.ALL, 4)
    layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
    dialog.SetSizer(layout)
    dialog.SetMinSize((500, 400))
    dialog.SetEscapeId(wx.ID_CANCEL)

    def report(message):
        status.ChangeValue(message)
        status.SetInsertionPoint(0)
        status.SetFocus()

    def changed():
        return frame.library is not library or getattr(frame, '_closing', False)

    def choose_destination():
        chooser = wx.FileDialog(dialog, 'Save diagnostics report',
            defaultFile='resonance-diagnostics.txt',
            wildcard='Text report (*.txt)|*.txt', style=wx.FD_SAVE)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return None
            return chooser.GetPath()
        finally:
            chooser.Destroy()

    def confirm_replacement():
        question = wx.MessageDialog(dialog,
            'Replace the existing file with this reviewed diagnostic report?',
            'Replace diagnostic report',
            style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            return question.ShowModal() == wx.ID_YES
        finally:
            question.Destroy()

    def on_save(event=None):
        try:
            receipt = save_diagnostic_preview(preview, private_root=library.root,
                choose_destination=choose_destination, confirm_replacement=confirm_replacement,
                cancelled=changed)
            if receipt is None:
                report('Save cancelled. No diagnostic file was written.')
            elif receipt.cleanup_warning:
                report('Diagnostics saved. Temporary-file cleanup is still pending.')
            else:
                report('Diagnostics saved. The report was not sent anywhere.')
        except TimeoutError:
            report('Save cancelled. Existing files were kept.')
        except FileExistsError:
            report('Not saved: the destination changed or already exists. Choose another file.')
        except PermissionError:
            report('Not saved: access to the selected destination was denied.')
        except ValueError:
            report('Not saved: the destination is protected or changed. Choose another file.')
        except OSError:
            report('Not saved: the write could not finish. Check free space and folder permissions.')
        except Exception:
            # Status is safe to copy too: never echo exception paths or data.
            report('Diagnostic export is unavailable. The preview remains available for review.')

    def on_close(event=None):
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)

    save.Bind(wx.EVT_BUTTON, on_save)
    close.Bind(wx.EVT_BUTTON, on_close)
    dialog.Bind(wx.EVT_CLOSE, on_close)
    close.SetDefault()
    content.SetInsertionPoint(0)
    content.SetFocus()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def install_diagnostic_actions(frame):
    import wx
    from .keyboard_settings import register_menu_command
    bar = frame.GetMenuBar()
    index = next((i for i in range(bar.GetMenuCount())
                  if bar.GetMenuLabel(i).replace('&', '') == 'Help'), None)
    if index is None:
        raise ValueError('The native Help menu is required for diagnostics.')
    item = bar.GetMenu(index).Append(wx.ID_ANY, '&Diagnostic preview and export...')
    frame.Bind(wx.EVT_MENU, lambda event: show_diagnostics(frame), item)
    register_menu_command(frame, DIAGNOSTICS_COMMAND, item)
