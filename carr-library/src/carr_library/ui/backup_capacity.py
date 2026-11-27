"""Explicit capacity selection on the existing full-backup worker.

Selection grants a finite resource allowance only. Manifest/source validation,
fresh destinations and the canonical publication boundary remain authoritative.
"""
from datetime import datetime
from pathlib import Path

from ..backup_capacity import LARGE_SOURCE_PROFILE
from ..durable_state import local_path, regular

CHOICES = (
    'Ordinary library: up to 10,000 original files; database up to 512 MiB',
    'Large library: up to 100,000 original files; database up to 64 GiB',
)


def choose_backup_capacity(parent):
    import wx
    dialog = wx.SingleChoiceDialog(parent,
        'Choose the size limit for this backup or restore. Large libraries can take more time and disk space. '
        'Choose the same option when restoring a large backup. '
        'Complete backups include unfinished imports and their saved originals.',
        'Complete-backup capacity', CHOICES)
    try:
        dialog.SetName('Complete-backup capacity')
        dialog.SetSelection(0)
        if dialog.ShowModal() != wx.ID_OK:
            return False, None
        selected = dialog.GetSelection()
        if type(selected) is not int or selected not in (0, 1):
            raise ValueError('Choose an explicit complete-backup capacity profile.')
        return True, None if selected == 0 else LARGE_SOURCE_PROFILE
    finally:
        dialog.Destroy()


def _bound(frame):
    library = frame.library
    root = local_path(library.root, exists=True)
    info = regular(root, directory=True)
    identity = info.st_dev, info.st_ino
    def current():
        if getattr(frame, '_closing', False) or frame.library is not library:
            return False
        try:
            if local_path(library.root, exists=True) != root:
                return False
            observed = regular(root, directory=True)
            return (observed.st_dev, observed.st_ino) == identity
        except (OSError, ValueError):
            return False
    def stopped():
        return not current() or frame.cancel_event.is_set()
    return library, current, stopped


def _directory(frame, title):
    import wx
    dialog = wx.DirDialog(frame, title)
    try:
        return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def _checksum(frame):
    from .desktop_app import text_dialog
    return text_dialog(frame, 'Verify backup',
        'Paste the manifest checksum saved separately when this backup was created.')


def _show(frame, title, text):
    from .desktop_app import show_text
    show_text(frame, title, text)


def show_complete_backup(frame, event=None, *, restore=False):
    """Use the host worker; do not create an executor, scheduler or service."""
    if getattr(frame, '_closing', False):
        return
    if getattr(frame, 'working', False):
        frame.report('An operation is already running. Wait or choose Cancel current operation.')
        return
    try:
        library, current, stopped = _bound(frame)
    except (OSError, ValueError, RuntimeError) as error:
        frame.report(str(error))
        return
    try:
        accepted, profile = choose_backup_capacity(frame)
        if not accepted or not current():
            return
        if restore:
            source = _directory(frame, 'Select the backup folder')
            if source is None or not current():
                return
            checksum = _checksum(frame)
            if checksum is None or not current():
                return
            parent = _directory(frame, 'Choose a parent folder for the restored libraries')
            prefix = 'ResearchLibrary-restored-'
        else:
            parent = _directory(frame,
                'Choose a parent folder for a complete backup, including validated pending imports')
            prefix = 'ResearchLibrary-backup-'
        if parent is None or not current():
            return
        destination = parent / (prefix + datetime.now().strftime('%Y%m%d-%H%M%S'))
        def work():
            if stopped():
                raise InterruptedError('The selected library closed, changed or cancelled before backup execution.')
            if restore:
                from ..library_backup import restore_backup
                return restore_backup(source, destination, expected_manifest_sha256=checksum.strip(),
                                      capacity_profile=profile, cancelled=stopped)
            return library.backup(destination, capacity_profile=profile, cancelled=stopped)
        def failed(error):
            if current():
                frame.report(str(error))
        def done(result):
            # A late Cancel does not relabel a returned committed backup.
            if not current():
                return
            if not restore:
                _show(frame, 'Backup complete',
                    f'Backup folder: {destination}\n\n'
                    'All libraries, original files, saved work, reading positions, bookmarks, '
                    'unfinished imports and their saved originals and reviews are included.\n\n'
                    f'Selected library size: {"Large library" if profile == LARGE_SOURCE_PROFILE else "Ordinary library"}.\n'
                    f'Keep this checksum separately; it is needed to verify a restore:\n{result}')
                return
            import wx
            answer = wx.MessageBox('Backup restored successfully. Open the restored libraries now?',
                'Restore complete', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, frame)
            if not current():
                return
            if answer == wx.YES:
                frame._open_data_root(result)
            else:
                _show(frame, 'Restore complete', f'Restored library folder: {result}\n\n'
                    'You can open it later through Library, Open existing library folder.')
        return frame.run_job('Restoring backup' if restore else 'Backing up all libraries',
                             work, done, failed)
    except (OSError, ValueError, RuntimeError) as error:
        if current():
            frame.report(str(error))
