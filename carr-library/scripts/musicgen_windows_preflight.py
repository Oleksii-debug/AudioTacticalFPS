#!/usr/bin/env python3
"""Bounded Windows runtime preflight for the existing local MusicGen worker.

No model checkpoint or audio is downloaded or generated. A complete disposable
Conda prefix is installed, inventoried and checked through the production worker
import prefix with an independently enforced Windows outbound firewall rule.
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
SOURCE_MODULES = ("audio/music.py", "audio/engines.py", "audio/model_manifest.py", "audio/ffmpeg.py", "audio/paths.py")
MIB = 1024 * 1024


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
    sys.path.insert(0, str(args.source_root / "src"))
    from carr_library.audio import model_manifest, music
    if music.MUSICGEN_SOURCE != AUDIOCRAFT_REVISION:
        raise ValueError("The production AudioCraft revision changed.")
    if model_manifest.MAX_MODEL_FILES != 40960:
        raise ValueError("Review the changed production inventory budget before qualification.")
    return model_manifest, music


def constraints(args):
    # Reuse only literal pinned data from the existing reviewed Linux qualifier;
    # importing that script would import the Linux-only resource module.
    tree = ast.parse((args.source_root / "scripts" / "musicgen_qualification.py").read_text(encoding="utf-8"))
    literals = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in {"AUDIOCRAFT_REVISION", "CONSTRAINTS"}:
                literals[node.targets[0].id] = ast.literal_eval(node.value)
    if literals.get("AUDIOCRAFT_REVISION") != AUDIOCRAFT_REVISION or not isinstance(literals.get("CONSTRAINTS"), str):
        raise ValueError("The reviewed qualification constraints changed.")
    return literals["CONSTRAINTS"]


def runtime_python(args):
    return args.root / "runtime" / "python.exe"


def environment(args, model_manifest):
    env = model_manifest.offline_environment()
    prefix = args.root / "runtime"
    system = Path(os.environ["SystemRoot"])
    env.update(PATH=os.pathsep.join(map(str, (prefix, prefix / "Library" / "bin", prefix / "Scripts", system / "System32"))),
               HF_HOME=str(args.root / "hf-cache"), HUGGINGFACE_HUB_CACHE=str(args.root / "hf-cache" / "hub"),
               AUDIOCRAFT_CACHE_DIR=str(args.root), TORCH_HOME=str(args.root / "torch-cache"),
               OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    for key in list(env):
        if key.upper().startswith(("CUDA_", "CONDA_", "PYTHON")) and key != "PYTHONDONTWRITEBYTECODE":
            env.pop(key, None)
    return env


def capture(args, code, request, env, *, timeout=240):
    """Bound child diagnostics; kill its whole process tree on failure/timeout."""
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        incoming.write(json.dumps(request).encode("utf-8")); incoming.seek(0)
        child = subprocess.Popen([str(runtime_python(args)), "-I", "-B", "-u", "-c", code],
                                 stdin=incoming, stdout=output, stderr=errors, cwd=args.root, env=env,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        started = time.monotonic()
        try:
            while child.poll() is None:
                if time.monotonic() - started > timeout:
                    raise TimeoutError("The Windows preflight worker exceeded its deadline.")
                if max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) > 128 * 1024:
                    raise ValueError("The Windows preflight worker exceeded its diagnostic budget.")
                time.sleep(.1)
            output.seek(0); errors.seek(0)
            stdout, stderr = output.read(128 * 1024 + 1), errors.read(128 * 1024 + 1)
            if max(len(stdout), len(stderr)) > 128 * 1024:
                raise ValueError("The Windows preflight output exceeded its diagnostic budget.")
            if child.returncode:
                emit({"stage": "worker_failure", "exit": child.returncode,
                      "diagnostics": stderr.decode("utf-8", "replace")[-16384:]})
                raise RuntimeError("The original Windows worker preflight failed.")
            return stdout.decode("utf-8")
        finally:
            if child.poll() is None:
                subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                                "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=10)
                child.wait(timeout=10)


NETWORK_PROBE = r'''
import json, socket, sys
request = json.loads(sys.stdin.buffer.read())
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
    model_manifest, _ = source_modules(args)
    if sys.platform != "win32":
        raise ValueError("This preparation runs only on an isolated Windows runner.")
    args.root.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.root.parent).free < 10 * 1024**3:
        raise OSError("The Windows runtime preflight needs at least 10 GiB free.")
    args.root.mkdir(exist_ok=False)
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
        if number > 2 * model_manifest.MAX_MODEL_DIRECTORY_ENTRIES:
            raise ValueError("The runtime preparation exceeded its traversal budget.")
        model_manifest.local_path(path)
    for path in prefix.rglob("*.pyc"):
        try:
            original = Path(importlib.util.source_from_cache(str(path)))
        except ValueError:
            original = path.with_suffix(".py")
        if original.is_relative_to(prefix) and original.is_file():
            removed += 1; removed_bytes += path.stat().st_size; path.unlink()
    baseline = json.loads(capture(args, NETWORK_PROBE, {"blocked": False}, environment(args, model_manifest), timeout=20))
    (args.root / "network-baseline.json").write_text(json.dumps(baseline), encoding="ascii")
    emit({"stage": "windows_runtime_installed", "pinned_conda_artifacts": pins,
          "miniforge_sha256": MINIFORGE_SHA256,
          "conda_base_explicit_sha256": digest(args.root / "runtime-conda-base-explicit.txt"),
          "removed_redundant_bytecode_files": removed, "removed_bytes": removed_bytes,
          "actual_musicgen_inference": "NOT_RUN", "model_weights": "NOT_DOWNLOADED"})


def inventory(args, model_manifest):
    files = {}; total = entries = 0
    for entries, path in enumerate(args.root.rglob("*"), 1):
        if entries + 32 > model_manifest.MAX_MODEL_DIRECTORY_ENTRIES:
            raise ValueError("The Windows runtime exceeds the existing directory budget with model reserve.")
        model_manifest.local_path(path)
        if path.is_file() and path != args.root / "runtime-inventory.json":
            relative = path.relative_to(args.root).as_posix()
            model_manifest._relative(relative)
            files[relative] = model_manifest.digest_file(path)
            total += path.stat().st_size
            if len(files) + 16 > model_manifest.MAX_MODEL_FILES or total > 64 * 1024**3:
                raise ValueError("The Windows runtime exceeds the existing file/byte budget with model reserve.")
    payload = {"purpose": "runtime_preflight_without_model_weights", "files": files}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) + 8192 > model_manifest.MAX_MODEL_MANIFEST_BYTES:
        raise ValueError("The complete Windows runtime exceeds the existing manifest byte budget.")
    return payload, {"files": len(files), "entries": entries, "bytes": total, "inventory_json_bytes": len(encoded)}


def audit(args):
    if sys.platform != "win32":
        raise ValueError("The audited preflight requires Windows.")
    model_manifest, music = source_modules(args)
    env = environment(args, model_manifest)
    baseline = json.loads((args.root / "network-baseline.json").read_text(encoding="ascii"))
    blocked = json.loads(capture(args, NETWORK_PROBE, {"blocked": True, "address": baseline["address"]}, env, timeout=20))
    emit(blocked)  # Unhooked socket probe confirms the operating-system barrier.
    before, counts = inventory(args, model_manifest)
    inventory_path = args.root / "runtime-inventory.json"
    inventory_path.write_text(json.dumps(before, sort_keys=True, separators=(",", ":")), encoding="ascii")
    marker = "\ntorch.manual_seed("
    if music._MUSICGEN_WORKER.count(marker) != 1:
        raise ValueError("The original worker import boundary changed.")
    prefix = music._MUSICGEN_WORKER.split(marker, 1)[0]
    ast.parse(prefix)
    probe = prefix + r'''
import importlib.metadata
import av, torchaudio, torchvision, transformers, audiocraft
from audiocraft.modules import transformer
root = pathlib.Path(request['root']).resolve()
for path in (sys.executable, sys.prefix, sys.base_prefix, os.__file__, torch.__file__,
             av.__file__, torchaudio.__file__, torchvision.__file__, transformers.__file__, audiocraft.__file__):
    if not pathlib.Path(path).resolve().is_relative_to(root):
        raise RuntimeError('An imported runtime component escapes the complete reviewed prefix')
if torch.__version__ != '2.1.0+cpu' or torch.version.cuda is not None or av.__version__ != '11.0.0':
    raise RuntimeError('Expected the reviewed CPU Torch and exact PyAV builds')
if transformer._efficient_attention_backend != 'torch':
    raise RuntimeError('The original CPU path requires upstream Torch attention')
transformer._verify_xformers_memory_efficient_compat()
sample = torch.zeros((1, 2, 4, 8))
result = torch.nn.functional.scaled_dot_product_attention(sample, sample, sample)
if not torch.isfinite(result).all():
    raise RuntimeError('Real CPU Torch attention failed')
if (av.library_versions['libavcodec'][0], av.library_versions['libavformat'][0], av.library_versions['libavutil'][0]) != (60, 60, 58):
    raise RuntimeError('PyAV did not load the expected FFmpeg 6 ABI')
# Confirm that imported native libraries come from the complete local prefix
# or Windows itself, rather than another Conda/GPU/tool installation on PATH.
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
    if path.is_relative_to(root):
        local_dlls.append(path.relative_to(root).as_posix())
    elif not path.is_relative_to(windows):
        raise RuntimeError('A loaded native library escapes the reviewed prefix and Windows system directory')
print(json.dumps({'stage':'audited_windows_framework_imports', 'python':sys.version.split()[0],
 'versions':{name:importlib.metadata.version(name) for name in
 ('torch','torchaudio','torchvision','transformers','xformers','av','numpy','scipy')},
 'av_library_versions':av.library_versions, 'local_native_modules':sorted(local_dlls),
 'attention_backend':transformer._efficient_attention_backend,
 'actual_musicgen_inference':'NOT_RUN', 'model_weights':'NOT_DOWNLOADED'}))
'''
    result = json.loads(capture(args, probe, {"root": str(args.root)}, env))
    after, after_counts = inventory(args, model_manifest)
    if before != after:
        raise ValueError("The complete runtime inventory changed during the original worker import probe.")
    result.update(status="PREFLIGHT_PASS", inventory=after_counts,
                  runtime_inventory_sha256=digest(inventory_path), source_revision=AUDIOCRAFT_REVISION,
                  application_commit=os.environ.get("GITHUB_SHA"),
                  code_sha256={name: digest(args.source_root / "src" / "carr_library" / name) for name in SOURCE_MODULES},
                  installed_versions=json.loads((args.root / "runtime-python-packages.json").read_text(encoding="utf-8")),
                  conda_base_explicit_lock=(args.root / "runtime-conda-base-explicit.txt").read_text(encoding="ascii").splitlines(),
                  network_control="Windows outbound program firewall plus unchanged production audit hook",
                  asset_uploads="NONE", actual_musicgen_inference="NOT_RUN", model_weights="NOT_DOWNLOADED")
    emit(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "audit"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    args.source_root = args.source_root.resolve(strict=True)
    args.root = args.root.absolute()
    if not args.root.is_absolute():
        raise ValueError("The disposable runtime requires an absolute local path.")
    {"prepare": prepare, "audit": audit}[args.stage](args)


if __name__ == "__main__":
    main()
