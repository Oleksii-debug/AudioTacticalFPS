"""Explicit selection of the existing complete-backup byte/record contract."""
from ..backup_capacity import LARGE_SOURCE_PROFILE

CHOICES = (
    'Ordinary backup: existing 512 MiB database and import-journal allowance',
    'Large originals and catalog: named large-source-v1 profile, including saved large TXT/PDF requests',
)


def choose_backup_capacity(parent):
    """Return (accepted, profile); cancelling never means ordinary approval."""
    import wx
    dialog = wx.SingleChoiceDialog(parent,
        'Choose the capacity profile for this complete backup. The large profile is required '
        'for explicitly prepared large TXT/PDF requests. Individual audio and other sidecar limits remain unchanged.',
        'Complete-backup capacity', CHOICES)
    try:
        dialog.SetSelection(0)
        if dialog.ShowModal() != wx.ID_OK:
            return False, None
        selected = dialog.GetSelection()
        if type(selected) is not int or selected not in (0, 1):
            raise ValueError('Choose an explicit complete-backup capacity profile.')
        return True, None if selected == 0 else LARGE_SOURCE_PROFILE
    finally:
        dialog.Destroy()
