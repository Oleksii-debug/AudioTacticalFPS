"""Build a private unsigned x64 launch entry using a hash-pinned Zig archive.

No download, signing, bundle mutation or artifact publication occurs here.
The output directory must be outside all Git worktrees.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import zipfile

from probe_pe import inspect_pe


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_compiler(executable, archive, source):
    lock = json.loads((source / "toolchain.lock.json").read_text())
    host = "x86_64-windows" if os.name == "nt" else "x86_64-linux"
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("This toolchain lock supports x86_64 build hosts only")
    expected = lock[host]
    if archive.stat().st_size != expected["bytes"] or digest(archive) != expected["sha256"]:
        raise ValueError("The compiler archive does not match the pinned primary-source digest")
    filename = "zig.exe" if os.name == "nt" else "zig"
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as packed:
            names = [name for name in packed.namelist() if name.endswith("/" + filename) and name.count("/") == 1]
            if len(names) != 1:
                raise ValueError("Ambiguous compiler executable in archive")
            with packed.open(names[0]) as stream:
                expected_binary = hashlib.file_digest(stream, "sha256").hexdigest()
    else:
        with tarfile.open(archive) as packed:
            names = [member for member in packed.getmembers() if member.isfile()
                     and member.name.endswith("/" + filename) and member.name.count("/") == 1]
            if len(names) != 1:
                raise ValueError("Ambiguous compiler executable in archive")
            with packed.extractfile(names[0]) as stream:
                expected_binary = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest(executable) != expected_binary:
        raise ValueError("The selected compiler is not the executable from the verified archive")
    version = subprocess.check_output([str(executable), "version"], text=True, timeout=15).strip()
    if version != lock["version"]:
        raise ValueError("Unexpected compiler version")
    return {"version": version, "archive_sha256": expected["sha256"],
            "archive_bytes": expected["bytes"], "binary_sha256": expected_binary, "primary_url": expected["url"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--toolchain-archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--build-probe", action="store_true", help="Build the synthetic Windows child only for private CI testing")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    output = args.output_dir.resolve()
    if any((parent / ".git").exists() for parent in (output, *output.parents)):
        raise ValueError("Private native binaries must be built outside Git worktrees")
    output.mkdir(parents=True, exist_ok=True)
    executable = output / "ResearchLibrary.exe"
    if executable.exists():
        raise FileExistsError("Choose a fresh private output directory")
    zig, archive = args.zig.resolve(strict=True), args.toolchain_archive.resolve(strict=True)
    toolchain = verified_compiler(zig, archive, source)
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(output.parent / "launcher-compiler-cache"),
               ZIG_LOCAL_CACHE_DIR=str(output / "local-cache"), SOURCE_DATE_EPOCH="0")
    resource = output / "launcher.res"
    subprocess.run([str(zig), "rc", "/x", "/c", "65001", "/fo", str(resource), "launcher.rc"],
                   cwd=source, env=env, check=True, timeout=300)
    flags = ["cc", "-target", "x86_64-windows-gnu", "-std=c11", "-Os", "-ffreestanding", "-fno-builtin",
             "-fno-stack-protector", "-nostdlib", "-Wall", "-Wextra", "-Werror", "-Wl,--entry=launcher_entry",
             "-Wl,--subsystem,windows", "-s"]
    subprocess.run([str(zig), *flags, "launcher.c", str(resource), "-lkernel32", "-luser32", "-lshell32",
                    "-o", str(executable)], cwd=source, env=env, check=True, timeout=300)
    report = {"schema": 1, "toolchain": toolchain,
              "source_sha256": {name: digest(source / name) for name in
                                ("launcher.c", "launcher_core.h", "launcher.rc", "launcher.manifest", "toolchain.lock.json")},
              "target": "x86_64-windows-gnu", "pe": inspect_pe(executable),
              "release_status": "unsigned-private-candidate"}
    if args.build_probe:
        subprocess.run([str(zig), *flags, "-Wno-unused-function", "windows_child_probe.c", "-lkernel32", "-lshell32", "-o",
                        str(output / "windows_child_probe.exe")], cwd=source, env=env, check=True, timeout=300)
    (output / "build-evidence.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
