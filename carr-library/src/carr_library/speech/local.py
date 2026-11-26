"""Render real local speech into checked WAV files, without downloads or playback.

SAPI5 runs in an isolated, installed Windows PowerShell process. The script is a
fixed adapter; user text and filenames travel only as JSON on standard input.
eSpeak NG/eSpeak is an optional, separately installed executable. No engine is
invented or substituted when the requested voice is unavailable.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import wave

from ..audio.ffmpeg import _local_path, _runtime_directories


MAX_TEXT_CHARACTERS = 65_536  # per synthesis chunk, never a whole-project limit
_MAX_PROCESS_OUTPUT = 2 * 1024 * 1024
_MAX_WAV_BYTES = 512 * 1024 * 1024  # scheduler must split long narration
_SAFE_ESPEAK_VOICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+\-]*\Z")


class SpeechError(RuntimeError):
    """A local speech operation failed without publishing a partial file."""


class SpeechUnavailable(SpeechError):
    """The explicitly requested local voice or capability is unavailable."""


class SpeechBudgetExceeded(SpeechError):
    """The caller's live output allowance was reached before publication."""


class RenderCancelled(SpeechError):
    """Cancellation was observed before the final output was published."""


@dataclass(frozen=True)
class Voice:
    id: str
    engine: str
    name: str
    language: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice_id: str
    language: str | None = None
    rate: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0


@dataclass(frozen=True)
class SpeechResult:
    path: Path
    sample_rate: int
    channels: int
    frames: int
    duration_seconds: float
    engine: str
    voice_id: str
    sha256: str
    engine_fingerprint: str


@dataclass(frozen=True)
class _Entry:
    voice: Voice
    executable: Path
    native_id: str
    identity: dict
    data_path: Path | None = None


_SAPI_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$voice = $null
$stream = $null
try {
    $voice = New-Object -ComObject SAPI.SpVoice
    $tokens = $voice.GetVoices()
    if ($request.action -eq 'list') {
        $items = @()
        for ($i = 0; $i -lt $tokens.Count; $i++) {
            $token = $tokens.Item($i)
            $attributes = @{}
            foreach ($key in @('Language', 'Vendor', 'Gender', 'Age', 'Name', 'Version')) {
                try { $attributes[$key] = [string]$token.GetAttribute($key) } catch {}
            }
            $language = $null
            if ($attributes.ContainsKey('Language')) {
                try {
                    $lcid = [Convert]::ToInt32(($attributes['Language'] -split ';')[0], 16)
                    $language = [Globalization.CultureInfo]::GetCultureInfo($lcid).Name
                } catch {}
            }
            $items += @{ id = [string]$token.Id; name = [string]$token.GetDescription();
                         language = $language; attributes = $attributes }
        }
        [Console]::Out.Write((ConvertTo-Json -InputObject @($items) -Depth 5 -Compress))
    } elseif ($request.action -eq 'render') {
        $selected = $null
        for ($i = 0; $i -lt $tokens.Count; $i++) {
            $candidate = $tokens.Item($i)
            if ([string]$candidate.Id -ceq [string]$request.voice) { $selected = $candidate; break }
        }
        if ($null -eq $selected) { throw 'Selected installed voice is unavailable.' }
        $voice.Voice = $selected
        $voice.Rate = [int]$request.rate
        $voice.Volume = [int]$request.volume
        $stream = New-Object -ComObject SAPI.SpFileStream
        $stream.Format.Type = 22 # SAFT22kHz16BitMono; SAPI converts local engine output
        $stream.Open([string]$request.output, 3, $false) # SSFMCreateForWrite, private stage
        $voice.AudioOutputStream = $stream
        [void]$voice.Speak([string]$request.text, 16) # SVSFIsNotXML; never file/SSML mode
        $stream.Close()
        $stream = $null
        [Console]::Out.Write('{"rendered":true}')
    } else { throw 'Unsupported speech operation.' }
} catch {
    # Do not echo input, paths, token data, or private narration through diagnostics.
    [Console]::Error.Write('Local SAPI operation failed.')
    exit 1
} finally {
    if ($null -ne $stream) { try { $stream.Close() } catch {} }
    if ($null -ne $voice) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($voice) }
}
'''


def _cancelled(callback) -> bool:
    if callback is None:
        return False
    if callable(callback):
        return bool(callback())
    if hasattr(callback, "is_set") and callable(callback.is_set):
        return bool(callback.is_set())
    raise TypeError("cancelled must be a callable or threading Event")


def _check_cancel(callback):
    if _cancelled(callback):
        raise RenderCancelled("Speech rendering was cancelled; no output was published.")


def _stop_process(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        process.wait(timeout=5)


def _run(args, payload: bytes, *, cancelled=None, timeout=30.0, output_path=None,
         max_output_bytes=None) -> bytes:
    """Bound process diagnostics on disk and poll cancellation while sending input."""
    _check_cancel(cancelled)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Speech timeout must be a positive finite number.")
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes < 1):
        raise ValueError("Speech output byte budget must be a positive integer.")
    output_limit = min(_MAX_WAV_BYTES, max_output_bytes) if max_output_bytes is not None else _MAX_WAV_BYTES
    def check_output():
        if output_path is not None and output_path.exists() and output_path.stat().st_size > output_limit:
            raise SpeechBudgetExceeded("The speech chunk exceeded its output byte budget; split the text or increase the project allowance.")
    started = time.monotonic()
    options = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=stdout,
                                       stderr=stderr, shell=False, **options)
        except OSError as exc:
            raise SpeechUnavailable("The selected local speech executable could not start.") from exc
        try:
            pending = payload
            while True:
                _check_cancel(cancelled)
                if time.monotonic() - started >= timeout:
                    raise SpeechError("The speech chunk exceeded its execution time budget.")
                if (os.fstat(stdout.fileno()).st_size > _MAX_PROCESS_OUTPUT
                        or os.fstat(stderr.fileno()).st_size > _MAX_PROCESS_OUTPUT):
                    raise SpeechError("The local speech engine exceeded its diagnostics budget.")
                check_output()
                try:
                    process.communicate(input=pending, timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pending = None
            _check_cancel(cancelled)
            check_output()
            if (os.fstat(stdout.fileno()).st_size > _MAX_PROCESS_OUTPUT
                    or os.fstat(stderr.fileno()).st_size > _MAX_PROCESS_OUTPUT):
                raise SpeechError("The local speech engine exceeded its diagnostics budget.")
            if process.returncode != 0:
                raise SpeechError(f"The local speech engine failed (exit {process.returncode}).")
            stdout.seek(0)
            result = stdout.read(_MAX_PROCESS_OUTPUT + 1)
            if len(result) > _MAX_PROCESS_OUTPUT:
                raise SpeechError("The local speech engine returned too much metadata.")
            return result
        finally:
            _stop_process(process)


def _powershell_args(executable: Path) -> list[str]:
    encoded = base64.b64encode(_SAPI_SCRIPT.encode("utf-16le")).decode("ascii")
    return [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _powershell_candidates() -> tuple[tuple[str, Path], ...]:
    if os.name != "nt":
        return ()
    system_root = _speech_path(os.environ.get("SystemRoot", r"C:\Windows"))
    is_64 = "64" in (os.environ.get("PROCESSOR_ARCHITECTURE", "")
                     + os.environ.get("PROCESSOR_ARCHITEW6432", ""))
    native_dir = "Sysnative" if os.environ.get("PROCESSOR_ARCHITEW6432") else "System32"
    choices = [("64" if is_64 else "32", system_root / native_dir)]
    if is_64:
        choices.append(("32", system_root / "SysWOW64"))
    return tuple((bits, directory / "WindowsPowerShell" / "v1.0" / "powershell.exe")
                 for bits, directory in choices
                 if _speech_path(directory / "WindowsPowerShell" / "v1.0" / "powershell.exe").is_file())


def _speech_path(value):
    try:
        return _local_path(value)
    except (ValueError, OSError) as exc:
        raise SpeechUnavailable("Speech runtimes and data must use regular local paths, not network shares or device paths.") from exc


def _espeak_runtime() -> tuple[Path, Path | None] | None:
    explicit = os.environ.get("CARR_ESPEAK_EXECUTABLE")
    if explicit:
        path = _speech_path(explicit)
        if not path.is_absolute() or not path.is_file():
            raise SpeechUnavailable("CARR_ESPEAK_EXECUTABLE must name an installed absolute executable.")
    else:
        # Executables in CWD are not discovery candidates, including Windows PATH '.'.
        found = None
        for name in ("espeak-ng", "espeak"):
            for directory in _runtime_directories():
                found = shutil.which(str(directory / name))
                if found:
                    break
            if found:
                break
        if not found:
            return None
        path = _speech_path(found)
    data = os.environ.get("CARR_ESPEAK_DATA_PATH")
    data_path = _speech_path(data) if data else None
    if data_path is not None and (not data_path.is_absolute() or not data_path.is_dir()):
        raise SpeechUnavailable("CARR_ESPEAK_DATA_PATH must name an installed absolute data directory.")
    return path.resolve(), data_path


def _espeak_args(executable, data_path):
    args = [str(executable)]
    if data_path is not None:
        args.append("--path=" + str(data_path))
    return args


def _entries(*, cancelled=None) -> tuple[_Entry, ...]:
    entries = []
    for bits, executable in _powershell_candidates():
        _check_cancel(cancelled)
        try:
            raw = _run(_powershell_args(executable), b'{"action":"list"}',
                       cancelled=cancelled, timeout=30)
            rows = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(rows, list) or len(rows) > 4096:
                raise SpeechError("Invalid installed SAPI voice inventory.")
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                        or not isinstance(row.get("name"), str) or len(row["id"]) > 4096):
                    raise SpeechError("Invalid installed SAPI voice metadata.")
                voice = Voice(f"sapi5:{bits}:{row['id']}", "sapi5", f"{row['name']} ({bits}-bit)",
                              row.get("language") or None, ("local", "wav", "rate", "volume"))
                entries.append(_Entry(voice, executable, row["id"], row))
        except RenderCancelled:
            raise
        except (SpeechError, ValueError, UnicodeError):
            # A broken 32-bit SAPI installation must not hide working 64-bit voices.
            continue
    runtime = _espeak_runtime()
    if runtime is not None:
        executable, data_path = runtime
        try:
            raw = _run(_espeak_args(executable, data_path) + ["--voices"], b"",
                       cancelled=cancelled, timeout=30)
            version = _run(_espeak_args(executable, data_path) + ["--version"], b"",
                           cancelled=cancelled, timeout=15).decode("utf-8", errors="replace").strip()
            seen = set()
            for line in raw.decode("utf-8", errors="replace").splitlines():
                columns = line.split(maxsplit=5)
                if len(columns) < 5 or not columns[0].isdigit():
                    continue
                language, name, descriptor = columns[1], columns[3], columns[4]
                if language in seen or not _SAFE_ESPEAK_VOICE.fullmatch(language):
                    continue
                # MBROLA voices need a separate installation and rights review.
                if descriptor.startswith("mb/") or descriptor.startswith("mbrola/"):
                    continue
                seen.add(language)
                engine = "espeak-ng" if "espeak-ng" in executable.name.lower() else "espeak"
                voice = Voice("espeak:" + language, engine, name.replace("_", " "), language,
                              ("local", "wav", "rate", "volume", "pitch-relative"))
                entries.append(_Entry(voice, executable, language,
                                      {"version": version, "descriptor": descriptor,
                                       "data_path": str(data_path) if data_path else None}, data_path))
        except RenderCancelled:
            raise
        except SpeechError:
            if os.environ.get("CARR_ESPEAK_EXECUTABLE"):
                raise
    return tuple(entries)


def discover_voices() -> tuple[Voice, ...]:
    """Discover installed voices only; absence returns an empty tuple, never a fake voice."""
    return tuple(entry.voice for entry in _entries())


def _entry(voice_id: str, cancelled=None) -> _Entry:
    if not isinstance(voice_id, str) or not voice_id or len(voice_id) > 8192:
        raise ValueError("A discovered local voice ID is required.")
    if voice_id.startswith("dordiai-voices"):
        raise SpeechUnavailable("dordiai-voices requires its actual versioned local integration contract; none is configured.")
    if voice_id.startswith("sapi4"):
        raise SpeechUnavailable("SAPI4 requires a separately verified 32-bit local adapter.")
    for entry in _entries(cancelled=cancelled):
        if entry.voice.id == voice_id:
            return entry
    raise SpeechUnavailable("The selected local voice is unavailable. Discover installed voices and select one explicitly.")


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(entry: _Entry) -> str:
    before = entry.executable.stat()
    content_hash = _file_digest(entry.executable)
    after = entry.executable.stat()
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise SpeechError("The speech executable changed during identity verification.")
    data = {"adapter": 1, "voice": entry.voice.id, "engine": entry.voice.engine,
            "executable": str(entry.executable), "executable_sha256": content_hash,
            "executable_stat": identity, "native": entry.identity}
    if entry.voice.engine.startswith("espeak"):
        data["language_data"] = _espeak_data_identity(entry)
        data["local_engine_libraries"] = _espeak_library_identity(entry.executable)
    return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _espeak_data_identity(entry: _Entry) -> dict:
    """Include phoneme/voice/dictionary bytes so language updates invalidate audio."""
    data_root = entry.data_path
    if data_root is None:
        marker = "Data at: "
        version = entry.identity.get("version", "")
        if marker in version:
            candidate = version.split(marker, 1)[1].splitlines()[0].strip()
            data_root = _speech_path(candidate)
    if data_root is None or not data_root.is_absolute():
        raise SpeechUnavailable("The local eSpeak data directory could not be identified for the render cache.")
    for name in ("espeak-ng-data", "espeak-data"):
        if (data_root / name).is_dir():
            data_root = data_root / name
            break
    if not data_root.is_dir():
        raise SpeechUnavailable("The installed eSpeak language data are unavailable.")
    descriptor = entry.identity["descriptor"]
    if (not isinstance(descriptor, str) or not re.fullmatch(r"[A-Za-z0-9_./+-]+", descriptor)
            or descriptor.startswith("/") or any(part in ("", ".", "..") for part in descriptor.split("/"))):
        raise SpeechUnavailable("Unsupported eSpeak voice descriptor path.")
    files = [data_root / name for name in ("phontab", "phonindex", "phondata", "intonations")]
    files.extend([data_root / "lang" / descriptor, data_root / "voices" / descriptor])
    # Language variants can share parent dictionaries (en-us -> en_dict).
    language = entry.voice.language or entry.native_id
    files.extend([data_root / (language + "_dict"), data_root / (language.split("-")[0] + "_dict")])
    result = {"path": str(data_root.resolve()), "files": {}}
    for path in dict.fromkeys(files):
        if not path.is_file():
            continue
        before = path.stat()
        if before.st_size > 64 * 1024 * 1024 or path.resolve().is_relative_to(data_root.resolve()) is False:
            raise SpeechUnavailable("Unsupported eSpeak language-data layout.")
        digest = _file_digest(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise SpeechError("Speech language data changed during identity verification.")
        result["files"][str(path.relative_to(data_root))] = digest
    if not result["files"]:
        raise SpeechUnavailable("No installed eSpeak language-data bytes were verified.")
    return result


def _espeak_library_identity(executable: Path) -> dict:
    """Cover installed shared-engine updates as well as the command-line wrapper.

    All existing candidates are hashed, rather than guessing which dynamic
    loader search entry won. Extra cache invalidation is safe; stale speech is
    not. No library is loaded into this process.
    """
    directories = [executable.parent, executable.parent.parent / "lib",
                   executable.parent.parent / "lib" / "x86_64-linux-gnu",
                   executable.parent.parent / "lib" / "aarch64-linux-gnu"]
    if os.name == "posix":
        directories.extend(Path(p) for p in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
                           if p and Path(p).is_absolute())
        directories.extend(Path(p) for p in ("/usr/lib", "/usr/lib64", "/lib",
                                             "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu"))
    names = ("libespeak-ng.so.1", "libespeak.so.1", "libsonic.so.0",
             "libespeak-ng.dll", "libespeak.dll", "espeak-ng.dll", "libsonic.dll",
             "libespeak-ng.dylib", "libespeak.dylib")
    result = {}
    for directory in dict.fromkeys(directories):
        directory = _speech_path(directory)
        for name in names:
            path = _speech_path(directory / name)
            if not path.is_file():
                continue
            path = path.resolve()
            if str(path) in result:
                continue
            before = path.stat()
            if before.st_size > 128 * 1024 * 1024:
                raise SpeechUnavailable("Installed speech-engine library exceeds its identity budget.")
            digest = _file_digest(path)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise SpeechError("Speech-engine library changed during identity verification.")
            result[str(path)] = digest
    return result


def engine_fingerprint(voice_id: str) -> str:
    """Fingerprint current native voice identity and executable; never a placeholder."""
    return _fingerprint(_entry(voice_id))


def _validate_request(request: SpeechRequest):
    if not isinstance(request, SpeechRequest):
        raise TypeError("SpeechRequest is required.")
    if (not isinstance(request.text, str) or not request.text.strip()
            or len(request.text) > MAX_TEXT_CHARACTERS):
        raise ValueError(f"A speech chunk must contain 1–{MAX_TEXT_CHARACTERS} text characters.")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in request.text):
        raise ValueError("Speech text contains unsupported control characters.")
    try:
        request.text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Speech text must contain valid Unicode scalar values.") from exc
    for field, low, high in (("rate", .5, 2.), ("pitch", -1., 1.), ("volume", 0., 1.)):
        value = getattr(request, field)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Speech {field} must be a finite number between {low} and {high}.")
    if (request.language is not None and (not isinstance(request.language, str)
            or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", request.language))):
        raise ValueError("Speech language must be a language tag or None.")


def _output_target(target) -> tuple[Path, tuple[int, int]]:
    target = _local_path(target)
    if str(target).startswith(("\\\\", "//")) or target.suffix.lower() != ".wav":
        raise ValueError("Speech output must be a local WAV path.")
    if any(ord(ch) < 32 for ch in str(target)) or ":" in target.name:
        raise ValueError("Invalid local speech output path.")
    target = Path(os.path.abspath(target))
    for parent in (*reversed(target.parent.parents), target.parent):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("Speech output directory must not contain symlinks or reparse points.")
    if target.exists() or target.is_symlink():
        raise FileExistsError("Speech output already exists; choose a new filename.")
    info = target.parent.stat()
    return target, (info.st_dev, info.st_ino)


def _inspect_wav(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_WAV_BYTES:
        raise SpeechError("Speech engine returned an unsafe or oversized audio file.")
    try:
        with wave.open(str(path), "rb") as audio:
            rate, channels, frames = audio.getframerate(), audio.getnchannels(), audio.getnframes()
            width = audio.getsampwidth()
            if (audio.getcomptype() != "NONE" or width != 2 or channels not in (1, 2)
                    or not 8_000 <= rate <= 192_000 or frames <= 0):
                raise SpeechError("Speech engine did not return supported nonempty PCM16 WAV audio.")
            total = 0
            while chunk := audio.readframes(65_536):
                total += len(chunk)
            if total != frames * channels * width:
                raise SpeechError("Speech engine returned truncated WAV audio.")
    except (wave.Error, EOFError) as exc:
        raise SpeechError("Speech engine returned an invalid WAV file.") from exc
    return rate, channels, frames


def _publish(stage: Path, target: Path, parent_identity):
    # Windows FlushFileBuffers requires a write-capable handle.
    with stage.open("r+b") as stream:
        os.fsync(stream.fileno())
    if os.name == "posix":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(target.parent, flags)
        stage_fd = os.open(stage.parent, flags)
        try:
            info = os.fstat(parent_fd)
            if (info.st_dev, info.st_ino) != parent_identity:
                raise SpeechError("Speech output directory changed during rendering.")
            os.link(stage.name, target.name, src_dir_fd=stage_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
            os.fsync(parent_fd)
        finally:
            os.close(stage_fd)
            os.close(parent_fd)
    else:
        info = target.parent.stat()
        if (info.st_dev, info.st_ino) != parent_identity:
            raise SpeechError("Speech output directory changed during rendering.")
        # Windows rename fails when destination exists; never os.replace.
        os.rename(stage, target)


def render_speech(request: SpeechRequest, target: Path, *, cancelled=None,
                  timeout_seconds: float = 600.0, max_output_bytes=None) -> SpeechResult:
    """Synthesize one bounded local chunk, verify it, and atomically publish once.

    rate is relative (0.5–2); volume is 0–1. pitch is an eSpeak-relative
    control (-1–1), not measured semitones. SAPI5 exposes no pitch capability.
    Engines render to disk, never the sound device. The caller owns playback,
    chunk scheduling, project duration, and the chosen output-directory scope.
    """
    _validate_request(request)
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes < 1):
        raise ValueError("Speech output byte budget must be a positive integer.")
    _check_cancel(cancelled)
    target, parent_identity = _output_target(target)
    entry = _entry(request.voice_id, cancelled)
    if request.language and (not entry.voice.language or
            request.language.casefold().split("-")[0] != entry.voice.language.casefold().split("-")[0]):
        raise SpeechUnavailable("The chosen voice does not advertise the requested language; select a matching voice.")
    if request.pitch and "pitch-relative" not in entry.voice.capabilities:
        raise SpeechUnavailable("The chosen voice does not support the requested pitch control.")
    fingerprint = _fingerprint(entry)
    with tempfile.TemporaryDirectory(prefix=".speech-", dir=target.parent) as directory:
        stage = Path(directory) / "speech.wav"
        if entry.voice.engine == "sapi5":
            payload = {"action": "render", "voice": entry.native_id, "text": request.text,
                       "output": str(stage), "rate": round(math.log2(request.rate) * 5),
                       "volume": round(request.volume * 100)}
            _run(_powershell_args(entry.executable),
                 json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                 cancelled=cancelled, timeout=timeout_seconds, output_path=stage,
                 max_output_bytes=max_output_bytes)
        else:
            args = _espeak_args(entry.executable, entry.data_path) + [
                "-v", entry.native_id, "-b", "1", "-s", str(round(175 * request.rate)),
                "-a", str(round(100 * request.volume)), "-p", str(round(50 + request.pitch * 49)),
                "-w", str(stage), "--stdin"]
            _run(args, request.text.encode("utf-8"), cancelled=cancelled,
                 timeout=timeout_seconds, output_path=stage, max_output_bytes=max_output_bytes)
        _check_cancel(cancelled)
        if max_output_bytes is not None and stage.stat().st_size > max_output_bytes:
            raise SpeechBudgetExceeded("The speech chunk exceeded its output byte budget.")
        rate, channels, frames = _inspect_wav(stage)
        digest = _file_digest(stage)
        if _fingerprint(entry) != fingerprint:
            raise SpeechError("The speech executable changed during rendering; output was not published.")
        _check_cancel(cancelled)
        _publish(stage, target, parent_identity)
    return SpeechResult(target, rate, channels, frames, frames / rate,
                        entry.voice.engine, entry.voice.id, digest, fingerprint)
