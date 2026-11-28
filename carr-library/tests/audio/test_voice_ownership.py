"""Real native owner-lock interoperability and retained cleanup boundaries."""
from __future__ import annotations

import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from carr_library.audio import voice_ownership as ownership
from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.models import AudioCleanupPending


# Exercise the owner's actual stdlib lock protocol in an isolated real process.
# No model, private owner implementation, network, or synthetic lock is loaded.
OWNER_CHILD = r"""
import errno,json,os,pathlib,sys,time
path,mode,ready,release=map(pathlib.Path,sys.argv[1:])
with path.open('a+b') as stream:
    stream.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError as error:
        if error.errno not in (errno.EACCES,errno.EAGAIN,errno.EWOULDBLOCK):
            raise
        print(json.dumps({'blocked':True}),flush=True)
        raise SystemExit(0)
    try:
        if str(mode)=='probe':
            print(json.dumps({'acquired':True}),flush=True)
        else:
            ready.write_bytes(b'held')
            deadline=time.monotonic()+30
            while not release.exists():
                if time.monotonic()>=deadline:
                    raise TimeoutError('Test owner was not released')
                time.sleep(.01)
            print(json.dumps({'released':True}),flush=True)
    finally:
        stream.seek(0)
        if os.name=='nt':
            msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        else:
            fcntl.flock(stream,fcntl.LOCK_UN)
"""

LIVE_CHILD = r"""
import pathlib,sys,time
ready=pathlib.Path(sys.argv[1])
ready.write_bytes(b'live')
time.sleep(30)
"""


def _runtime(tmp_path, payload=b'0'):
    root = tmp_path / 'runtime'
    packages = root / 'models' / 'voices' / 'packages'
    packages.mkdir(parents=True)
    path = packages / '.voice-packages.lock'
    path.write_bytes(payload)
    return root, path


def _identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


def _stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=10)


def _wait_file(path, process):
    deadline = time.monotonic() + 20
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail('Actual child exited before its checkpoint: ' + repr((stdout, stderr)))
        if time.monotonic() >= deadline:
            raise TimeoutError('Actual child did not reach its checkpoint.')
        time.sleep(.01)


def _probe(path):
    result = subprocess.run([sys.executable, '-I', '-B', '-c', OWNER_CHILD,
        str(path), 'probe', str(path.parent / 'unused-ready'), str(path.parent / 'unused-release')],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _start_owner(path, tmp_path):
    ready, release = tmp_path / 'owner-ready', tmp_path / 'owner-release'
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', OWNER_CHILD,
        str(path), 'hold', str(ready), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _wait_file(ready, process)
    except BaseException:
        _stop(process)
        raise
    return process, release


@pytest.mark.parametrize('payload', [b'', b'0'])
def test_real_owner_and_client_exclude_each_other_without_modifying_lock(tmp_path, payload):
    root, path = _runtime(tmp_path, payload)
    original = _identity(path)
    ticket = ownership.capture_voice_ownership(root)
    with ownership.hold_voice_ownership(ticket, timeout=0) as lease:
        lease.checkpoint()
        assert lease.held and lease.identity == ticket.identity
        assert lease.lock_sha256() == sha256(payload).hexdigest()
        assert not os.get_inheritable(lease._descriptor)
        assert _probe(path) == {'blocked': True}
    assert not lease.held
    assert _probe(path) == {'acquired': True}

    process, release = _start_owner(path, tmp_path)
    try:
        # Capture must not read the byte currently locked by another Windows owner.
        current = ownership.capture_voice_ownership(root, expected=ticket.identity)
        with pytest.raises(TimeoutError, match='Timed out'):
            with ownership.hold_voice_ownership(current, timeout=.1):
                pytest.fail('A second owner entered the live native lock.')
        assert process.poll() is None
        release.write_bytes(b'release')
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == {'released': True}
    finally:
        _stop(process)
    with ownership.hold_voice_ownership(ticket, timeout=0) as lease:
        lease.checkpoint()
    assert _identity(path) == original and path.read_bytes() == payload


def test_real_wait_cancellation_does_not_reclaim_the_other_owner(tmp_path):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    process, release = _start_owner(path, tmp_path)
    stopped = threading.Event()
    timer = threading.Timer(.1, stopped.set)
    try:
        timer.start()
        with pytest.raises(InterruptedError, match='cancelled'):
            with ownership.hold_voice_ownership(ticket, timeout=5, cancelled=stopped.is_set):
                pytest.fail('Cancellation admitted a second owner.')
        assert stopped.is_set() and process.poll() is None
        assert _probe(path) == {'blocked': True}
    finally:
        timer.cancel()
        timer.join(timeout=10)
        release.write_bytes(b'release')
        _stop(process)
    assert not timer.is_alive() and path.read_bytes() == b'0'
    assert _probe(path) == {'acquired': True}


@pytest.mark.parametrize('component', ['runtime', 'references', 'packages', 'lock'])
def test_same_byte_replacement_refuses_saved_ticket_without_touching_competitor(tmp_path, component):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    target = getattr(ticket, component)
    retired = target.with_name(target.name + '-retired')
    target.rename(retired)
    if component == 'lock':
        path.write_bytes(b'0')
    else:
        path.parent.mkdir(parents=True)
        path.write_bytes(b'0')
    replacement = _identity(path)
    with pytest.raises(ValueError, match='changed'):
        with ownership.hold_voice_ownership(ticket, timeout=0):
            pytest.fail('A replaced ownership scope was admitted.')
    with pytest.raises(ValueError, match='identity changed'):
        ownership.capture_voice_ownership(root, expected=ticket.identity)
    assert _identity(path) == replacement and path.read_bytes() == b'0'
    assert _probe(path) == {'acquired': True}


def test_callback_root_swap_is_rechecked_before_any_native_acquisition(tmp_path, monkeypatch):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    attempts = []
    monkeypatch.setattr(ownership, '_try_lock', lambda descriptor: attempts.append(descriptor))
    changed = False
    def cancelled():
        nonlocal changed
        if not changed:
            changed = True
            root.rename(tmp_path / 'retired')
            path.parent.mkdir(parents=True)
            path.write_bytes(b'0')
        return False
    with pytest.raises(ValueError, match='directory changed'):
        with ownership.hold_voice_ownership(ticket, cancelled=cancelled):
            pytest.fail('The callback supplied a different root.')
    assert changed and attempts == [] and path.read_bytes() == b'0'


def test_replacement_between_check_and_open_closes_only_the_opened_descriptor(tmp_path, monkeypatch):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    original_open = os.open
    opened = []
    def replacing_open(selected, flags, *args, **kwargs):
        assert Path(selected) == path
        assert not flags & (os.O_CREAT | os.O_TRUNC)
        path.rename(path.with_name('retired-lock'))
        path.write_bytes(b'0')
        descriptor = original_open(selected, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor
    with monkeypatch.context() as patch:
        patch.setattr(ownership.os, 'open', replacing_open)
        with pytest.raises(ValueError, match='lock changed'):
            with ownership.hold_voice_ownership(ticket):
                pytest.fail('The newly opened competing lock was used.')
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF
    assert path.read_bytes() == b'0'
    assert _probe(path) == {'acquired': True}


@pytest.mark.parametrize('payload', [b'x', b'00', b'\x00', b'\n'])
def test_invalid_lock_bytes_are_refused_without_rewrite_or_retained_lock(tmp_path, payload):
    root, path = _runtime(tmp_path, payload)
    before = _identity(path)
    with pytest.raises(ValueError, match='(contents|lock changed)'):
        ticket = ownership.capture_voice_ownership(root)
        with ownership.hold_voice_ownership(ticket):
            pytest.fail('Invalid owner lock bytes were accepted.')
    assert path.read_bytes() == payload and _identity(path) == before
    assert _probe(path) == {'acquired': True}


def test_checkpoint_rejects_changed_lock_bytes_while_descriptor_is_still_owned(tmp_path):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    with ownership.hold_voice_ownership(ticket) as lease:
        os.lseek(lease._descriptor, 0, os.SEEK_SET)
        os.write(lease._descriptor, b'x')  # Actual fixture tampering, not a patched stat result.
        with pytest.raises(ValueError, match='contents'):
            lease.checkpoint()
        assert lease.held and _probe(path) == {'blocked': True}
    assert path.read_bytes() == b'x' and _probe(path) == {'acquired': True}


def test_close_never_resolves_or_removes_a_replaced_named_path(tmp_path, monkeypatch):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    with ownership.hold_voice_ownership(ticket) as lease:
        # A release must depend only on its own fd, even after the scope checker
        # would refuse. Actual directory substitution is separately covered above.
        with monkeypatch.context() as patch:
            patch.setattr(ownership, '_check_ticket',
                          lambda *_: (_ for _ in ()).throw(AssertionError('release read named scope')))
            lease.close()
            lease.close()
    assert not lease.held and path.read_bytes() == b'0'
    assert _probe(path) == {'acquired': True}
    with pytest.raises(ValueError, match='no longer held'):
        lease.checkpoint()


@pytest.mark.parametrize('missing', ['lock', 'packages'])
def test_missing_explicit_owner_paths_are_never_created_by_capture(tmp_path, missing):
    root, path = _runtime(tmp_path)
    path.unlink()
    if missing == 'packages':
        path.parent.rmdir()
    before = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
    with pytest.raises(FileNotFoundError):
        ownership.capture_voice_ownership(root)
    assert before == sorted(str(p.relative_to(root)) for p in root.rglob('*'))


@pytest.mark.parametrize('kind', ['directory', 'hardlink'])
def test_nonregular_owner_lock_is_refused_before_open(tmp_path, kind):
    root, path = _runtime(tmp_path)
    if kind == 'directory':
        path.unlink()
        path.mkdir()
    else:
        os.link(path, path.with_name('second-link'))
    with pytest.raises(ValueError, match='regular local files'):
        ownership.capture_voice_ownership(root)


def test_identity_is_detached_and_explicit_reference_directory_stays_in_runtime(tmp_path):
    root, _ = _runtime(tmp_path)
    selected = root / 'alternate' / 'references'
    (selected / 'packages').mkdir(parents=True)
    path = selected / 'packages' / '.voice-packages.lock'
    path.write_bytes(b'0')
    ticket = ownership.capture_voice_ownership(root, references=selected)
    saved = ticket.identity
    assert saved['references']['path'] == str(selected.resolve())
    assert ownership.capture_voice_ownership(root, references=selected, expected=saved).identity == saved
    saved['lock']['inode'] += 1
    assert ticket.identity['lock']['inode'] != saved['lock']['inode']
    with pytest.raises(ValueError, match='identity changed'):
        ownership.capture_voice_ownership(root, references=selected, expected=saved)
    with ownership.hold_voice_ownership(ticket):
        assert _probe(path) == {'blocked': True}
    with pytest.raises(ValueError, match='outside'):
        ownership.capture_voice_ownership(root, references=tmp_path)


@pytest.mark.parametrize('timeout', [True, -1, float('nan'), float('inf'), 601])
def test_invalid_admission_budget_fails_before_any_descriptor_open(tmp_path, monkeypatch, timeout):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    attempts = []
    with monkeypatch.context() as patch:
        patch.setattr(ownership.os, 'open', lambda *a, **k: attempts.append((a, k)))
        with pytest.raises(ValueError, match='finite'):
            with ownership.hold_voice_ownership(ticket, timeout=timeout):
                pytest.fail('An invalid deadline was admitted.')
    assert attempts == [] and path.read_bytes() == b'0'


@pytest.mark.parametrize('interruption', [None, KeyboardInterrupt, SystemExit])
def test_pending_native_child_keeps_owner_lock_until_real_quiescence(tmp_path, interruption):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    ready = tmp_path / 'native-ready'
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', LIVE_CHILD, str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    allow, attempted, stopped = threading.Event(), threading.Event(), threading.Event()
    raised = interruption('deferred native cleanup') if interruption else None
    sent = False
    def finish_native():
        nonlocal sent
        if raised is not None and not sent:
            sent = True
            raise raised
        attempted.set()
        if not allow.is_set():
            return False
        _stop(process)
        assert process.poll() is not None
        return True
    native_owner = CleanupOwner(finish_native)
    pending = AudioCleanupPending('Actual child still owns its resources.', cleanup_owner=native_owner)
    pending.completed_operation = {'id': 'actual-prior-result'}
    result, leases = [], []
    def operation():
        try:
            with ownership.hold_voice_ownership(ticket, cancelled=stopped.is_set) as lease:
                leases.append(lease)
                raise pending
        except BaseException as error:
            result.append(error)
    thread = threading.Thread(target=operation)
    try:
        _wait_file(ready, process)
        thread.start()
        assert attempted.wait(20), 'Existing CleanupOwner was not retried.'
        assert process.poll() is None and leases[0].held
        assert _probe(path) == {'blocked': True}
        stopped.set()  # Cancellation cannot release a still-live native owner.
        assert leases[0].held and not native_owner.quiescent
        allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert native_owner.quiescent and process.poll() is not None
        assert result == [raised if raised is not None else pending]
        assert pending.completed_operation == {'id': 'actual-prior-result'}
        assert not leases[0].held and path.read_bytes() == b'0'
        assert _probe(path) == {'acquired': True}
    finally:
        allow.set()
        if thread.ident is not None:
            thread.join(timeout=20)
        _stop(process)
    assert not thread.is_alive()


def test_cleanup_settlement_requires_the_actual_current_owner_error():
    with pytest.raises(TypeError, match='actual pending'):
        ownership.settle_voice_cleanup(RuntimeError('not a cleanup receipt'))
    with pytest.raises(TypeError, match='current native'):
        ownership.settle_voice_cleanup(AudioCleanupPending('invalid', cleanup_owner=object()))


def test_held_lock_digest_never_reopens_the_named_locked_file(tmp_path, monkeypatch):
    root, path = _runtime(tmp_path)
    ticket = ownership.capture_voice_ownership(root)
    with ownership.hold_voice_ownership(ticket) as lease:
        def forbidden_open(*args, **kwargs):
            raise AssertionError('The held lock was reopened through a second handle.')
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', forbidden_open)
            assert lease.lock_sha256() == sha256(b'0').hexdigest()
            lease.checkpoint()
    assert path.read_bytes() == b'0' and _probe(path) == {'acquired': True}
