#!/usr/bin/env python3
"""Bounded Windows runtime preflight for the canonical local MusicGen worker.

No model checkpoint or audio is downloaded or generated. A complete disposable
Conda prefix is installed, inventoried and checked through the production worker
import prefix, exact -I -S -B startup and request-file protocol, with an
independently enforced Windows outbound firewall rule. No canonical model
admission, checkpoint loading, generation or desktop availability is claimed.
This qualification helper is never an application startup installer.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


AUDIOCRAFT_REVISION = "896ec7c47f5e5d1e5aa1e4b260c4405328bf009d"
MINIFORGE_NAME = "Miniforge3-26.9.4-2-Windows-x86_64.exe"
MINIFORGE_URL = "https://github.com/conda-forge/miniforge/releases/download/26.9.4-2/" + MINIFORGE_NAME
MINIFORGE_SIZE = 158341784
MINIFORGE_SHA256 = "9d83983669bc5d3b5dd4c631454bf953c3caf4ac78b9c9ada9971e5b021d83c3"
AV_NAME = "av-11.0.0-py311ha83308b_0.conda"
AV_SIZE = 789730
AV_SHA256 = "0535da653551e31631684b1cf4d5be8a9816043852fd7f55c5b85eec9f056b5f"
PYTHON_NAME = "python-3.11.14-h0159041_3_cpython.conda"
PYTHON_SIZE = 18353938
PYTHON_SHA256 = "5676dadd9d4fba1bce51bd7e5cf8fcf76f85b88b7baa15bd10ca00557e67f10e"
CANONICAL_BASE = "6ed9cb20f039b8ef227c3a28d69b4acb305dd90a"
SOURCE_BLOBS = {
    "audio/music.py": "b1403b597ee832d0f250e0bf9633626fc9a1bb05",
    "audio/models.py": "085587e46381a5b2db17dd75fa8b7b793f4094d1",
    "audio/dsp.py": "d33bc8d32751794fa24b7cf2ffa9c7e26b38e351",
    "audio/process.py": "93fe48b15bf8c0cd19086eeedae28c8a7815f169",
    "durable_state.py": "b2a05f907dd4fe95c61013964c1b36e37e28597e",
}
# Canonical validate_model admits 100,000 files / 8 MiB JSON. The separate
# qualification traversal bound is narrower; no production cap changes.
MAX_CANONICAL_FILES = 100_000
MAX_CANONICAL_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_DIRECTORY_ENTRIES = 65_536
MIB = 1024 * 1024

# Exact constraints reused from the successful public-runtime qualification.
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
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            value.update(block)
    return value.hexdigest()


def pinned_download(url, target, size, expected):
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=30) as response, target.open("xb") as output:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise ValueError("A reviewed installation download redirected outside HTTPS.")
        count = 0
        while True:
            if time.monotonic() - started > 180:
                raise TimeoutError("The pinned installer download exceeded its deadline.")
            block = response.read(min(MIB, size + 1 - count))
            if not block:
                break
            count += len(block)
            if count > size:
                raise ValueError("A pinned installer exceeded its exact byte count.")
            output.write(block)
    if count != size or digest(target) != expected:
        raise ValueError("A pinned installer failed its exact byte/hash identity.")


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


def constraints(args):
    return CONSTRAINTS


def runtime_python(args):
    return args.root / "runtime" / "python.exe"


def environment(args, stage):
    # Match canonical _neural's retained variables and explicit offline caches.
    # The reviewed configured runtime PATH is the only installation-specific
    # launch preparation; no .pth/sitecustomize hooks or Python paths are added.
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "LANG") if key in os.environ}
    prefix = args.root / "runtime"
    system = Path(os.environ["SystemRoot"])
    env["PATH"] = os.pathsep.join(map(str, (prefix, prefix / "Library" / "bin", prefix / "Scripts", system / "System32")))
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "HF_HOME": str(args.root / "hf-cache"), "TORCH_HOME": str(stage / "torch-cache"),
                "TMPDIR": str(stage), "TEMP": str(stage), "TMP": str(stage),
                "ACESTEP_CHECKPOINTS_DIR": str(args.root / "checkpoints"), "ACESTEP_DISABLE_TQDM": "1"})
    return env


def capture(args, code, request, *, timeout=240):
    """Use the canonical startup/request protocol and bound child diagnostics."""
    with tempfile.TemporaryDirectory(prefix=".canonical-probe-", dir=args.stage) as temporary:
        stage = Path(temporary)
        job = dict(request, stage=str(stage), output=str(stage / "not-generated.wav"))
        request_path = stage / "request.json"
        request_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        with tempfile.TemporaryFile(dir=stage) as output, tempfile.TemporaryFile(dir=stage) as errors:
            child = subprocess.Popen([str(runtime_python(args)), "-I", "-S", "-B", "-c", code, str(request_path)],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=errors, cwd=stage,
                                     env=environment(args, stage), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            started = time.monotonic()
            try:
                while child.poll() is None:
                    if time.monotonic() - started > timeout:
                        raise TimeoutError("The canonical Windows preflight worker exceeded its deadline.")
                    if max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) > 128 * 1024:
                        raise ValueError("The canonical Windows worker exceeded its diagnostic budget.")
                    time.sleep(.1)
                output.seek(0); errors.seek(0)
                stdout, stderr = output.read(128 * 1024 + 1), errors.read(128 * 1024 + 1)
                if max(len(stdout), len(stderr)) > 128 * 1024:
                    raise ValueError("The canonical Windows preflight output exceeded its diagnostic budget.")
                if child.returncode:
                    emit({"stage": "canonical_worker_failure", "exit": child.returncode,
                          "diagnostics": stderr.decode("utf-8", "replace")[-16384:]})
                    raise RuntimeError("The canonical Windows worker import preflight failed.")
                return stdout.decode("utf-8")
            finally:
                if child.poll() is None:
                    subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                                    "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=10)
                    child.wait(timeout=10)


NETWORK_PROBE = r'''
import json, pathlib, socket, sys
request = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
address = request.get('address') or socket.gethostbyname('pypi.org')
connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
connection.settimeout(5)
try:
    connection.connect((address, 443))
except OSError as exc:
    if not request['blocked']:
        raise
    print(json.dumps({'stage':'os_firewall_probe', 'connection':'blocked', 'error':exc.errno}))
else:
    if request['blocked']:
        raise RuntimeError('The Windows outbound firewall did not block this interpreter')
    print(json.dumps({'stage':'os_firewall_probe', 'connection':'available', 'address':address}))
finally:
    connection.close()
'''


def verify_pinned_conda(prefix, cache):
    pins = ((AV_NAME, AV_SHA256, AV_SIZE), (PYTHON_NAME, PYTHON_SHA256, PYTHON_SIZE))
    for filename, expected, size in pins:
        archive = cache / filename
        record = prefix / "conda-meta" / (filename.removesuffix(".conda") + ".json")
        if archive.stat().st_size != size or digest(archive) != expected:
            raise ValueError("Conda did not install the exact reviewed AV/Python artifact.")
        metadata = json.loads(record.read_text(encoding="utf-8"))
        if metadata.get("sha256") != expected or metadata.get("fn") != filename:
            raise ValueError("The installed Conda record does not match the reviewed artifact.")
    return [{"filename": name, "sha256": sha, "bytes": size} for name, sha, size in pins]


def prepare(args):
    paths, _ = source_modules(args)
    if sys.platform != "win32":
        raise ValueError("This preparation runs only on an isolated Windows runner.")
    args.root.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.root.parent).free < 10 * 1024**3:
        raise OSError("The Windows runtime preflight needs at least 10 GiB free.")
    paths.local_path(args.root)
    paths.local_path(args.stage)
    args.root.mkdir(exist_ok=False)
    args.stage.mkdir(exist_ok=False)
    bootstrap = args.root.parent / "carr-miniforge-bootstrap"
    installer = args.root.parent / MINIFORGE_NAME
    pinned_download(MINIFORGE_URL, installer, MINIFORGE_SIZE, MINIFORGE_SHA256)
    subprocess.run([str(installer), "/InstallationType=JustMe", "/RegisterPython=0", "/AddToPath=0", "/S", "/D=" + str(bootstrap)],
                   check=True, timeout=240)
    conda = bootstrap / "Scripts" / "conda.exe"
    cache = args.root.parent / "carr-conda-package-cache"
    online = dict(os.environ, CONDA_PKGS_DIRS=str(cache), CONDA_ALWAYS_COPY="true", PYTHONDONTWRITEBYTECODE="1")
    prefix = args.root / "runtime"
    subprocess.run([str(conda), "create", "--yes", "--copy", "--no-default-packages", "--prefix", str(prefix), "--override-channels",
                    "--channel", "conda-forge", "--strict-channel-priority", "python=3.11.14=h0159041_3_cpython",
                    "av=11.0.0=py311ha83308b_0", "ffmpeg=6", "numpy=1.26.4", "pip=24.3.1"],
                   env=online, check=True, timeout=360)
    pins = verify_pinned_conda(prefix, cache)
    lock = subprocess.check_output([str(conda), "list", "--prefix", str(prefix), "--explicit", "--sha256"],
                                   env=online, text=True, timeout=60)
    lock_lines = [line for line in lock.splitlines() if line and not line.startswith("#")]
    if not lock_lines or lock_lines[0] != "@EXPLICIT" or len(lock_lines) > 512:
        raise ValueError("The complete Conda base lock has an unexpected shape.")
    for line in lock_lines[1:]:
        url = urllib.parse.urlsplit(line)
        if (url.scheme != "https" or url.netloc != "conda.anaconda.org" or
                not url.path.startswith("/conda-forge/") or url.query or
                len(url.fragment) != 64 or any(char not in "0123456789abcdef" for char in url.fragment)):
            raise ValueError("The Conda base lock contains an unreviewed origin or missing SHA256.")
    (args.root / "runtime-conda-base-explicit.txt").write_text("\n".join(lock_lines) + "\n", encoding="ascii")
    pin_file = args.root / "qualification-constraints.txt"
    pin_file.write_text(constraints(args), encoding="ascii")
    python = runtime_python(args)
    pip = [str(python), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
           "install", "--no-cache-dir", "--no-compile"]
    subprocess.run(pip + ["--index-url", "https://pypi.org/simple", "setuptools==68.2.2", "wheel==0.43.0", "Cython==3.0.12"],
                   env=online, check=True, timeout=180)
    subprocess.run(pip + ["--index-url", "https://download.pytorch.org/whl/cpu",
                         "torch==2.1.0+cpu", "torchaudio==2.1.0+cpu", "torchvision==0.16.0+cpu"],
                   env=online, check=True, timeout=360)
    upstream = args.root.parent / "carr-windows-audiocraft-source"
    subprocess.run(["git", "init", str(upstream)], check=True, timeout=15)
    subprocess.run(["git", "-C", str(upstream), "-c", "credential.helper=", "fetch", "--depth=1",
                    "https://github.com/facebookresearch/audiocraft.git", AUDIOCRAFT_REVISION], check=True, timeout=120)
    subprocess.run(["git", "-C", str(upstream), "checkout", "--detach", "FETCH_HEAD"], check=True, timeout=30)
    if subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip() != AUDIOCRAFT_REVISION:
        raise ValueError("The upstream AudioCraft source identity changed.")
    subprocess.run(pip + ["--index-url", "https://pypi.org/simple", "--no-build-isolation", "-c", str(pin_file),
                         "-r", str(upstream / "requirements.txt")], env=online, check=True, timeout=480)
    subprocess.run(pip + ["--no-build-isolation", "--no-deps", str(upstream)], env=online, check=True, timeout=120)
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "check"], env=online, check=True, timeout=60)
    frozen = subprocess.check_output([str(python), "-I", "-B", "-m", "pip", "list", "--format=json"], env=online, text=True, timeout=60)
    (args.root / "runtime-python-packages.json").write_text(frozen, encoding="utf-8")
    cache_root = args.root / "hf-cache" / "hub"
    cache_root.mkdir(parents=True)
    (cache_root / "version.txt").write_text("1", encoding="ascii")
    # No package code, tests, licenses or sourceless bytecode is removed.
    removed = removed_bytes = 0
    for number, path in enumerate(prefix.rglob("*"), 1):
        if number > 2 * MAX_RUNTIME_DIRECTORY_ENTRIES:
            raise ValueError("The runtime preparation exceeded its traversal budget.")
        paths.local_path(path, root=prefix, exists=True)
    for path in prefix.rglob("*.pyc"):
        try:
            original = Path(importlib.util.source_from_cache(str(path)))
        except ValueError:
            original = path.with_suffix(".py")
        if original.is_relative_to(prefix) and original.is_file():
            removed += 1; removed_bytes += path.stat().st_size; path.unlink()
    baseline = json.loads(capture(args, NETWORK_PROBE, {"blocked": False}, timeout=20))
    (args.root / "network-baseline.json").write_text(json.dumps(baseline), encoding="ascii")
    emit({"stage": "windows_runtime_installed", "pinned_conda_artifacts": pins,
          "miniforge_sha256": MINIFORGE_SHA256,
          "conda_base_explicit_sha256": digest(args.root / "runtime-conda-base-explicit.txt"),
          "removed_redundant_bytecode_files": removed, "removed_bytes": removed_bytes,
          "actual_musicgen_inference": "NOT_RUN", "model_weights": "NOT_DOWNLOADED"})


def runtime_code_files(args, paths, music):
    package = args.root / "runtime" / "Lib" / "site-packages" / "audiocraft"
    paths.local_path(package, root=args.root, exists=True)
    records = {}
    for path in package.rglob("*.py"):
        paths.local_path(path, root=package, exists=True)
        records[path.relative_to(package).as_posix()] = digest(path)
        if len(records) > MAX_CANONICAL_FILES:
            raise ValueError("The complete canonical SDK source inventory exceeds its bound.")
    if not records:
        raise ValueError("The installed AudioCraft SDK has no source inventory.")
    for name, checksum in music._REVIEWED_ENTRYPOINTS["musicgen"].items():
        if records.get(name) != checksum:
            raise ValueError("The installed SDK does not match canonical reviewed upstream entrypoints.")
    return records


def inventory(args, paths, music):
    files = {}; total = entries = 0
    for entries, path in enumerate(args.root.rglob("*"), 1):
        if entries + 32 > MAX_RUNTIME_DIRECTORY_ENTRIES:
            raise ValueError("The Windows runtime exceeds this qualification's directory allowance.")
        paths.local_path(path, root=args.root, exists=True)
        if path.is_file() and path != args.root / "runtime-inventory.json":
            relative = path.relative_to(args.root).as_posix()
            files[relative] = digest(path)
            total += path.stat().st_size
            if len(files) + 16 > MAX_CANONICAL_FILES or total > 64 * 1024**3:
                raise ValueError("The complete runtime exceeds its file/byte allowance.")
    records = runtime_code_files(args, paths, music)
    payload = {"purpose": "canonical_runtime_preflight_without_model_weights",
               "files": files, "runtime_code_files": records}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    # This is a footprint projection, NOT a valid model manifest: checkpoints
    # are deliberately absent and canonical configure/probe/render are not run.
    projected = dict(schema=1, engine="musicgen", code_revision=AUDIOCRAFT_REVISION,
                     weights_license="CC-BY-NC-4.0", noncommercial_only=True,
                     output_rights_notice="Qualification requires separately reviewed private noncommercial model assets.",
                     files=files, runtime_code_files=records)
    projected_bytes = len(json.dumps(projected, sort_keys=True, separators=(",", ":")).encode("ascii"))
    if projected_bytes + 8192 > MAX_CANONICAL_MANIFEST_BYTES:
        raise ValueError("The complete runtime cannot fit the canonical manifest with model reserve.")
    return payload, {"files": len(files), "entries": entries, "bytes": total,
                     "runtime_code_files": len(records), "inventory_json_bytes": len(encoded),
                     "projected_canonical_manifest_bytes_with_reserve": projected_bytes + 8192}


def audit(args):
    if sys.platform != "win32":
        raise ValueError("The audited preflight requires Windows.")
    paths, music = source_modules(args)
    baseline = json.loads((args.root / "network-baseline.json").read_text(encoding="ascii"))
    blocked = json.loads(capture(args, NETWORK_PROBE,
                                {"blocked": True, "address": baseline["address"]}, timeout=20))
    emit(blocked)  # Unhooked socket confirms the OS barrier independently.
    before, counts = inventory(args, paths, music)
    inventory_path = args.root / "runtime-inventory.json"
    inventory_path.write_text(json.dumps(before, sort_keys=True, separators=(",", ":")), encoding="ascii")
    marker = "\nif job['engine'] == 'musicgen':"
    if music._MODEL_WORKER.count(marker) != 1:
        raise ValueError("The canonical worker import boundary changed.")
    prefix = music._MODEL_WORKER.split(marker, 1)[0]
    if not prefix.startswith(music._SITE_BOOTSTRAP):
        raise ValueError("The canonical isolated site bootstrap is missing.")
    ast.parse(prefix)
    probe = prefix + r'''
from audiocraft.models import MusicGen
import importlib.metadata
import av, torchaudio, torchvision, transformers, audiocraft
from audiocraft.modules import transformer
root = pathlib.Path(job['directory']).resolve()
runtime_root = root / 'runtime'
if not (sys.flags.isolated == 1 and sys.flags.no_site == 1 and sys.flags.dont_write_bytecode == 1):
    raise RuntimeError('Canonical isolated/no-site/no-bytecode startup flags were not applied')
if not pathlib.Path(sys.argv[1]).resolve().is_relative_to(stage):
    raise RuntimeError('The canonical request file is outside its staging directory')
for path in (sys.executable, sys.prefix, sys.base_prefix, os.__file__, torch.__file__,
             av.__file__, torchaudio.__file__, torchvision.__file__, transformers.__file__, audiocraft.__file__):
    if not pathlib.Path(path).resolve().is_relative_to(runtime_root):
        raise RuntimeError('An imported component escapes the complete physical runtime prefix')
if pathlib.Path(audiocraft.__file__).resolve().parent != runtime_root / 'Lib' / 'site-packages' / 'audiocraft':
    raise RuntimeError('The imported SDK differs from the inventoried canonical code package')
if torch.__version__ != '2.1.0+cpu' or torch.version.cuda is not None or av.__version__ != '11.0.0':
    raise RuntimeError('Expected reviewed CPU Torch and exact PyAV builds')
if transformer._efficient_attention_backend != 'torch':
    raise RuntimeError('The canonical CPU path requires upstream Torch attention')
transformer._verify_xformers_memory_efficient_compat()
sample = torch.zeros((1, 2, 4, 8))
result = torch.nn.functional.scaled_dot_product_attention(sample, sample, sample)
if not torch.isfinite(result).all():
    raise RuntimeError('Real CPU Torch attention failed')
if (av.library_versions['libavcodec'][0], av.library_versions['libavformat'][0], av.library_versions['libavutil'][0]) != (60, 60, 58):
    raise RuntimeError('PyAV did not load the reviewed FFmpeg 6 ABI')
# Exercise the unchanged canonical hook, not a replacement diagnostic hook.
def expect_denied(operation, message):
    try:
        operation()
    except PermissionError as exc:
        if str(exc) != message:
            raise RuntimeError('An unrelated permission failure cannot prove the canonical audit hook') from exc
    else:
        raise RuntimeError('The canonical audit hook did not deny the operation')
deny_network = 'Local music worker denies network and child commands.'
expect_denied(lambda: socket.getaddrinfo('localhost', 443), deny_network)
import subprocess
expect_denied(lambda: subprocess.run([sys.executable, '-I', '-S', '-B', '-c', 'pass'],
                                    check=True, timeout=5), deny_network)
outside = root / '.canonical-preflight-must-not-write'
expect_denied(lambda: outside.write_text('forbidden', encoding='ascii'),
              'Local music worker may only write its staging directory.')
if outside.exists():
    raise RuntimeError('A write escaped canonical staging')
inside = stage / 'staging-control-proof.txt'
inside.write_text('allowed', encoding='ascii')
if inside.read_text(encoding='ascii') != 'allowed':
    raise RuntimeError('Canonical staging writes failed')
# Native modules must come from this physical prefix or Windows itself.
import ctypes
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.K32EnumProcessModules.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
kernel.K32EnumProcessModules.restype = wintypes.BOOL
kernel.GetModuleFileNameW.argtypes = (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
kernel.GetModuleFileNameW.restype = wintypes.DWORD
modules = (wintypes.HMODULE * 2048)()
needed = wintypes.DWORD()
if not kernel.K32EnumProcessModules(kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed)) or needed.value > ctypes.sizeof(modules):
    raise RuntimeError('The loaded native-library inventory is unavailable or exceeds its bound')
windows = pathlib.Path(os.environ['SystemRoot']).resolve()
local_dlls = []
for index in range(needed.value // ctypes.sizeof(wintypes.HMODULE)):
    name = ctypes.create_unicode_buffer(32768)
    count = kernel.GetModuleFileNameW(modules[index], name, len(name))
    if not count or count >= len(name):
        raise RuntimeError('A loaded native library has an unreadable origin')
    path = pathlib.Path(name.value).resolve()
    if path.is_relative_to(runtime_root):
        local_dlls.append(path.relative_to(root).as_posix())
    elif not path.is_relative_to(windows):
        raise RuntimeError('A loaded native library escapes the physical runtime and Windows directory')
print(json.dumps({'stage':'canonical_audited_windows_framework_imports', 'python':sys.version.split()[0],
 'versions':{name:importlib.metadata.version(name) for name in
 ('torch','torchaudio','torchvision','transformers','xformers','av','numpy','scipy')},
 'av_library_versions':av.library_versions, 'local_native_modules':sorted(local_dlls),
 'attention_backend':transformer._efficient_attention_backend,
 'startup_flags':{'isolated':sys.flags.isolated,'no_site':sys.flags.no_site,'dont_write_bytecode':sys.flags.dont_write_bytecode},
 'canonical_request_protocol':'JSON file at argv[1]', 'audit_controls':'network and child commands denied; writes confined to staging',
 'canonical_model_admission':'NOT_RUN_NO_CHECKPOINTS', 'actual_musicgen_inference':'NOT_RUN',
 'model_weights':'NOT_DOWNLOADED'}))
'''
    ast.parse(probe)
    job = {"engine": "musicgen", "directory": str(args.root), "duration": 2,
           "prompt": "Runtime import qualification only; no generation.", "seed": 0,
           "tempo": 90, "key": "C", "mood": "calm", "device": "cpu", "threads": 2, "model_name": None}
    result = json.loads(capture(args, probe, job))
    after, after_counts = inventory(args, paths, music)
    if before != after:
        raise ValueError("The complete runtime/code inventory changed during the canonical worker import probe.")
    result.update(status="PREFLIGHT_PASS", inventory=after_counts,
                  runtime_inventory_sha256=digest(inventory_path), source_revision=AUDIOCRAFT_REVISION,
                  application_commit=os.environ.get("GITHUB_SHA"), canonical_base_commit=CANONICAL_BASE,
                  code_sha256={name: digest(args.source_root / "src" / "carr_library" / name) for name in SOURCE_BLOBS},
                  canonical_worker_sha256=hashlib.sha256(music._MODEL_WORKER.encode("utf-8")).hexdigest(),
                  installed_versions=json.loads((args.root / "runtime-python-packages.json").read_text(encoding="utf-8")),
                  conda_base_explicit_lock=(args.root / "runtime-conda-base-explicit.txt").read_text(encoding="ascii").splitlines(),
                  network_control="Windows outbound program firewall plus unchanged canonical audit hook",
                  launch_environment="Explicit physical runtime, Library/bin and Scripts PATH; unconfigured desktop launch NOT_RUN",
                  asset_uploads="NONE", canonical_model_admission="NOT_RUN_NO_CHECKPOINTS",
                  actual_musicgen_inference="NOT_RUN", model_weights="NOT_DOWNLOADED")
    emit(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "audit"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    mode = args.stage
    args.source_root = args.source_root.resolve(strict=True)
    if not args.root.is_absolute():
        raise ValueError("The disposable runtime requires an absolute local path.")
    args.root = args.root.absolute()
    args.stage = args.root.parent / (args.root.name + "-staging")
    {"prepare": prepare, "audit": audit}[mode](args)


if __name__ == "__main__":
    main()
