"""Actual DSP child-family lifetime and retained private staging.

The fault fixture substitutes a fixed Python actor at the existing native launch
boundary; it never qualifies a model or substitutes generated audio for success.
"""
import os
from pathlib import Path
import shutil
import sys
import threading
import wave

import pytest

from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.audio import cleanup, dsp, process
from carr_library.audio.cleanup import CleanupOwner, retained_temporary_directory
from carr_library.audio.models import AudioCleanupPending
from carr_library.audio.windows_job import JobProcess


def silence(path):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\0\0' * 2400)
    return path


@pytest.mark.parametrize('operation', ['render', 'concatenate'])
def test_actual_descendant_and_native_stage_remain_owned_until_cleanup(
        tmp_path, monkeypatch, operation):
    from filelock import FileLock, Timeout

    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the pinned local audio runtime.'
    runtime = AudioRuntime(tmp_path, budget=AudioBudget(timeout=20, reserve_disk_bytes=0))
    source = silence(tmp_path / 'source.wav')
    ready, descendant_lock = tmp_path / 'descendant-ready', tmp_path / 'descendant.lock'
    output = tmp_path / 'must-not-publish.wav'
    allowed, entered, owners, attempts = threading.Event(), [], [], []
    target, name = (JobProcess, 'close') if os.name == 'nt' else (process, '_terminate')
    native_close = getattr(target, name)
    def denied(child):
        if entered and not allowed.is_set():
            attempts.append(child)
            raise OSError('injected native child-family cleanup failure')
        return native_close(child)
    monkeypatch.setattr(target, name, denied)
    native_run = dsp.run_process

    descendant = (
        "import os,pathlib,time\n"
        "stream=open(" + repr(str(descendant_lock)) + ",'w+b');stream.write(b'x');stream.flush();stream.seek(0)\n"
        "if os.name=='nt':\n"
        " import msvcrt;msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)\n"
        "else:\n"
        " import fcntl;fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
        "pathlib.Path(" + repr(str(ready)) + ").write_text('ready')\n"
        "while True: time.sleep(1)\n"
    )
    # Separate immutable argv keeps fixture text out of shell interpretation.
    def launch(command, **options):
        if not options.get('watch'):
            return native_run(command, **options)
        assert options.get('owned_tree') is True
        staged = Path(options['watch'][0])
        entered.append(staged)
        actor = (
            "import pathlib,subprocess,sys,time\n"
            "stream=open(" + repr(str(staged)) + ",'wb');stream.write(b'owned unfinished bytes');stream.flush()\n"
            "child=subprocess.Popen([sys.executable,'-I','-c'," + repr(descendant) + "])\n"
            "while True: time.sleep(1)\n"
        )
        try:
            return process.run_process([str(Path(sys.executable).resolve()), '-I', '-c', actor],
                                       **options)
        except AudioCleanupPending as exc:
            owners.append(exc.cleanup_owner)
            raise
    monkeypatch.setattr(dsp, 'run_process', launch)
    competitor = FileLock(descendant_lock, timeout=0)
    released, stages = threading.Event(), []
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            if operation == 'render':
                runtime.mix((), output, duration=.1, cancelled=ready.exists)
            else:
                runtime.concatenate((source, source), output, cancelled=ready.exists)
        owner = failure.value.cleanup_owner
        owner.add_quiescent_callback(released.set)
        assert owners == [owner] and ready.exists()
        assert attempts and attempts[0].poll() is None
        assert entered[0].read_bytes() == b'owned unfinished bytes'
        stages = list(tmp_path.glob('.audio-*'))
        assert len(stages) == (2 if operation == 'concatenate' else 1)
        assert not output.exists()
        with pytest.raises(Timeout):
            competitor.acquire()
    finally:
        allowed.set()
        for owner in owners:
            # A failed assertion still releases the actual child family.
            owner.add_quiescent_callback(released.set)
            owner.close()
            assert released.wait(10) and owner.quiescent
    assert all(not stage.exists() for stage in stages)
    assert not output.exists()
    # The descendant keeps this native lock for its entire lifetime.
    with competitor:
        pass


def test_pending_exception_is_preserved_when_deferred_stage_deletion_fails(tmp_path, monkeypatch):
    objects, calls = [], []
    factory = cleanup.TemporaryDirectory
    allowed = threading.Event()
    owner = CleanupOwner(allowed.is_set)
    pending = AudioCleanupPending('original native owner', cleanup_owner=owner)
    def temporary(**options):
        value = factory(**options)
        objects.append(value)
        actual = value.cleanup
        def denied():
            calls.append(True)
            raise OSError('injected staged-file deletion failure')
        value.cleanup = denied
        value.original_cleanup = actual
        return value
    monkeypatch.setattr(cleanup, 'TemporaryDirectory', temporary)
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            with retained_temporary_directory(prefix='.native-', dir=tmp_path) as folder:
                stage = Path(folder)
                (stage / 'owned').write_bytes(b'preserve until native silence')
                raise pending
        assert failure.value is pending and calls == [] and stage.exists()
        assert owner.close() is False and calls == []
        allowed.set()
        assert owner.close() is True
        assert calls == [True] and stage.exists()
        assert owner.quiescent
    finally:
        allowed.set()
        owner.close()
        for value in objects:
            value.original_cleanup()
