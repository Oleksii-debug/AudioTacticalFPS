"""Bind a native overwrite answer to one inspected destination state."""
from ..research.export_file import capture_export_destination


def confirm_export_destination(parent, path, *, private_root, title='Replace export file'):
    """Return the captured destination, or None when replacement is declined.

    Call before rendering or queuing an export and pass this same snapshot to
    the writer as ``approval``. This helper never renders or publishes content.
    """
    approval = capture_export_destination(path, private_root=private_root)
    if approval.require_absent:
        return approval
    import wx

    dialog = wx.MessageDialog(parent,
        f'Replace the existing file {approval.target.name}?', title,
        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
    try:
        return approval if dialog.ShowModal() == wx.ID_YES else None
    finally:
        dialog.Destroy()
