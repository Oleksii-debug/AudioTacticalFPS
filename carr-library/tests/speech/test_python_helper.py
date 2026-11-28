"""Public stdlib-only process fixtures; no private voice engine or acoustic claim."""
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import struct
import sys
import time
import wave

import pytest

from carr_library.speech import LocalHelperProvider, SpeechSettings, VoiceRegistry
from carr_library.speech import _io as speech_io
from carr_library.speech import runtime


_HELPER = r'''
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import wave

root = Path(__file__).parent
request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
assert request["schema"] == "carr.local-voice.v1"
trace = {
    "request": request,
    "argv": sys.argv,
    "flags": {name: getattr(sys.flags, name) for name in
              ("isolated", "ignore_environment", "no_user_site", "no_site", "dont_write_bytecode")},
    "dont_write_bytecode": sys.dont_write_bytecode,
    "site_loaded": "site" in sys.modules,
    "sitecustomize_loaded": "sitecustomize" in sys.modules,
    "injected_available": importlib.util.find_spec("hostile_voice_injection") is not None,
    "path": sys.path,
}
(root / "observed.json").write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
if request["operation"] == "voices":
    response = {"schema": "carr.local-voice.v1", "voices": [{
        "id": "synthetic-uk",
        "name": "Synthetic PCM fixture",
        "language": "uk",
        "capabilities": ["rate", "volume"],
        "license": "Synthetic test fixture; no voice model.",
    }]}
else:
    assert request["operation"] == "render"
    assert request["voice_id"] == "synthetic-uk"
    if (root / "block-render").exists():
        child_code = (
            "import json,os,sys,time; from pathlib import Path; "
            "p=Path(sys.argv[1]); t=p.with_suffix('.pending'); "
            "t.write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid()}),encoding='ascii'); "
            "t.replace(p); time.sleep(60)"
        )
        child_marker = root / "descendant.pid"
        child = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c", child_code, str(child_marker)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 25
        while not child_marker.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Synthetic descendant did not become ready")
            time.sleep(.02)
        child_identity = json.loads(child_marker.read_text(encoding="ascii"))
        assert all(type(child_identity[key]) is int and child_identity[key] > 0 for key in ("pid", "parent"))
        assert child_identity["parent"] == (os.getpid() if child_identity["pid"] == child.pid else child.pid)
        ready = root / "render-ready.json"
        pending = ready.with_suffix(".pending")
        pending.write_text(json.dumps({
            "parent": os.getpid(), "parent_parent": os.getppid(),
            "child": child_identity["pid"], "child_parent": child_identity["parent"],
            "child_launcher": child.pid,
        }), encoding="ascii")
        pending.replace(ready)
        time.sleep(60)
        raise RuntimeError("Synthetic blocked render unexpectedly resumed")
    output = request["output"]
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(struct.pack("<320h", *([1200, -1200] * 160)))
    response = {"schema": "carr.local-voice.v1", "status": "rendered"}
sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
'''


@pytest.fixture
def helper(tmp_path):
    root = tmp_path.resolve(strict=True) / "помічник голосу"
    root.mkdir()
    script = root / "синтетичний міст.py"
    script.write_text(_HELPER, encoding="utf-8")
    executable = Path(sys.executable).resolve(strict=True)
    return executable, script


def _configuration(helper, **overrides):
    executable, script = helper
    values = dict(
        sha256=sha256(executable.read_bytes()).hexdigest(),
        engine_name="Public synthetic Python helper",
        enabled=True,
        script=script,
        script_sha256=sha256(script.read_bytes()).hexdigest(),
    )
    values.update(overrides)
    return values


def _provider(helper, **overrides):
    return LocalHelperProvider(helper[0], **_configuration(helper, **overrides))


@pytest.mark.parametrize("missing", ("script", "script_sha256"))
def test_python_helper_requires_script_and_hash_together(helper, missing):
    settings = _configuration(helper)
    settings.pop(missing)
    with pytest.raises(ValueError, match="together"):
        LocalHelperProvider(helper[0], **settings)


@pytest.mark.parametrize("bad_hash", ("", "0" * 64, "A" * 64, "z" * 64, 123, True))
def test_python_helper_rejects_invalid_or_mismatched_script_hash(helper, bad_hash):
    with pytest.raises(ValueError, match="hash verification"):
        _provider(helper, script_sha256=bad_hash)


def test_python_helper_keeps_explicit_trust_and_binary_hash_checks(helper):
    with pytest.raises(ValueError, match="Explicitly enable"):
        _provider(helper, enabled=False)
    with pytest.raises(ValueError, match="hash verification"):
        _provider(helper, sha256="0" * 64)
    assert not helper[1].with_name("observed.json").exists()


@pytest.mark.parametrize("kind", ("missing", "directory"))
def test_python_helper_requires_existing_regular_script(helper, kind):
    path = helper[1].parent / kind
    if kind == "directory":
        path.mkdir()
    with pytest.raises((OSError, ValueError)):
        _provider(helper, script=path)


def test_actual_python_helper_voices_and_unicode_wav(helper, tmp_path):
    provider = _provider(helper)
    registry = VoiceRegistry([provider])
    assert not registry.notices
    voices = registry.voices()
    assert len(voices) == 1
    voice = voices[0]
    assert voice.name == "Synthetic PCM fixture"
    assert voice.engine == "Public synthetic Python helper"
    assert voice.language == "uk"
    assert voice.capabilities == ("rate", "volume")
    assert voice.license == "Synthetic test fixture; no voice model."
    assert voice.id.startswith("helper:")
    text = 'Український текст. <voice>literal</voice> $(no command) & "quotes".'
    target = tmp_path.resolve(strict=True) / "виміряний синтетичний звук.wav"
    settings = SpeechSettings(rate=2, volume=43)
    result = registry.synthesize(text, voice.id, target, settings=settings)
    assert result.path == target
    assert (result.frames, result.sample_rate, result.channels, result.chunks) == (320, 16000, 1, 1)
    assert result.duration_seconds == 320 / 16000
    assert result.sha256 == sha256(target.read_bytes()).hexdigest()
    assert result.text_sha256 == sha256(text.encode("utf-8")).hexdigest()
    with wave.open(str(target), "rb") as audio:
        assert audio.getsampwidth() == 2
        assert audio.getnframes() == 320
        samples = struct.unpack("<320h", audio.readframes(320))
    assert samples == tuple([1200, -1200] * 160)
    trace = json.loads(helper[1].with_name("observed.json").read_text(encoding="utf-8"))
    assert trace["request"]["text"] == text
    assert trace["request"]["voice_id"] == "synthetic-uk"
    assert trace["request"]["settings"]["rate"] == 2
    assert trace["request"]["settings"]["volume"] == 43
    assert trace["argv"] == [str(helper[1])]



def test_persisted_python_helper_configuration_reloads_and_enumerates(helper, tmp_path, monkeypatch):
    from carr_library import runtime_settings

    state_root = tmp_path.resolve(strict=True) / "стан налаштувань"
    (state_root / "audio").mkdir(parents=True)
    settings = runtime_settings.load_settings(state_root)
    declaration = {
        "executable": str(helper[0]),
        **_configuration(helper, script=str(helper[1])),
    }
    settings["voice_helpers"] = [declaration]
    runtime_settings.save_settings(state_root, settings)
    loaded = runtime_settings.load_settings(state_root)
    assert loaded == settings
    persisted = json.loads(runtime_settings.settings_path(state_root).read_text(encoding="utf-8"))
    assert persisted["voice_helpers"][0]["script"] == str(helper[1])
    assert persisted["voice_helpers"][0]["script_sha256"] == sha256(helper[1].read_bytes()).hexdigest()
    # Isolate optional OS/FFmpeg discovery; the registered helper still runs.
    def configured_registry(cls, *, ffmpeg, cancelled, timeout_seconds, extra_providers=()):
        return cls(extra_providers, cancelled=cancelled, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(VoiceRegistry, "discover", classmethod(configured_registry))
    registry = runtime_settings.voice_registry(state_root)
    assert not registry.notices
    assert len(registry.voices()) == 1
    assert registry.voices()[0].name == "Synthetic PCM fixture"
    observed = json.loads(helper[1].with_name("observed.json").read_text(encoding="utf-8"))
    assert observed["request"] == {"schema": "carr.local-voice.v1", "operation": "voices"}


def test_actual_python_helper_ignores_environment_and_startup_hooks(helper, tmp_path, monkeypatch):
    hostile = tmp_path.resolve(strict=True) / "untrusted-python-path"
    hostile.mkdir()
    executed = hostile / "hook-executed"
    payload = "from pathlib import Path\nPath(" + repr(str(executed)) + ").write_text('bad')\n"
    (hostile / "sitecustomize.py").write_text(payload, encoding="utf-8")
    (hostile / "hostile_voice_injection.py").write_text(payload, encoding="utf-8")
    # A sibling hook is also outside the isolated script's module search path.
    helper[1].with_name("sitecustomize.py").write_text(payload, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.setenv("PYTHONSTARTUP", str(hostile / "sitecustomize.py"))
    monkeypatch.setenv("PYTHONUSERBASE", str(hostile))
    provider = _provider(helper)
    registry = VoiceRegistry([provider])
    assert len(registry.voices()) == 1, registry.notices
    voice = registry.voices()[0]
    registry.synthesize("Synthetic isolation test.", voice.id, tmp_path.resolve() / "isolated.wav")
    trace = json.loads(helper[1].with_name("observed.json").read_text(encoding="utf-8"))
    assert trace["flags"] == {
        "isolated": 1, "ignore_environment": 1, "no_user_site": 1,
        "no_site": 1, "dont_write_bytecode": 1,
    }
    assert trace["dont_write_bytecode"] is True
    assert trace["site_loaded"] is False
    assert trace["sitecustomize_loaded"] is False
    assert trace["injected_available"] is False
    assert str(hostile) not in trace["path"]
    assert str(helper[1].parent) not in trace["path"]
    assert not executed.exists()
    assert not list(helper[1].parent.rglob("*.pyc"))
    assert not list(hostile.rglob("*.pyc"))


@pytest.mark.parametrize("operation", ("voices", "fingerprint", "render"))
def test_changed_python_script_is_rejected_before_launch_or_cache_use(helper, tmp_path, monkeypatch, operation):
    provider = _provider(helper)
    registry = VoiceRegistry([provider])
    assert len(registry.voices()) == 1, registry.notices
    voice = registry.voices()[0]
    original = helper[1].read_bytes()
    helper[1].write_bytes(original + b"\n# changed after approval\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("Modified helper must be rejected before process launch")

    monkeypatch.setattr(runtime, "run_process", forbidden)
    target = tmp_path.resolve(strict=True) / "must-not-publish.wav"
    with pytest.raises(ValueError, match="script hash verification"):
        if operation == "voices":
            provider.voices()
        elif operation == "fingerprint":
            registry.fingerprint(voice.id)
        else:
            registry.synthesize("Do not execute changed code.", voice.id, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".speech-*"))


def test_approved_script_revision_changes_cache_fingerprint(helper):
    first = VoiceRegistry([_provider(helper)])
    assert len(first.voices()) == 1, first.notices
    voice_id = first.voices()[0].id
    before = first.fingerprint(voice_id)
    helper[1].write_bytes(helper[1].read_bytes() + b"\n# separately reviewed script revision\n")
    second = VoiceRegistry([_provider(helper)])
    assert second.voices() == first.voices()
    after = second.fingerprint(voice_id)
    assert len(before) == len(after) == 64
    assert before != after


def test_legacy_bare_helper_keeps_exact_single_executable_command(helper, monkeypatch):
    calls = []

    def capture(args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({"schema": "carr.local-voice.v1", "voices": []}).encode("utf-8"), b"", 0

    monkeypatch.setattr(runtime, "run_process", capture)
    provider = LocalHelperProvider(
        helper[0], sha256=sha256(helper[0].read_bytes()).hexdigest(),
        engine_name="Existing bare helper", enabled=True)
    assert provider.voices() == ()
    assert len(calls) == 1
    command, options = calls[0]
    assert command == [helper[0]]
    assert json.loads(options["input_bytes"]) == {"schema": "carr.local-voice.v1", "operation": "voices"}


class _OwnedDescendant:
    """Retain this fixture's process identity across parent-tree termination."""
    def __init__(self, pid, parent_pid):
        self.pid = pid
        self.kernel = None
        self.handle = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            self.ctypes = ctypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            self.kernel.TerminateProcess.restype = wintypes.BOOL
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.kernel.CloseHandle.restype = wintypes.BOOL
            # Open while the actual parent and acknowledged child are alive.
            # The retained handle still identifies that process if its PID is reused.
            self.handle = self.kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | TERMINATE
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            assert os.getpgid(parent_pid) == parent_pid
            self.identity = self._posix_identity()
            assert self.identity is not None
            assert self.identity[0] == parent_pid

    def _posix_identity(self):
        try:
            group = os.getpgid(self.pid)
        except ProcessLookupError:
            return None
        try:
            fields = Path(f"/proc/{self.pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        except FileNotFoundError:
            if Path("/proc").is_dir():
                return None
            return group, None, None
        # Fields after comm start at field 3; starttime is field 22.
        return group, fields[19], fields[0]

    def alive(self):
        if self.kernel is not None:
            state = self.kernel.WaitForSingleObject(self.handle, 0)
            if state not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            return state == 258
        current = self._posix_identity()
        return (current is not None and current[:2] == self.identity[:2]
                and current[2] != "Z")

    def wait_for_exit(self, timeout):
        if self.kernel is not None:
            state = self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000))
            if state not in (0, 258):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            return state == 0
        deadline = time.monotonic() + timeout
        while self.alive() and time.monotonic() < deadline:
            time.sleep(.02)
        return not self.alive()

    def cleanup(self):
        if self.kernel is not None:
            try:
                if self.alive():
                    if not self.kernel.TerminateProcess(self.handle, 1):
                        raise self.ctypes.WinError(self.ctypes.get_last_error())
                    assert self.wait_for_exit(5), "Owned synthetic child resisted cleanup"
            finally:
                self.kernel.CloseHandle(self.handle)
        elif self.alive():
            # Never signal a PID whose captured group/starttime has changed.
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("termination", ("cancel", "timeout"))
def test_actual_python_helper_stop_reaps_parent_and_descendant(helper, tmp_path, monkeypatch, termination):
    registry = VoiceRegistry([_provider(helper)])
    assert len(registry.voices()) == 1, registry.notices
    voice = registry.voices()[0]
    root = helper[1].parent
    (root / "block-render").write_bytes(b"synthetic blocking render")
    ready = root / "render-ready.json"
    original_popen = speech_io.subprocess.Popen
    processes, descendants, identities = [], [], []

    def launch_at_boundary(args, *positional, **options):
        process = original_popen(args, *positional, **options)
        # taskkill and any unrelated process retain their original behavior.
        if str(helper[1]) not in [os.fspath(value) for value in args]:
            return process
        processes.append(process)
        try:
            deadline = time.monotonic() + 30
            while not ready.exists():
                if process.poll() is not None:
                    # This is a public synthetic fixture, never private engine stderr.
                    options["stderr"].seek(0)
                    diagnostic = options["stderr"].read(8192).decode("utf-8", errors="replace")
                    raise AssertionError(f"Synthetic helper exited before readiness ({process.returncode}): {diagnostic}")
                assert time.monotonic() < deadline, "Synthetic helper never reached readiness"
                time.sleep(.02)
            identity = json.loads(ready.read_text(encoding="ascii"))
            assert set(identity) == {"parent", "parent_parent", "child", "child_parent", "child_launcher"}
            assert all(type(value) is int and value > 0 for value in identity.values())
            # Windows venv python.exe is a launcher. Its interpreted process and
            # a nested interpreted child can have different PIDs from Popen.pid.
            assert identity["parent_parent"] == (os.getpid() if identity["parent"] == process.pid else process.pid)
            assert identity["child_parent"] == (identity["parent"]
                if identity["child"] == identity["child_launcher"] else identity["child_launcher"])
            assert identity["child"] != identity["parent"]
            assert identity["child_launcher"] != identity["parent"]
            identities.append(identity)
            owned = {identity["parent"], identity["child_launcher"], identity["child"]} - {process.pid}
            for pid in sorted(owned):
                descendant = _OwnedDescendant(pid, process.pid)
                descendants.append(descendant)
                assert descendant.alive()
            assert process.poll() is None
            return process
        except BaseException:
            # run_process cannot own cleanup until Popen has returned.
            speech_io.stop_process(process)
            raise

    monkeypatch.setattr(speech_io.subprocess, "Popen", launch_at_boundary)
    target = tmp_path.resolve(strict=True) / "stopped-synthetic.wav"
    expected_error = InterruptedError if termination == "cancel" else TimeoutError
    cancelled = lambda: termination == "cancel" and ready.exists()
    started = time.monotonic()
    try:
        with pytest.raises(expected_error):
            registry.synthesize("Hold this synthetic render.", voice.id, target,
                                settings=SpeechSettings(timeout_seconds=3), cancelled=cancelled)
        assert ready.exists()
        identity = json.loads(ready.read_text(encoding="ascii"))
        assert len(processes) == 1
        assert identities == [identity]
        assert processes[0].poll() is not None
        owned = {identity["parent"], identity["child_launcher"], identity["child"]} - {processes[0].pid}
        assert {descendant.pid for descendant in descendants} == owned
        assert len(descendants) == len(owned)
        for descendant in descendants:
            assert descendant.wait_for_exit(5), "Speech stop leaked an owned interpreted process or launcher"
        assert time.monotonic() - started < 60
        assert not target.exists()
        assert not list(tmp_path.glob(".speech-*"))
    finally:
        try:
            for process in processes:
                speech_io.stop_process(process)
        finally:
            for descendant in descendants:
                descendant.cleanup()
