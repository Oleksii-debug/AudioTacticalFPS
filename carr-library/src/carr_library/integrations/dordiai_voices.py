"""Bounded local adapter for the actual dordiai-voices NVDA worker protocol.

The upstream worker has no HTTP API or version/capability RPC. This module
versions CARR's adapter contract and fingerprints the reviewed upstream script.
An installed private runtime remains external; no model or voice is bundled.
See docs/DORDIAI_LOCAL_SPEECH.md for the public contract and acceptance limits.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import subprocess
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import Callable, Iterator

from ..speech._io import stop_process
from ..durable_state import local_path

ENGINE_ID = "dordiai-voices"
ADAPTER_API_VERSION = 1
REVIEWED_WORKER_SHA256 = "8fb4303098489bc8ad255696895a05f3daf731e60c4dbb8b271c16fdc31d121a"
SAMPLE_RATE = 24000
MAX_REPLY_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 16384
# A production timeline uses many requests; one malformed endless utterance
# must not consume unbounded storage even when its individual replies are small.
MAX_RENDERED_FRAMES = SAMPLE_RATE * 4 * 60 * 60
LOADING_STAGES = frozenset({"language-source", "selected-voice", "voice-decoder", "language-phonetics"})
_PROBE_FORMAT = "carr-dordiai-inventory-v1"


class DordiaiError(RuntimeError):
    """An actionable adapter failure without private text or engine logs."""


class DordiaiUnavailable(DordiaiError):
    pass


class DordiaiProtocolError(DordiaiError):
    pass


class DordiaiTimeout(DordiaiError):
    pass


class DordiaiCancelled(DordiaiError):
    pass


class DordiaiBusy(DordiaiError):
    pass


class DordiaiEngineError(DordiaiError):
    def __init__(self, error_type: str):
        self.error_type = error_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type) else "EngineError"
        super().__init__(f"dordiai-voices worker failed: {self.error_type}")


@dataclass(frozen=True)
class DordiaiVoice:
    id: str
    name: str
    languages: tuple[str, ...]
    styles: tuple[str, ...] = ("neutral",)


@dataclass(frozen=True)
class DordiaiCapabilities:
    engine_version: str | None
    worker_sha256: str
    engine_id: str = ENGINE_ID
    adapter_api_version: int = ADAPTER_API_VERSION
    upstream_protocol_version: None = None
    transport: str = "local-jsonl-stdio"
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    sample_width: int = 2
    sample_format: str = "s16le"
    streaming_pcm: bool = True
    cancellation: bool = True
    loading_progress: bool = True
    rendered_frame_progress: bool = True
    controls: tuple[str, ...] = ("speed", "pitch", "timbre", "volume")
    styles: tuple[str, ...] = ("neutral",)
    ssml: bool = False
    phoneme_input: bool = False
    pronunciation_dictionary: bool = False
    word_timestamps: bool = False
    emotion: bool = False
    pronunciation_verified: bool = False


@dataclass(frozen=True)
class PCMChunk:
    data: bytes
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    sample_width: int = 2

    @property
    def frames(self) -> int:
        return len(self.data) // (self.channels * self.sample_width)


@dataclass(frozen=True)
class DordiaiProgress:
    stage: str
    rendered_frames: int
    sample_rate: int = SAMPLE_RATE

    @property
    def rendered_seconds(self) -> float:
        return self.rendered_frames / self.sample_rate


def _offline_environment() -> dict[str, str]:
    # Keep the same explicit host allowance as the canonical speech worker.
    names = ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL',
             'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOME', 'PATHEXT')
    result = {key: os.environ[key] for key in names if key in os.environ}
    result.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
                  TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                  DO_NOT_TRACK='1', WANDB_DISABLED='true', PYTHONDONTWRITEBYTECODE='1')
    return result


def _local_path(value: str | Path, *, directory: bool) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or "\x00" in raw or "://" in raw or raw.startswith(('//', '\\\\')):
        raise DordiaiUnavailable("Select a local dordiai-voices installation path")
    try:
        path = local_path(Path(raw).expanduser(), exists=True)
    except (OSError, ValueError, RuntimeError):
        raise DordiaiUnavailable("The configured local dordiai-voices path is unavailable") from None
    if (directory and not path.is_dir()) or (not directory and not path.is_file()):
        raise DordiaiUnavailable("The configured local dordiai-voices path has the wrong type")
    return path


def _inside(root: Path, relative: str, *, directory: bool = False) -> Path:
    path = _local_path(root / relative, directory=directory)
    if not path.is_relative_to(root):
        raise DordiaiUnavailable("An installed dordiai-voices path escapes its runtime directory")
    return path


class _JsonLinesProcess:
    """One owned process, one bounded reader, finite waits and no shell."""

    def __init__(self, command: list[str], cwd: Path, *, max_reply_bytes: int = MAX_REPLY_BYTES):
        self._closed = Event()
        self._close_lock = Lock()
        self._cleanup_done = False
        self._send_lock = Lock()
        self._responses: Queue[dict | Exception | None] = Queue(maxsize=8)
        self._outgoing: Queue[bytes] = Queue(maxsize=2)
        self._max_reply_bytes = max_reply_bytes
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            self.process = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            env=_offline_environment(), creationflags=flags,
                                            start_new_session=os.name == 'posix', shell=False)
        except (OSError, ValueError):
            raise DordiaiUnavailable("The private dordiai-voices interpreter could not start") from None
        self._reader = Thread(target=self._read, daemon=True, name="CARR dordiai replies")
        self._writer = Thread(target=self._write, daemon=True, name="CARR dordiai requests")
        try:
            self._reader.start()
            self._writer.start()
        except BaseException:
            self.close()
            raise

    def _put(self, value: dict | Exception | None) -> None:
        while not self._closed.is_set():
            try:
                self._responses.put(value, timeout=0.1)
                return
            except Full:
                continue

    def _read(self) -> None:
        try:
            while not self._closed.is_set():
                line = self.process.stdout.readline(self._max_reply_bytes + 1)
                if not line:
                    self._put(None)
                    return
                if len(line) > self._max_reply_bytes or not line.endswith(b"\n"):
                    raise DordiaiProtocolError("dordiai-voices reply exceeds its framing limit")
                try:
                    value = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError, RecursionError):
                    raise DordiaiProtocolError("dordiai-voices returned invalid JSON") from None
                if not isinstance(value, dict):
                    raise DordiaiProtocolError("dordiai-voices reply must be an object")
                self._put(value)
        except (OSError, ValueError, DordiaiProtocolError) as error:
            self._put(error if isinstance(error, DordiaiError)
                      else DordiaiProtocolError("dordiai-voices response pipe failed"))

    def send(self, request: dict) -> None:
        try:
            payload = (json.dumps(request, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        except (TypeError, ValueError):
            raise DordiaiProtocolError("Invalid dordiai-voices request") from None
        if len(payload) > MAX_REQUEST_BYTES:
            raise DordiaiProtocolError("dordiai-voices request exceeds its size limit")
        with self._send_lock:
            if self._closed.is_set() or self.process.poll() is not None:
                raise DordiaiProtocolError("dordiai-voices worker is no longer running")
            try:
                self._outgoing.put_nowait(payload)
            except Full:
                raise DordiaiProtocolError("dordiai-voices is not consuming its bounded request queue") from None

    def _write(self) -> None:
        # A worker can stall before creating its stdin reader. Never let a long
        # Unicode request block the caller in a full OS pipe. receive() enforces
        # the request deadline; close() terminates a blocked owned child before
        # attempting to close the writer's buffered file object.
        while not self._closed.is_set():
            try:
                payload = self._outgoing.get(timeout=0.05)
            except Empty:
                continue
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (OSError, ValueError):
                self._put(DordiaiProtocolError("dordiai-voices request pipe failed"))
                return

    def receive(self, timeout: float) -> dict:
        if timeout <= 0:
            raise DordiaiTimeout("dordiai-voices reply deadline expired")
        try:
            value = self._responses.get(timeout=timeout)
        except Empty:
            raise DordiaiTimeout("dordiai-voices did not reply within the configured deadline") from None
        if value is None:
            raise DordiaiProtocolError("dordiai-voices closed its response pipe")
        if isinstance(value, Exception):
            raise value
        return value

    def close(self) -> None:
        with self._close_lock:
            self._close()

    def _close(self) -> None:
        if self._cleanup_done:
            return
        self._closed.set()
        # This pipe owns the warm worker and its process group. Stop that tree
        # before closing stdin: a root that exits on EOF could otherwise leave
        # model subprocesses holding our reply pipe after their parent is gone.
        stop_process(self.process)
        if self._writer.ident is not None:
            self._writer.join(timeout=1.0)
        if not self._writer.is_alive():
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
        if self._reader.ident is not None:
            self._reader.join(timeout=1.0)
        if not self._reader.is_alive():
            self.process.stdout.close()
        if self._writer.is_alive() or self._reader.is_alive() or self.process.poll() is None:
            raise DordiaiError('The local voice worker has not confirmed cleanup; its handle is retained for retry.')
        self._cleanup_done = True


class DordiaiVoicesAdapter:
    """Discover installed voices and stream their PCM through the owned worker.

    ``runtime_directory`` is the installed add-on's ``kx47Runtime`` directory.
    ``python_executable`` normally stays unset, selecting its embedded Python.
    An override supports a separately prepared *local* runtime and diagnostics.
    A new upstream worker hash must be reviewed before being explicitly passed.
    """

    def __init__(self, runtime_directory: str | Path, *,
                 python_executable: str | Path | None = None,
                 expected_worker_sha256: str = REVIEWED_WORKER_SHA256,
                 request_timeout: float = 120.0):
        self.runtime = _local_path(runtime_directory, directory=True)
        self.python = _local_path(python_executable or self.runtime / "python/python.exe", directory=False)
        if not isinstance(expected_worker_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_worker_sha256):
            raise ValueError("A reviewed lowercase SHA-256 worker fingerprint is required")
        if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
                or not math.isfinite(request_timeout) or not 0 < request_timeout <= 1800):
            raise ValueError("The request timeout must be finite and between zero and 1800 seconds")
        self.expected_worker_sha256 = expected_worker_sha256
        self.request_timeout = float(request_timeout)
        self._state = RLock()
        self._operation = Lock()
        self._worker: _JsonLinesProcess | None = None
        self._probe_child: _JsonLinesProcess | None = None
        self._inventory: dict | None = None
        self._inventory_epoch: tuple | None = None
        self._active_cancel: Event | None = None
        self._closed = False
        self._validate_installation()

    def _validate_installation(self) -> None:
        script = _inside(self.runtime, "nvda_worker.py")
        if script.stat().st_size > 1024 * 1024:
            raise DordiaiProtocolError("Unreviewed dordiai-voices worker size")
        if hashlib.sha256(script.read_bytes()).hexdigest() != self.expected_worker_sha256:
            raise DordiaiProtocolError("The installed dordiai-voices worker fingerprint has not been reviewed")
        _inside(self.runtime, "models", directory=True)
        _inside(self.runtime, "models/voices", directory=True)
        _inside(self.runtime, "models/voices/catalog.json")
        for name in ("windows_runtime", "voice_policy", "voice_assets", "language_catalog", "wide_languages"):
            _inside(self.runtime, name + ".py")

    def _epoch(self) -> tuple:
        rows = []
        for name in ("nvda_worker.py", "installed_resource_packs.json", "models/voices/catalog.json",
                     "models/voices/packages/voice-packages.json"):
            try:
                info = (self.runtime / name).stat()
                rows.append((name, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino))
            except FileNotFoundError:
                rows.append((name, None))
        return tuple(rows)

    def _probe(self, refresh: bool = False, *,
               cancelled: Event | Callable[[], bool] | None = None) -> dict:
        def check_cancelled() -> None:
            requested = (bool(cancelled()) if callable(cancelled)
                         else cancelled is not None and cancelled.is_set())
            if requested:
                raise DordiaiCancelled("dordiai-voices discovery was cancelled")

        check_cancelled()
        with self._state:
            check_cancelled()
            if self._closed:
                raise DordiaiUnavailable("The dordiai-voices adapter is closed")
            epoch = self._epoch()
            if not refresh and self._inventory is not None and epoch == self._inventory_epoch:
                return self._inventory
            if self._active_cancel is not None:
                raise DordiaiBusy("Refresh dordiai-voices after the current render finishes")
            if self._probe_child is not None:
                # A failed cleanup must not orphan this exact child or permit
                # another probe to replace its retained cleanup authority.
                self._probe_child.close()
                self._probe_child = None
            if self._worker is not None:
                self._worker.close()
                self._worker = None
            self._inventory = None
            self._inventory_epoch = None
            self._validate_installation()
            helper = Path(__file__).with_name("_dordiai_probe.py").resolve()
            check_cancelled()
            deadline = monotonic() + min(self.request_timeout, 60.0)
            child = _JsonLinesProcess([str(self.python), "-I", "-B", "-u", str(helper), str(self.runtime)], self.runtime)
            self._probe_child = child
            try:
                while True:
                    check_cancelled()
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise DordiaiTimeout("dordiai-voices discovery exceeded the configured deadline")
                    try:
                        inventory = child.receive(min(remaining, 0.05))
                    except DordiaiTimeout:
                        continue
                    check_cancelled()
                    break
            finally:
                child.close()
                self._probe_child = None
            check_cancelled()
            self._validate_inventory(inventory)
            if epoch != self._epoch():
                raise DordiaiUnavailable("dordiai-voices packages changed during discovery; retry")
            check_cancelled()
            self._inventory, self._inventory_epoch = inventory, epoch
            return inventory

    def _validate_inventory(self, value: dict) -> None:
        if "probe_error" in value:
            raise DordiaiUnavailable("The installed dordiai-voices metadata helpers could not inspect its resources")
        if (value.get("format") != _PROBE_FORMAT or value.get("engine_id") != ENGINE_ID
                or value.get("worker_sha256") != self.expected_worker_sha256
                or value.get("sample_rate") != SAMPLE_RATE
                or not isinstance(value.get("voices"), list)
                or not isinstance(value.get("languages"), list)):
            raise DordiaiProtocolError("Unexpected dordiai-voices inventory contract")
        ids, codes = set(), set()
        for language in value["languages"]:
            if (not isinstance(language, dict) or not isinstance(language.get("code"), str)
                    or not language["code"] or language["code"] in codes
                    or not isinstance(language.get("resources_present"), bool)
                    or language.get("route") not in {None, "wide", "native_profile", "native_locale", "ukrainian"}):
                raise DordiaiProtocolError("Invalid dordiai-voices language inventory")
            codes.add(language["code"])
        for voice in value["voices"]:
            if (not isinstance(voice, dict) or not isinstance(voice.get("id"), str)
                    or not voice["id"] or voice["id"] in ids
                    or not isinstance(voice.get("name"), str)
                    or not isinstance(voice.get("languages"), list)
                    or not all(isinstance(code, str) and code in codes for code in voice["languages"])
                    or voice.get("styles") != ["neutral"]):
                raise DordiaiProtocolError("Invalid dordiai-voices voice inventory")
            ids.add(voice["id"])

    def voices(self, refresh: bool = False, *,
               cancelled: Event | Callable[[], bool] | None = None) -> tuple[DordiaiVoice, ...]:
        """Only structurally available installed voices; no fixed catalogue."""
        return tuple(DordiaiVoice(v["id"], v["name"], tuple(v["languages"]), tuple(v["styles"]))
                     for v in self._probe(refresh, cancelled=cancelled)["voices"] if v["languages"])

    def capabilities(self, *, cancelled: Event | Callable[[], bool] | None = None) -> DordiaiCapabilities:
        inventory = self._probe(cancelled=cancelled)
        return DordiaiCapabilities(inventory.get("engine_version"), inventory["worker_sha256"])

    def language_statuses(self, *, cancelled: Event | Callable[[], bool] | None = None) -> tuple[dict, ...]:
        """Copy of installed-resource status, including missing/partial entries."""
        return tuple(dict(row) for row in self._probe(cancelled=cancelled)["languages"])

    def _start_worker(self) -> _JsonLinesProcess:
        with self._state:
            if self._closed:
                raise DordiaiUnavailable("The dordiai-voices adapter is closed")
            if self._worker is None:
                self._validate_installation()
                reference_model = _inside(self.runtime, "models/Holos", directory=True)
                command = [str(self.python), "-I", "-B", "-u", str(self.runtime / "nvda_worker.py"),
                           str(self.runtime / "models"), "--reference-model", str(reference_model),
                           "--references", str(self.runtime / "models/voices"),
                           "--reference-engine", "holos", "--preload-language", "mixed"]
                self._worker = _JsonLinesProcess(command, self.runtime)
            return self._worker

    def _forget_worker(self, worker: _JsonLinesProcess) -> None:
        worker.close()
        with self._state:
            if self._worker is worker:
                self._worker = None

    def _drain_after_cancel(self, worker: _JsonLinesProcess, pending: bool) -> None:
        """Cancel has no acknowledgement; drain the old reply before drop's ack."""
        try:
            worker.send({"mode": "cancel"})
            deadline = monotonic() + 2.0
            while pending:
                reply = worker.receive(deadline - monotonic())
                pending = "loadingStage" in reply
            worker.send({"mode": "drop"})
            reply = worker.receive(deadline - monotonic())
            if reply != {"finished": True}:
                raise DordiaiProtocolError("dordiai-voices stream did not close cleanly")
        except DordiaiError:
            self._forget_worker(worker)

    def stream_pcm(self, text: str, voice_id: str, language: str, *, speed: float = 1.0,
                   pitch: int = 50, timbre: int = 50, volume: int = 100,
                   style: str = "neutral", cancelled: Event | Callable[[], bool] | None = None,
                   progress: Callable[[DordiaiProgress], None] | None = None) -> Iterator[PCMChunk]:
        """Yield s16le mono 24 kHz PCM; cancellation raises, never succeeds partly.

        Call from a background production/reader worker. Keep language explicit;
        original-document editing and pronunciation substitutions belong to the
        caller's separate narration derivative. No SSML is interpreted here.
        """
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
            raise ValueError(f"Supply nonempty text of at most {MAX_TEXT_CHARACTERS} characters per chunk")
        if (isinstance(speed, bool) or not isinstance(speed, (int, float))
                or not math.isfinite(speed) or not 0.25 <= speed <= 4.0):
            raise ValueError("Speech speed must be finite and between 0.25 and 4.0")
        if any(type(value) is not int or not 0 <= value <= 100 for value in (pitch, timbre, volume)):
            raise ValueError("Pitch, timbre and volume must be integers from 0 through 100")
        if style != "neutral":
            raise ValueError("The reviewed dordiai-voices adapter supports neutral style only")
        if not isinstance(language, str):
            raise ValueError("An explicit installed language code is required")
        language = language.lower().replace("_", "-")
        if not self._operation.acquire(blocking=False):
            raise DordiaiBusy("This dordiai-voices adapter already has an active render")
        worker = None
        pending = False
        completed = False
        local_cancel = Event()

        def cancellation_requested() -> bool:
            return local_cancel.is_set() or (bool(cancelled()) if callable(cancelled)
                                             else cancelled is not None and cancelled.is_set())

        try:
            inventory = self._probe(cancelled=cancellation_requested)
            voice = next((row for row in inventory["voices"] if row["id"] == voice_id), None)
            selected = next((row for row in inventory["languages"] if row["code"] == language), None)
            if voice is None or language not in voice["languages"] or selected is None or not selected["resources_present"]:
                raise DordiaiUnavailable("The selected voice and language have no compatible installed resources")
            with self._state:
                self._active_cancel = local_cancel
            if cancellation_requested():
                raise DordiaiCancelled("dordiai-voices rendering was cancelled")
            worker = self._start_worker()
            route = selected["route"]
            request = {"mode": "reference", "profile": voice_id, "language": language,
                       "text": text, "speed": float(speed), "stream": True,
                       "controls": {"pitch": pitch, "timbre": timbre, "volume": volume},
                       "nativeProfile": route == "native_profile", "nativeLocale": route == "native_locale",
                       "wideLocale": route == "wide", "reportLoading": True,
                       "nativeEnglish": False, "character": False, "numberLanguage": "auto"}
            worker.send(request)
            pending = True
            deadline = monotonic() + self.request_timeout
            cancel_sent = False
            frames = 0
            while True:
                is_cancelled = cancellation_requested()
                if is_cancelled and not cancel_sent:
                    worker.send({"mode": "cancel"})
                    cancel_sent = True
                    deadline = min(deadline, monotonic() + 2.0)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    if is_cancelled:
                        raise DordiaiCancelled("dordiai-voices rendering was cancelled")
                    raise DordiaiTimeout("dordiai-voices rendering exceeded the configured deadline")
                try:
                    reply = worker.receive(min(remaining, 0.05))
                except DordiaiTimeout:
                    continue
                if "loadingStage" in reply:
                    if reply["loadingStage"] not in LOADING_STAGES or len(reply) != 1:
                        raise DordiaiProtocolError("Unexpected dordiai-voices loading stage")
                    if progress is not None and not is_cancelled:
                        progress(DordiaiProgress(reply["loadingStage"], frames))
                    continue
                pending = False
                if cancellation_requested():
                    raise DordiaiCancelled("dordiai-voices rendering was cancelled")
                if self._inventory_epoch != self._epoch():
                    raise DordiaiUnavailable("dordiai-voices packages changed during rendering; retry")
                if "error" in reply:
                    raise DordiaiEngineError(str(reply["error"]))
                if reply == {"finished": True}:
                    if not frames:
                        raise DordiaiEngineError("EmptyAudio")
                    completed = True
                    return
                if "chunk" not in reply or not isinstance(reply["chunk"], str) or "finished" in reply:
                    raise DordiaiProtocolError("Unexpected dordiai-voices PCM reply")
                try:
                    data = base64.b64decode(reply["chunk"], validate=True)
                except (ValueError, TypeError):
                    raise DordiaiProtocolError("dordiai-voices returned malformed base64 PCM") from None
                if not data or len(data) % 2:
                    raise DordiaiProtocolError("dordiai-voices returned malformed 16-bit PCM")
                chunk = PCMChunk(data)
                frames += chunk.frames
                if frames > MAX_RENDERED_FRAMES:
                    raise DordiaiProtocolError("dordiai-voices utterance exceeds its audio budget")
                if progress is not None:
                    progress(DordiaiProgress("pcm", frames))
                if cancellation_requested():
                    raise DordiaiCancelled("dordiai-voices rendering was cancelled")
                yield chunk
                if cancellation_requested():
                    raise DordiaiCancelled("dordiai-voices rendering was cancelled")
                worker.send({"mode": "next"})
                pending = True
                deadline = monotonic() + self.request_timeout
        finally:
            if worker is not None and not completed:
                self._drain_after_cancel(worker, pending)
            with self._state:
                self._active_cancel = None
            self._operation.release()

    def cancel(self) -> None:
        """Signal only this adapter's active request; no fabricated cancel RPC ack."""
        with self._state:
            event = self._active_cancel
        if event is not None:
            event.set()

    def close(self) -> None:
        self.cancel()
        with self._state:
            self._closed = True
            children = (("_probe_child", self._probe_child), ("_worker", self._worker))
        errors = []
        for name, child in children:
            if child is None:
                continue
            try:
                child.close()
            except (Exception, KeyboardInterrupt) as exc:
                errors.append(exc)
            else:
                with self._state:
                    if getattr(self, name) is child:
                        setattr(self, name, None)
        if errors:
            raise errors[0]

    def __enter__(self) -> DordiaiVoicesAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
