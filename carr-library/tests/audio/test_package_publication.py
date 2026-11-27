"""Actual filesystem controls for reviewed reference-package publication."""
from hashlib import sha256
import os
from pathlib import Path
import threading

import pytest

from carr_library.audio import package_publication, process
from carr_library.audio.models import AudioCancelled, AudioError
from carr_library.studio.publishing import capture_publication_destination


@pytest.fixture
def selected(tmp_path):
    private, output = tmp_path / 'library', tmp_path / 'exports'
    private.mkdir()
    output.mkdir()
    stage = output / '.owned-stage'
    stage.mkdir()
    staged = stage / 'profile.dordiai-voice'
    raw = b'actual synthetic reviewed package bytes\n' * 1024
    staged.write_bytes(raw)
    target = output / 'selected.dordiai-voice'
    approval = capture_publication_destination(target, private_root=private)
    return private, staged, target, raw, approval


def publish(selected, **kwargs):
    private, staged, target, raw, approval = selected
    return package_publication.publish_package(staged, target, private_root=private,
        expected_sha256=sha256(raw).hexdigest(), maximum_bytes=len(raw),
        approval=approval, **kwargs)


def test_publishes_only_exact_reviewed_stage_and_returns_current_export_receipt(selected):
    _, staged, target, raw, _ = selected
    receipt = publish(selected)
    assert receipt.path == target and receipt.cleanup_warning is None
    assert target.read_bytes() == raw
    assert os.path.samefile(staged, target)
    assert staged.stat().st_nlink == target.stat().st_nlink == 2


def test_fsync_equal_size_mutation_cannot_publish_stale_checksum(selected, monkeypatch):
    _, staged, target, raw, _ = selected
    original, changed = process.os.fsync, []
    owner = staged.stat()
    def fsync(descriptor):
        original(descriptor)
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (owner.st_dev, owner.st_ino):
            staged.write_bytes(b'X' * len(raw))
            os.utime(staged, ns=(owner.st_atime_ns, owner.st_mtime_ns))
            changed.append(True)
    monkeypatch.setattr(process.os, 'fsync', fsync)
    with pytest.raises(AudioError, match='package changed|inspected bytes'):
        publish(selected)
    assert changed == [True] and not target.exists()


def test_last_precommit_parent_replacement_fails_without_publishing(selected, monkeypatch):
    _, staged, target, raw, _ = selected
    original = package_publication.publish_file
    previous = target.parent.with_name('old-reviewed-directory')
    boundaries = []
    def publisher(source, destination, *, cancelled):
        def check():
            boundaries.append(True)
            if len(boundaries) == 2:
                target.parent.rename(previous)
                target.parent.mkdir()
            return cancelled()
        return original(source, destination, cancelled=check)
    monkeypatch.setattr(package_publication, 'publish_file', publisher)
    with pytest.raises((AudioError, ValueError, FileNotFoundError)):
        publish(selected)
    assert boundaries == [True, True]
    assert not target.exists() and not (previous / target.name).exists()
    assert (previous / staged.parent.name / staged.name).read_bytes() == raw


def test_competing_target_is_preserved_without_reading_its_bytes(selected, monkeypatch):
    _, _, target, _, _ = selected
    original = process.os.link
    def compete(source, destination, **kwargs):
        target.write_bytes(b'unrelated new occupant')
        return original(source, destination, **kwargs)
    monkeypatch.setattr(process.os, 'link', compete)
    original_digest = package_publication._digest
    def digest(path, *args, **kwargs):
        assert Path(path) != target, 'foreign target bytes must not be read'
        return original_digest(path, *args, **kwargs)
    monkeypatch.setattr(package_publication, '_digest', digest)
    with pytest.raises(FileExistsError):
        publish(selected)
    assert target.read_bytes() == b'unrelated new occupant'


def test_postlink_equal_size_mutation_is_retained_but_never_claimed_verified(selected, monkeypatch):
    _, staged, target, raw, _ = selected
    original, owner = process.os.link, staged.stat()
    def link(source, destination, **kwargs):
        original(source, destination, **kwargs)
        staged.write_bytes(b'Y' * len(raw))
        os.utime(staged, ns=(owner.st_atime_ns, owner.st_mtime_ns))
    monkeypatch.setattr(process.os, 'link', link)
    with pytest.raises(AudioError, match='final identity could not be confirmed'):
        publish(selected)
    assert target.read_bytes() == b'Y' * len(raw)
    assert os.path.samefile(staged, target)


def test_third_link_never_qualifies_as_this_exact_publication_pair(selected, monkeypatch):
    _, staged, target, _, _ = selected
    original = process.os.link
    foreign = target.with_name('unexpected-third-link')
    def link(source, destination, **kwargs):
        original(source, destination, **kwargs)
        original(source, foreign, **kwargs)
    monkeypatch.setattr(process.os, 'link', link)
    with pytest.raises(AudioError, match='final identity could not be confirmed'):
        publish(selected)
    assert staged.stat().st_nlink == target.stat().st_nlink == foreign.stat().st_nlink == 3


@pytest.mark.parametrize('late', ['cancel', 'directory-error'])
def test_exact_committed_package_survives_late_cancel_or_finalization_error(selected, monkeypatch, late):
    _, _, target, raw, _ = selected
    stopped = threading.Event()
    original = process.fsync_directory
    def sync(path):
        original(path)
        stopped.set()
        if late == 'directory-error':
            raise OSError('synthetic late directory finalization failure')
    monkeypatch.setattr(process, 'fsync_directory', sync)
    receipt = publish(selected, cancelled=stopped.is_set)
    assert stopped.is_set() and receipt.path == target
    assert target.read_bytes() == raw and sha256(raw).hexdigest() == sha256(target.read_bytes()).hexdigest()
    if late == 'directory-error':
        assert 'saved and verified' in receipt.cleanup_warning
    else:
        assert receipt.cleanup_warning is None


def test_precommit_cancel_preserves_absent_output(selected):
    _, _, target, _, _ = selected
    with pytest.raises(AudioCancelled):
        publish(selected, cancelled=lambda: True)
    assert not target.exists()
