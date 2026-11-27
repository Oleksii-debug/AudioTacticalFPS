#!/usr/bin/env python3
"""Opt-in actual Windows qualification through the canonical music service.

The separately reviewed runtime helper is reused byte-for-byte. Checkpoints are
acquired only after its no-weight audit receipt and full inventory are verified.
The real render uses the existing AudioRuntime / LocalModelConfig API. This is
not an application startup installer and does not supply another music worker.
No models, runtime archives, caches or audio are uploaded.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.parse
import urllib.request
import wave

AUDIOCRAFT_REVISION = "896ec7c47f5e5d1e5aa1e4b260c4405328bf009d"
MODEL_REVISION = "4c8334b02c6ec4e8664a91979669a501ec497792"
T5_REVISION = "a9723ea7f1b39c1eae772870f3b547bf6ef7e6c1"
CANONICAL_BASE = "a708de1dac9f7fce719e5588ace8001a666f5d99"
PREFLIGHT_HELPER_BLOB = "1c75f4672d4db6234b32fbc352d7e9191a5caab1"
PACKAGER_BLOB = "91f2d49b17c6f4f56da7578f0e30451a4ae8c1a5"
FFMPEG_LOCK_BLOB = "365097854290e7c4a6e0b1c5e26a900a98d011cf"
GIB = 1024**3
MAX_FILES = 100_000
MAX_ENTRIES = 65_536
MAX_MANIFEST_BYTES = 8 * 1024**2
ASSETS = (
    ("facebook/musicgen-small", MODEL_REVISION, "state_dict.bin", "state_dict.bin",
     840843863, "830fe5771e2835cbd305f3ffdda22d3ff38718fbbe8274bd19dc203265de7244"),
    ("facebook/musicgen-small", MODEL_REVISION, "compression_state_dict.bin", "compression_state_dict.bin",
     236001935, "37d256b525d4117f8bbf790ab448a8e9f4746cd401900fe1ae72e154b1513a30"),
    ("google-t5/t5-base", T5_REVISION, "model.safetensors", "t5/model.safetensors",
     891646390, "a90903540cc02cbeb7ff9f823f1a80eb778c7e22426a0e620b01c77a5ec8f5b4"),
    ("google-t5/t5-base", T5_REVISION, "config.json", "t5/config.json",
     1208, "46dd7cb62d29c81fb551e0ef1ea274c24a46ba441eeb948897706252933df033"),
    ("google-t5/t5-base", T5_REVISION, "spiece.model", "t5/spiece.model",
     791656, "d60acb128cf7b7f2536e8f38a5b18a05535c9e14c7a355904270e15b0945ea86"),
    ("google-t5/t5-base", T5_REVISION, "tokenizer.json", "t5/tokenizer.json",
     1389353, "d2acde0d8d71dd30a711834b07781b9c89feaac33fd332f60507699282740066"),
)


def emit(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            value.update(block)
    return value.hexdigest()


def require_blob(path, expected):
    raw = path.read_bytes()
    actual = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if actual != expected:
        raise ValueError("Review changed qualification source before execution: " + path.name)


def helper(args):
    path = args.source_root / "scripts" / "musicgen_canonical_windows_preflight.py"
    require_blob(path, PREFLIGHT_HELPER_BLOB)
    spec = importlib.util.spec_from_file_location("_canonical_windows_preflight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_modules(args):
    module = helper(args)
    paths, music = module.source_modules(args)
    return module, paths, music


def read_json(path, maximum):
    if path.stat().st_size > maximum:
        raise ValueError("Qualification evidence exceeded its byte allowance.")
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise ValueError("Qualification evidence grew beyond its byte allowance.")
    return json.loads(raw.decode("utf-8-sig"))


def verify_audio_runtime(args, paths):
    lock_path = args.source_root / "packaging" / "windows-audio.lock.json"
    require_blob(lock_path, FFMPEG_LOCK_BLOB)
    require_blob(args.source_root / "packaging" / "prepare_windows_audio.py", PACKAGER_BLOB)
    lock = read_json(lock_path, 128 * 1024)
    paths.local_path(args.ffmpeg_root, exists=True)
    record_path = args.ffmpeg_root / "runtime-manifest.json"
    paths.local_path(record_path, root=args.ffmpeg_root, exists=True)
    receipt = read_json(record_path, 128 * 1024)
    expected = {name: {"size": item["size"], "sha256": item["sha256"]}
                for name, item in lock["output_files"].items()}
    if receipt.get("lock_sha256") != digest(lock_path) or receipt.get("files") != expected:
        raise ValueError("The prepared FFmpeg receipt differs from the reviewed canonical lock.")
    observed_names = set()
    for number, path in enumerate(args.ffmpeg_root.rglob("*"), 1):
        if number > 16:
            raise ValueError("The reviewed four-file FFmpeg payload has unexpected entries.")
        paths.local_path(path, root=args.ffmpeg_root, exists=True)
        if path.is_file():
            observed_names.add(path.relative_to(args.ffmpeg_root).as_posix())
    if observed_names != set(expected) | {"runtime-manifest.json"}:
        raise ValueError("The prepared FFmpeg directory differs from its complete payload inventory.")
    for name, entry in expected.items():
        path = args.ffmpeg_root / name
        if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
            raise ValueError("A prepared FFmpeg file differs from its pinned size or SHA256.")
    return {"engine_version": lock["engine_version"], "lock_sha256": digest(lock_path),
            "manifest_sha256": digest(record_path), "files": expected,
            "role": "Separate CLI converter; PyAV retains its Conda FFmpeg 6 DLL closure"}


def require_preflight(args, module, paths, music):
    paths.local_path(args.preflight_receipt, exists=True)
    if args.preflight_receipt.is_relative_to(args.root):
        raise ValueError("Preflight console evidence must remain outside the immutable runtime.")
    if args.preflight_receipt.stat().st_size > 128 * 1024:
        raise ValueError("The preflight console receipt exceeds its diagnostic allowance.")
    raw = args.preflight_receipt.read_bytes()
    if len(raw) > 128 * 1024:
        raise ValueError("The preflight console receipt grew beyond its diagnostic allowance.")
    rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    matches = [row for row in rows if isinstance(row, dict) and row.get("status") == "PREFLIGHT_PASS"]
    if len(matches) != 1:
        raise ValueError("Exactly one successful canonical no-weight preflight is required.")
    result = matches[0]
    if (not os.environ.get("GITHUB_SHA") or result.get("application_commit") != os.environ["GITHUB_SHA"]
            or result.get("model_weights") != "NOT_DOWNLOADED"
            or result.get("actual_musicgen_inference") != "NOT_RUN"
            or result.get("canonical_worker_sha256") != hashlib.sha256(music._MODEL_WORKER.encode("utf-8")).hexdigest()
            or result.get("code_sha256") != {name: digest(args.source_root / "src" / "carr_library" / name)
                                           for name in module.SOURCE_BLOBS}):
        raise ValueError("The successful preflight is not bound to this exact source and no-weight stage.")
    snapshot = args.root / "runtime-inventory.json"
    paths.local_path(snapshot, root=args.root, exists=True)
    if digest(snapshot) != result.get("runtime_inventory_sha256"):
        raise ValueError("The complete runtime snapshot differs from the successful preflight.")
    before = read_json(snapshot, MAX_MANIFEST_BYTES)
    current, counts = module.inventory(args, paths, music)
    if current != before:
        raise ValueError("The full runtime changed after the successful no-weight preflight.")
    emit({"stage": "canonical_windows_preflight_receipt_verified",
          "application_commit": os.environ["GITHUB_SHA"], "runtime_inventory_sha256": digest(snapshot),
          "inventory": counts, "actual_musicgen_inference": "NOT_RUN"})
    return result


def asset_target(root, destination):
    if destination.startswith("t5/"):
        return root / "hf-cache" / "hub" / "models--t5-base" / "snapshots" / T5_REVISION / destination[3:]
    return root / destination


def prepare_assets(args):
    module, paths, music = source_modules(args)
    require_preflight(args, module, paths, music)
    verify_audio_runtime(args, paths)
    required = sum(item[4] for item in ASSETS)
    if shutil.disk_usage(args.root).free < required + 2 * GIB:
        raise OSError("The pinned assets require their exact bytes plus a 2 GiB reserve.")
    started = time.monotonic()
    for repository, revision, name, destination, size, expected in ASSETS:
        target = asset_target(args.root, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        paths.local_path(target, root=args.root)
        if target.exists():
            raise ValueError("The no-weight preflight must precede a fresh model acquisition.")
        partial = target.with_name(target.name + ".qualification-download")
        url = f"https://huggingface.co/{repository}/resolve/{revision}/{name}"
        observed = hashlib.sha256()
        count = 0
        file_started = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=60) as incoming, partial.open("xb") as output:
                if urllib.parse.urlsplit(incoming.geturl()).scheme != "https":
                    raise ValueError("A reviewed model download redirected outside HTTPS.")
                while block := incoming.read(1024 * 1024):
                    if time.monotonic() - file_started > 300 or time.monotonic() - started > 900:
                        raise TimeoutError("Pinned model acquisition exceeded its bounded deadline.")
                    count += len(block)
                    if count > size:
                        raise ValueError("The model download exceeds its exact reviewed size.")
                    observed.update(block)
                    output.write(block)
            if count != size or observed.hexdigest() != expected:
                raise ValueError("The model download differs from its exact size and SHA256.")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        emit({"stage": "asset_verified", "repository": repository, "revision": revision,
              "file": name, "bytes": size, "sha256": expected})
    hub = args.root / "hf-cache" / "hub"
    refs = hub / "models--t5-base" / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(T5_REVISION, encoding="ascii")
    (hub / "version.txt").write_text("1", encoding="ascii")
    emit({"stage": "assets_ready", "bytes": required, "actual_musicgen_inference": "NOT_RUN"})


def manifest_payload(files, records=None):
    return {"schema": 1, "engine": "musicgen", "code_revision": AUDIOCRAFT_REVISION,
            "weights_license": "CC-BY-NC-4.0", "noncommercial_only": True,
            "output_rights_notice": "Qualification-only original instrumental prompt. MusicGen weights CC-BY-NC-4.0; private noncommercial use. No commercial-use or third-party rights clearance is asserted.",
            "files": files, "runtime_code_files": records or {}}


def complete_inventory(args, module, paths, music):
    files = {}
    total = entries = 0
    for entries, path in enumerate(args.root.rglob("*"), 1):
        if entries > MAX_ENTRIES:
            raise ValueError("The installation exceeds this qualification's traversal allowance.")
        paths.local_path(path, root=args.root, exists=True)
        if path.is_file() and path != args.root / "local-model.json":
            files[path.relative_to(args.root).as_posix()] = digest(path)
            total += path.stat().st_size
            if len(files) > MAX_FILES or total > 64 * GIB:
                raise ValueError("The complete installation exceeds its file or byte allowance.")
    records = module.runtime_code_files(args, paths, music)
    for _, _, _, destination, size, expected in ASSETS:
        target = asset_target(args.root, destination)
        if files.get(target.relative_to(args.root).as_posix()) != expected or target.stat().st_size != size:
            raise ValueError("An exact reviewed model asset is missing from the complete inventory.")
    encoded = json.dumps(manifest_payload(files, records), sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("The complete installation exceeds the canonical manifest byte allowance.")
    return encoded, {"files": len(files), "entries": entries, "bytes": total,
                     "runtime_code_files": len(records), "manifest_bytes": len(encoded)}


def model_config(args, module):
    from carr_library.audio.models import LocalModelConfig
    return LocalModelConfig("musicgen", module.runtime_python(args), args.root,
                            args.root / "local-model.json", device="cpu",
                            private_noncommercial=True, max_chunk_seconds=2)


def make_manifest(args):
    module, paths, music = source_modules(args)
    verify_audio_runtime(args, paths)
    encoded, counts = complete_inventory(args, module, paths, music)
    target = args.root / "local-model.json"
    with target.open("xb") as output:
        output.write(encoded)
    music.validate_model(model_config(args, module))
    emit({"stage": "complete_canonical_windows_manifest_verified", "inventory": counts,
          "manifest_sha256": digest(target), "actual_musicgen_inference": "NOT_RUN"})


def render(args):
    module, paths, music = source_modules(args)
    if Path(sys.executable).resolve() != module.runtime_python(args).resolve():
        raise ValueError("Actual inference must run from the complete physical Windows runtime.")
    # The production worker inherits PATH; use exactly the configured prefix
    # already qualified by the no-site import gate. No Python path hooks.
    os.environ["PATH"] = module.environment(args, args.stage)["PATH"]
    baseline = read_json(args.root / "network-baseline.json", 8192)
    network_before = json.loads(module.capture(args, module.NETWORK_PROBE,
                                {"blocked": True, "address": baseline["address"]}, timeout=20))
    ffmpeg_before = verify_audio_runtime(args, paths)
    if args.output is None or args.output.is_relative_to(args.root) or args.output.exists():
        raise ValueError("Select a new output WAV outside the complete immutable installation.")
    paths.local_path(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    from carr_library.audio import AudioBudget, AudioRuntime, MusicSpec
    config = model_config(args, module)
    before = music.validate_model(config)
    manifest_hash = digest(config.manifest)
    encoded_before, counts_before = complete_inventory(args, module, paths, music)
    if config.manifest.read_bytes() != encoded_before:
        raise ValueError("The admitted manifest does not cover the full current installation.")
    runtime = AudioRuntime(args.output.parent, ffmpeg=args.ffmpeg_root / "bin" / "ffmpeg.exe",
                           ffprobe=args.ffmpeg_root / "bin" / "ffprobe.exe",
                           budget=AudioBudget(timeout=600, threads=2, max_output_bytes=64 * 1024**2))
    started = time.monotonic()
    runtime.configure_music_model(config)
    configured = dict(runtime.music_model_status["musicgen"])
    if configured.get("inference_tested") or configured.get("runtime_code_verified"):
        raise ValueError("Configuration incorrectly claimed runtime or inference acceptance.")
    probed = runtime.probe_music_model("musicgen")
    if not probed.get("runtime_code_verified") or probed.get("inference_tested"):
        raise ValueError("Canonical source probe confused code verification with inference.")
    try:
        result = runtime.render_music(
            MusicSpec(duration=2, engine="musicgen", mood="hopeful", tempo=100, seed=107,
                      prompt="Original instrumental music, no vocals, hopeful mood, 100 beats per minute."),
            args.output, sample_rate=48000, channels=1,
            progress=lambda seconds: emit({"stage": "canonical_windows_audio_progress", "seconds": seconds}))
    except Exception as exc:
        emit({"stage": "canonical_windows_render_failure", "diagnostics": str(exc)[-16384:],
              "actual_musicgen_inference": "FAIL"})
        raise
    with wave.open(str(result.path), "rb") as audio:
        actual = (audio.getnframes(), audio.getframerate(), audio.getnchannels(), audio.getsampwidth())
        if actual != (96000, 48000, 1, 2):
            raise ValueError("Generated audio does not match the two-second PCM contract.")
        samples = array("h", audio.readframes(96000))
        if sys.byteorder != "little":
            samples.byteswap()
    peak = max(map(abs, samples))
    if (len(samples) != 96000 or peak == 0 or result.frames != 96000 or result.sample_rate != 48000
            or result.channels != 1 or result.sha256 != digest(result.path)):
        raise ValueError("Canonical AudioInfo and actual nonzero PCM do not agree.")
    after = music.validate_model(config)
    encoded_after, counts_after = complete_inventory(args, module, paths, music)
    if (before != after or digest(config.manifest) != manifest_hash
            or encoded_before != encoded_after or counts_before != counts_after
            or verify_audio_runtime(args, paths) != ffmpeg_before):
        raise ValueError("A complete model/runtime/SDK/FFmpeg inventory changed during inference.")
    network_after = json.loads(module.capture(args, module.NETWORK_PROBE,
                               {"blocked": True, "address": baseline["address"]}, timeout=20))
    state = next(row for row in runtime.music_capabilities() if row["engine"] == "musicgen")
    if not state["runtime_code_verified"] or not state["inference_tested"] or not state["available"]:
        raise ValueError("Canonical service did not record its real verified inference.")
    emit({"status": "PASS", "actual_musicgen_inference": "PASS", "windows_inference": "PASS",
          "duration_seconds": 2, "frames": 96000, "sample_rate": 48000, "channels": 1,
          "peak_pcm16": peak, "audio_sha256": result.sha256,
          "elapsed_seconds": round(time.monotonic() - started, 3),
          "device": "cpu", "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
          "inventory": counts_after, "manifest_sha256": manifest_hash, "ffmpeg_runtime": ffmpeg_before,
          "network_control": {"before": network_before, "after": network_after,
                              "policy": "Windows program outbound rules plus unchanged canonical audit hook"},
          "application_commit": os.environ.get("GITHUB_SHA"), "canonical_base_commit": CANONICAL_BASE,
          "code_sha256": {name: digest(args.source_root / "src" / "carr_library" / name)
                          for name in module.SOURCE_BLOBS},
          "canonical_worker_sha256": hashlib.sha256(music._MODEL_WORKER.encode("utf-8")).hexdigest(),
          "qualification_helper_sha256": digest(Path(__file__)),
          "preflight_console_sha256": digest(args.preflight_receipt),
          "source_revision": AUDIOCRAFT_REVISION, "model_revision": MODEL_REVISION,
          "text_encoder_revision": T5_REVISION,
          "installed_versions": {p.metadata["Name"]: p.version for p in importlib.metadata.distributions()},
          "canonical_status_after_configure": configured, "canonical_status_after_probe": probed,
          "canonical_status_after_render": state,
          "launch_environment": "Explicit physical runtime, Library/bin and Scripts PATH; unconfigured desktop launch NOT_RUN",
          "listening": "NOT_RUN", "NVDA": "NOT_RUN", "multi_hour_neural_generation": "NOT_RUN",
          "asset_uploads": "NONE"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare-assets", "manifest", "render"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ffmpeg-root", type=Path, required=True)
    parser.add_argument("--preflight-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--accept-personal-noncommercial", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32" or not args.accept_personal_noncommercial:
        raise ValueError("This Windows trial requires explicit trusted-checkpoint/private-use acceptance.")
    for key in ("source_root", "root", "ffmpeg_root", "preflight_receipt"):
        path = getattr(args, key)
        if not path.is_absolute():
            raise ValueError("Qualification paths must be absolute.")
        setattr(args, key, path.absolute())
    args.stage = args.root.parent / (args.root.name + "-staging")
    if args.output is not None:
        args.output = args.output.absolute()
    {"prepare-assets": prepare_assets, "manifest": make_manifest, "render": render}[args.command](args)


if __name__ == "__main__":
    main()
