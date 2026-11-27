"""Bind native export confirmation to the exact selected destination.

The file picker chooses a path. The existing research export publisher checks
the same immutable destination snapshot after any queued rendering. A new path
always keeps no-clobber semantics, even if another file appears while waiting.
"""
from ..research.export_file import capture_export_destination


def _confirm_replace(parent, destination):
    import wx

    question = wx.MessageDialog(
        parent, f'Replace the existing export file?\n{destination}',
        'Replace export file',
        style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
    try:
        return question.ShowModal() == wx.ID_YES
    finally:
        question.Destroy()


def approve_export_destination(parent, destination, *, private_root, confirm=None):
    """Capture before asking; return None on decline, otherwise the same snapshot.

    The optional confirm native-dialog adapter must return True for affirmative
    consent. Capturing a snapshot alone grants no overwrite consent. Native
    callers pass the result unchanged to the existing export writer.
    """
    approval = capture_export_destination(destination, private_root=private_root)
    if not approval.require_absent:
        ask = _confirm_replace if confirm is None else confirm
        if ask(parent, approval.target) is not True:
            return None
    return approval
