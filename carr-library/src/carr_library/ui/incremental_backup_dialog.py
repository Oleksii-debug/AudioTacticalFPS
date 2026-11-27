"""Native entry to the existing canonical backup repository service.

The shared frame owns the worker, cancellation and progress. A captured library
object and root remain authoritative across queued work and late callbacks.
"""
from datetime import datetime
from pathlib import Path

from .. import incremental_backup as repository

ACTIONS = (
    ('init', 'Initialize a new local repository'),
    ('create', 'Create snapshot of all libraries and retained inputs'),
    ('list', 'List completed snapshot identities (payloads not verified)'),
    ('verify', 'Verify a snapshot using a separately saved SHA256'),
    ('materialize', 'Export a snapshot as an ordinary full backup'),
    ('restore', 'Restore a snapshot to a new library folder'),
)
COMMAND_ID = 'library.incremental_backup'


def _directory(frame, title):
    import wx
    dialog = wx.DirDialog(frame, title, style=wx.DD_DIR_MUST_EXIST)
    try:
        return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def _text(frame, title, message, value=''):
    import wx
    dialog = wx.TextEntryDialog(frame, message, title, value, style=wx.OK | wx.CANCEL)
    dialog.SetName(title)
    try:
        return dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def _action(frame):
    import wx
    dialog = wx.SingleChoiceDialog(frame,
        'Choose one operation. Repositories are local and created only by Initialize. '
        'Every restore uses the existing complete-library backup validator.',
        'Incremental backup repository', [label for _, label in ACTIONS])
    dialog.SetName('Incremental backup action')
    try:
        return ACTIONS[dialog.GetSelection()][0] if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def _new_folder(frame, title, default):
    parent = _directory(frame, title)
    if parent is None:
        return None
    name = _text(frame, 'New folder name',
                 'Enter one new folder name. Existing folders and their data are never overwritten.', default)
    if name is None:
        return None
    name = name.strip()
    parsed = repository._name(name)
    if len(parsed.parts) != 1:
        raise ValueError('Choose one new folder name, not a path.')
    return parent / name


def _result_text(command, result):
    if command == 'list':
        lines = ['These are committed descriptor identities, not a verification of their payloads.',
                 'Keep each snapshot SHA256 separately from the repository.',
                 'Unmarked interrupted reservations are not completed snapshots.', '']
        for row in result:
            lines += [row['created_at'], 'Snapshot SHA256: ' + row['snapshot_sha256'],
                      'Canonical manifest SHA256: ' + row['backup_manifest_sha256'],
                      f"Files: {row['files']}; bytes: {row['payload_bytes']}", '']
        if any(row.get('older_history_unavailable') for row in result):
            lines.append('Older parent metadata is unavailable. A separately saved child token still restores that full snapshot.')
        if not result:
            lines.append('No completed snapshots are present.')
        return '\n'.join(lines)
    if command == 'init':
        return ('Local backup repository created:\n' + result['path'] +
                '\n\nChoose Create snapshot to save data. Initialization alone does not back up a library.')
    lines = []
    if command == 'create':
        lines += ['Snapshot published.', 'Snapshot SHA256: ' + result['snapshot_sha256'],
                  'Canonical manifest SHA256: ' + result['backup_manifest_sha256'],
                  f"New chunks: {result['new_chunks']} ({result['new_bytes']} bytes).",
                  f"Reused chunks: {result['reused_chunks']} ({result['reused_bytes']} bytes).",
                  'Keep both checksums separately. Every retained payload was read and verified.',
                  'Includes the same typed state and retained inputs as the complete-library backup. '
                  'Unmanaged external files and credentials are excluded.']
    elif command == 'verify':
        lines += ['All snapshot payloads and the complete canonical library backup were verified.',
                  'Snapshot SHA256: ' + result['snapshot_sha256'],
                  'Canonical manifest SHA256: ' + result['backup_manifest_sha256']]
    elif command == 'materialize':
        lines += ['Ordinary full backup published:\n' + result['path'],
                  'Keep its manifest checksum separately:\n' + result['backup_manifest_sha256']]
    else:
        lines += ['Library backup restored:\n' + result['path'],
                  'Open it explicitly through Library, Open existing library folder.',
                  'Restoration does not resume imports, contact providers or open old Studio outputs.']
    if result.get('resource_cleanup_pending'):
        lines.append('The snapshot was saved; its resource reservation needs review before another operation.')
    if result.get('staging_cleanup_pending'):
        lines += ['The operation completed, but its temporary verification copy could not be removed.',
                  'Retained temporary folder: ' + result['retained_temporary_directory']]
    if result.get('durability_confirmed') is False:
        lines.append('The snapshot or full backup was published, but disk synchronization did not confirm durability. '
                     'Keep its identity and verify it before relying on it.')
    return '\n\n'.join(lines)


def show_incremental_backup(frame, event=None):
    if getattr(frame, '_closing', False):
        return
    if getattr(frame, 'working', False):
        frame.report('An operation is already running. Wait or choose Cancel current operation.')
        return
    library = frame.library
    root = Path(library.root)

    def current():
        return (not getattr(frame, '_closing', False) and frame.library is library
                and Path(library.root) == root)

    def stopped():
        return not current() or frame.cancel_event.is_set()

    try:
        command = _action(frame)
        if command is None:
            return
        if command == 'init':
            selected = _new_folder(frame, 'Choose a parent folder outside the library', 'Resonance-backups')
            if selected is None:
                return
            operation = lambda: repository.initialize_repository(selected, cancelled=stopped)
        else:
            selected = _directory(frame, 'Select an existing incremental backup repository')
            if selected is None:
                return
            if command == 'create':
                parent = _text(frame, 'Optional parent snapshot',
                    'Paste a separately saved predecessor SHA256, or leave empty for an independent first snapshot. '
                    'For a nonempty repository this must match its current snapshot. The predecessor is fully verified; each new snapshot restores independently.')
                if parent is None:
                    return
                parent = parent.strip() or None
                if parent is not None:
                    repository._digest(parent)
                operation = lambda: repository.create_snapshot(library, selected, parent=parent,
                                                                cancelled=stopped)
            elif command == 'list':
                operation = lambda: repository.list_snapshots(selected, cancelled=stopped)
            else:
                checksum = _text(frame, 'Independently saved snapshot SHA256',
                    'Paste the snapshot SHA256 retained separately when it was created. '
                    'A listing alone does not establish trust in a changed repository.')
                if checksum is None:
                    return
                checksum = checksum.strip()
                repository._digest(checksum)
                if command == 'verify':
                    operation = lambda: repository.verify_snapshot(selected, checksum, cancelled=stopped)
                else:
                    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                    destination = _new_folder(frame, 'Choose a parent folder for the new destination',
                        ('Resonance-full-backup-' if command == 'materialize' else 'Resonance-restored-') + stamp)
                    if destination is None:
                        return
                    if command == 'materialize':
                        operation = lambda: repository.materialize_snapshot(selected, checksum, destination,
                                                                             cancelled=stopped)
                    else:
                        operation = lambda: repository.restore_snapshot(selected, checksum, destination,
                                                                        cancelled=stopped)
        if not current():
            raise InterruptedError('The selected library changed before the backup operation started.')

        def work():
            if stopped():
                raise InterruptedError('The selected library closed or changed before backup execution.')
            return operation()

        def done(result):
            # Late Cancel cannot turn a committed result into a cancelled result.
            # A replacement/closed frame must not receive results from the old scope.
            if current():
                from .desktop_app import show_text
                show_text(frame, 'Incremental backup result', _result_text(command, result))

        return frame.run_job(dict(ACTIONS)[command], work, done)
    except (OSError, ValueError, RuntimeError) as exc:
        if current():
            frame.report(str(exc))


def install_incremental_backup(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    index = frame.menu_bar.FindMenu('Library')
    if index == wx.NOT_FOUND:
        raise ValueError('The Library menu is required for incremental backups.')
    menu = frame.menu_bar.GetMenu(index)
    item = menu.Append(wx.ID_ANY, 'Incremental backup repository...')
    frame.Bind(wx.EVT_MENU, lambda event: show_incremental_backup(frame, event), item)
    register_menu_command(frame, CommandSpec(COMMAND_ID, 'Incremental backup repository', 'main', ''), item)
    return item
