"""Prepare an offline Inno Setup installer from an explicitly pinned sealed bundle.

This is a standard per-user installer, not an update/signing/entitlement service.
Only a caller-supplied, hash-pinned local compiler may execute. Supplied bundle
code is never imported or executed by this builder.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def _delivery():
    spec = importlib.util.spec_from_file_location("carr_installer_delivery", HERE / "create_delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hash(value, label):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError(label + " must be an independently recorded lowercase SHA256.")
    return value


def _iss(value):
    # Windows inventory names already reject quotes, controls and separators.
    # Braces are valid names but would otherwise become Inno runtime constants.
    return value.replace("{", "{{").replace("/", "\\")


def _pascal(value):
    return value.replace("'", "''").replace("/", "\\")


def recipe(files, source_commit, manifest_sha256):
    """Use explicit entries; never a recursive wildcard over the supplied tree."""
    lines, temporary, checks, copies = [], [], [], []
    for name, digest in sorted(files.items()):
        directory = name.rpartition("/")[0]
        destination = "{code:PayloadDir}" + ("\\" + _iss(directory) if directory else "")
        lines.append('Source: "payload\\' + _iss(name) + '"; DestDir: "' + destination
                     + '"; Flags: ignoreversion')
        temp_destination = "{tmp}\\resonance-payload" + ("\\" + _iss(directory) if directory else "")
        temporary.append('Source: "payload\\' + _iss(name) + '"; DestDir: "' + temp_destination
                         + '"; Flags: dontcopy')
        checks.append("  CheckPayload('" + _pascal(name) + "', '" + digest + "');")
        copies.append("    StagePayload('" + _pascal(name) + "', '" + digest + "');")
    temporary.append('Source: "payload\\bundle-manifest.json"; DestDir: "{tmp}\\resonance-payload"; Flags: dontcopy')
    # PrepareToInstall has a documented nonempty-error return contract. Inno
    # deliberately swallows AfterInstall exceptions, so bootstrap cannot go there.
    lines.append('Source: "payload\\bundle-manifest.json"; DestDir: "{code:PayloadDir}"; '
                 'Flags: ignoreversion')
    text = (HERE / "standard_windows.iss.in").read_text(encoding="utf-8")
    for key, value in {"@SOURCE@": source_commit, "@SHORT_SOURCE@": source_commit[:12],
                       "@MANIFEST@": manifest_sha256, "@PAYLOAD_FILES@": "\n".join(lines),
                       "@TEMP_FILES@": "\n".join(temporary), "@PAYLOAD_COPIES@": "\n".join(copies),
                       "@PAYLOAD_CHECKS@": "\n".join(checks)}.items():
        text = text.replace(key, value)
    return text


def prepare(bundle, source_commit, manifest_sha256, output, *, compiler=None,
            compiler_sha256=None, cancelled=lambda: False):
    delivery = _delivery()
    delivery._check(cancelled)
    manifest_sha256 = _hash(manifest_sha256, "Bundle manifest digest")
    if (compiler is None) != (compiler_sha256 is None):
        raise ValueError("Supply both the local Inno compiler and its recorded SHA256.")
    if compiler is not None:
        _hash(compiler_sha256, "Inno compiler digest")
        if os.name != "nt":
            raise RuntimeError("Inno compilation requires native Windows; preparation is portable.")
        compiler = delivery._absolute(compiler)
        with delivery._file(compiler) as stream:
            actual, _ = delivery._stream(stream, cancelled)
        if actual != compiler_sha256:
            raise ValueError("Inno compiler differs from its recorded SHA256.")
    bundle, output = delivery._absolute(bundle), delivery._absolute(output)
    delivery._directories(bundle)
    parents = delivery._directories(output.parent)
    if os.path.lexists(output):
        raise FileExistsError("Use a new installer output directory.")
    if output.is_relative_to(bundle) or bundle.is_relative_to(output):
        raise ValueError("Installer output must be outside its input bundle.")
    raw = delivery._read_manifest(bundle, cancelled)
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("Bundle manifest differs from its recorded SHA256.")
    manifest = delivery._manifest(raw, source_commit)
    delivery._require_transferable_audio_inventory(manifest)
    files = manifest["files"]
    if any("{" in name or "}" in name for name in files):
        # Inno has both runtime constants and an inline preprocessor. Reject
        # these rare inventory names rather than interpret them as build code.
        raise ValueError("Installer payload paths cannot contain Inno constant braces.")
    wheels = [n for n in files if n.startswith("wheelhouse/universal_research_library-")
              and n.endswith(".whl")]
    if (manifest.get("bundled_python") is not True or "runtime/python.exe" not in files
            or manifest.get("bundled_native_launcher") is not True
            or not {"ResearchLibrary.exe", "native-launcher-build.json"}.issubset(files)
            or len(wheels) != 1):
        raise ValueError("The standard installer needs bundled Python, one application wheel and the native launcher.")
    # These are exclusively installer-generated paths, never payload authority.
    if any(n.split("/", 1)[0].casefold() in {".venv", "maintenance-setup.exe"} for n in files):
        raise ValueError("Bundle collides with an installer-owned generated path.")
    sizes = [delivery._plain(bundle / name).st_size for name in files]
    if max(sizes) > delivery.MAX_FILE or sum(sizes) > delivery.MAX_TOTAL:
        raise ValueError("Bundle exceeds the delivery size limit.")
    delivery._trusted_verify(bundle, manifest_bytes=raw)
    parent_fd = (os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                 if os.name != "nt" else None)
    stage = identity = None
    try:
        delivery._unchanged_directories(parents)
        stage = Path(tempfile.mkdtemp(prefix=".carr-standard-installer-", dir=output.parent))
        identity = delivery._identity(delivery._plain(stage, directory=True))
        payload = stage / "payload"
        payload.mkdir()
        for name, expected in sorted(files.items()):
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with delivery._file(bundle / name) as source, target.open("xb") as destination:
                actual, _ = delivery._stream(source, cancelled, destination=destination,
                                              limit=delivery.MAX_FILE)
                destination.flush()
                os.fsync(destination.fileno())
            if actual != expected:
                raise ValueError("Bundle changed during installer preparation: " + name)
        (payload / delivery.MANIFEST).write_bytes(raw)
        script = recipe(files, source_commit, manifest_sha256)
        (stage / "Resonance.iss").write_text(script, encoding="utf-8-sig")
        (stage / "install-layout-v1.txt").write_bytes(b"Resonance.StandardInstall/1\r\n")
        delivery._trusted_verify(payload, manifest_bytes=raw)
        if delivery._read_manifest(bundle, cancelled) != raw:
            raise ValueError("Bundle manifest changed during installer preparation.")
        report = {"schema": 1, "kind": "standard-windows-installer",
                  "managed_update_protocol": "resonance-standard-update/1",
                  "source_commit": source_commit, "manifest_sha256": manifest_sha256,
                  "payload_files": len(files), "status": "prepared",
                  "recipe_sha256": hashlib.sha256((stage / "Resonance.iss").read_bytes()).hexdigest(),
                  "native_install_repair_uninstall": "NOT_RUN",
                  "authenticode_verified": False, "nvda_verified": False,
                  "update_or_entitlement_authority": False, "automatic_network": False}
        if compiler is not None:
            delivery._check(cancelled)
            with delivery._file(compiler) as stream:
                if delivery._stream(stream, cancelled)[0] != compiler_sha256:
                    raise ValueError("Inno compiler changed before execution.")
            subprocess.run([str(compiler), "/Qp", str(stage / "Resonance.iss")],
                           cwd=stage, check=True, timeout=900)
            executable = stage / "dist" / ("Resonance-Setup-" + source_commit[:12] + ".exe")
            with delivery._file(executable) as stream:
                setup_hash, size = delivery._stream(stream, cancelled)
            if not size:
                raise ValueError("Inno produced an empty installer.")
            report.update(status="compiled", compiler_sha256=compiler_sha256,
                          installer={"path": executable.relative_to(stage).as_posix(),
                                     "sha256": setup_hash, "bytes": size})
        # The generated recipe independently rechecks these bytes at installation.
        # This final trusted rehash also rejects mutation while the compiler ran.
        delivery._trusted_verify(payload, manifest_bytes=raw)
        if (delivery._read_manifest(payload, cancelled) != raw
                or delivery._read_manifest(bundle, cancelled) != raw
                or (stage / "Resonance.iss").read_text(encoding="utf-8-sig") != script):
            raise ValueError("Installer inputs changed before publication.")
        if {p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()} != {*files, delivery.MANIFEST}:
            raise ValueError("Unexpected installer payload files.")
        (stage / "installer-build.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        delivery._check(cancelled)
        delivery._unchanged_directories(parents)
        if delivery._identity(delivery._plain(stage, directory=True)) != identity:
            raise ValueError("Installer staging directory changed before publication.")
        delivery._publish(stage, output, parent_fd)
        stage = None
        return {"output": str(output), **report}
    finally:
        try:
            if stage is not None:
                delivery._cleanup(stage, identity, parent_fd)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, help="Explicit local Inno Setup 6.4+ ISCC.exe; no download")
    parser.add_argument("--compiler-sha256")
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.bundle, args.source_commit, args.manifest_sha256, args.output,
                             compiler=args.compiler, compiler_sha256=args.compiler_sha256), indent=2))


if __name__ == "__main__":
    main()
