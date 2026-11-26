"""Local, non-destructive audio operations through a user-installed FFmpeg.

Only numeric, allowlisted effects become filter expressions. Inputs are forced
through known audio demuxers, with file-only protocols. No arbitrary command,
filter, playlist, URL or plug-in is accepted. Long output is streamed to disk;
WAV uses RF64 automatically. The caller owns authorization of selected paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping, Sequence


class AudioError(RuntimeError):
    """The selected local runtime could not perform an audio operation."""


class AudioCancelled(AudioError):
    """Rendering stopped; an incomplete output was not published."""


@dataclass(frozen=True)
class AudioInfo:
    path: Path
    sample_rate: int
    channels: int
    frames: int
    codec: str
    exact_frames: bool = True

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate


@dataclass(frozen=True)
class Effect:
    name: str
    parameters: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Track:
    path: Path
    start_frame: int = 0
    pan: float = 0.0
    gain: float = 1.0
    kind: str = "voice"
    effects: tuple[Effect, ...] = ()
    preserve_stereo: bool = False


# name: (FFmpeg filter, parameter: (default, minimum, maximum, unit))
EFFECTS = {
    "eq": ("equalizer", {"frequency": (1000, 20, 20000, "Hz"),
        "gain": (0, -24, 24, "dB"), "q": (1, .1, 20, "Q")}),
    "gate": ("agate", {"threshold": (.02, .0001, 1, "linear"),
        "ratio": (4, 1, 100, "ratio"), "attack": (10, .01, 1000, "ms"),
        "release": (150, .01, 9000, "ms")}),
    "compressor": ("acompressor", {"threshold": (.2, .001, 1, "linear"),
        "ratio": (3, 1, 20, "ratio"), "attack": (20, .01, 1000, "ms"),
        "release": (200, .01, 9000, "ms"), "makeup": (1, 1, 8, "linear")}),
    "limiter": ("alimiter", {"limit": (.95, .0625, 1, "linear")}),
    "denoise": ("afftdn", {"reduction": (12, .01, 97, "dB"),
        "floor": (-50, -80, -20, "dB")}),
    "delay": ("aecho", {"delay_ms": (180, .1, 90000, "ms"),
        "decay": (.25, 0, .9, "linear")}),
    "fade_in": ("afade", {"seconds": (.25, .001, 86400, "s")}),
    "fade_out": ("afade", {"seconds": (.25, .001, 86400, "s")}),
    "normalize": ("loudnorm", {"lufs": (-18, -70, -5, "LUFS"),
        "true_peak": (-1.5, -9, -.1, "dBTP"), "range": (11, 1, 50, "LU")}),
    "highpass": ("highpass", {"frequency": (80, 10, 20000, "Hz")}),
    "lowpass": ("lowpass", {"frequency": (12000, 10, 24000, "Hz")}),
    "gain": ("volume", {"db": (0, -96, 12, "dB")}),
}

_DEMUXERS = {".wav": "wav", ".flac": "flac", ".mp3": "mp3",
    ".ogg": "ogg", ".opus": "ogg", ".aac": "aac", ".m4a": "mov",
    ".m4b": "mov", ".aif": "aiff", ".aiff": "aiff", ".wma": "asf"}
_FORMATS = {"wav": ("pcm_s16le", "wav", ()), "flac": ("flac", "flac", ()),
    "mp3": ("libmp3lame", "mp3", ("-b:a", "160k")),
    "opus": ("libopus", "ogg", ("-b:a", "96k")),
    "ogg": ("libvorbis", "ogg", ("-q:a", "5")),
    "aac": ("aac", "adts", ("-b:a", "160k")),
    "m4a": ("aac", "ipod", ("-b:a", "160k")),
    "m4b": ("aac", "ipod", ("-b:a", "160k")),
    "aiff": ("pcm_s16be", "aiff", ()), "wma": ("wmav2", "asf", ("-b:a", "160k"))}
_MAX_CAPTURE = 256 * 1024


def _number(value, low, high, name):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number from {low} to {high}.")
    return float(value)


def _frames(value, name="frames", *, zero=False):
    # FFmpeg timestamps use signed 64-bit integers. This is a representation
    # limit, not an arbitrary duration ceiling on a user's project.
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**62:
        raise ValueError(f"{name} must be a {'nonnegative' if zero else 'positive'} frame count.")
    return value


def _check_cancel(cancel, deadline):
    stopped = (cancel.is_set() if hasattr(cancel, "is_set") else cancel()) if cancel else False
    if stopped:
        raise AudioCancelled("Audio operation cancelled; completed source files are unchanged.")
    if deadline is not None and time.monotonic() >= deadline:
        raise AudioCancelled("Audio operation exceeded the approved time budget.")


def _local(path, *, existing=True):
    path = Path(path).expanduser()
    if any(ord(c) < 32 for c in str(path)) or str(path).startswith(("//", "\\\\")):
        raise ValueError("Choose a local file path without control characters or a network share.")
    path = path.absolute()
    # Inspect before resolving: a Windows junction can redirect a local drive
    # into a network share, and Python 3.11 has no Path.is_junction(). lstat's
    # reparse flag also covers other redirected Windows filesystem objects.
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue  # A new output filename may not exist yet.
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("Symbolic-link or reparse-point audio paths are not permitted.")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError("Mapped network drives are unavailable in LOCAL ONLY mode.")
    if existing and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Select a regular local file.")
    return path


def _identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _unchanged(inputs):
    for path, identity in inputs:
        if _identity(_local(path)) != identity:
            raise AudioError("An input changed during rendering. The output was not published.")


def _publish(staged, target):
    # Staging is deliberately on the destination volume. Hard-link creation is
    # an atomic no-replace operation on NTFS and normal local POSIX filesystems.
    # A filesystem without it fails safely rather than falling back to replace.
    _local(staged)
    _local(target, existing=False)
    with staged.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    os.link(staged, target)
    if os.name != "nt":
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def _destination(output):
    target = _local(output, existing=False)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Output already exists: {target.name}")
    if not target.parent.is_dir():
        raise ValueError("The output directory must already exist.")
    with tempfile.TemporaryDirectory(prefix=".carr-audio-", dir=target.parent) as temp:
        yield target, Path(temp) / ("render" + target.suffix.lower())


def _run(args, *, cancel=None, deadline=None, cwd=None, capture=False):
    """Drain child pipes continuously while retaining bounded diagnostic tails."""
    _check_cancel(cancel, deadline)
    tails = [bytearray(), bytearray()]
    overflow = [False, False]

    def drain(stream, index):
        try:
            while block := stream.read(16384):
                tails[index].extend(block)
                if len(tails[index]) > _MAX_CAPTURE:
                    overflow[index] = True
                    del tails[index][:-_MAX_CAPTURE]
        finally:
            stream.close()

    process = subprocess.Popen([str(arg) for arg in args], cwd=cwd,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    readers = [threading.Thread(target=drain, args=(process.stderr, 1), daemon=True)]
    if capture:
        readers.append(threading.Thread(target=drain, args=(process.stdout, 0), daemon=True))
    for reader in readers:
        reader.start()
    try:
        while process.poll() is None:
            _check_cancel(cancel, deadline)
            time.sleep(.03)
        _check_cancel(cancel, deadline)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=3)
    error = bytes(tails[1]).decode("utf-8", errors="replace")
    if process.returncode:
        raise AudioError(f"Local audio process failed ({process.returncode}): {error[-4000:].strip()}")
    if capture and overflow[0]:
        raise AudioError("Local audio runtime returned an oversized response.")
    return bytes(tails[0]).decode("utf-8", errors="replace"), error


class AudioBackend:
    def __init__(self, ffmpeg=None, ffprobe=None, *, sample_rate=24000, max_tracks=128):
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("Sample rate must be an integer from 8000 to 192000 Hz.")
        if type(max_tracks) is not int or max_tracks < 1:
            raise ValueError("The simultaneous track budget must be positive.")
        ffmpeg = ffmpeg or shutil.which("ffmpeg")
        ffprobe = ffprobe or shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise AudioError("FFmpeg and ffprobe are not installed. Choose verified local executables in Audio Studio.")
        self.ffmpeg, self.ffprobe = _local(ffmpeg), _local(ffprobe)
        self.sample_rate, self.max_tracks = sample_rate, max_tracks
        self._caps = None
        self._identity_value = None
        self._binary_signature = None

    @property
    def identity(self):
        """Cache identity binds executable bytes and reported linked versions."""
        signature = (_identity(self.ffmpeg), _identity(self.ffprobe))
        if self._identity_value is None or signature != self._binary_signature:
            digest = hashlib.sha256(b"carr-ffmpeg-adapter-v1\0")
            for binary in (self.ffmpeg, self.ffprobe):
                with binary.open("rb") as handle:
                    digest.update(hashlib.file_digest(handle, "sha256").digest())
                version, _ = _run([binary, "-version"], capture=True, deadline=time.monotonic() + 15)
                digest.update(version.encode("utf-8"))
            self._identity_value = "ffmpeg-v1:" + digest.hexdigest()
            self._binary_signature = signature
            self._caps = None
        return self._identity_value

    def capabilities(self):
        if self._caps is None:
            deadline = time.monotonic() + 30
            version, _ = _run([self.ffmpeg, "-hide_banner", "-version"], capture=True, deadline=deadline)
            filters, _ = _run([self.ffmpeg, "-hide_banner", "-filters"], capture=True, deadline=deadline)
            encoders, _ = _run([self.ffmpeg, "-hide_banner", "-encoders"], capture=True, deadline=deadline)
            names = set(re.findall(r"^\s*[TSC.]{3}\s+(\w+)\s", filters, re.M))
            encoder_names = set(re.findall(r"^\s*[VAS][A-Z.]{5}\s+(\w+)\s", encoders, re.M))
            self._caps = {"version": version.splitlines()[0], "filters": sorted(names),
                "formats": sorted(k for k, (codec, _, _) in _FORMATS.items() if codec in encoder_names),
                "effects": sorted(k for k, (name, _) in EFFECTS.items() if name in names),
                "inputs": sorted(_DEMUXERS), "sample_rate": self.sample_rate,
                "local_only": True, "realtime_effects": False,
                "pan": "Equal-power mono placement; stereo balance preserves existing channels.",
                "unsupported": ["native room reverb", "arbitrary VST plug-ins", "formant adjustment",
                                "binaural placement", "neural music generation"]}
        return dict(self._caps)

    def effect_schema(self):
        available = set(self.capabilities()["effects"])
        return {name: {"available": name in available, "parameters": {
            key: {"default": values[0], "minimum": values[1], "maximum": values[2], "unit": values[3]}
            for key, values in params.items()}} for name, (_, params) in EFFECTS.items()}

    def _input_args(self, source):
        path = _local(source)
        if path.suffix.lower() not in _DEMUXERS:
            raise ValueError("Unsupported audio input format; playlists and URLs are not accepted.")
        args = ["-protocol_whitelist", "file", "-f", _DEMUXERS[path.suffix.lower()]]
        if _DEMUXERS[path.suffix.lower()] == "mov":
            args += ["-enable_drefs", "0"]
        return path, [*args, "-i", str(path)]

    def probe(self, path, *, cancel=None, deadline=None):
        path, args = self._input_args(path)
        _check_cancel(cancel, deadline)
        probe_deadline = min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30
        data, _ = _run([self.ffprobe, "-v", "error", *args,
            "-select_streams", "a:0", "-show_entries",
            "stream=codec_name,sample_rate,channels,duration_ts,time_base,duration:format=duration",
            "-of", "json"], capture=True, cancel=cancel, deadline=probe_deadline)
        try:
            parsed = json.loads(data)
            stream, = parsed["streams"]
            rate, channels = int(stream["sample_rate"]), int(stream["channels"])
            if not 1 <= rate <= 768000 or not 1 <= channels <= 64:
                raise ValueError()
            if "duration_ts" in stream and "time_base" in stream:
                raw_frames = int(stream["duration_ts"]) * Fraction(stream["time_base"]) * rate
                frames = round(raw_frames)
                exact = raw_frames.denominator == 1 and str(stream["codec_name"]).startswith(("pcm_", "flac"))
            else:
                seconds = Fraction(str(stream.get("duration", parsed.get("format", {}).get("duration"))))
                frames, exact = round(seconds * rate), False
            _frames(frames)
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            raise AudioError("Audio duration, channel count or sample rate could not be verified.") from exc
        return AudioInfo(path, rate, channels, frames, str(stream["codec_name"]), exact)

    def _effects(self, effects, duration_frames):
        if not isinstance(effects, (tuple, list)) or len(effects) > 64:
            raise ValueError("Provide a bounded ordered list of at most 64 typed effects.")
        result, tail = [], 0
        for effect in effects:
            if not isinstance(effect, Effect) or effect.name not in EFFECTS:
                raise ValueError("Unsupported effect. Use the available Audio Studio effect controls.")
            name, schema = EFFECTS[effect.name]
            if effect.name not in self.capabilities()["effects"]:
                raise AudioError(f"This FFmpeg build does not provide the {effect.name} effect.")
            if not isinstance(effect.parameters, Mapping) or set(effect.parameters) - set(schema):
                raise ValueError(f"Unsupported parameters for {effect.name}.")
            p = {key: _number(effect.parameters.get(key, value[0]), value[1], value[2], key)
                 for key, value in schema.items()}
            if "frequency" in p and p["frequency"] >= self.sample_rate / 2:
                raise ValueError("Filter frequency must be below half the project sample rate.")
            if effect.name == "eq":
                expression = f"equalizer=f={p['frequency']}:t=q:w={p['q']}:g={p['gain']}"
            elif effect.name in ("gate", "compressor"):
                expression = name + "=" + ":".join(f"{key}={value}" for key, value in p.items())
            elif effect.name == "limiter":
                expression = f"alimiter=limit={p['limit']}:level=0:latency=1"
            elif effect.name == "denoise":
                expression = f"afftdn=nr={p['reduction']}:nf={p['floor']}"
            elif effect.name == "delay":
                expression = f"aecho=1:1:{p['delay_ms']}:{p['decay']}"
                tail += math.ceil(p["delay_ms"] * self.sample_rate / 1000)
            elif effect.name in ("fade_in", "fade_out"):
                seconds = min(p["seconds"], duration_frames / self.sample_rate)
                start = 0 if effect.name == "fade_in" else max(0, duration_frames / self.sample_rate - seconds)
                expression = f"afade=t={'in' if effect.name == 'fade_in' else 'out'}:st={start}:d={seconds}"
            elif effect.name == "normalize":
                expression = f"loudnorm=I={p['lufs']}:TP={p['true_peak']}:LRA={p['range']}"
            elif effect.name in ("highpass", "lowpass"):
                expression = f"{name}=f={p['frequency']}"
            else:
                expression = f"volume={p['db']}dB"
            result.append(expression)
        return result, tail

    def _encoder_args(self, format):
        if format not in self.capabilities()["formats"]:
            raise AudioError(f"Output format {format!r} is not supported by the selected local FFmpeg build.")
        codec, muxer, extra = _FORMATS[format]
        args = ["-c:a", codec, *extra, "-f", muxer]
        if format == "wav":
            args += ["-rf64", "auto"]
        return args

    def _base(self):
        return [str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1"]

    def _verify_output(self, path, *, expected_frames=None, expected_rate=None, cancel=None, deadline=None):
        info = self.probe(path, cancel=cancel, deadline=deadline)
        if info.sample_rate != (expected_rate or self.sample_rate) or info.channels != 2:
            raise AudioError("Rendered audio does not match the project sample rate and stereo layout.")
        if expected_frames is not None and info.frames != expected_frames:
            raise AudioError(f"Rendered frame count differs from the plan ({info.frames} versus {expected_frames}).")
        return info

    def mix(self, tracks: Sequence[Track], output, *, duration_frames=None,
            duck_music=True, master_effects=(), cancel=None, deadline=None):
        if not isinstance(tracks, (tuple, list)) or len(tracks) > self.max_tracks:
            raise ValueError("Scene exceeds the configured simultaneous track budget. Split it into scenes or raise the budget.")
        if type(duck_music) is not bool:
            raise ValueError("Music ducking must be enabled or disabled explicitly.")
        args, sources, prepared, natural_end = self._base(), [], [], 0
        for track in tracks:
            if not isinstance(track, Track) or track.kind not in ("voice", "music", "effect", "ambience"):
                raise ValueError("Every scene entry must be a typed voice, music, effect or ambience track.")
            start = _frames(track.start_frame, "start_frame", zero=True)
            pan = _number(track.pan, -1, 1, "pan")
            gain = _number(track.gain, 0, 8, "gain")
            path, input_args = self._input_args(track.path)
            identity = _identity(path)
            info = self.probe(path, cancel=cancel, deadline=deadline)
            if info.channels > 2:
                raise ValueError("Explicit multichannel routing is not implemented; choose a mono or stereo source.")
            if type(track.preserve_stereo) is not bool:
                raise ValueError("Stereo preservation must be an explicit boolean.")
            if track.preserve_stereo and (info.channels != 2 or pan != 0):
                raise ValueError("Stereo preservation requires a stereo source and zero additional pan.")
            source_frames = math.ceil(Fraction(info.frames * self.sample_rate, info.sample_rate))
            filters, tail = self._effects(track.effects, source_frames)
            natural_end = max(natural_end, start + source_frames + tail)
            prepared.append((track, info, pan, gain, filters))
            sources.append((path, identity))
            args.extend(input_args)
        if duration_frames is None:
            duration_frames = natural_end
        duration_frames = _frames(duration_frames, "duration_frames")
        master, _ = self._effects(master_effects, duration_frames)
        graph, speech, background = [], [], []
        for index, (track, info, pan, gain, filters) in enumerate(prepared):
            layout = "mono" if info.channels == 1 else "stereo"
            chain = [f"aresample={self.sample_rate}", f"aformat=sample_fmts=fltp:channel_layouts={layout}",
                     *filters, f"aresample={self.sample_rate}", f"volume={gain}"]
            if info.channels == 1:
                left, right = math.sqrt((1-pan)/2), math.sqrt((1+pan)/2)
                chain.append(f"pan=stereo|c0={left:.12f}*c0|c1={right:.12f}*c0")
            else:
                left, right = min(1, 1-pan), min(1, 1+pan)
                chain.append(f"pan=stereo|c0={left:.12f}*c0|c1={right:.12f}*c1")
            chain += [f"adelay={track.start_frame}S:all=1", f"apad=whole_len={duration_frames}",
                      f"atrim=end_sample={duration_frames}", "asetpts=N/SR/TB"]
            label = f"t{index}"
            graph.append(f"[{index}:a:0]" + ",".join(chain) + f"[{label}]")
            (background if track.kind in ("music", "ambience") else speech).append(label)

        def group(labels, output_label):
            if len(labels) == 1:
                graph.append(f"[{labels[0]}]anull[{output_label}]")
            else:
                graph.append("".join(f"[{label}]" for label in labels) +
                    f"amix=inputs={len(labels)}:duration=longest:normalize=0[{output_label}]")

        if not prepared:
            graph.append(f"anullsrc=r={self.sample_rate}:cl=stereo,atrim=end_sample={duration_frames}[mixed]")
        elif speech and background:
            group(speech, "speech")
            group(background, "background")
            if duck_music:
                graph += ["[speech]asplit=2[direct][detector]",
                    "[background][detector]sidechaincompress=threshold=0.04:ratio=8:attack=10:release=150[ducked]",
                    "[direct][ducked]amix=inputs=2:duration=longest:normalize=0[mixed]"]
            else:
                graph.append("[speech][background]amix=inputs=2:duration=longest:normalize=0[mixed]")
        else:
            group(speech or background, "mixed")
        graph.append("[mixed]" + ",".join([*master, f"aresample={self.sample_rate}",
            "alimiter=limit=0.95:level=0:latency=1", f"apad=whole_len={duration_frames}",
            f"atrim=end_sample={duration_frames}"]) + "[out]")
        format = Path(output).suffix.lower().lstrip(".")
        if format not in ("wav", "flac"):
            raise ValueError("Scene masters must be WAV or FLAC; convert the verified master for compressed delivery.")
        with _destination(output) as (target, staged):
            _run([*args, "-filter_complex_threads", "1", "-filter_complex", ";".join(graph),
                "-map", "[out]", "-map_metadata", "-1", "-ar", self.sample_rate, "-ac", "2",
                *self._encoder_args(format), "-n", staged], cancel=cancel, deadline=deadline)
            info = self._verify_output(staged, expected_frames=duration_frames, cancel=cancel, deadline=deadline)
            _unchanged(sources)
            _check_cancel(cancel, deadline)
            _publish(staged, target)
        return AudioInfo(target, info.sample_rate, info.channels, info.frames, info.codec, info.exact_frames)

    render_scene = mix

    def concatenate(self, paths, output, *, cancel=None, deadline=None):
        if not isinstance(paths, (tuple, list)) or not paths:
            raise ValueError("Choose at least one rendered scene to assemble.")
        sources, infos, inspected = [], [], {}
        for path in paths:
            path = _local(path)
            if path.suffix.lower() not in (".wav", ".flac"):
                raise ValueError("Lossless scene masters are required for frame-exact assembly.")
            identity = _identity(path)
            sources.append((path, identity))
            key = (path, identity)
            if key not in inspected:
                inspected[key] = self.probe(path, cancel=cancel, deadline=deadline)
            info = inspected[key]
            if (info.sample_rate, info.channels) != (self.sample_rate, 2) or not info.exact_frames:
                raise ValueError("All scene masters must match the project sample rate and stereo layout.")
            infos.append(info)
        expected = _frames(sum(info.frames for info in infos))
        if len({info.codec for info in infos}) != 1:
            raise ValueError("Scene masters must use one lossless encoding. Convert them to WAV or FLAC before joining.")
        format = Path(output).suffix.lower().lstrip(".")
        if format not in ("wav", "flac"):
            raise ValueError("Assemble to WAV or FLAC, then select a compressed export.")
        with _destination(output) as (target, staged):
            manifest = staged.parent / "scenes.ffconcat"
            with manifest.open("w", encoding="utf-8", newline="\n") as listing:
                listing.write("ffconcat version 1.0\n")
                for index, (path, _) in enumerate(sources):
                    _check_cancel(cancel, deadline)
                    # Safe generated relative names prevent any playlist or
                    # filter escaping ambiguity, including Unicode originals.
                    name = f"scene{index:08d}{path.suffix.lower()}"
                    linked = staged.parent / name
                    try:
                        os.link(path, linked)
                    except OSError:
                        # Cross-volume input: use a bounded-buffer snapshot.
                        with path.open("rb") as source, linked.open("xb") as dest:
                            while block := source.read(1024 * 1024):
                                _check_cancel(cancel, deadline)
                                dest.write(block)
                    listing.write(f"file '{name}'\n")
            _run([*self._base(), "-protocol_whitelist", "file", "-format_whitelist", "concat,wav,flac",
                "-f", "concat", "-safe", "1", "-i", manifest,
                "-map", "0:a:0", "-map_metadata", "-1", "-ar", self.sample_rate, "-ac", "2",
                *self._encoder_args(format), "-n", staged], cancel=cancel, deadline=deadline)
            info = self._verify_output(staged, expected_frames=expected, cancel=cancel, deadline=deadline)
            _unchanged(sources)
            _check_cancel(cancel, deadline)
            _publish(staged, target)
        return AudioInfo(target, info.sample_rate, info.channels, info.frames, info.codec, info.exact_frames)

    def convert(self, source, output, *, format="flac", effects=(), cancel=None, deadline=None):
        path, input_args = self._input_args(source)
        identity = _identity(path)
        info = self.probe(path, cancel=cancel, deadline=deadline)
        frames = math.ceil(Fraction(info.frames * self.sample_rate, info.sample_rate))
        filters, tail = self._effects(effects, frames)
        if format == "aiff" and (frames + tail) * 4 >= 2**32 - 4096:
            raise ValueError("This output exceeds AIFF's file-size limit; choose RF64 WAV or FLAC.")
        suffix = Path(output).suffix.lower().lstrip(".")
        if suffix != format and not (format == "aiff" and suffix == "aif"):
            raise ValueError("Output extension must match the selected format.")
        # Opus uses a 48 kHz decoded clock irrespective of its input rate.
        output_rate = 48000 if format == "opus" else self.sample_rate
        with _destination(output) as (target, staged):
            _run([*self._base(), *input_args, "-map", "0:a:0", "-map_metadata", "-1",
                "-af", ",".join([f"aresample={self.sample_rate}", *filters,
                    f"aresample={self.sample_rate}", "alimiter=limit=0.95:level=0:latency=1"]),
                "-ar", output_rate, "-ac", "2", *self._encoder_args(format), "-n", staged],
                cancel=cancel, deadline=deadline)
            result = self._verify_output(staged, expected_rate=output_rate, cancel=cancel, deadline=deadline)
            _unchanged([(path, identity)])
            _check_cancel(cancel, deadline)
            _publish(staged, target)
        return AudioInfo(target, result.sample_rate, result.channels, result.frames, result.codec, result.exact_frames)

    def measure(self, source, *, cancel=None, deadline=None):
        """Measure actual EBU R128 loudness and true peak; silence reports None."""
        path, input_args = self._input_args(source)
        info = self.probe(path, cancel=cancel, deadline=deadline)
        _, log = _run([self.ffmpeg, "-hide_banner", "-loglevel", "info", "-nostdin",
            *input_args, "-map", "0:a:0", "-af", "loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"], cancel=cancel, deadline=deadline)
        match = re.search(r'\{\s*"input_i"\s*:.*?\}', log, re.S)
        if not match:
            raise AudioError("The selected runtime did not report loudness measurements.")
        values = json.loads(match.group(0))
        def measured(key):
            value = float(values[key])
            return value if math.isfinite(value) else None
        return {"integrated_lufs": measured("input_i"), "true_peak_dbfs": measured("input_tp"),
                "loudness_range_lu": measured("input_lra"), "duration_seconds": info.duration_seconds,
                "method": "FFmpeg loudnorm input EBU R128 / true-peak measurement"}

    def _generated(self, expression, output, *, duration_frames, cancel=None, deadline=None):
        """Internal source entrypoint; only music.py's numeric compiler calls it."""
        duration_frames = _frames(duration_frames)
        format = Path(output).suffix.lower().lstrip(".")
        if format not in ("wav", "flac"):
            raise ValueError("Generated masters must be WAV or FLAC.")
        with _destination(output) as (target, staged):
            _run([*self._base(), "-f", "lavfi", "-i", expression,
                "-af", f"aresample={self.sample_rate},apad=whole_len={duration_frames},atrim=end_sample={duration_frames},alimiter=limit=0.95:level=0:latency=1",
                "-map_metadata", "-1", "-ar", self.sample_rate, "-ac", "2",
                *self._encoder_args(format), "-n", staged], cancel=cancel, deadline=deadline)
            result = self._verify_output(staged, expected_frames=duration_frames, cancel=cancel, deadline=deadline)
            _check_cancel(cancel, deadline)
            _publish(staged, target)
        return AudioInfo(target, result.sample_rate, result.channels, result.frames, result.codec, result.exact_frames)
