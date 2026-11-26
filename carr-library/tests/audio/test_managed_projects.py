"""Actual project persistence, portable recovery and private namespace boundaries."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

from carr_library.audio.managed import ManagedProjects
from carr_library.ingest.writer_guard import exclusive_writer, WriterBusy
from carr_library.ui.audio_studio import StudioController


def controller(root):
    root.mkdir(parents=True, exist_ok=True)
    value = StudioController(private_root=root)
    value.new('First original.\r\nПривіт, світе.', title='A portable project')
    return value


def test_first_save_is_owned_atomic_and_listed(tmp_path):
    value = controller(tmp_path / 'library')
    saved = value.save_in_library()
    assert saved == value.private_root / 'audio-projects' / value.project_id / 'studio' / 'project.json'
    assert value.output_dir == saved.parent.parent / 'audio'
    assert value.output_dir.is_dir() and not value.dirty
    assert ManagedProjects(value.private_root).projects() == ((value.title, saved),)
    assert not list(saved.parent.glob('.studio-save-*'))
    before = saved.read_bytes()
    value.edit_script('A separate reading version.')
    value.save()
    assert saved.read_bytes() != before
    assert value.draft.source_text == 'First original.\r\nПривіт, світе.'


def test_relocated_project_and_recovery_cannot_write_into_old_library(tmp_path):
    first = controller(tmp_path / 'original-library')
    path = first.save_in_library()
    first.edit_script('An unsaved recovery version.')
    first.save_recovery()
    original_hash = sha256(path.read_bytes()).hexdigest()
    moved = tmp_path / 'restored-library'
    shutil.copytree(first.private_root, moved)
    restored_path = moved / path.relative_to(first.private_root)
    restored = StudioController(private_root=moved)
    restored.open(restored_path)
    assert restored.output_dir == restored_path.parent.parent / 'audio'
    assert restored._file_hash == original_hash
    restored.recover()
    assert restored.script == 'An unsaved recovery version.'
    assert restored.output_dir.is_relative_to(moved)
    restored.save()
    assert sha256(path.read_bytes()).hexdigest() == original_hash
    assert json.loads(restored_path.read_text())['output_dir'] == str(restored.output_dir)


def test_library_writer_and_draft_lock_prevent_partial_saves(tmp_path):
    from filelock import FileLock
    value = controller(tmp_path / 'library')
    path = value.save_in_library()
    old = path.read_bytes()
    value.edit_script('Keep this change pending.')
    with exclusive_writer(value.private_root):
        with pytest.raises(WriterBusy):
            value.save()
    lock = path.parent.parent / '.draft.lock'
    with FileLock(lock).acquire(timeout=0):
        with pytest.raises(RuntimeError, match='saved or backed up'):
            value.save()
    assert path.read_bytes() == old and value.dirty


def test_cancel_and_disk_failure_preserve_committed_project(tmp_path, monkeypatch):
    value = controller(tmp_path / 'library')
    path = value.save_in_library()
    old = path.read_bytes()
    value.edit_script('New draft')
    with pytest.raises(TimeoutError):
        value.save(cancelled=lambda: True)
    def fail_sync(_descriptor):
        raise OSError('Disk full during durable save')
    monkeypatch.setattr('carr_library.audio.managed.os.fsync', fail_sync)
    with pytest.raises(OSError, match='Disk full'):
        value.save()
    assert path.read_bytes() == old and value.dirty
    assert not list(path.parent.glob('.studio-save-*'))


def test_external_modification_and_cross_project_paths_are_rejected(tmp_path):
    value = controller(tmp_path / 'library')
    path = value.save_in_library()
    path.write_text('External edit', encoding='utf-8')
    with pytest.raises(ValueError, match='changed outside'):
        value.save()
    source = value.private_root / 'source.txt'
    source.write_text('Original evidence', encoding='utf-8')
    with pytest.raises(ValueError, match='reserved Audio Studio'):
        value.save(source, overwrite=True)
    other = value.private_root / 'audio-projects' / ('f' * 32) / 'studio' / 'project.json'
    with pytest.raises(ValueError, match='different managed project'):
        value.save(other)
    assert source.read_text() == 'Original evidence'


def test_managed_save_as_detaches_output_without_touching_original(tmp_path):
    value = controller(tmp_path / 'library')
    path = value.save_in_library()
    original = path.read_bytes()
    outside = tmp_path / 'standalone.json'
    value.save(outside)
    assert path.read_bytes() == original
    assert value.output_dir is None and json.loads(outside.read_text())['output_dir'] is None
    value.configure(output_dir=tmp_path)


def test_private_paths_are_not_external_audio_destinations(tmp_path):
    value = controller(tmp_path / 'library')
    path = value.save_in_library()
    value.configure(output_dir=value.output_dir)
    for target in (value.private_root, path.parent, tmp_path):
        with pytest.raises(ValueError):
            value.configure(output_dir=target)
    original = value.output_dir
    with pytest.raises(TimeoutError):
        value.save_in_library(cancelled=lambda: True)
    assert value.output_dir == original


def test_managed_hardlinked_document_rejected(tmp_path):
    value = controller(tmp_path / 'library')
    path = value.save_in_library()
    os.link(path, tmp_path / 'alias.json')
    with pytest.raises(ValueError):
        value.save()
    with pytest.raises(ValueError):
        StudioController(private_root=value.private_root).open(path)


def test_project_id_must_match_saved_directory_before_state_changes(tmp_path):
    first = controller(tmp_path / 'library')
    path = first.save_in_library()
    data = json.loads(path.read_text())
    data['project_id'] = 'e' * 32
    path.write_text(json.dumps(data), encoding='utf-8')
    opened = StudioController(private_root=first.private_root)
    old = opened.to_dict()
    with pytest.raises(ValueError, match='different managed project'):
        opened.open(path)
    assert opened.to_dict() == old
