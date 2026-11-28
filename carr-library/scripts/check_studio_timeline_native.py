"""Verify the fixed Studio native report and its pre-run identities for CI."""
from __future__ import annotations

import argparse
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
import sysconfig

DATA_GIT = "5d6c9fa8eed10b037bdb056ce0d942a0ce21de00"
NATIVE_GIT = "e202ebe7f61490d2745d315f31330f97372ec705"
AUDIO_LOCK_GIT = "2bc706ffc6208f5596a10abee61d3da33f20e6c6"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            getattr(info, "st_file_attributes", 0)]


def descriptor_identity(info, path):
    value = identity(info)
    # CPython 3.11 adds these pathname-derived bits only to Windows stat.
    if os.name == "nt" and path.suffix.lower() in (".exe", ".bat", ".cmd", ".com"):
        value[2] &= ~0o111
    return value


def read_checked(path, limit, *, git=None, digest=None, size=None, keep=False):
    path = Path(path).absolute()
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and not getattr(before, "st_file_attributes", 0) & 0x400,
            "The CI input is not an ordinary single-link file: " + str(path))
    require(0 <= before.st_size <= limit and (size is None or before.st_size == size),
            "The CI input size differs: " + str(path))
    blob = sha1(b"blob " + str(before.st_size).encode("ascii") + b"\0")
    digest256 = sha256()
    parts, total = [], 0
    with path.open("rb") as stream:
        require(descriptor_identity(os.fstat(stream.fileno()), path)
                == descriptor_identity(before, path), "Input changed while opening")
        while True:
            part = stream.read(min(1024 * 1024, limit + 1 - total))
            if not part:
                break
            total += len(part)
            require(total <= limit, "Input grew past its exact bound")
            blob.update(part)
            digest256.update(part)
            if keep:
                parts.append(part)
        require(descriptor_identity(os.fstat(stream.fileno()), path)
                == descriptor_identity(before, path), "Opened input changed while reading")
    require(identity(path.lstat()) == identity(before) and total == before.st_size,
            "Named input changed while reading")
    git_value, digest_value = blob.hexdigest(), digest256.hexdigest()
    require(git is None or git_value == git, "Reviewed Git input bytes differ: " + str(path))
    require(digest is None or digest_value == digest, "Pinned executable bytes differ: " + str(path))
    record = {"path": str(path), "identity": identity(before),
              "git_blob": git_value, "sha256": digest_value}
    return record, b"".join(parts) if keep else None


def capture(audio_directory):
    checkout = Path.cwd().resolve(strict=True)
    require((checkout / "src/carr_library").is_dir(), "Run from the carr-library checkout")
    current = Path(__file__).absolute()
    require(current == checkout / "scripts/check_studio_timeline_native.py",
            "The native verifier must be the current checked-out helper")
    evidence = {}
    evidence["helper"], _ = read_checked(current, 65536)
    evidence["data"], raw = read_checked(
        checkout / "scripts/ci_studio_timeline_cases.json", 19559,
        git=DATA_GIT, size=19559, keep=True)
    data = json.loads(raw)
    require(set(data) == {"schema", "suites", "native", "native_script_git_blob"}
            and data["schema"] == "carr-studio-timeline-contracts/1"
            and data["native_script_git_blob"] == NATIVE_GIT, "Studio data schema differs")
    native = data["native"]
    require(native["path"] == "scripts/windows_clip_editor_smoke.py"
            and native["installed_only"] is True and native["result_key"] == "result",
            "Native script contract differs")
    require(native["required_unique_count"] == 92
            and len(native["required_unique_checks"]) == 92
            and len(set(native["required_unique_checks"])) == 92
            and native["origin_count"] == 15 == len(native["required_origins"])
            and len(native["required_fields"]) == 13, "Native coverage contract differs")
    evidence["native_script"], _ = read_checked(checkout / native["path"], 65536, git=NATIVE_GIT)
    evidence["audio_lock"], lock_raw = read_checked(
        checkout / "packaging/windows-audio.lock.json", 65536, git=AUDIO_LOCK_GIT, keep=True)
    lock = json.loads(lock_raw)
    prefix = Path(sys.prefix).resolve(strict=True)
    installed_root = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    require(installed_root.is_relative_to(prefix) and "site-packages" in installed_root.parts
            and not installed_root.is_relative_to(checkout), "Expected actual installed package")
    installed_paths = {}
    for name, entry in native["required_origins"].items():
        relative = Path(*name.split(".")).with_suffix(".py")
        require(entry["path"] == "carr-library/src/" + relative.as_posix(),
                "Reviewed source origin mapping differs")
        source = checkout / "src" / relative
        installed = installed_root / relative
        evidence["source:" + name], _ = read_checked(source, 2 * 1024 * 1024, git=entry["sha"])
        evidence["installed:" + name], _ = read_checked(installed, 2 * 1024 * 1024, git=entry["sha"])
        installed_paths[name] = str(installed)
    audio = Path(audio_directory).resolve(strict=True)
    for name in ("ffmpeg", "ffprobe"):
        expected = lock["output_files"]["bin/" + name + ".exe"]
        size = expected["size"]
        require(type(size) is int and 0 < size <= 512 * 1024 * 1024, "Audio lock size differs")
        evidence["audio:" + name], _ = read_checked(
            audio / "bin" / (name + ".exe"), size, size=size, digest=expected["sha256"])
    snapshot = {"schema": "carr-studio-native-preflight/1", "checkout": str(checkout),
                "prefix": str(prefix), "installed_root": str(installed_root),
                "audio_directory": str(audio), "files": evidence}
    return snapshot, native, lock, installed_paths


def write_new(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    with path.open("xb") as stream:
        stream.write(raw)
    return sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("pre", "post"))
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--audio-directory", required=True, type=Path)
    parser.add_argument("--proof-sha256")
    args = parser.parse_args()
    require(os.name == "nt", "This fixed native acceptance requires Windows")
    log = args.log.absolute()
    require(log.suffix == ".txt" and log.parent.is_dir() and not log.parent.is_symlink(),
            "Use the existing CI result directory and a .txt log name")
    proof = log.with_suffix(".pre.json")
    output = log.with_suffix(".json")
    snapshot, native, lock, installed_paths = capture(args.audio_directory)
    if args.phase == "pre":
        require(args.proof_sha256 is None, "Preflight cannot accept a prior proof digest")
        digest = write_new(proof, snapshot)
        print(json.dumps({"status": "PREPARED", "proof": str(proof),
                          "proof_sha256": digest, "native_execution": "NOT_RUN"}), flush=True)
        return 0
    require(isinstance(args.proof_sha256, str)
            and re.fullmatch(r"[a-f0-9]{64}", args.proof_sha256) is not None,
            "Postflight requires the digest captured before native execution")
    _, original = read_checked(proof, 65536, digest=args.proof_sha256, keep=True)
    require(json.loads(original) == snapshot,
            "The native owner did not preserve the original preflight file identities")
    _, raw = read_checked(log, 1024 * 1024, keep=True)
    text = raw.decode("utf-8-sig")
    reports = []
    for match in re.finditer(r"(?m)^\{", text):
        try:
            report, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(report, dict) and "native_crossfade" in report:
            reports.append(report)
    require(len(reports) == 1, "Native Studio report is missing or ambiguous")
    report = reports[0]
    require(all(type(report.get(key)) is str and report[key] == value
                for key, value in native["required_fields"].items()),
            "Native Studio result or qualification fields differ")
    checks = report.get("checks")
    require(isinstance(checks, list) and all(type(value) is str for value in checks)
            and set(native["required_unique_checks"]) <= set(checks),
            "A required successful native Studio check is missing")
    require(checks.count("original selection requires an existing file") >=
            native["check_multiplicity"]["original_file_picker_minimum"],
            "Native original-file admission was not exercised for every retained input")
    origins = report.get("module_origins")
    require(isinstance(origins, dict) and set(origins) == set(installed_paths),
            "Native installed-origin inventory differs")
    for name, expected in installed_paths.items():
        require(type(origins[name]) is str and Path(origins[name]) == Path(expected),
                "Native reported module origin differs: " + name)
    runtime = report.get("audio_runtime")
    require(isinstance(runtime, dict) and set(runtime) == {"ffmpeg", "ffprobe"},
            "Native audio proof inventory differs")
    for name, record in runtime.items():
        expected_path = Path(snapshot["audio_directory"]) / "bin" / (name + ".exe")
        require(isinstance(record, dict) and set(record) == {"path", "sha256"}
                and type(record["path"]) is str and Path(record["path"]) == expected_path
                and record["sha256"] == lock["output_files"]["bin/" + name + ".exe"]["sha256"],
                "Native explicit audio proof differs")
    require(capture(args.audio_directory)[0] == snapshot,
            "Inputs changed during postflight report verification")
    digest = write_new(output, report)
    print(json.dumps({"status": "PASS", "required_unique_checks": 92,
                      "observed_unique_checks": len(set(checks)),
                      "check_occurrences": len(checks), "origins": 15,
                      "original_preflight_preserved": True,
                      "report": str(output), "report_sha256": digest}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
