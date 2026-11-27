"""External-decoder and streaming-byte oracles, independent of renderer helpers.

The final file is decoded by a separate real FFmpeg process. Its entire PCM
payload is counted and hashed; a plausible duration in a header is insufficient.
Scene WAVs use Python's independent wave reader to establish concatenation order.
All reads have bounded buffers. Nothing in this module synthesizes a renderer
result, trusts the renderer's WAV inspector, or downloads a runtime.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import wave


BLOCK = 1024 * 1024


@dataclass(frozen=True)
class DecodedPCM:
    frames: int
    bytes: int
    sha256: str
    nonzero_bytes: int

    def to_dict(self):
        return asdict(self)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_pcm(path: Path, ffprobe: str, *, rate: int, frames: int) -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_name,sample_rate,channels,bits_per_sample,duration_ts,time_base",
         "-of", "json", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=30,
    )
    streams = json.loads(result.stdout)["streams"]
    if len(streams) != 1:
        raise AssertionError("Expected exactly one audio stream.")
    value = streams[0]
    if (value.get("codec_name"), int(value["sample_rate"]), value["channels"],
            value["bits_per_sample"]) != ("pcm_s16le", rate, 2, 16):
        raise AssertionError(f"Unexpected actual output format: {value!r}")
    if Fraction(value["duration_ts"]) * Fraction(value["time_base"]) * rate != frames:
        raise AssertionError("The independent probe reports an incorrect frame count.")
    return value


def decode_pcm(path: Path, ffmpeg: str, *, expected_frames: int,
               expected_sha256: str | None = None) -> DecodedPCM:
    """Read every decoded sample; never capture an entire waveform in memory."""
    digest, count, nonzero = hashlib.sha256(), 0, 0
    # A file avoids deadlock or an unbounded stderr PIPE. The trusted local
    # decoder receives only test-owned, finite, prepared PCM inputs.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
             "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:a:0",
             "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
        )
        try:
            assert process.stdout is not None
            for block in iter(lambda: process.stdout.read(BLOCK), b""):
                count += len(block)
                if count > expected_frames * 4:
                    raise AssertionError("Decoded output exceeds the approved frame count.")
                digest.update(block)
                nonzero += len(block) - block.count(b"\0")
            code = process.wait(timeout=30)
            errors.seek(0)
            detail = errors.read(8192).decode("utf-8", "replace")
            if code or detail.strip():
                raise AssertionError(f"Actual decoder rejected the output ({code}): {detail}")
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    if count != expected_frames * 4:
        raise AssertionError(f"Decoded {count} bytes, expected {expected_frames * 4}.")
    value = DecodedPCM(count // 4, count, digest.hexdigest(), nonzero)
    if expected_sha256 is not None and value.sha256 != expected_sha256:
        raise AssertionError("Decoded PCM differs from the approved source/scene sequence.")
    return value


def sequence_pcm(paths: list[Path], *, rate: int, frames_per_scene: list[int]) -> dict:
    """Hash raw scene frames in explicit order using stdlib, not FFmpeg concat."""
    if len(paths) != len(frames_per_scene):
        raise AssertionError("Scene list and frame list differ.")
    digest, total, hashes = hashlib.sha256(), 0, []
    for path, expected in zip(paths, frames_per_scene):
        local, count = hashlib.sha256(), 0
        with wave.open(str(path), "rb") as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth(),
                    source.getcomptype(), source.getnframes()) != (rate, 2, 2, "NONE", expected):
                raise AssertionError("Scene PCM framing differs from the approved plan.")
            while block := source.readframes(BLOCK // 4):
                count += len(block)
                digest.update(block)
                local.update(block)
        if count != expected * 4:
            raise AssertionError("A scene has a truncated PCM payload.")
        total += count
        hashes.append(local.hexdigest())
    return {"frames": total // 4, "bytes": total, "sha256": digest.hexdigest(),
            "scene_pcm_sha256": hashes}


def verify_publication(project_dir: Path, manifest_path: Path, ffmpeg: str,
                       ffprobe: str, *, expected_frames: int, rate: int) -> dict:
    """Check independent format/bytes plus receipt hashes and all chapter extents."""
    root = project_dir.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest["frames"], manifest["sample_rate"]) != (expected_frames, rate):
        raise AssertionError("Manifest frame count/rate does not match the requested timeline.")
    output = (root / manifest["outputs"]["mix"]["path"]).resolve()
    if not output.is_relative_to(root):
        raise AssertionError("Manifest refers outside the test project.")
    probe = probe_pcm(output, ffprobe, rate=rate, frames=expected_frames)
    whole_sha = file_sha256(output)
    if whole_sha != manifest["outputs"]["mix"]["sha256"]:
        raise AssertionError("Published file checksum disagrees with its manifest.")
    cache = root / ".audio-cache" / manifest["project"]["id"]
    position, paths, lengths = 0, [], []
    if len(manifest["chapters"]) != len(manifest["project"]["scenes"]):
        raise AssertionError("Manifest dropped or added a scene.")
    for chapter, scene in zip(manifest["chapters"], manifest["project"]["scenes"]):
        frames = (scene["duration_ms"] * rate + 500) // 1000
        if (chapter["id"], chapter["start_frame"], chapter["end_frame"]) != (
                scene["id"], position, position + frames):
            raise AssertionError("Chapter boundaries are reordered, overlapping, or incomplete.")
        receipt = json.loads((cache / (chapter["scene_sha256"] + ".json")).read_text())
        scene_path = (cache / receipt["outputs"]["mix"]["file"]).resolve()
        if not scene_path.is_relative_to(cache.resolve()):
            raise AssertionError("Receipt refers outside the test cache.")
        if file_sha256(scene_path) != chapter["mix_sha256"]:
            raise AssertionError("Completed scene bytes changed after publication.")
        paths.append(scene_path)
        lengths.append(frames)
        position += frames
    if position != expected_frames:
        raise AssertionError("Chapter extents do not cover the complete output.")
    sequence = sequence_pcm(paths, rate=rate, frames_per_scene=lengths)
    decoded = decode_pcm(output, ffmpeg, expected_frames=expected_frames,
                         expected_sha256=sequence["sha256"])
    if decoded.nonzero_bytes == 0:
        raise AssertionError("The prepared-track proof unexpectedly produced only silence.")
    for relative, expected in manifest["input_sha256"].items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or file_sha256(source) != expected:
            raise AssertionError("An original test asset changed.")
    return {"file_sha256": whole_sha, "probe": probe, "decoded": decoded.to_dict(),
            "scene_count": len(paths), "scene_pcm_sha256": sequence["scene_pcm_sha256"]}
