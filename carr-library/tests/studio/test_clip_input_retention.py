"""Real retained-byte and native-owner boundaries for selected Studio inputs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.audio.models import AudioBudget, AudioCancelled
from carr_library.audio.process import _start_process, _stop_owned
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.operations._locks import NativeLock
from carr_library.operations.models import ResourceStateError
from carr_library.studio import clips
from carr_library.studio.clips import ClipEditor, retain_audio_input
from carr_library.studio.render import ProductionService


def prepared(tmp_path):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    source = tmp_path / 'explicit selected bytes.wav'
    source.write_bytes(b'Exact caller-selected fixture bytes.\r\n' * 1024)
    return inputs, source


def free_lock(inputs):
    lock = NativeLock(inputs / '.selected-clip.lock')
    assert lock.acquire(), 'The completed input transaction kept its native lock.'
    lock.release()


@pytest.mark.parametrize('route', ('retention', 'editor-admission'))
@pytest.mark.parametrize('wrapping', ('direct', 'cause', 'context'))
@pytest.mark.parametrize('failed_retry', (False, True), ids=('first-retry', 'retained-after-failed-retry'))
def test_pending_actual_child_keeps_stage_and_lock_until_owned_quiescence(
        tmp_path, route, wrapping, failed_retry):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    acknowledge = tmp_path / 'child-opened.json'
    admitted = Event()
    child = owner = None
    broker = editor = None
    if route == 'editor-admission':
        library = tmp_path / 'library'
        library.mkdir()
        broker = ResourceBroker.for_app(library, limits=ResourceLimits(cpu_slots=1,
            memory_bytes=1024**3, disk_bytes=512 * 1024**2, min_free_disk_bytes=0))
        service = ProductionService(library / 'state/studio', voices=SimpleNamespace(),
                                    broker=broker)
        inputs = service.inputs
        editor = ClipEditor(library, service=service)
    staged = []
    remaining = [int(failed_retry)]
    observed = []
    code = (
        "import hashlib,json,os,pathlib,sys,time\n"
        "source=pathlib.Path(sys.argv[1]);ack=pathlib.Path(sys.argv[2])\n"
        "with source.open('rb') as held:\n"
        " data=held.read()\n"
        " temp=ack.with_suffix('.pending')\n"
        " temp.write_bytes(json.dumps({'pid':os.getpid(),'sha256':hashlib.sha256(data).hexdigest()}).encode())\n"
        " os.replace(temp,ack)\n"
        " while True: time.sleep(.05)\n"
    )

    def inspect(path, *, cancelled):
        nonlocal child, owner
        staged.append(path)
        child = _start_process([sys.executable, '-I', '-B', '-c', code,
                                str(path), str(acknowledge)], owned_tree=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

        def attempt():
            if not admitted.is_set():
                return False
            if remaining[0]:
                remaining[0] -= 1
                raise RuntimeError('Synthetic first native-cleanup proof failure.')
            return _stop_owned(child)

        owner = CleanupOwner(attempt)
        deadline = time.monotonic() + 15
        while not acknowledge.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                break
            time.sleep(.01)
        if not acknowledge.exists():
            admitted.set()
            remaining[0] = 0
            owner.close()
            pytest.fail('The actual isolated inspection child did not acknowledge its opened source.')
        receipt = json.loads(acknowledge.read_bytes())
        assert type(receipt['pid']) is int and receipt['pid'] > 0
        assert receipt['sha256'] == sha256(original).hexdigest()
        # A Windows venv launcher PID need not equal the actual Python worker.
        observed.append(receipt['pid'])
        pending = AudioCleanupPending('Synthetic native inspection still owns its input.',
                                      cleanup_owner=owner)
        if wrapping == 'cause':
            error = RuntimeError('A later inspection-status write failed.')
            error.__cause__ = pending
            raise error
        if wrapping == 'context':
            try:
                raise pending
            except AudioCleanupPending:
                raise RuntimeError('A later inspection-status write failed.')
        raise pending

    try:
        expected = AudioCleanupPending if wrapping == 'direct' else RuntimeError
        with pytest.raises(expected) as raised:
            if editor is None:
                retain_audio_input(inputs, selected, inspect=inspect)
            else:
                runtime = SimpleNamespace(budget=AudioBudget(threads=1), inspect=inspect)
                service.runtime_factory = lambda *_args, **_kwargs: runtime
                editor.retain(selected)
        assert clips._pending_native_owner(raised.value) is owner
        assert observed and child.poll() is None
        if broker is not None:
            snapshot = broker.snapshot()
            assert snapshot['usage']['cpu_slots'] == 1
            assert snapshot['usage']['disk_bytes'] >= len(original) + 64 * 1024**2
            assert len(snapshot['leases']) == 1
        assert len(staged) == 1 and staged[0].read_bytes() == original
        competitor = NativeLock(inputs / '.selected-clip.lock')
        assert not competitor.acquire()
        with pytest.raises(RuntimeError, match='already'):
            retain_audio_input(inputs, selected,
                inspect=lambda *_args, **_kwargs: pytest.fail('A second decoder was admitted.'))
        assert not owner.close() and not owner.quiescent
        assert staged[0].exists() and not competitor.acquire()
        admitted.set()
        if failed_retry:
            assert not owner.close() and not owner.quiescent
            assert isinstance(owner.last_error, RuntimeError)
            assert staged[0].exists() and not competitor.acquire()
        result = []
        cleanup = Thread(target=lambda: result.append(owner.close()))
        cleanup.start()
        cleanup.join(timeout=15)
        assert not cleanup.is_alive() and result == [True]
        assert owner.quiescent and child.poll() is not None
        if broker is not None:
            assert broker.snapshot()['leases'] == []
        assert not staged[0].exists()
        assert not list(inputs.glob('*.audio'))
        assert competitor.acquire()
        competitor.release()
        assert owner.close() is True
        assert selected.read_bytes() == original
    finally:
        admitted.set()
        remaining[0] = 0
        if owner is not None:
            for _ in range(3):
                if owner.close():
                    break
                time.sleep(.02)
            assert owner.quiescent, 'The actual owned child did not confirm final cleanup.'
        elif child is not None:
            _stop_owned(child)


def test_no_pending_error_releases_private_stage_and_admission(tmp_path):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    seen = []

    def rejected(path, *, cancelled):
        seen.append(path)
        raise ValueError('Unsupported selected audio fixture.')

    with pytest.raises(ValueError, match='Unsupported'):
        retain_audio_input(inputs, selected, inspect=rejected)
    assert seen and not seen[0].exists()
    free_lock(inputs)
    assert selected.read_bytes() == original


def test_failed_native_cleanup_error_walk_is_cycle_safe_and_exact(tmp_path):
    owner = CleanupOwner(lambda: False)
    pending = AudioCleanupPending('Existing owner only.', cleanup_owner=owner)
    outer, inner = RuntimeError('outer'), ValueError('inner')
    outer.__context__, inner.__context__ = inner, outer
    assert clips._pending_native_owner(outer) is None
    inner.__cause__ = pending
    assert clips._pending_native_owner(outer) is owner


def test_cancelled_verification_never_publishes_retained_media(tmp_path):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    stop = Event()
    seen = []

    def inspect(path, *, cancelled):
        seen.append(path)
        assert path.read_bytes() == original
        stop.set()

    with pytest.raises((AudioCancelled, InterruptedError)):
        retain_audio_input(inputs, selected, inspect=inspect, cancelled=stop.is_set)
    assert seen and not seen[0].exists()
    assert not list(inputs.glob('*.audio'))
    free_lock(inputs)
    assert selected.read_bytes() == original


def test_staging_collision_preserves_the_unowned_existing_file(tmp_path, monkeypatch):
    inputs, selected = prepared(tmp_path)
    identity = 'a' * 32
    foreign = inputs / ('.pending-' + identity + '.audio')
    foreign.write_bytes(b'Unowned prior bytes must remain.')
    monkeypatch.setattr(clips, 'uuid4', lambda: SimpleNamespace(hex=identity))
    with pytest.raises(FileExistsError):
        retain_audio_input(inputs, selected,
            inspect=lambda *_args, **_kwargs: pytest.fail('A colliding stage reached the decoder.'))
    assert foreign.read_bytes() == b'Unowned prior bytes must remain.'
    free_lock(inputs)


def test_replaced_stage_is_not_deleted_by_failed_retention(tmp_path):
    inputs, selected = prepared(tmp_path)
    foreign = b'The inspected pathname was replaced by a different file.'
    captured = []

    def replace_stage(path, *, cancelled):
        # Keep the old inode live so a filesystem cannot immediately reuse it.
        moved = path.with_suffix('.owned-old')
        path.rename(moved)
        path.write_bytes(foreign)
        captured.append((path, moved))

    with pytest.raises(ValueError, match='replaced'):
        retain_audio_input(inputs, selected, inspect=replace_stage)
    path, moved = captured[0]
    assert path.read_bytes() == foreign
    assert moved.read_bytes() == selected.read_bytes()
    assert not list(inputs.glob('[0-9a-f]' * 64 + '.audio'))
    free_lock(inputs)


def test_replaced_input_directory_never_deletes_the_foreign_tree(tmp_path):
    inputs, selected = prepared(tmp_path)
    old = tmp_path / 'retained original directory'
    replacement = []
    protected = []

    def replace_root(path, *, cancelled):
        try:
            inputs.rename(old)
        except OSError:
            if os.name != 'nt':
                raise
            # Some Windows filesystems reject renaming a directory containing
            # the held native lock. That protection must also preserve bytes.
            protected.append(True)
            raise ValueError('Windows protected the live input directory.') from None
        inputs.mkdir()
        foreign = inputs / path.name
        foreign.write_bytes(b'Another directory owns these bytes.')
        replacement.append(foreign)

    with pytest.raises((ValueError, ResourceStateError), match='changed|lock|protected'):
        retain_audio_input(inputs, selected, inspect=replace_root)
    if protected:
        assert not replacement and not old.exists()
        assert not list(inputs.glob('*.audio'))
        free_lock(inputs)
        return
    assert replacement[0].read_bytes() == b'Another directory owns these bytes.'
    own, = old.glob('.pending-*.audio')
    assert own.read_bytes() == selected.read_bytes()
    assert not list(inputs.glob('[0-9a-f]' * 64 + '.audio'))


@pytest.mark.parametrize('expected', ('bad', '', 'A' * 64, True), ids=('short', 'empty', 'uppercase', 'boolean'))
def test_malformed_expected_digest_is_rejected_before_selected_input_io(tmp_path, expected):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    with pytest.raises(ValueError, match='digest'):
        retain_audio_input(inputs, tmp_path / 'must-not-be-read',
            expected_sha256=expected, inspect=lambda *_args, **_kwargs: pytest.fail('Decoder ran.'))
    assert list(inputs.iterdir()) == []


def test_successful_selected_byte_retention_is_content_bound_and_reusable(tmp_path):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    checks = []
    def inspect(path, *, cancelled):
        checks.append(path.read_bytes())
    first = retain_audio_input(inputs, selected, inspect=inspect,
                               expected_sha256=sha256(original).hexdigest())
    second = retain_audio_input(inputs, selected, inspect=inspect,
                                expected_sha256=sha256(original).hexdigest())
    assert first == second == inputs / (sha256(original).hexdigest() + '.audio')
    assert first.read_bytes() == selected.read_bytes() == original
    assert checks == [original, original]
    assert not list(inputs.glob('.pending-*'))
    free_lock(inputs)



@pytest.mark.parametrize('change', ['foreign-target', 'third-link', 'changed-bytes', 'foreign-stage'])
def test_only_exact_owned_publication_allows_private_stage_release(tmp_path, monkeypatch, change):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    publish = clips.publish_file
    observed = {}

    def changed_after_link(staged, target, **options):
        publish(staged, target, **options)
        assert staged.samefile(target) and staged.stat().st_nlink == 2
        # Global regular-file admission must remain strict, even for our link.
        with pytest.raises(ValueError):
            clips.regular(staged)
        observed.update(stage=staged, target=target)
        if change == 'foreign-target':
            foreign = inputs / 'foreign-equal-copy'
            foreign.write_bytes(original)
            assert not foreign.samefile(staged)
            os.replace(foreign, target)
        elif change == 'third-link':
            other = tmp_path / 'unapproved-third-link.audio'
            os.link(staged, other)
            observed['other'] = other
        elif change == 'changed-bytes':
            before = target.stat()
            with target.open('r+b') as stream:
                stream.write(b'!')  # same size; the packet/file digest must still reject it
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        else:
            moved = staged.with_suffix('.owned-original')
            staged.rename(moved)
            staged.write_bytes(b'Foreign private-stage replacement must remain.')
            observed['other'] = moved

    monkeypatch.setattr(clips, 'publish_file', changed_after_link)
    with pytest.raises(ValueError, match='publication|replaced'):
        retain_audio_input(inputs, selected, inspect=lambda *_args, **_kwargs: None)
    stage, target = observed['stage'], observed['target']
    assert stage.is_file() and target.is_file()
    assert selected.read_bytes() == original
    if change == 'foreign-target':
        assert stage.read_bytes() == target.read_bytes() == original
        assert not stage.samefile(target)
    elif change == 'third-link':
        assert stage.samefile(target) and stage.samefile(observed['other'])
        assert stage.stat().st_nlink == 3
        assert observed['other'].read_bytes() == original
    elif change == 'changed-bytes':
        assert stage.read_bytes() == target.read_bytes() == b'!' + original[1:]
    else:
        assert stage.read_bytes() == b'Foreign private-stage replacement must remain.'
        assert observed['other'].samefile(target)
        assert observed['other'].read_bytes() == original
    free_lock(inputs)


def test_foreign_output_racing_before_link_is_not_adopted_or_deleted(tmp_path, monkeypatch):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    observed = {}

    def other_producer(staged, target, **options):
        observed.update(stage=staged, target=target)
        target.write_bytes(original)
        assert not target.samefile(staged)
        raise FileExistsError('A foreign producer claimed the same content name.')

    monkeypatch.setattr(clips, 'publish_file', other_producer)
    with pytest.raises(ValueError, match='exact publication'):
        retain_audio_input(inputs, selected, inspect=lambda *_args, **_kwargs: None)
    assert observed['stage'].read_bytes() == observed['target'].read_bytes() == original
    assert not observed['stage'].samefile(observed['target'])
    assert selected.read_bytes() == original
    free_lock(inputs)


def test_last_prelink_stage_mutation_is_rejected_before_retained_output(tmp_path, monkeypatch):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    publish = clips.publish_file
    observed = []

    def before_link(staged, target, *, cancelled):
        calls = 0
        def mutate():
            nonlocal calls
            calls += 1
            if calls == 2:
                before = staged.stat()
                with staged.open('r+b') as stream:
                    stream.write(b'!')
                os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                observed.append(staged)
            return cancelled()
        return publish(staged, target, cancelled=mutate)

    monkeypatch.setattr(clips, 'publish_file', before_link)
    with pytest.raises(ValueError, match='stage changed before publication'):
        retain_audio_input(inputs, selected, inspect=lambda *_args, **_kwargs: None)
    assert len(observed) == 1 and not observed[0].exists()
    assert not (inputs / (sha256(original).hexdigest() + '.audio')).exists()
    assert selected.read_bytes() == original
    free_lock(inputs)


def test_late_cancel_after_real_link_keeps_verified_completed_retention(tmp_path, monkeypatch):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    stopped = Event()
    publish = clips.publish_file

    def commit_then_cancel(staged, target, **options):
        publish(staged, target, **options)
        assert staged.samefile(target) and staged.stat().st_nlink == 2
        stopped.set()
        raise AudioCancelled('Cancellation arrived after the actual retained-file commit.')

    monkeypatch.setattr(clips, 'publish_file', commit_then_cancel)
    target = retain_audio_input(inputs, selected, inspect=lambda *_args, **_kwargs: None,
                                cancelled=stopped.is_set)
    assert stopped.is_set()
    assert target == inputs / (sha256(original).hexdigest() + '.audio')
    assert target.read_bytes() == original and clips.regular(target).st_nlink == 1
    assert selected.read_bytes() == original and not list(inputs.glob('.pending-*'))
    free_lock(inputs)


def test_postcommit_finalization_error_names_retained_file_and_preserves_reuse(tmp_path, monkeypatch):
    inputs, selected = prepared(tmp_path)
    original = selected.read_bytes()
    target = inputs / (sha256(original).hexdigest() + '.audio')
    publish = clips.publish_file

    def commit_then_fail(staged, output, **options):
        publish(staged, output, **options)
        raise OSError('Synthetic directory flush error after the real link.')

    with monkeypatch.context() as patch:
        patch.setattr(clips, 'publish_file', commit_then_fail)
        with pytest.raises(ValueError, match='audio was retained with verified bytes') as failure:
            retain_audio_input(inputs, selected, inspect=lambda *_args, **_kwargs: None)
    assert isinstance(failure.value.__cause__, OSError)
    assert target.read_bytes() == original and clips.regular(target).st_nlink == 1
    assert not list(inputs.glob('.pending-*'))
    reused = retain_audio_input(inputs, selected, inspect=lambda *_args, **_kwargs: None)
    assert reused == target and reused.read_bytes() == original
    assert selected.read_bytes() == original
    free_lock(inputs)
