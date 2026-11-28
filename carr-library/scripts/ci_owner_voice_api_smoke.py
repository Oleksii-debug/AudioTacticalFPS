"""Execute the pinned owner's real data-only package API on generated test data.

Acquisition is explicit and bounded. Three already public, immutable owner files
stay in a temporary directory and are never included in the report or artifact.
No model, NVDA, synthesis, microphone or playback API is used.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha1, sha256
import json
import math
import os
from pathlib import Path
import platform
import stat
import struct
import sys
from tempfile import TemporaryDirectory
import time
import types
import urllib.parse
import urllib.request
import wave

OWNER = "Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension"
REVISION = "a1c73a5747bc86eb11f0c037120442e0c2593e91"
PREFIX = "dordiai-voices/source/0.12.2/kx47Runtime"
SOURCE_BLOBS = {
    "voice_policy.py": "de305d15a955b92dc1a8bfb719b7f9df501a94d8",
    "voice_packages.py": "2abe2a50bcf58aea423764ef15acc04538ee2f9b",
    "voice_assets.py": "6cabd9c44a3979e2b543f2272915fdc83523c87c",
}
MAX_SOURCE_BYTES = 128 * 1024


def git_blob(raw):
    return sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def acquire(root):
    root.mkdir(mode=0o700)
    receipts, buffers = {}, {}
    for name, expected in SOURCE_BLOBS.items():
        url = "https://raw.githubusercontent.com/" + OWNER + "/" + REVISION + "/" + PREFIX + "/" + name
        with urllib.request.urlopen(url, timeout=30) as response:
            target = urllib.parse.urlsplit(response.geturl())
            if (target.scheme != "https" or target.hostname != "raw.githubusercontent.com"
                    or target.username is not None or target.password is not None):
                raise ValueError("The immutable public source changed its download origin.")
            raw = response.read(MAX_SOURCE_BYTES + 1)
        if not raw or len(raw) > MAX_SOURCE_BYTES or git_blob(raw) != expected:
            raise ValueError("Owner source differs from the reviewed immutable Git blob: " + name)
        compile(raw, str(root / name), "exec")
        with (root / name).open("xb") as stream:
            stream.write(raw)
        buffers[name] = raw
        receipts[name] = {"git_blob": expected, "sha256": sha256(raw).hexdigest(),
                          "size_bytes": len(raw)}
    return buffers, receipts


def load_owner(root, buffers):
    for name in SOURCE_BLOBS:
        identity = name[:-3]
        if identity in sys.modules:
            raise ValueError("An owner module was already loaded.")
        module = types.ModuleType(identity)
        module.__file__ = str(root / name)
        sys.modules[identity] = module
        exec(compile(buffers[name], str(root / name), "exec"), module.__dict__)
    return sys.modules["voice_packages"], sys.modules["voice_assets"]


def offline(event, args):
    if event.startswith(("socket.", "urllib.", "subprocess.")) or event in {"os.system", "os.posix_spawn"}:
        raise RuntimeError("Owner metadata acceptance cannot use network or launch another process.")


def ordinary_inventory(root):
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AssertionError("Unexpected linked fixture path.")
        if path.is_file():
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                raise AssertionError("Unexpected fixture file kind or size.")
            result[path.relative_to(root).as_posix()] = sha256(path.read_bytes()).hexdigest()
        if len(result) > 64:
            raise AssertionError("Fixture inventory exceeded its bound.")
    return result


def make_reference(root, version):
    root.mkdir()
    audio = root / "reference.wav"
    with wave.open(str(audio), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        frequency = 240 if version == "1.0.0" else 320
        output.writeframes(b"".join(struct.pack("<h", int(500 * math.sin(
            2 * math.pi * frequency * index / 16000))) for index in range(1600)))
    text = root / "reference.txt"
    text.write_text("Generated synthetic tone fixture " + version + ". No person's voice.\n", encoding="utf-8")
    document = {
        "format": "dordiai-voice-package", "format_version": 1,
        "id": "carr_synthetic_owner", "version": version,
        "name": "Carr synthetic owner API acceptance",
        "engine": {"id": "omnivoice", "compatibility_version": 1},
        "languages": ["uk", "sk"], "styles": ["neutral"],
        "provenance": {"source": "Generated synthetic PCM and text; no person's recording",
                       "license": "CC0 generated acceptance fixture"},
        "files": {},
    }
    for path, role in ((audio, "reference_audio"), (text, "reference_text")):
        raw = path.read_bytes()
        document["files"][path.name] = {"sha256": sha256(raw).hexdigest(),
                                      "size_bytes": len(raw), "role": role}
    return document


def exercise(root, packages, assets, checks):
    started = time.monotonic()

    def check(label, condition):
        if time.monotonic() - started > 90:
            raise TimeoutError("The owner metadata acceptance exceeded 90 seconds.")
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def rejected(label, operation):
        try:
            operation()
        except packages.PackageError:
            check(label, True)
        else:
            raise AssertionError(label + ": unexpectedly accepted")

    first_source, second_source = root / "source-one", root / "source-two"
    first, second = make_reference(first_source, "1.0.0"), make_reference(second_source, "2.0.0")
    original_inputs = {str(path.name): ordinary_inventory(path) for path in (first_source, second_source)}
    first_zip, second_zip = root / "version-one.dordiai-voice", root / "version-two.dordiai-voice"
    first_copy, export_zip = root / "reproducible-copy.dordiai-voice", root / "reviewed-export.dordiai-voice"
    references = root / "references"
    identity = first["id"]
    check("actual owner adapter accepts the generated OmniVoice reference recipe",
          assets.validate_adapter(first) == first and assets.validate_adapter(second) == second)
    check("actual owner verifies generated audio and text checksums",
          packages.verify_assets(first_source, first) == first)
    packages.create_package(first_source, first, first_zip)
    check("actual owner creates and inspects exact version one",
          assets.inspect_package(first_zip) == first)
    packages.create_package(first_source, first, first_copy)
    check("owner packaging is reproducible for the same exact data",
          first_zip.read_bytes() == first_copy.read_bytes())
    rejected("Holos cannot install an OmniVoice reference package",
             lambda: assets.install_voice_package(first_zip, references, engine_id="holos", resources={}))
    check("incompatible installation did not create a package destination", not references.exists())
    installed_first = assets.install_voice_package(first_zip, references, engine_id="omnivoice", resources={})
    check("actual owner installs the exact immutable version directory",
          installed_first == references / "packages" / identity / "1.0.0"
          and packages.verify_assets(installed_first, first) == first)
    first_inventory = ordinary_inventory(installed_first)
    discovered = assets.discover_voices(references, engine_id="omnivoice")
    check("actual discovery exposes the installed version through its declared adapter",
          set(discovered) == {identity} and discovered[identity]["version"] == "1.0.0")
    check("actual discovery never presents this reference as a Holos profile",
          assets.discover_voices(references, engine_id="holos") == {})
    check("actual owner language capability follows only declared languages",
          assets.supports_language(references, identity, "uk")
          and assets.supports_language(references, identity, "sk")
          and assets.supports_language(references, identity, "en") is False)
    check("actual owner resolves only the checksum-verified selected reference assets",
          assets.asset_path(references, identity, "reference_audio", required=True).read_bytes()
          == (first_source / "reference.wav").read_bytes()
          and assets.asset_path(references, identity, "reference_text", required=True).read_bytes()
          == (first_source / "reference.txt").read_bytes())
    packages.create_package(second_source, second, second_zip)
    check("actual owner inspects the exact second version", assets.inspect_package(second_zip) == second)
    installed_second = assets.install_voice_package(second_zip, references, engine_id="omnivoice", resources={})
    check("second install preserves the first immutable version",
          ordinary_inventory(installed_first) == first_inventory
          and packages.verify_assets(installed_second, second) == second)
    check("actual owner lists both retained versions",
          [row["version"] for row in assets.voice_versions(references, identity)] == ["1.0.0", "2.0.0"])
    check("second installation atomically selects its version",
          assets.discover_voices(references, engine_id="omnivoice")[identity]["version"] == "2.0.0")
    index = assets.package_root(references) / packages.INDEX_NAME
    before_collision = index.read_bytes()
    changed = deepcopy(first)
    changed["name"] = "Different metadata under an already installed version"
    collision_zip = root / "version-collision.dordiai-voice"
    packages.create_package(first_source, changed, collision_zip)
    rejected("owner refuses different data under an existing version",
             lambda: assets.install_voice_package(collision_zip, references, engine_id="omnivoice", resources={}))
    check("refused version collision preserves previous data and activation",
          ordinary_inventory(installed_first) == first_inventory and index.read_bytes() == before_collision)
    assets.activate_voice_version(references, identity, "1.0.0", engine_id="omnivoice", resources={})
    check("explicit rollback selects and verifies the retained old version",
          assets.discover_voices(references, engine_id="omnivoice")[identity]["version"] == "1.0.0"
          and packages.verify_assets(installed_first, first) == first)
    packages.create_package(installed_first, first, export_zip)
    check("actual owner exports the exact selected retained version",
          packages.inspect_package(export_zip) == first and export_zip.read_bytes() == first_zip.read_bytes())
    packages.uninstall_package(assets.package_root(references), identity, keep_files=True)
    assets.clear_cache()
    inactive = packages.list_installed_packages(assets.package_root(references))
    check("deactivation retains explicit inactive administrative ownership",
          len(inactive) == 1 and inactive[0]["id"] == identity
          and inactive[0]["active"] is False and inactive[0]["status"] == "inactive")
    check("deactivated package disappears from actual speech discovery",
          assets.discover_voices(references, engine_id="omnivoice") == {})
    check("deactivation keeps both immutable versions available for explicit review",
          len(assets.voice_versions(references, identity)) == 2
          and ordinary_inventory(installed_first) == first_inventory)
    assets.activate_voice_version(references, identity, "2.0.0", engine_id="omnivoice", resources={})
    check("explicit reactivation selects the reviewed second version",
          assets.discover_voices(references, engine_id="omnivoice")[identity]["version"] == "2.0.0")
    selected_text = installed_second / "reference.txt"
    exact_text = selected_text.read_bytes()
    selected_text.write_bytes(b"X" + exact_text[1:])
    assets.clear_cache()
    rejected("actual owner refuses a same-size modified installed reference",
             lambda: assets.asset_path(references, identity, "reference_text", required=True))
    selected_text.write_bytes(exact_text)
    assets.clear_cache()
    check("restored exact fixture bytes verify before any further operation",
          packages.verify_assets(installed_second, second) == second)
    unsafe = deepcopy(first)
    unsafe["files"]["run.py"] = unsafe["files"].pop("reference.txt")
    rejected("actual manifest refuses executable package payloads", lambda: packages.validate_manifest(unsafe))
    unsafe = deepcopy(first)
    unsafe["files"]["../outside.txt"] = unsafe["files"].pop("reference.txt")
    rejected("actual manifest refuses escaping package paths", lambda: packages.validate_manifest(unsafe))
    assets.uninstall_voice_package(references, identity)
    check("explicit removal deletes retained versions and administrative registration",
          not installed_first.parent.exists() and packages.list_installed_packages(assets.package_root(references)) == []
          and assets.discover_voices(references, engine_id="omnivoice") == {})
    check("all generated source originals remain exact through management operations",
          {str(path.name): ordinary_inventory(path) for path in (first_source, second_source)} == original_inputs)
    check("metadata operations loaded no synthesis model, NVDA or tensor runtime",
          not any(name == "torch" or name.startswith(("torch.", "nvda", "synthDrivers", "numpy"))
                  for name in sys.modules))
    return {"profile": identity, "versions": ["1.0.0", "2.0.0"],
            "package_sha256": {"1.0.0": sha256(first_zip.read_bytes()).hexdigest(),
                               "2.0.0": sha256(second_zip.read_bytes()).hexdigest()},
            "operation_seconds": time.monotonic() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acquire", action="store_true", required=True,
                        help="Explicitly acquire only three pinned public owner API files.")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not sys.flags.isolated or not sys.flags.dont_write_bytecode or not __debug__:
        raise RuntimeError("Use the isolated, nonoptimized, no-bytecode interpreter.")
    report = {"schema": "carr-actual-owner-voice-api/1", "status": "FAIL",
              "owner_repository": OWNER, "owner_revision": REVISION, "owner_prefix": PREFIX,
              "platform": platform.system(), "python": platform.python_version(), "checks": [],
              "scope": "Real pinned owner data-management API with generated synthetic PCM and text only",
              "limits": {"current_application_manager": "NOT_RUN", "private_model_inference": "NOT_RUN",
                         "acoustic_quality": "NOT_RUN", "nvda": "NOT_RUN", "release": "NOT_RUN",
                         "public_owner_source_or_audio_payload": False}}
    code = 1
    try:
        with TemporaryDirectory(prefix="carr-owner-metadata-") as temporary:
            root = Path(temporary).resolve()
            owner_root = root / "owner"
            buffers, receipts = acquire(owner_root)
            report["source_receipts"] = receipts
            packages, assets = load_owner(owner_root, buffers)
            sys.addaudithook(offline)
            report["operations"] = exercise(root, packages, assets, report["checks"])
            if {name: sha256((owner_root / name).read_bytes()).hexdigest() for name in SOURCE_BLOBS} != {
                    name: row["sha256"] for name, row in receipts.items()}:
                raise AssertionError("Owner source changed during metadata acceptance.")
            report["checks"].append("all three executed owner source files remain exact")
        if root.exists():
            raise AssertionError("Temporary source and generated audio were not retired.")
        report["checks"].append("temporary owner source, packages and generated audio were retired")
        report["status"], code = "PASS", 0
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)[:4096]}
    raw = (json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(raw) > 32 * 1024:
        raise RuntimeError("The bounded metadata receipt is oversized.")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("xb") as stream:
        stream.write(raw)
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]),
                      "report_sha256": sha256(raw).hexdigest(),
                      "owner_revision": REVISION, "platform": report["platform"],
                      "error": report.get("error")}, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
