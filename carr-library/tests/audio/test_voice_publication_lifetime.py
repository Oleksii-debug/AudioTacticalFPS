"""Real create-only publication and retained native cleanup, without model inference."""
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from carr_library.audio import package_publication, process
from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.models import AudioCancelled, AudioCleanupPending
from carr_library.audio.voice_transport import retained_context, retained_temporary_directory
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest
from carr_library.audio.package_publication import capture_package_destination


def prepare(tmp_path):
    private = tmp_path / 'library'
    (private / 'staging').mkdir(parents=True)
    staged = private / 'staging/reference.zip'
    staged.write_bytes(b'exact inspected data-only package bytes')
    output = tmp_path / 'selected.zip'
    approval = capture_package_destination(output, private_root=private)
    arguments = dict(private_root=private, expected_sha256=sha256(staged.read_bytes()).hexdigest(),
                     maximum_bytes=1024, approval=approval)
    return staged, output, arguments


def test_exact_package_publication_uses_actual_create_only_link_and_receipt(tmp_path):
    staged, output, arguments = prepare(tmp_path)
    expected = staged.read_bytes()
    receipt = package_publication.publish_package(staged, output, **arguments)
    assert receipt.path == output and not receipt.cleanup_warning
    assert output.read_bytes() == expected
    assert (staged.stat().st_dev, staged.stat().st_ino) == (output.stat().st_dev, output.stat().st_ino)
    assert output.stat().st_nlink == 2
    staged.unlink()
    assert output.stat().st_nlink == 1 and output.read_bytes() == expected


@pytest.mark.parametrize('same_bytes', [False, True])
def test_existing_competitor_is_never_read_overwritten_or_mislabeled_as_our_commit(tmp_path, monkeypatch, same_bytes):
    staged, output, arguments = prepare(tmp_path)
    competitor = staged.read_bytes() if same_bytes else b'independent foreign file'
    output.write_bytes(competitor)
    identity = output.stat().st_dev, output.stat().st_ino
    original = package_publication._digest
    def no_foreign_hash(path, *args, **kwargs):
        assert path != output, 'A same-byte competitor is not our publication.'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(package_publication, '_digest', no_foreign_hash)
    with pytest.raises(FileExistsError):
        package_publication.publish_package(staged, output, **arguments)
    assert output.read_bytes() == competitor and (output.stat().st_dev, output.stat().st_ino) == identity
    assert output.stat().st_nlink == staged.stat().st_nlink == 1


def test_same_size_write_during_actual_fsync_fails_before_link(tmp_path, monkeypatch):
    staged, output, arguments = prepare(tmp_path)
    previous = staged.stat()
    original = process.os.fsync
    changed = []
    def mutate(descriptor):
        result = original(descriptor)
        if not changed and (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) == (previous.st_dev, previous.st_ino):
            changed.append(True)
            data = bytearray(staged.read_bytes())
            data[-1] ^= 1
            staged.write_bytes(data)
            os.utime(staged, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        return result
    monkeypatch.setattr(process.os, 'fsync', mutate)
    with pytest.raises(Exception, match='changed|differs'):
        package_publication.publish_package(staged, output, **arguments)
    assert changed == [True] and not output.exists()
    assert sha256(staged.read_bytes()).hexdigest() != arguments['expected_sha256']


@pytest.mark.parametrize('late', ['cancel', 'interrupt', 'exit'])
def test_verified_actual_link_retains_commit_truth_after_late_control(tmp_path, monkeypatch, late):
    staged, output, arguments = prepare(tmp_path)
    real = package_publication.publish_file
    original = {'cancel': AudioCancelled('late cancel'), 'interrupt': KeyboardInterrupt(),
                'exit': SystemExit(79)}[late]
    def committed(*args, **kwargs):
        real(*args, **kwargs)
        raise original
    monkeypatch.setattr(package_publication, 'publish_file', committed)
    if late == 'cancel':
        receipt = package_publication.publish_package(staged, output, **arguments)
    else:
        with pytest.raises(type(original)) as caught:
            package_publication.publish_package(staged, output, **arguments)
        assert caught.value is original
        receipt = caught.value.publication_receipt
    assert receipt.path == output
    assert sha256(output.read_bytes()).hexdigest() == arguments['expected_sha256']
    assert output.stat().st_nlink == 2


@pytest.mark.parametrize('masked', [False, True], ids=['direct-pending', 'exception-chain'])
def test_real_pending_child_keeps_stage_and_resource_lease_until_exact_quiescence(tmp_path, masked):
    library = tmp_path / 'library'
    library.mkdir()
    broker = ResourceBroker.for_app(library, limits=ResourceLimits(
        cpu_slots=1, memory_bytes=64 * 1024**2, disk_bytes=32 * 1024**2, min_free_disk_bytes=0))
    release = tmp_path / 'release'
    child_ready = tmp_path / 'child-ready'
    entered, done = threading.Event(), threading.Event()
    children, stages, errors, owners, originals = [], [], [], [], []
    script = r"""
import sys,time
from pathlib import Path
ready,release=map(Path,sys.argv[1:])
ready.write_bytes(b'actual child live')
deadline=time.monotonic()+15
while not release.exists():
    if time.monotonic()>deadline: raise TimeoutError('parent did not settle fixture')
    time.sleep(.01)
"""
    def work():
        try:
            with retained_context(broker.reserve(ResourceRequest(cpu_slots=1),
                    timeout=5, run_timeout=30, operation_id='voice-lifetime-fixture')):
                with retained_temporary_directory(prefix='voice-private-', dir=tmp_path) as temporary:
                    stage = Path(temporary)
                    (stage / 'private-reference').write_bytes(b'synthetic owned bytes')
                    stages.append(stage)
                    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', script,
                                              str(child_ready), str(release)],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    children.append(child)
                    deadline = time.monotonic() + 5
                    while not child_ready.exists():
                        assert child.poll() is None
                        assert time.monotonic() < deadline
                        time.sleep(.01)
                    def settle():
                        if child.poll() is None:
                            return False
                        assert child.wait(timeout=1) == 0
                        return True
                    owner = CleanupOwner(settle)
                    owners.append(owner)
                    pending = AudioCleanupPending('actual child has not yet settled', cleanup_owner=owner)
                    originals.append(pending)
                    entered.set()
                    if masked:
                        try:
                            raise pending
                        except AudioCleanupPending as error:
                            raise RuntimeError('outer operation failure') from error
                    raise pending
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=work, name='actual-voice-operation')
    thread.start()
    try:
        assert entered.wait(6)
        assert children[0].poll() is None and not owners[0].quiescent_event.is_set()
        assert not done.is_set() and stages[0].is_dir()
        assert len(broker.snapshot()['leases']) == 1
        release.write_bytes(b'actual child may finish')
        thread.join(6)
        assert not thread.is_alive() and done.is_set()
        assert owners[0].quiescent_event.is_set() and children[0].returncode == 0
        assert not stages[0].exists() and broker.snapshot()['leases'] == []
        assert len(errors) == 1
        if masked:
            assert type(errors[0]) is RuntimeError and errors[0].__cause__ is originals[0]
        else:
            assert errors[0] is originals[0]
    finally:
        release.write_bytes(b'cleanup')
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()
        thread.join(6)
        assert not thread.is_alive()


def test_package_destination_capture_never_opens_an_existing_competitor(tmp_path, monkeypatch):
    from carr_library.research import export_file
    private = tmp_path / 'library'
    private.mkdir()
    output = tmp_path / 'previous.zip'
    previous = b'private independent bytes that package creation may not inspect'
    output.write_bytes(previous)
    original_path_open, original_os_open = Path.open, os.open
    def path_open(path, *args, **kwargs):
        assert path != output, 'Package review cannot open a competing file.'
        return original_path_open(path, *args, **kwargs)
    def os_open(path, *args, **kwargs):
        assert isinstance(path, int) or Path(path) != output, 'Package review cannot open a competing descriptor.'
        return original_os_open(path, *args, **kwargs)
    def no_handle(*args, **kwargs):
        pytest.fail('Package review cannot acquire a native metadata handle for a competitor.')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', path_open)
        patch.setattr(os, 'open', os_open)
        patch.setattr(export_file, '_windows_change_time', no_handle)
        with pytest.raises(FileExistsError):
            capture_package_destination(output, private_root=private)
    assert output.read_bytes() == previous


@pytest.mark.parametrize('change', ['competing-file', 'replaced-parent', 'late-cancel'])
def test_package_destination_capture_rechecks_original_route_after_callback(tmp_path, change):
    private, parent = tmp_path / 'library', tmp_path / 'exports'
    private.mkdir()
    parent.mkdir()
    output, retained = parent / 'selected.zip', tmp_path / 'retained-exports'
    calls = []
    def cancelled():
        calls.append(True)
        if len(calls) == 2:
            if change == 'competing-file':
                output.write_bytes(b'independent concurrent output')
            elif change == 'replaced-parent':
                parent.rename(retained)
                parent.mkdir()
            return change == 'late-cancel'
        return False
    expected = {'competing-file': FileExistsError, 'replaced-parent': ValueError,
                'late-cancel': AudioCancelled}[change]
    with pytest.raises(expected):
        capture_package_destination(output, private_root=private, cancelled=cancelled)
    assert len(calls) == 2
    if change == 'competing-file':
        assert output.read_bytes() == b'independent concurrent output'
    else:
        assert not output.exists()
    if change == 'replaced-parent':
        assert retained.is_dir()
    assert list(private.iterdir()) == []


def test_package_destination_preserves_requested_route_through_actual_publication(tmp_path):
    staged, output, arguments = prepare(tmp_path)
    requested = tmp_path / '..' / tmp_path.name / output.name
    approval = capture_package_destination(requested, private_root=arguments['private_root'])
    assert approval.requested == requested.absolute()
    assert approval.target == output and approval.require_absent
    assert approval.state is None and approval.content_sha256 is None
    arguments['approval'] = approval
    receipt = package_publication.publish_package(staged, requested, **arguments)
    assert receipt.path == output and not receipt.cleanup_warning
    assert sha256(output.read_bytes()).hexdigest() == arguments['expected_sha256']


def test_package_destination_precancellation_performs_no_path_inspection(tmp_path):
    with pytest.raises(AudioCancelled):
        capture_package_destination(tmp_path / 'absent-parent' / 'selected.zip',
            private_root=tmp_path / 'missing-library', cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []
