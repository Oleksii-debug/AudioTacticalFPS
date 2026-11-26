"""Native publication guarantees on the actual executing operating system."""
from pathlib import Path

import pytest

from carr_library.file_publication import publish_directory_no_replace


def test_actual_native_rename_publishes_complete_tree(tmp_path):
    stage, target = tmp_path / 'stage', tmp_path / 'target'
    stage.mkdir()
    (stage / 'complete').write_bytes(b'checked result')
    publish_directory_no_replace(stage, target)
    assert not stage.exists()
    assert (target / 'complete').read_bytes() == b'checked result'


@pytest.mark.parametrize('kind', ['empty_directory', 'nonempty_directory', 'file'])
def test_actual_native_rename_preserves_every_existing_destination(tmp_path, kind):
    stage, target = tmp_path / 'stage', tmp_path / 'target'
    stage.mkdir()
    (stage / 'complete').write_bytes(b'new result')
    if kind == 'file':
        target.write_bytes(b'keep file')
    else:
        target.mkdir()
        if kind == 'nonempty_directory':
            (target / 'keep').write_bytes(b'keep tree')
    identity = target.stat().st_ino
    with pytest.raises(FileExistsError):
        publish_directory_no_replace(stage, target)
    assert (stage / 'complete').read_bytes() == b'new result'
    assert target.stat().st_ino == identity
    if kind == 'file':
        assert target.read_bytes() == b'keep file'
    elif kind == 'nonempty_directory':
        assert (target / 'keep').read_bytes() == b'keep tree'
    else:
        assert list(target.iterdir()) == []

