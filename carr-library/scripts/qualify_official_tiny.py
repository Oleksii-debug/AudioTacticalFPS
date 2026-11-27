"""Bounded public tiny-model acoustic proof, independent of any private worker.

This is a qualification script, not a product engine, downloader, or runtime
registration shortcut. Only the exact public pack below is eligible.
"""
from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import venv
import wave
import zipfile

PINS = {
    "annotated-doc": "0.0.7", "anyio": "4.16.0", "av": "17.1.0",
    "certifi": "2026.11.20", "click": "8.5.1", "ctranslate2": "4.8.0",
    "faster-whisper": "1.2.1", "filelock": "4.3.1", "flatbuffers": "25.12.19",
    "fsspec": "2026.9.0", "h11": "0.16.0", "hf-xet": "1.8.0",
    "httpcore": "1.0.9", "httpx": "0.29.0", "huggingface-hub": "1.19.0",
    "idna": "3.20", "markdown-it-py": "4.3.0", "mdurl": "0.1.2",
    "numpy": "2.4.6", "onnxruntime": "1.26.0", "packaging": "26.4",
    "protobuf": "7.36.2", "pygments": "2.21.0", "pyyaml": "6.0.4",
    "rich": "15.0.0", "setuptools": "84.2.1", "shellingham": "1.5.4",
    "tokenizers": "0.23.1", "tqdm": "4.70.1", "typer": "0.25.1",
    "typing-extensions": "4.16.0",
}
WINDOWS_PINS = {"colorama": "0.4.6"}
ROOTS = ("faster-whisper", "ctranslate2", "av", "numpy", "onnxruntime",
         "tokenizers", "huggingface-hub", "packaging")
MODEL_REPO = "Systran/faster-whisper-tiny"
MODEL_REVISION = "d90ca5fe260221311c53c58e660288d3deb8d356"
MODEL_FILES = {
    "model.bin": (75538270, "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"),
    "config.json": (2249, "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8"),
    "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
    "README.md": (1991, "29fb8444a02c1f133ab7e538525aa478ff68845964f21cf42500842b0e4a7b20"),
}
KNOWN_WHEELS = {
    "faster-whisper": "79a66ad50688c0b794dd501dc340a736992a6342f7f95e5811be60b5224a26a7",
    "packaging": "81383611a09c1bc0b08b76e0fd5829d7c6824c65b27a6aee339da133f4233ea2",
}
TEXT = (
    "The library stores the original recording. A separate transcript can be "
    "reviewed and edited. This short test runs on the local computer."
)
MAX_JSON = 256 * 1024
MAX_WHEELS = 512 * 1024**2
MAX_FILES = 50000


def normalized(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def checked_file(path, maximum):
    path = Path(path)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, "st_file_attributes", 0) & 0x400
            or not 0 <= before.st_size <= maximum):
        raise ValueError("A qualification input is not a bounded ordinary file.")
    digest = hashlib.sha256()
    count = 0
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_nlink)
    stamp = lambda item: (*identity(item), item.st_mtime_ns, item.st_ctime_ns)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if identity(opened) != identity(before):
            raise ValueError("Qualification input changed while opening.")
        while block := stream.read(1024 * 1024):
            count += len(block)
            if count > maximum:
                raise ValueError("Qualification input grew beyond its byte budget.")
            digest.update(block)
        after_open = os.fstat(stream.fileno())
    after = path.lstat()
    # Windows APIs can represent timestamps differently. Compare each API to
    # itself while binding both observations to the same identity and size.
    if (count != before.st_size or stamp(opened) != stamp(after_open)
            or stamp(before) != stamp(after) or identity(after_open) != identity(after)):
        raise ValueError("Qualification input changed during verification: " + json.dumps({
            "path_before": stamp(before), "handle_before": stamp(opened),
            "handle_after": stamp(after_open), "path_after": stamp(after)}))
    return {"sha256": digest.hexdigest(), "size_bytes": count}


def raw_json(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def write_json(path, value):
    raw = raw_json(value)
    if len(raw) > MAX_JSON:
        raise ValueError("Qualification evidence exceeds its JSON byte budget.")
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_json(path, maximum=MAX_JSON):
    checked_file(path, maximum)
    raw = Path(path).read_bytes()
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate qualification JSON key.")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON.")))


def download(url, target, maximum):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Qualification downloads require HTTPS.")
    request = urllib.request.Request(url, headers={"User-Agent": "Resonance-public-tiny-qualification/1"})
    with urllib.request.urlopen(request, timeout=90) as incoming:
        if urllib.parse.urlsplit(incoming.url).scheme != "https":
            raise ValueError("A download redirected outside HTTPS.")
        with Path(target).open("xb") as outgoing:
            count = 0
            while block := incoming.read(1024 * 1024):
                count += len(block)
                if count > maximum:
                    raise ValueError("A public input exceeded its acquisition byte budget.")
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    return checked_file(target, maximum)


def model_inventory(root):
    root = Path(root)
    if set(path.name for path in root.iterdir()) != set(MODEL_FILES):
        raise ValueError("The exact complete official tiny pack is required.")
    result = {}
    for name, (size, digest) in MODEL_FILES.items():
        identity = checked_file(root / name, size)
        if identity != {"sha256": digest, "size_bytes": size}:
            raise ValueError("The official model file differs from its pinned identity: " + name)
        result[name] = identity
    card = (root / "README.md").read_text(encoding="utf-8")
    if not re.search(r"(?m)^license:\s*mit\s*$", card):
        raise ValueError("The pinned model card does not declare the reviewed MIT license.")
    return result


def dependency_closure(metadata, versions, *, windows):
    # Use the trusted installer parser, before importing downloaded runtime code.
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.version import Version
    environment = default_environment()
    if windows != (environment["sys_platform"] == "win32"):
        raise ValueError("Dependency markers must describe the actual selected platform.")
    extras, pending, visited = {}, [(name, "") for name in ROOTS], set()
    while pending:
        name, extra = pending.pop()
        key = (name, extra)
        if key in visited:
            continue
        visited.add(key)
        if name not in metadata or name not in versions:
            raise ValueError("The pinned closure omits a required package: " + name)
        selected = metadata[name]
        requires_python = selected.get("Requires-Python")
        if requires_python and Version(environment["python_full_version"]) not in SpecifierSet(requires_python):
            raise ValueError("A pinned runtime distribution rejects this Python.")
        extras.setdefault(name, set()).add(extra)
        for raw in selected.get_all("Requires-Dist", []):
            requirement = Requirement(raw)
            if requirement.url is not None:
                raise ValueError("Direct-URL runtime requirements are not admitted.")
            if requirement.marker and not requirement.marker.evaluate({**environment, "extra": extra}):
                continue
            dependency = normalized(requirement.name)
            if dependency not in versions or Version(versions[dependency]) not in requirement.specifier:
                raise ValueError("Pinned runtime versions do not close requirement: " + raw)
            pending.append((dependency, ""))
            pending.extend((dependency, item) for item in requirement.extras)
    if set(extras) != set(versions):
        raise ValueError("The proposed runtime is not the exact active dependency closure: "
                         + ", ".join(sorted(set(versions) ^ set(extras))))
    return sorted(extras)


def wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > MAX_FILES:
            raise ValueError("Invalid wheel inventory.")
        for name in names:
            parts = PurePosixPath(name)
            if (parts.is_absolute() or ".." in parts.parts or "\\" in name or ":" in name
                    or any(ord(char) < 32 for char in name)):
                raise ValueError("Unsafe wheel member.")
        selected = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(selected) != 1 or archive.getinfo(selected[0]).file_size > 2 * 1024**2:
            raise ValueError("A wheel must contain one bounded metadata record.")
        raw = archive.read(selected[0])
        metadata = email.parser.BytesParser().parsebytes(raw)
        notices = {}
        for name in names:
            if name.endswith("/"):
                continue
            leaf = PurePosixPath(name).name.lower()
            if leaf.startswith(("license", "copying", "notice", "authors")):
                info = archive.getinfo(name)
                if info.file_size > 2 * 1024**2 or len(notices) >= 128:
                    raise ValueError("Wheel notices exceed their review budget.")
                notice = archive.read(name)
                notices[name] = {"sha256": hashlib.sha256(notice).hexdigest(),
                                 "size_bytes": len(notice)}
        declaration = metadata.get("License-Expression") or metadata.get("License")
        classifiers = [item for item in metadata.get_all("Classifier", [])
                       if item.startswith("License ::")]
        return metadata, hashlib.sha256(raw).hexdigest(), notices, declaration, classifiers


def installed_inventory(environment):
    root = Path(environment)
    site = root / "Lib/site-packages"
    result, count, total = {}, 0, 0
    for info in sorted(site.glob("*.dist-info")):
        metadata = importlib.metadata.PathDistribution(info)
        name = normalized(metadata.metadata["Name"])
        rows = {}
        record = info / "RECORD"
        for relative, digest, length in csv.reader(record.read_text(encoding="utf-8").splitlines()):
            path_parts = PurePosixPath(relative)
            if "__pycache__" in path_parts.parts or relative.endswith((".pyc", ".pyo")):
                continue
            path = (site / Path(*path_parts.parts)).resolve(strict=True)
            if not path.is_relative_to(root.resolve()):
                raise ValueError("An installed runtime record escapes the owned environment.")
            count += 1
            if count > MAX_FILES:
                raise ValueError("Installed runtime exceeds its file budget.")
            identity = checked_file(path, MAX_WHEELS)
            total += identity["size_bytes"]
            if total > 2 * 1024**3:
                raise ValueError("Installed runtime exceeds its byte budget.")
            if relative in rows:
                raise ValueError("Duplicate installed runtime record.")
            rows[relative] = identity
        if name in result:
            raise ValueError("Duplicate installed runtime distribution.")
        result[name] = {
            "version": metadata.version, "files": len(rows),
            "installed_files_sha256": hashlib.sha256(raw_json(rows)).hexdigest(),
            "size_bytes": sum(row["size_bytes"] for row in rows.values()),
        }
    return result


def prepare(root):
    if os.name != "nt":
        raise RuntimeError("This proof requires Windows SAPI and Windows Filtering Platform.")
    root.mkdir(parents=True, exist_ok=False)
    downloads = root / "wheels"
    downloads.mkdir()
    pins = {**PINS, **WINDOWS_PINS}
    requirement_file = root / "versions.txt"
    requirement_file.write_text("".join(name + "==" + version + "\n"
                                       for name, version in sorted(pins.items())), encoding="ascii")
    command = [sys.executable, "-I", "-m", "pip", "--isolated", "download",
               "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
               "--no-deps", "--no-cache-dir", "--dest", str(downloads),
               "-r", str(requirement_file)]
    subprocess.run(command, check=True, timeout=300)
    wheels = sorted(downloads.glob("*.whl"))
    if len(wheels) != len(pins):
        raise ValueError("One wheel per exact runtime pin is required.")
    metadata, evidence, locks, total = {}, {}, [], 0
    for wheel in wheels:
        meta, meta_hash, notices, declaration, classifiers = wheel_metadata(wheel)
        name, version = normalized(meta["Name"]), meta["Version"]
        if name not in pins or pins[name] != version or name in metadata:
            raise ValueError("The wheel set differs from the exact pin set.")
        identity = checked_file(wheel, MAX_WHEELS)
        total += identity["size_bytes"]
        if total > MAX_WHEELS:
            raise ValueError("The selected runtime exceeds its wheel byte budget.")
        url = "https://pypi.org/pypi/" + urllib.parse.quote(name) + "/" + version + "/json"
        public_metadata = root / (name + ".pypi.json")
        download(url, public_metadata, 4 * 1024**2)
        published = load_json(public_metadata, 4 * 1024**2)
        matches = [item for item in published["urls"] if item["filename"] == wheel.name]
        if len(matches) != 1 or matches[0].get("yanked"):
            raise ValueError("The wheel must be one unyanked exact PyPI release artifact.")
        approved = matches[0]
        if identity != {"sha256": approved["digests"]["sha256"], "size_bytes": approved["size"]}:
            raise ValueError("A downloaded wheel fails the official PyPI hash/size binding.")
        if name in KNOWN_WHEELS and identity["sha256"] != KNOWN_WHEELS[name]:
            raise ValueError("A bootstrap/root wheel differs from its prior reviewed bytes.")
        declaration = declaration or published["info"].get("license_expression") or published["info"].get("license")
        classifiers = classifiers or [item for item in published["info"].get("classifiers", [])
                                     if item.startswith("License ::")]
        if not declaration and not classifiers and not notices:
            raise ValueError("No public license declaration or bundled notices are available for " + name)
        evidence[name] = {
            "version": version, "wheel": wheel.name, **identity,
            "metadata_sha256": meta_hash, "notices": notices,
            "license_declaration": str(declaration or "")[:1024],
            "license_classifiers": classifiers[:16],
            "source": url,
        }
        metadata[name] = meta
        locks.append(name + "==" + version + " --hash=sha256:" + identity["sha256"] + "\n")
    closure = dependency_closure(metadata, pins, windows=True)
    model = root / "model"
    model.mkdir()
    for name, (size, digest) in MODEL_FILES.items():
        download("https://huggingface.co/" + MODEL_REPO + "/resolve/"
                 + MODEL_REVISION + "/" + name, model / name, size)
    model_files = model_inventory(model)
    lock = root / "requirements.lock"
    lock.write_text("".join(sorted(locks)), encoding="ascii")
    environment = root / "runtime"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "Scripts/python.exe"
    subprocess.run([sys.executable, "-I", "-m", "pip", "--isolated", "--python", str(python),
                    "install", "--no-index", "--no-deps", "--no-compile", "--no-cache-dir",
                    "--find-links", str(downloads), "--require-hashes", "-r", str(lock)],
                   check=True, timeout=300)
    installed = installed_inventory(environment)
    if {name: row["version"] for name, row in installed.items()} != pins:
        raise ValueError("The installed runtime differs from the exact reviewed closure.")
    write_json(root / "acquisition.json", {
        "kind": "official-tiny-acquisition", "version": 1, "status": "verified",
        "python_version": sys.version.split()[0],
        "python_executable": checked_file(Path(sys.executable), 64 * 1024**2),
        "core_pin_count": len(PINS), "platform_additions": WINDOWS_PINS,
        "closure": closure, "packages": evidence, "installed": installed,
        "model": {"repository": MODEL_REPO, "revision": MODEL_REVISION,
                  "license": "MIT", "files": model_files},
        "rights_scope": "Ephemeral qualification only; no wheel, model, voice, or audio redistribution.",
    })


def synthesize(root, source):
    if os.name != "nt":
        raise RuntimeError("Installed SAPI is required.")
    sys.path.insert(0, str(source.resolve(strict=True)))
    from carr_library.audio.engines import SAPI5Engine
    engine = SAPI5Engine(timeout=60)
    voices = engine.voices()
    voice = next((voice for voice in voices
                  if any(language.lower().split("-")[0] == "en" for language in voice.languages)), None)
    if voice is None:
        raise RuntimeError("No actual enabled English SAPI voice is installed.")
    target = root / "synthetic.wav"
    engine.synthesize(TEXT, target, voice=voice.id, language="en", rate=-1)
    with wave.open(str(target), "rb") as stream:
        duration = stream.getnframes() / stream.getframerate()
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1 or not 2 <= duration <= 60:
            raise ValueError("The actual speech fixture is not bounded PCM16 speech.")
        audio = {"frames": stream.getnframes(), "sample_rate": stream.getframerate(),
                 "channels": stream.getnchannels(), "duration_seconds": duration}
    write_json(root / "fixture.json", {
        "kind": "public-sapi-fixture", "version": 1, "text": TEXT,
        "speech_engine": "installed SAPI 5", "voice_name": voice.name,
        "voice_revision": voice.revision, "languages": list(voice.languages),
        "voice_identity": engine.cache_identity(voice.id),
        "source": checked_file(target, 4 * 1024**2), **audio,
        "distribution": "This WAV stays on the ephemeral runner and is not uploaded.",
    })


def probe_network(root, blocked):
    path = root / "network-probe.json"
    if blocked:
        data = load_json(path)
        target = data["ipv4"]
    else:
        target = socket.gethostbyname("pypi.org")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        try:
            connection.connect((target, 443))
        except OSError as error:
            if not blocked or getattr(error, "winerror", None) != 10013:
                raise RuntimeError("Network denial was not demonstrated as native WSAEACCES.") from error
            return {"native_connect": "denied", "winsock_error": 10013,
                    "comparison": "same IPv4 and port connected before the firewall rule"}
    if blocked:
        raise RuntimeError("The inference process can still open an external TCP connection.")
    write_json(path, {"ipv4": target, "port": 443, "connected_before_rule": True})
    return {"native_connect": "connected"}


def word_error_rate(reference, hypothesis):
    expected = re.findall(r"[a-z0-9]+", reference.lower())
    observed = re.findall(r"[a-z0-9]+", hypothesis.lower())
    if not expected or len(expected) > 512 or len(observed) > 2048:
        raise ValueError("The acoustic comparison exceeds its token budget.")
    previous = list(range(len(observed) + 1))
    for i, source in enumerate(expected, 1):
        row = [i]
        for j, actual in enumerate(observed, 1):
            row.append(min(row[-1] + 1, previous[j] + 1,
                           previous[j - 1] + (source != actual)))
        previous = row
    return previous[-1] / len(expected)


def infer(root):
    if os.name != "nt" or not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError("Inference requires the isolated, site-disabled Windows runtime.")
    environment = Path(sys.executable).absolute().parent.parent
    if environment != root / "runtime":
        raise ValueError("Inference must use the newly owned runtime.")
    acquisition = load_json(root / "acquisition.json")
    fixture = load_json(root / "fixture.json")
    if installed_inventory(environment) != acquisition["installed"]:
        raise ValueError("The reviewed installed runtime bytes changed before inference.")
    if model_inventory(root / "model") != acquisition["model"]["files"]:
        raise ValueError("The model inventory changed before inference.")
    if checked_file(root / "synthetic.wav", 4 * 1024**2) != fixture["source"]:
        raise ValueError("The synthetic input changed before inference.")
    before_network = probe_network(root, True)
    sys.path.append(str(environment / "Lib/site-packages"))
    for name, value in {
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }.items():
        os.environ[name] = value
    import ctranslate2
    from faster_whisper import WhisperModel
    started = time.monotonic()
    model = WhisperModel(str(root / "model"), device="cpu", compute_type="int8",
                         cpu_threads=1, num_workers=1, local_files_only=True)
    if model.model.device != "cpu" or model.model.compute_type not in ("int8", "int8_float32"):
        raise ValueError("The engine did not use the requested bounded CPU/int8 execution.")
    segments, info = model.transcribe(str(root / "synthetic.wav"), language="en",
                                     task="transcribe", beam_size=5, temperature=0,
                                     vad_filter=False, condition_on_previous_text=False,
                                     word_timestamps=True)
    rows = []
    for segment in segments:
        if len(rows) >= 128 or not isinstance(segment.text, str) or len(segment.text) > 8192:
            raise ValueError("The acoustic output exceeds its segment/text budget.")
        if (not math.isfinite(segment.start) or not math.isfinite(segment.end)
                or not 0 <= segment.start <= segment.end <= fixture["duration_seconds"] + 1):
            raise ValueError("The acoustic engine returned invalid segment timing.")
        rows.append({"start": segment.start, "end": segment.end, "text": segment.text})
    text = "".join(row["text"] for row in rows).strip()
    wer = word_error_rate(TEXT, text)
    elapsed = time.monotonic() - started
    acoustic_passed = bool(rows) and info.language == "en" and wer <= .35 and elapsed <= 300
    after_network = probe_network(root, True)
    if installed_inventory(environment) != acquisition["installed"]:
        raise ValueError("The installed runtime bytes changed during inference.")
    if model_inventory(root / "model") != acquisition["model"]["files"]:
        raise ValueError("The official model files changed during inference.")
    if checked_file(root / "synthetic.wav", 4 * 1024**2) != fixture["source"]:
        raise ValueError("The original synthetic input changed during inference.")
    write_json(root / "inference.json", {
        "kind": "official-tiny-acoustic-proof", "version": 1,
        "status": "passed" if acoustic_passed else "failed",
        "scope": "One synthetic English SAPI recording; direct official engine only.",
        "private_worker_interoperability": "NOT_RUN",
        "private_worker_cache_and_cancel": "NOT_RUN",
        "human_audio_and_multilingual_qualification": "NOT_RUN",
        "network_before": before_network, "network_after": after_network,
        "device": model.model.device, "requested_compute_type": "int8",
        "effective_compute_type": model.model.compute_type,
        "cpu_threads": 1, "model_workers": 1, "ctranslate2_version": ctranslate2.__version__,
        "language": info.language, "text": text, "segments": rows,
        "word_error_rate": wer, "maximum_word_error_rate": .35,
        "elapsed_seconds": elapsed, "source": fixture["source"],
        "source_unchanged": True, "model_unchanged": True, "runtime_unchanged": True,
    })
    if not acoustic_passed:
        raise ValueError("The real tiny English acoustic smoke did not meet its declared gate; measured output is preserved in the JSON evidence.")


def evidence(root, output):
    value = {"kind": "public-official-tiny-qualification", "version": 1,
             "status": "failed", "private_worker_interoperability": "NOT_RUN",
             "source_commit": os.environ.get("GITHUB_SHA", ""),
             "qualification_script": checked_file(Path(__file__), 1024 * 1024)}
    for name in ("acquisition", "fixture", "inference", "firewall"):
        path = root / (name + ".json")
        if path.exists():
            value[name] = load_json(path)
    if (value.get("acquisition", {}).get("status") == "verified"
            and value.get("fixture", {}).get("kind") == "public-sapi-fixture"
            and value.get("inference", {}).get("status") == "passed"
            and value.get("firewall", {}).get("status") == "enforced"
            and value.get("firewall", {}).get("cleanup_succeeded") is True):
        value["status"] = "passed"
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, value)
    print(json.dumps({"status": value["status"], "sha256": checked_file(output, MAX_JSON)["sha256"],
                      "private_worker_interoperability": "NOT_RUN"}, sort_keys=True))
    return 0 if value["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "synthesize", "probe", "infer", "evidence"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--blocked", action="store_true")
    args = parser.parse_args()
    root = args.root.absolute()
    if args.operation == "prepare":
        prepare(root)
    elif args.operation == "synthesize":
        if args.source is None:
            parser.error("--source is required for the existing SAPI adapter.")
        synthesize(root, args.source)
    elif args.operation == "probe":
        probe_network(root, args.blocked)
    elif args.operation == "infer":
        infer(root)
    else:
        if args.output is None:
            parser.error("--output is required for bounded evidence.")
        return evidence(root, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
