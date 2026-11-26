"""Windows file-sync requirements exercised without weakening publication."""
import errno
import os
import stat

import pytest

from carr_library.audio.backup import backup_project, restore_project
from carr_library.audio.jobs import publish_new
from carr_library.ui.audio_studio import StudioController


def test_publication_snapshot_and_restore_use_flushable_file_handles(tmp_path, monkeypatch):
    """Use native Windows fsync, or emulate its stricter file API on POSIX."""
    if os.name == 'posix':
        import fcntl

        real_fsync = os.fsync

        def windows_file_fsync(descriptor):
            if (stat.S_ISREG(os.fstat(descriptor).st_mode)
                    and fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY):
                raise OSError(errno.EBADF, 'Windows requires a writable descriptor for file fsync')
            return real_fsync(descriptor)

        monkeypatch.setattr(os, 'fsync', windows_file_fsync)
    source, destination = tmp_path / 'stage', tmp_path / 'accepted'
    source.write_bytes(b'Generated stage remains byte-identical.')
    publish_new(source, destination)
    assert destination.read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        publish_new(source, destination)

    output = tmp_path / 'audio'
    output.mkdir()
    controller = StudioController(private_root=tmp_path / 'private')
    controller.new('Exact source text.\r\nOriginal Unicode: українська.')
    controller.configure(output_dir=output)
    project = tmp_path / 'studio.json'
    controller.save(project)
    controller.edit_script(controller.script + '\nAn explicit reading-copy edit.')
    controller.save_recovery()
    original = project.read_bytes()
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(project, snapshot, project_dir=output)
    restored = restore_project(snapshot, tmp_path / 'restored')
    reopened = StudioController(private_root=tmp_path / 'other-private')
    reopened.open(restored['project_file'])
    assert reopened.draft.source_text == controller.draft.source_text
    reopened.recover()
    assert reopened.script == controller.script
    assert project.read_bytes() == original
