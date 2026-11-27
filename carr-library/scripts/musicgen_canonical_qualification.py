#!/usr/bin/env python3
"""Opt-in CPU qualification through canonical AudioRuntime and LocalModelConfig.

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
import socket
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
CANONICAL_BASE = "2bc7f306744a62d58695bb0852a986850679d75e"
SOURCE_BLOBS = {
    "audio/music.py": "418ba0cede508c3bc4d2054cce9cde0a0cc1f437",
    "audio/models.py": "085587e46381a5b2db17dd75fa8b7b793f4094d1",
    "audio/dsp.py": "d33bc8d32751794fa24b7cf2ffa9c7e26b38e351",
    "audio/process.py": "93fe48b15bf8c0cd19086eeedae28c8a7815f169",
    "durable_state.py": "b2a05f907dd4fe95c61013964c1b36e37e28597e",
}
MAX_CANONICAL_FILES = 100_000
MAX_CANONICAL_MANIFEST_BYTES = 8 * 1024**2
MAX_QUALIFICATION_ENTRIES = 65_536
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


def manifest_payload(files, records=None):
    return {"schema": 1, "engine": "musicgen", "code_revision": AUDIOCRAFT_REVISION,
            "weights_license": "CC-BY-NC-4.0", "noncommercial_only": True,
            "output_rights_notice": "Qualification-only original instrumental prompt. MusicGen weights CC-BY-NC-4.0; private noncommercial use. No commercial-use or third-party rights clearance is asserted.",
            "files": files, "runtime_code_files": records or {}}


def sdk_inventory(root, paths):
    package = root / "runtime" / "lib" / "python3.11" / "site-packages" / "audiocraft"
    paths.local_path(package, root=root, exists=True)
    records = {}
    for path in package.rglob("*.py"):
        paths.local_path(path, root=package, exists=True)
        records[path.relative_to(package).as_posix()] = digest(path)
        if len(records) > MAX_CANONICAL_FILES:
            raise ValueError("The complete SDK source inventory exceeds the canonical bound.")
    if not records:
        raise ValueError("The installed AudioCraft package has no source inventory.")
    return records


def inventory_files(root, paths):
    files = {}; total = 0
    for count, path in enumerate(root.rglob("*"), 1):
        if count > MAX_QUALIFICATION_ENTRIES:
            raise ValueError("The installation exceeds this qualification's traversal allowance.")
        paths.local_path(path, root=root, exists=True)
        if path.is_file() and path != root / "local-model.json":
            files[path.relative_to(root).as_posix()] = digest(path)
            total += path.stat().st_size
            if len(files) > MAX_CANONICAL_FILES or total > 64 * GIB:
                raise ValueError("The installation exceeds its complete file/byte allowance.")
    return files


def measure_inventory(root, paths):
    entries = files = total_bytes = path_json_bytes = 0
    for path in root.rglob("*"):
        entries += 1
        if entries > 2 * MAX_QUALIFICATION_ENTRIES:
            raise ValueError("Inventory measurement exceeded its bounded traversal.")
        paths.local_path(path, root=root, exists=True)
        if path.is_file() and path != root / "local-model.json":
            files += 1
            total_bytes += path.stat().st_size
            path_json_bytes += len(json.dumps(path.relative_to(root).as_posix()).encode("ascii")) + 68
    records = sdk_inventory(root, paths)
    base = len(json.dumps(manifest_payload({}, records), separators=(",", ":")).encode("ascii"))
    return {"files": files, "entries": entries, "bytes": total_bytes,
            "runtime_code_files": len(records),
            "estimated_manifest_bytes": base + path_json_bytes - bool(files),
            "file_limit": MAX_CANONICAL_FILES, "entry_limit": MAX_QUALIFICATION_ENTRIES,
            "manifest_byte_limit": MAX_CANONICAL_MANIFEST_BYTES}



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
    for name, expected in SOURCE_BLOBS.items():
        raw = (args.source_root / "src" / "carr_library" / name).read_bytes()
        identity = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if identity != expected:
            raise ValueError("Review changed canonical production source before qualification: " + name)
    sys.path.insert(0, str(args.source_root / "src"))
    from carr_library import durable_state
    from carr_library.audio import music
    if music.MUSICGEN_REVISION != AUDIOCRAFT_REVISION:
        raise ValueError("The canonical AudioCraft revision changed.")
    return durable_state, music


def environment(root, stage):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG",
                                          "LD_LIBRARY_PATH", "CUDA_PATH") if key in os.environ}
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "HF_HOME": str(root / "hf-cache"), "TORCH_HOME": str(stage / "torch-cache"),
                "NUMBA_CACHE_DIR": str(stage / "numba-cache"),
                "TMPDIR": str(stage), "TEMP": str(stage), "TMP": str(stage),
                "ACESTEP_CHECKPOINTS_DIR": str(root / "checkpoints"), "ACESTEP_DISABLE_TQDM": "1"})
    return env


def capture_worker(python, code, request, *, root, timeout):
    """Run the canonical request-file startup, with bounded diagnostics."""
    with tempfile.TemporaryDirectory(prefix=root.name + "-import-", dir=root.parent) as temporary:
        stage = Path(temporary)
        job = dict(request, stage=str(stage), output=str(stage / "not-generated.wav"))
        request_path = stage / "request.json"
        request_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        with tempfile.TemporaryFile(dir=stage) as output, tempfile.TemporaryFile(dir=stage) as errors:
            process = subprocess.Popen([str(python), "-I", "-S", "-B", "-c", code, str(request_path)],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                       cwd=stage, env=environment(root, stage), start_new_session=True)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started > timeout:
                        raise TimeoutError("The canonical import worker exceeded its deadline.")
                    if max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) > 128 * 1024:
                        raise ValueError("Qualification diagnostics exceeded their byte budget.")
                    time.sleep(.1)
                output.seek(0); errors.seek(0)
                stdout, stderr = output.read(128 * 1024 + 1), errors.read(128 * 1024 + 1)
                if max(len(stdout), len(stderr)) > 128 * 1024:
                    raise ValueError("Qualification diagnostics exceeded their byte budget.")
                if process.returncode:
                    emit({"stage": "canonical_import_failure", "worker_exit": process.returncode,
                          "diagnostics": stderr.decode("utf-8", "replace")[-16384:]})
                    raise RuntimeError("The canonical import worker failed qualification.")
                return stdout.decode("utf-8")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def verify_network_namespace():
    interfaces = sorted(path.name for path in Path("/sys/class/net").iterdir())
    if interfaces != ["lo"]:
        raise RuntimeError("Run this stage inside the reviewed empty Linux network namespace.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        try:
            connection.connect(("1.1.1.1", 443))
        except OSError as exc:
            if exc.errno not in (1, 13, 101, 113):
                raise RuntimeError("The network denial was not the expected OS route/permission failure.") from exc
            return {"interfaces": interfaces, "unhooked_socket": "blocked", "errno": exc.errno}
    raise RuntimeError("The isolated runtime unexpectedly reached an external network.")


def preflight_receipt(root):
    return root.parent / (root.name + "-canonical-preflight.json")



def prepare_runtime(args):
    if sys.version_info[:2] != (3, 11) or sys.platform != "linux":
        raise ValueError("This candidate runtime requires Linux CPython 3.11.")
    paths, _ = source_modules(args)
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
    # the complete runtime's no-link checks or omit files from its inventory.
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
    hub = args.root / "hf-cache" / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    (hub / "version.txt").write_text("1", encoding="ascii")
    emit({"stage": "runtime_inventory_before_bytecode_cleanup",
          **measure_inventory(args.root, paths)})
    cleaned = remove_redundant_bytecode(args.root / "runtime")
    emit({"stage": "runtime_inventory_after_bytecode_cleanup", **cleaned,
          **measure_inventory(args.root, paths),
          "distribution_record_counts": distribution_record_counts(args.root)})
    emit({"stage": "runtime_installed", "elapsed_seconds": round(time.monotonic() - started, 3),
          "actual_musicgen_inference": "NOT_RUN"})


def audit_import(args):
    paths, music = source_modules(args)
    network = verify_network_namespace()
    before = inventory_files(args.root, paths)
    records = sdk_inventory(args.root, paths)
    for name, checksum in music._REVIEWED_ENTRYPOINTS["musicgen"].items():
        if records.get(name) != checksum:
            raise ValueError("The installed SDK differs from canonical reviewed entrypoints.")
    marker = "\nif job['engine'] == 'musicgen':"
    if music._MODEL_WORKER.count(marker) != 1:
        raise ValueError("The canonical worker import boundary changed.")
    prefix = music._MODEL_WORKER.split(marker, 1)[0]
    if not prefix.startswith(music._SITE_BOOTSTRAP):
        raise ValueError("The canonical isolated site bootstrap is absent.")
    ast.parse(prefix)
    probe = prefix + r'''
from audiocraft.models import MusicGen
import importlib.metadata
import av, torchaudio, torchvision, transformers, audiocraft, numba
from audiocraft.modules import transformer
root = pathlib.Path(job['directory']).resolve()
for path in (sys.executable, sys.prefix, sys.base_prefix, os.__file__, torch.__file__,
             av.__file__, torchaudio.__file__, torchvision.__file__, transformers.__file__, audiocraft.__file__):
    if not pathlib.Path(path).resolve().is_relative_to(root / 'runtime'):
        raise RuntimeError('An imported component escapes the physical runtime prefix')
if not (sys.flags.isolated == 1 and sys.flags.no_site == 1 and sys.flags.dont_write_bytecode == 1):
    raise RuntimeError('Canonical isolated startup flags were not applied')
if pathlib.Path(numba.config.CACHE_DIR).resolve() != stage / 'numba-cache' or not (stage / 'numba-cache').is_dir():
    raise RuntimeError('Numba must use the current canonical staging cache')
if torch.__version__ != '2.1.0+cpu' or torch.version.cuda is not None:
    raise RuntimeError('Expected the reviewed CPU Torch build')
if transformer._efficient_attention_backend != 'torch':
    raise RuntimeError('The reviewed CPU path requires upstream Torch attention')
transformer._verify_xformers_memory_efficient_compat()
sample = torch.zeros((1, 2, 4, 8))
result = torch.nn.functional.scaled_dot_product_attention(sample, sample, sample)
if not torch.isfinite(result).all():
    raise RuntimeError('Real CPU Torch attention failed')
def denied(operation, expected):
    try:
        operation()
    except PermissionError as exc:
        if str(exc) != expected:
            raise RuntimeError('An unrelated error does not prove canonical audit denial') from exc
    else:
        raise RuntimeError('The canonical audit hook did not deny the operation')
denied(lambda: socket.getaddrinfo('localhost', 443), 'Local music worker denies network and child commands.')
outside = root / '.canonical-must-not-write'
denied(lambda: outside.write_text('forbidden', encoding='ascii'),
       'Local music worker may only write its staging directory.')
inside = stage / 'staging-control.txt'
inside.write_text('allowed', encoding='ascii')
if outside.exists() or inside.read_text(encoding='ascii') != 'allowed':
    raise RuntimeError('Canonical staging control failed')
print(json.dumps({'stage':'canonical_audited_real_framework_imports', 'python':sys.version.split()[0],
 'versions':{name:importlib.metadata.version(name) for name in
 ('torch','torchaudio','torchvision','transformers','xformers','av','numpy','scipy')},
 'attention_backend':transformer._efficient_attention_backend, 'av_library_versions':av.library_versions,
 'numba_cache_scope':'current operation staging', 'actual_musicgen_inference':'NOT_RUN'}))
'''
    ast.parse(probe)
    job = {"engine": "musicgen", "directory": str(args.root), "duration": 2, "prompt": "Import qualification only.",
           "seed": 107, "tempo": 100, "key": "C", "mood": "hopeful", "device": "cpu", "threads": 2, "model_name": None}
    observed = json.loads(capture_worker(runtime_python(args.root), probe, job, root=args.root, timeout=180))
    after = inventory_files(args.root, paths)
    if before != after or records != sdk_inventory(args.root, paths):
        raise ValueError("The complete runtime/SDK inventory changed during guarded imports.")
    inventory = measure_inventory(args.root, paths)
    failures = []
    if inventory["entries"] + 32 > inventory["entry_limit"]:
        failures.append("directory entries")
    if inventory["files"] + 16 > inventory["file_limit"]:
        failures.append("file count")
    if inventory["estimated_manifest_bytes"] + 8192 > inventory["manifest_byte_limit"]:
        failures.append("canonical manifest bytes")
    if failures:
        raise ValueError("The complete runtime exceeds pre-model capacity: " + ", ".join(failures))
    observed.update(status="PREFLIGHT_PASS", application_commit=os.environ.get("GITHUB_SHA"),
                    helper_sha256=digest(Path(__file__)), network_control=network, inventory=inventory,
                    runtime_files_sha256=hashlib.sha256(json.dumps(after, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest(),
                    model_weights="NOT_DOWNLOADED")
    with preflight_receipt(args.root).open("x", encoding="utf-8") as output:
        json.dump(observed, output, sort_keys=True)
    emit(observed)


def require_preflight(args, paths):
    receipt = preflight_receipt(args.root)
    paths.local_path(receipt, exists=True)
    if receipt.stat().st_size > 65536:
        raise ValueError("The runtime preflight receipt exceeds its allowance.")
    proof = json.loads(receipt.read_text(encoding="utf-8"))
    files = inventory_files(args.root, paths)
    actual = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
    if (proof.get("status") != "PREFLIGHT_PASS" or proof.get("helper_sha256") != digest(Path(__file__))
            or proof.get("application_commit") != os.environ.get("GITHUB_SHA")
            or proof.get("runtime_files_sha256") != actual):
        raise ValueError("A matching successful canonical runtime preflight is required before weights.")



def asset_target(root, destination):
    if destination.startswith("t5/"):
        return root / "hf-cache" / "hub" / "models--t5-base" / "snapshots" / T5_REVISION / destination[3:]
    return root / destination


def prepare_assets(args):
    if not args.accept_personal_noncommercial:
        raise ValueError("Pass the explicit personal noncommercial/trusted-checkpoint acceptance flag.")
    paths, _ = source_modules(args)
    require_preflight(args, paths)
    required = sum(item[4] for item in ASSETS)
    if shutil.disk_usage(args.root).free < required + 2 * GIB:
        raise OSError("Insufficient space for the reviewed assets and a 2 GiB runner reserve.")
    for repository, revision, name, destination, size, expected in ASSETS:
        target = asset_target(args.root, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        paths.local_path(target, root=args.root)
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


def model_config(args):
    from carr_library.audio.models import LocalModelConfig
    return LocalModelConfig("musicgen", runtime_python(args.root), args.root,
                            args.root / "local-model.json", device="cpu",
                            private_noncommercial=True, max_chunk_seconds=2)


def make_manifest(args):
    if not args.accept_personal_noncommercial:
        raise ValueError("Explicit reviewed checkpoint and private-use acceptance is required.")
    paths, music = source_modules(args)
    files = inventory_files(args.root, paths)
    records = sdk_inventory(args.root, paths)
    for name, expected in music._REVIEWED_ENTRYPOINTS["musicgen"].items():
        if records.get(name) != expected:
            raise ValueError("The installed SDK differs from canonical reviewed entrypoints.")
    for _, _, _, destination, _, expected in ASSETS:
        relative = asset_target(args.root, destination).relative_to(args.root).as_posix()
        if files.get(relative) != expected:
            raise ValueError("An exact reviewed model asset is missing from the complete inventory.")
    encoded = json.dumps(manifest_payload(files, records), sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_CANONICAL_MANIFEST_BYTES:
        raise ValueError("The complete model/runtime manifest exceeds the canonical byte bound.")
    target = args.root / "local-model.json"
    with target.open("xb") as output:
        output.write(encoded)
    music.validate_model(model_config(args))
    emit({"stage": "complete_canonical_manifest_verified", **measure_inventory(args.root, paths),
          "manifest_sha256": digest(target), "actual_musicgen_inference": "NOT_RUN"})


def render(args):
    if not args.accept_personal_noncommercial:
        raise ValueError("Explicit private noncommercial qualification acceptance is required.")
    paths, music = source_modules(args)
    network = verify_network_namespace()
    if args.output is None or args.output.is_relative_to(args.root):
        raise ValueError("Select a new output WAV outside the immutable installation.")
    paths.local_path(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("The runner needs actual FFmpeg and ffprobe executables.")
    from carr_library.audio import AudioBudget, AudioRuntime, MusicSpec
    config = model_config(args)
    before = music.validate_model(config)
    manifest_hash = digest(config.manifest)
    runtime = AudioRuntime(args.output.parent, ffmpeg=Path(ffmpeg).resolve(), ffprobe=Path(ffprobe).resolve(),
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
            progress=lambda seconds: emit({"stage": "canonical_audio_progress", "seconds": seconds}))
    except Exception as exc:
        emit({"stage": "canonical_render_failure", "diagnostics": str(exc)[-16384:],
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
    if before != after or digest(config.manifest) != manifest_hash:
        raise ValueError("The complete model/runtime manifest changed during actual inference.")
    state = next(row for row in runtime.music_capabilities() if row["engine"] == "musicgen")
    if not state["runtime_code_verified"] or not state["inference_tested"] or not state["available"]:
        raise ValueError("Canonical service did not record its real verified inference.")
    emit({"status": "PASS", "actual_musicgen_inference": "PASS", "duration_seconds": 2,
          "frames": 96000, "sample_rate": 48000, "channels": 1, "peak_pcm16": peak,
          "audio_sha256": result.sha256, "elapsed_seconds": round(time.monotonic() - started, 3),
          "child_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
          "device": "cpu", "cpu_count": os.cpu_count(), "inventory": measure_inventory(args.root, paths),
          "manifest_sha256": manifest_hash, "network_control": network,
          "application_commit": os.environ.get("GITHUB_SHA"), "canonical_base_commit": CANONICAL_BASE,
          "code_sha256": {p: digest(args.source_root / "src" / "carr_library" / p) for p in SOURCE_BLOBS},
          "canonical_worker_sha256": hashlib.sha256(music._MODEL_WORKER.encode("utf-8")).hexdigest(),
          "source_revision": AUDIOCRAFT_REVISION, "model_revision": MODEL_REVISION, "text_encoder_revision": T5_REVISION,
          "installed_versions": {p.metadata["Name"]: p.version for p in importlib.metadata.distributions()},
          "canonical_status_after_configure": configured, "canonical_status_after_probe": probed,
          "canonical_status_after_render": state, "windows_inference": "NOT_RUN", "listening": "NOT_RUN",
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
        emit({"status": "INFERENCE_NOT_RUN", "runtime": "Canonical Linux CPython3.11 / Torch2.1.0+cpu",
              "exact_model_and_tokenizer_bytes": sum(x[4] for x in ASSETS),
              "initial_free_disk_requirement_bytes": 10 * GIB,
              "source_revision": AUDIOCRAFT_REVISION, "model_revision": MODEL_REVISION,
              "text_encoder_revision": T5_REVISION, "candidate_runtime_constraints": CONSTRAINTS.splitlines()})
    else:
        {"prepare-runtime": prepare_runtime, "audit-import": audit_import, "prepare-assets": prepare_assets,
         "manifest": make_manifest, "render": render}[args.command](args)


if __name__ == "__main__":
    main()
