"""Bind native overwrite confirmation to one inspected export destination."""
from ..research.export_file import capture_export_destination


def confirm_export_destination(parent, path, *, private_root, wx_module=None,
                               title='Replace export'):
    """Capture before confirmation; the writer rechecks this exact snapshot."""
    approval = capture_export_destination(path, private_root=private_root)
    if not approval.require_absent:
        if wx_module is None:
            import wx as wx_module
        if wx_module.MessageBox(
                f'Replace the existing export {approval.target.name}?', title,
                wx_module.YES_NO | wx_module.NO_DEFAULT | wx_module.ICON_QUESTION,
                parent) != wx_module.YES:
            return None
    return approval
