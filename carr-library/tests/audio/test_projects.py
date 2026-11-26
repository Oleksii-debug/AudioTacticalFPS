from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.audio.contracts import parse_script
from carr_library.audio.project import ProjectStore, read_json, restore_project


def project():
    return parse_script("Author: Source stays readable.\n", [SimpleNamespace(provider="flite", id="slt")])


def test_atomic_save_reopen_and_stale_writer_preserves_draft(tmp_path):
    first = ProjectStore.create(tmp_path / "проєкт", project())
    a, b = first.load(), first.load()
    first.save(replace(a.project, title="First change"), expected_sha256=a.file_sha256)
    with pytest.raises(ValueError, match="changed on disk"):
        first.save(replace(b.project, title="Unsaved change"), expected_sha256=b.file_sha256)
    assert first.load().project.title == "First change"
    assert b.project.source_text == project().source_text


def test_create_never_adopts_existing_folder(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    source = target / "keep.txt"
    source.write_text("Keep this.")
    with pytest.raises(FileExistsError):
        ProjectStore.create(target, project())
    assert source.read_text() == "Keep this."


def test_portable_project_restore_requires_checksum_and_new_destination(tmp_path):
    store = ProjectStore.create(tmp_path / "source", project())
    checksum = store.backup(tmp_path / "backup", include_audio=False)
    restored = restore_project(tmp_path / "backup", tmp_path / "restored", manifest_sha256=checksum)
    assert restored.load().project == store.load().project
    with pytest.raises(FileExistsError):
        restore_project(tmp_path / "backup", tmp_path / "restored", manifest_sha256=checksum)
    with pytest.raises(ValueError, match="checksum"):
        restore_project(tmp_path / "backup", tmp_path / "wrong", manifest_sha256="0" * 64)
    assert not (tmp_path / "wrong").exists()


def test_corrupt_backup_never_publishes_destination(tmp_path):
    store = ProjectStore.create(tmp_path / "source", project())
    checksum = store.backup(tmp_path / "backup")
    (tmp_path / "backup" / "project.json").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="Corrupted"):
        restore_project(tmp_path / "backup", tmp_path / "restored", manifest_sha256=checksum)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{bad', b'\xff'])
def test_json_duplicates_nonfinite_and_invalid_encoding_fail(tmp_path, raw):
    path = tmp_path / "data.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read_json(path)
