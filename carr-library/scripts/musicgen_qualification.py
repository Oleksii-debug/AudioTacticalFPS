#!/usr/bin/env python3
"""Opt-in, ephemeral CPU qualification of CARR's existing MusicGen adapter.

This is a qualification tool, never an application startup installer. It does
not implement another music worker. Runtime installation and model acquisition
are separate explicit commands; inference invokes carr_library.audio.music.
No models, runtime, manifest, or generated audio should be uploaded as artifacts.
"""
from __future__ import annotations

import argparse
from array import array
import ast
import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave


AUDIOCRAFT_REVISION = "896ec7c47f5e5d1e5aa1e4b260c4405328bf009d"
MODEL_REVISION = "4c8334b02c6ec4e8664a91979669a501ec497792"
T5_REVISION = "a9723ea7f1b39c1eae772870f3b547bf6ef7e6c1"
GIB = 1024**3
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

# These constrain upstream's requirements, including all its declared imports.
# The actual installed transitive inventory is hashed in the complete manifest
# and its versions are reported. This is a candidate runtime, not a tested lock.
CONSTRAINTS = """torch==2.1.0+cpu
torchaudio==2.1.0+cpu
torchvision==0.16.0+cpu
torchtext==0.16.0
torchdata==0.7.0
numpy==1.26.4
scipy==1.11.4
transformers==4.33.3
huggingface-hub==0.19.4
tokenizers==0.13.3
safetensors==0.4.3
sentencepiece==0.1.99
protobuf==4.25.3
xformers==0.0.22.post7
av==11.0.0
spacy==3.7.6
thinc==8.2.5
einops==0.7.0
flashy==0.0.2
dora-search==0.1.12
hydra-core==1.3.2
hydra-colorlog==1.2.0
julius==0.2.7
demucs==4.0.1
librosa==0.10.1
numba==0.59.1
llvmlite==0.42.0
soundfile==0.12.1
gradio==3.50.2
torchmetrics==1.2.1
encodec==0.1.1
pesq==0.0.4
pystoi==0.4.1
torchdiffeq==0.2.4
setuptools==68.2.2
wheel==0.43.0
Cython==3.0.12
sympy==1.12
"""


def emit(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def runtime_python(root):
    return root / "runtime" / "bin" / "python3.11"


def manifest_payload(files):
    return {"schema_version": 1, "engine": "musicgen", "model_id": "facebook/musicgen-small",
            "revision": MODEL_REVISION, "license": "CC-BY-NC-4.0", "files": files,
            "license_accepted": True, "trusted_weights": True, "personal_noncommercial": True,
            "settings": {"source_revision": AUDIOCRAFT_REVISION, "device": "cpu",
                         "python": "runtime/bin/python3.11", "timeout_seconds": 600}}


def measure_inventory(root, manifest_module):
    """Count the real tree without hiding files or changing acceptance caps."""
    entries = files = total_bytes = path_json_bytes = 0
    scan_limit = 2 * manifest_module.MAX_MODEL_DIRECTORY_ENTRIES
    for path in root.rglob("*"):
        entries += 1
        if entries > scan_limit:
            emit({"stage": "inventory_measurement_limit", "entries_lower_bound": entries,
                  "files_lower_bound": files, "qualification_scan_limit": scan_limit})
            raise ValueError("Inventory measurement exceeded its separate bounded traversal.")
        manifest_module.local_path(path)
        if path.is_file() and path != root / "local-model.json":
            files += 1
            total_bytes += path.stat().st_size
            relative = path.relative_to(root).as_posix()
            # Exact compact JSON contribution of path + 64-digit digest,
            # including quotes, colon and comma. Hash contents do not change it.
            path_json_bytes += len(json.dumps(relative).encode("ascii")) + 68
    files_json_bytes = 2 + path_json_bytes - bool(files)
    base_bytes = len(json.dumps(manifest_payload({}), separators=(",", ":")).encode("ascii"))
    return {"files": files, "entries": entries, "bytes": total_bytes,
            "estimated_manifest_bytes": base_bytes - 2 + files_json_bytes,
            "file_limit": manifest_module.MAX_MODEL_FILES,
            "entry_limit": manifest_module.MAX_MODEL_DIRECTORY_ENTRIES,
            "manifest_byte_limit": manifest_module.MAX_MODEL_MANIFEST_BYTES}


def distribution_record_counts(root):
    """Report public package counts, never individual installed machine paths."""
    prefix = root / "runtime"
    site = prefix / "lib" / "python3.11" / "site-packages"
    counts = {}
    total_rows = 0
    csv.field_size_limit(8192)
    for number, record in enumerate(sorted(site.glob("*.dist-info/RECORD")), 1):
        if number > 512:
            raise ValueError("Runtime distribution inventory exceeds its diagnostic budget.")
        declared = present = 0
        with record.open(encoding="utf-8", newline="") as stream:
            for row in csv.reader(stream):
                total_rows += 1
                if total_rows > 262144 or len(row) != 3:
                    raise ValueError("Runtime distribution records exceed their diagnostic budget.")
                declared += 1
                path = (site / row[0]).resolve()
                if not path.is_relative_to(prefix):
                    raise ValueError("A distribution record escapes the reviewed runtime.")
                present += path.is_file()
        counts[record.parent.name.removesuffix(".dist-info")] = {
            "declared_record_rows": declared, "present_files": present}
    return counts


def remove_redundant_bytecode(prefix):
    """Remove only cache files whose corresponding Python source still exists."""
    count = removed_bytes = 0
    for path in prefix.rglob("*.pyc"):
        try:
            source = Path(importlib.util.source_from_cache(str(path)))
        except ValueError:
            source = path.with_suffix(".py")
        if source.is_file() and source.is_relative_to(prefix):
            removed_bytes += path.stat().st_size
            path.unlink()
            count += 1
    for directory in sorted(prefix.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()  # only empty cache directories, never modules/tests
        except OSError:
            pass
    return {"removed_redundant_bytecode_files": count, "removed_bytes": removed_bytes}


def source_modules(args):
    source = args.source_root / "src"
    sys.path.insert(0, str(source))
    from carr_library.audio import model_manifest, music
    if music.MUSICGEN_SOURCE != AUDIOCRAFT_REVISION:
        raise ValueError("The reviewed CARR AudioCraft source revision changed.")
    if getattr(model_manifest, "MAX_MODEL_FILES", 0) < 10703:
        raise ValueError("Integrate the reviewed complete-runtime manifest fix first.")
    if not hasattr(music, "_MUSICGEN_WORKER"):
        raise ValueError("The CARR worker entry point changed; review this qualification tool.")
    return model_manifest, music


def environment(root, manifest_module):
    env = manifest_module.offline_environment()
    env.update(HF_HOME=str(root / "hf-cache"),
               HUGGINGFACE_HUB_CACHE=str(root / "hf-cache" / "hub"),
               AUDIOCRAFT_CACHE_DIR=str(root), TORCH_HOME=str(root / "torch-cache"),
               OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1",
               LD_LIBRARY_PATH=str(root / "runtime" / "lib"))
    return env


def capture_worker(python, code, request, *, root, env, timeout):
    """Capture bounded public qualification diagnostics without changing CARR."""
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        incoming.write(json.dumps(request).encode("utf-8"))
        incoming.seek(0)
        process = subprocess.Popen([str(python), "-I", "-B", "-u", "-c", code],
                                   stdin=incoming, stdout=output, stderr=errors,
                                   cwd=root, env=env, start_new_session=True)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    raise TimeoutError("Qualification worker exceeded its time budget.")
                if max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) > 128 * 1024:
                    raise ValueError("Qualification diagnostics exceeded their byte budget.")
                time.sleep(.1)
            output.seek(0)
            errors.seek(0)
            stdout, stderr = output.read(128 * 1024 + 1), errors.read(128 * 1024 + 1)
            if max(len(stdout), len(stderr)) > 128 * 1024:
                raise ValueError("Qualification diagnostics exceeded their byte budget.")
            if process.returncode:
                # Only fixed synthetic inputs and a disposable public runtime
                # are used here. Do not use this diagnostic tool with books.
                emit({"worker_exit": process.returncode,
                      "diagnostics": stderr.decode("utf-8", "replace")[-16384:]})
                raise RuntimeError("The existing CARR worker failed qualification.")
            return stdout.decode("utf-8")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def prepare_runtime(args):
    if sys.version_info[:2] != (3, 11) or sys.platform != "linux":
        raise ValueError("This candidate runtime requires Linux CPython 3.11.")
    model_manifest, _ = source_modules(args)  # Refuse the known defect before any install.
    args.root.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.root.parent).free < 10 * GIB:
        raise OSError("Qualification needs at least 10 GiB free before installation.")
    args.root.mkdir(exist_ok=False)
    started = time.monotonic()
    # An ordinary venv, even --copies, still uses a base stdlib outside the
    # manifest. Copy the complete prefix and dereference its trusted links.
    shutil.copytree(Path(sys.base_prefix), args.root / "runtime", symlinks=False)
    python = runtime_python(args.root)
    constraints = args.root / "qualification-constraints.txt"
    constraints.write_text(CONSTRAINTS, encoding="ascii")
    online_env = dict(os.environ)
    online_env.update(LD_LIBRARY_PATH=str(args.root / "runtime" / "lib"),
                      PYTHONDONTWRITEBYTECODE="1")
    pip = [str(python), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
           "install", "--no-cache-dir", "--no-compile"]
    subprocess.run(pip + ["--index-url", "https://pypi.org/simple", "pip==24.3.1",
                         "setuptools==68.2.2", "wheel==0.43.0", "Cython==3.0.12", "numpy==1.26.4"],
                   env=online_env, check=True, timeout=180)
    subprocess.run(pip + ["--index-url", "https://download.pytorch.org/whl/cpu",
                         "torch==2.1.0+cpu", "torchaudio==2.1.0+cpu", "torchvision==0.16.0+cpu"],
                   env=online_env, check=True, timeout=360)
    upstream = args.root.parent / (args.root.name + "-audiocraft-source")
    subprocess.run(["git", "init", str(upstream)], check=True, timeout=15)
    subprocess.run(["git", "-C", str(upstream), "-c", "credential.helper=", "fetch", "--depth=1",
                    "https://github.com/facebookresearch/audiocraft.git", AUDIOCRAFT_REVISION],
                   check=True, timeout=120)
    subprocess.run(["git", "-C", str(upstream), "checkout", "--detach", "FETCH_HEAD"], check=True, timeout=30)
    actual = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if actual != AUDIOCRAFT_REVISION:
        raise ValueError("The upstream source identity does not match the reviewed revision.")
    # PyAV11 has no PyPI wheels. Ubuntu22.04's FFmpeg4.4 development headers,
    # and the explicitly installed Cython, build its upstream source unchanged.
    subprocess.run(pip + ["--index-url", "https://pypi.org/simple", "--no-build-isolation",
                         "-c", str(constraints), "-r", str(upstream / "requirements.txt")],
                   env=online_env, check=True, timeout=600)
    subprocess.run(pip + ["--no-build-isolation", "--no-deps", str(upstream)],
                   env=online_env, check=True, timeout=120)
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "check"], env=online_env, check=True, timeout=60)
    # Materialize any installer-created links as regular files; do not weaken
    # LocalModelManifest's no-link checks or omit files from its inventory.
    for entry in sorted((args.root / "runtime").rglob("*")):
        if entry.is_symlink():
            target = entry.resolve(strict=True)
            if not target.is_file() or not target.is_relative_to(args.root / "runtime"):
                raise ValueError("A runtime installer created an unsupported external or directory link.")
            temporary = entry.with_name(entry.name + ".qualification-link-copy")
            if temporary.exists():
                raise ValueError("A runtime link materialization destination already exists.")
            shutil.copy2(target, temporary)
            os.replace(temporary, entry)
    emit({"stage": "runtime_inventory_before_bytecode_cleanup",
          **measure_inventory(args.root, model_manifest)})
    cleaned = remove_redundant_bytecode(args.root / "runtime")
    emit({"stage": "runtime_inventory_after_bytecode_cleanup", **cleaned,
          **measure_inventory(args.root, model_manifest),
          "distribution_record_counts": distribution_record_counts(args.root)})
    emit({"stage": "runtime_installed", "elapsed_seconds": round(time.monotonic() - started, 3),
          "actual_musicgen_inference": "NOT_RUN"})


def audit_import(args):
    model_manifest, music = source_modules(args)
    worker = music._MUSICGEN_WORKER
    marker = "\ntorch.manual_seed("
    if worker.count(marker) != 1:
        raise ValueError("The worker import boundary changed; review before executing.")
    prefix = worker.split(marker, 1)[0]
    ast.parse(prefix)
    # The audit hook and framework imports below are the exact production
    # worker prefix. No stub modules, monkeypatches, or guard relaxation.
    probe = prefix + r'''
import importlib.metadata
import torchaudio, torchvision, transformers, audiocraft
from audiocraft.modules import transformer
root = pathlib.Path(request['root']).resolve()
for path in (sys.executable, os.__file__, torch.__file__, torchaudio.__file__,
             torchvision.__file__, transformers.__file__, audiocraft.__file__):
    if not pathlib.Path(path).resolve().is_relative_to(root):
        raise RuntimeError('A runtime component escapes the complete local installation')
if torch.__version__ != '2.1.0+cpu' or torch.version.cuda is not None:
    raise RuntimeError('Expected the reviewed CPU Torch build')
if transformer._efficient_attention_backend != 'torch':
    raise RuntimeError('The reviewed CPU path requires upstream Torch attention')
transformer._verify_xformers_memory_efficient_compat()
sample = torch.zeros((1, 2, 4, 8))
result = torch.nn.functional.scaled_dot_product_attention(sample, sample, sample)
if not torch.isfinite(result).all():
    raise RuntimeError('Real CPU Torch attention failed')
print(json.dumps({'stage':'audited_real_framework_imports', 'python':sys.version.split()[0],
 'versions':{name:importlib.metadata.version(name) for name in
 ('torch','torchaudio','torchvision','transformers','xformers','av','numpy','scipy')},
 'attention_backend':transformer._efficient_attention_backend,
 'actual_musicgen_inference':'NOT_RUN'}))
'''
    print(capture_worker(runtime_python(args.root), probe, {"root": str(args.root)}, root=args.root,
                         env=environment(args.root, model_manifest), timeout=120), end="")
    inventory = measure_inventory(args.root, model_manifest)
    emit({"stage": "runtime_inventory_preflight", **inventory, "actual_musicgen_inference": "NOT_RUN"})
    # Leave room for model/cache files; preserve all application acceptance caps.
    failures = []
    if inventory["entries"] + 32 > inventory["entry_limit"]:
        failures.append(f'entries {inventory["entries"]} + 32 reserve > {inventory["entry_limit"]}')
    if inventory["files"] + 16 > inventory["file_limit"]:
        failures.append(f'files {inventory["files"]} + 16 reserve > {inventory["file_limit"]}')
    if inventory["estimated_manifest_bytes"] + 8192 > inventory["manifest_byte_limit"]:
        failures.append(f'JSON bytes {inventory["estimated_manifest_bytes"]} + 8192 reserve > {inventory["manifest_byte_limit"]}')
    if failures:
        raise ValueError("Real runtime exceeds the reviewed manifest capacity before model acquisition: " + "; ".join(failures))


def asset_target(root, destination):
    if destination.startswith("t5/"):
        return root / "hf-cache" / "hub" / "models--t5-base" / "snapshots" / T5_REVISION / destination[3:]
    return root / destination


def prepare_assets(args):
    if not args.accept_personal_noncommercial:
        raise ValueError("Pass the explicit personal noncommercial/trusted-checkpoint acceptance flag.")
    model_manifest, _ = source_modules(args)
    required = sum(item[4] for item in ASSETS)
    if shutil.disk_usage(args.root).free < required + 2 * GIB:
        raise OSError("Insufficient space for the reviewed assets and a 2 GiB runner reserve.")
    for repository, revision, name, destination, size, expected in ASSETS:
        target = asset_target(args.root, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        model_manifest.local_path(target, must_exist=False)
        if target.exists():
            if target.stat().st_size == size and digest(target) == expected:
                continue
            raise ValueError("An existing model asset does not match the reviewed identity.")
        partial = target.with_name(target.name + ".qualification-download")
        url = f"https://huggingface.co/{repository}/resolve/{revision}/{name}"
        observed = hashlib.sha256()
        count = 0
        try:
            with urllib.request.urlopen(url, timeout=60) as incoming, partial.open("xb") as output:
                while block := incoming.read(1024 * 1024):
                    count += len(block)
                    if count > size:
                        raise ValueError("The model download exceeds its reviewed exact size.")
                    observed.update(block)
                    output.write(block)
            if count != size or observed.hexdigest() != expected:
                raise ValueError("The model download does not match the reviewed size and SHA256.")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        emit({"stage": "asset_verified", "repository": repository, "revision": revision,
              "file": name, "bytes": size, "sha256": expected})
    # The immutable checkpoint explicitly requests t5-base. HF cache snapshots
    # are real files under this legacy alias, avoiding symlinks and a duplicate
    # 892 MB blob. The pinned canonical repository is recorded above.
    hub = args.root / "hf-cache" / "hub"
    refs = hub / "models--t5-base" / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(T5_REVISION, encoding="ascii")
    (hub / "version.txt").write_text("1", encoding="ascii")
    emit({"stage": "assets_ready", "bytes": required, "actual_musicgen_inference": "NOT_RUN"})


def make_manifest(args):
    if not args.accept_personal_noncommercial:
        raise ValueError("Explicit reviewed checkpoint and personal-use acceptance is required.")
    model_manifest, _ = source_modules(args)
    files = {}
    for count, path in enumerate(sorted(args.root.rglob("*")), 1):
        if count > model_manifest.MAX_MODEL_DIRECTORY_ENTRIES:
            raise ValueError("Runtime traversal exceeds the application manifest boundary.")
        model_manifest.local_path(path)
        if path.is_file():
            relative = path.relative_to(args.root).as_posix()
            if relative != "local-model.json":
                files[relative] = digest(path)
        if len(files) > model_manifest.MAX_MODEL_FILES:
            raise ValueError("Runtime file inventory exceeds the application manifest boundary.")
    for _, _, _, destination, _, expected in ASSETS:
        relative = asset_target(args.root, destination).relative_to(args.root).as_posix()
        if files.get(relative) != expected:
            raise ValueError("An exact reviewed model asset is missing from the inventory.")
    manifest = manifest_payload(files)
    target = args.root / "local-model.json"
    with target.open("x", encoding="utf-8") as output:
        json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
    checked = model_manifest.LocalModelManifest.load(target, engine="musicgen")
    emit({"stage": "complete_manifest_verified", **checked.verify(require_complete=True),
          "actual_musicgen_inference": "NOT_RUN"})


def render(args):
    model_manifest, music = source_modules(args)
    if args.output is None or args.output.is_relative_to(args.root):
        raise ValueError("Select a new output WAV outside the immutable installation.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("The runner has no actual FFmpeg executable.")
    ffmpeg = Path(ffmpeg).resolve()
    manifest = model_manifest.LocalModelManifest.load(args.root / "local-model.json", engine="musicgen")
    started = time.monotonic()
    try:
        result = music.render_music(args.output, engine="musicgen", duration=2.0,
                                   mood="bright", tempo=100, seed=107, sample_rate=48000,
                                   ffmpeg=ffmpeg, manifest=manifest, max_output_bytes=1024 * 1024,
                                   on_status=lambda message: emit({"progress": message}))
    except music.EngineUnavailable:
        # CARR intentionally sanitizes subprocess errors. Reproduce the same
        # fixed worker, only after a real adapter failure, to obtain bounded
        # public dependency diagnostics. This can never turn a failure into PASS.
        manifest.verify(require_complete=True)
        diagnostic = args.output.with_name("diagnostic-only.wav")
        request = {"root": str(args.root), "device": "cpu", "seed": 107, "duration": 2,
                   "previous": None, "prompt": "Original instrumental music, no vocals, bright mood, 100 beats per minute.",
                   "output": str(diagnostic)}
        try:
            capture_worker(runtime_python(args.root), music._MUSICGEN_WORKER, request,
                           root=args.root, env=environment(args.root, model_manifest), timeout=120)
        finally:
            diagnostic.unlink(missing_ok=True)
        raise
    with wave.open(str(result["path"]), "rb") as audio:
        actual = (audio.getnframes(), audio.getframerate(), audio.getnchannels(), audio.getsampwidth())
        if actual != (96000, 48000, 1, 2):
            raise ValueError("The actual generated audio does not match the two-second PCM contract.")
        samples = array("h", audio.readframes(96000))
        if sys.byteorder != "little":
            samples.byteswap()
    peak = max(map(abs, samples))
    if (len(samples) != 96000 or peak == 0 or
            result["provenance"]["engine"] != "musicgen" or result["midi"] is not None):
        raise ValueError("The adapter did not return genuine nonzero neural output provenance.")
    inventory = manifest.verify(require_complete=True)
    source_paths = ("audio/music.py", "audio/model_manifest.py", "audio/engines.py")
    emit({"status": "PASS", "actual_musicgen_inference": "PASS", "duration_seconds": 2,
          "frames": 96000, "sample_rate": 48000, "peak_pcm16": peak,
          "audio_sha256": digest(result["path"]), "elapsed_seconds": round(time.monotonic() - started, 3),
          "child_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
          "device": "cpu", "cpu_count": os.cpu_count(), "inventory": inventory,
          "code_sha256": {p: digest(args.source_root / "src" / "carr_library" / p) for p in source_paths},
          "source_revision": AUDIOCRAFT_REVISION, "model_revision": MODEL_REVISION,
          "text_encoder_revision": T5_REVISION,
          "installed_versions": {p.metadata["Name"]: p.version for p in importlib.metadata.distributions()},
          "windows_inference": "NOT_RUN", "listening": "NOT_RUN",
          "multi_hour_neural_generation": "NOT_RUN", "asset_uploads": "NONE"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("describe", "prepare-runtime", "audit-import", "prepare-assets", "manifest", "render"))
    parser.add_argument("--source-root", type=Path, required=True, help="Reviewed checkout's carr-library directory.")
    parser.add_argument("--root", type=Path, required=True, help="Dedicated disposable model/runtime directory.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--accept-personal-noncommercial", action="store_true")
    args = parser.parse_args()
    args.source_root, args.root = args.source_root.absolute(), args.root.absolute()
    if args.output is not None:
        args.output = args.output.absolute()
    if args.command == "describe":
        emit({"status": "INFERENCE_NOT_RUN", "runtime": "Linux CPython3.11 / Torch2.1.0+cpu",
              "exact_model_and_tokenizer_bytes": sum(x[4] for x in ASSETS),
              "initial_free_disk_requirement_bytes": 10 * GIB,
              "source_revision": AUDIOCRAFT_REVISION, "model_revision": MODEL_REVISION,
              "text_encoder_revision": T5_REVISION, "candidate_runtime_constraints": CONSTRAINTS.splitlines()})
    else:
        {"prepare-runtime": prepare_runtime, "audit-import": audit_import, "prepare-assets": prepare_assets,
         "manifest": make_manifest, "render": render}[args.command](args)


if __name__ == "__main__":
    main()
