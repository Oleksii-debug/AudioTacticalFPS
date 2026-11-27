"""Build a Windows offline wheel bundle using a pre-resolved hash lock.

No dependency resolver, wheel extractor, downloader or installer is reimplemented.
The build uses packaging for wheel/requirement validation; installation uses pip.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import tarfile
import sys
from zipfile import ZipFile
from email.parser import BytesParser

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


TARGET = "Windows x64 / CPython 3.11"
# Exact unsigned candidate built and exercised by the reviewed Windows producer.
NATIVE_LAUNCHER_SHA256 = "5ba9218a329f8d002539bd57f73d91170125f5ec91c78d49ff85ca6c74443cec"
HERE = Path(__file__).resolve().parent
TAGS = set(cpython_tags((3, 11), abis=["cp311"], platforms=["win_amd64"]))
TAGS.update(compatible_tags((3, 11), interpreter="cp311", platforms=["win_amd64"]))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"expected a regular file: {path.name}")
    return path


def read_lock(path):
    """Accept named exact pins and SHA256 only, without executable pip options."""
    text = regular(path).read_text(encoding="utf-8")
    logical = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
        else:
            logical.append(pending + line)
            pending = ""
    if pending:
        raise ValueError("incomplete requirements line")
    pins = {}
    for line in logical:
        chunks = line.split()
        req = Requirement(chunks[0])
        specs = list(req.specifier)
        if (req.url or req.extras or req.marker or len(specs) != 1
                or specs[0].operator != "==" or "*" in specs[0].version):
            raise ValueError("a named exact requirement without markers is required")
        name = canonicalize_name(req.name)
        hashes = set()
        for chunk in chunks[1:]:
            match = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})", chunk)
            if not match:
                raise ValueError("unexpected lock option")
            hashes.add(match[1])
        if not hashes or name in pins:
            raise ValueError("missing hashes or duplicate requirement")
        pins[name] = (specs[0].version, hashes)
    if not pins:
        raise ValueError("empty dependency lock")
    return pins


def wheel_metadata(path):
    name, version, _build, tags = parse_wheel_filename(path.name)
    if not tags.intersection(TAGS):
        raise ValueError(f"wheel does not target {TARGET}: {path.name}")
    with ZipFile(path) as archive:
        metadata = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or archive.getinfo(metadata[0]).file_size > 4 * 1024 * 1024:
            raise ValueError("invalid wheel metadata")
        parsed = BytesParser().parsebytes(archive.read(metadata[0]))
    if canonicalize_name(parsed["Name"]) != name or parsed["Version"] != str(version):
        raise ValueError("wheel name and metadata differ")
    return name, version, parsed


def verify_application_source(wheel, source_commit):
    """Bind a release source claim to the wheel's actual committed payload."""
    spec = importlib.util.spec_from_file_location(
        "carr_source_wheel_verifier", HERE / "source_wheel.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    evidence = verifier.verify_application_wheel(wheel, HERE.parent.parent, source_commit)
    evidence["source_tree"] = _native_git("rev-parse", "--verify", source_commit + "^{tree}").decode("ascii").strip()
    return evidence


def verify_dependency_closure(selected):
    """Check the exact Windows dependency graph without invoking a resolver."""
    environment = default_environment()
    environment.update(os_name="nt", sys_platform="win32", platform_system="Windows",
                       platform_machine="AMD64", python_version="3.11",
                       python_full_version="3.11.0", implementation_name="cpython",
                       platform_python_implementation="CPython", extra="")
    for _path, _version, metadata in selected.values():
        for raw in metadata.get_all("Requires-Dist", []):
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate(environment):
                continue
            dependency = selected.get(canonicalize_name(req.name))
            if req.url or dependency is None or dependency[1] not in req.specifier:
                raise ValueError(f"unsatisfied Windows dependency: {req}")


def _native_unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate native launcher proof key")
        result[name] = value
    return result


def _native_git(*arguments):
    try:
        return subprocess.check_output(
            ["git", "--no-replace-objects", "-C", str(HERE.parent.parent), *arguments],
            stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("native launcher source is unavailable in the selected commit") from exc


def verify_native_launcher(executable, proof, expected_sha256, source_commit):
    """Capture a pinned producer PE and bind its build inputs to committed source.

    This uses the existing Zig build evidence and PE inspector. It neither builds
    nor executes native code, and does not claim signing or Windows acceptance.
    A delivery caller must independently bind these exact bytes to its CI run.
    """
    if (not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit)
            or not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        raise ValueError("native launcher requires exact source and executable hashes")
    executable, proof = regular(Path(executable)), regular(Path(proof))
    if not 0 < executable.stat().st_size <= 2 * 1024 * 1024 or not 0 < proof.stat().st_size <= 65536:
        raise ValueError("native launcher or build proof exceeds its size limit")
    executable_bytes, proof_bytes = executable.read_bytes(), proof.read_bytes()
    if hashlib.sha256(executable_bytes).hexdigest() != expected_sha256:
        raise ValueError("native launcher checksum differs from its explicit pin")
    data = json.loads(proof_bytes, object_pairs_hook=_native_unique)
    if (not isinstance(data, dict) or data.get("schema") != 1
            or data.get("target") != "x86_64-windows-gnu"
            or data.get("release_status") != "unsigned-private-candidate"):
        raise ValueError("expected unsigned pinned Zig native launcher evidence")
    spec = importlib.util.spec_from_file_location(
        "carr_native_launcher_pe", HERE / "native-launcher" / "probe_pe.py")
    inspector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inspector)
    pe = inspector.inspect_pe(executable)
    if (pe != data.get("pe") or pe["file"] != "ResearchLibrary.exe"
            or pe["sha256"] != expected_sha256 or pe["bytes"] != len(executable_bytes)):
        raise ValueError("native launcher PE differs from the producer build proof")
    if _native_git("rev-parse", "--verify", source_commit + "^{commit}").decode("ascii").strip() != source_commit:
        raise ValueError("native launcher source commit differs")
    source_tree = _native_git("rev-parse", "--verify", source_commit + "^{tree}").decode("ascii").strip()
    sources = ("launcher.c", "launcher_core.h", "launcher.rc", "launcher.manifest", "toolchain.lock.json")
    declared = data.get("source_sha256")
    if not isinstance(declared, dict) or set(declared) != set(sources):
        raise ValueError("native launcher build source inventory differs")
    committed = {name: _native_git("cat-file", "blob", source_commit +
                                  ":carr-library/packaging/native-launcher/" + name) for name in sources}
    source_hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in committed.items()}
    if declared != source_hashes:
        raise ValueError("native launcher build proof differs from committed source")
    toolchain_lock = json.loads(committed["toolchain.lock.json"], object_pairs_hook=_native_unique)
    compiler = data.get("toolchain")
    if (not isinstance(compiler, dict) or compiler.get("version") != toolchain_lock.get("version")
            or not isinstance(compiler.get("binary_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", compiler["binary_sha256"])):
        raise ValueError("native launcher compiler identity is missing")
    hosts = [host for host in ("x86_64-linux", "x86_64-windows")
             if compiler.get("archive_sha256") == toolchain_lock[host]["sha256"]
             and compiler.get("archive_bytes") == toolchain_lock[host]["bytes"]
             and compiler.get("primary_url") == toolchain_lock[host]["url"]]
    if len(hosts) != 1:
        raise ValueError("native launcher compiler is not admitted by the committed toolchain lock")
    report = {"schema": 1, "status": "PASS", "source_commit": source_commit, "source_tree": source_tree,
              "launcher_sha256": expected_sha256, "launcher_bytes": len(executable_bytes),
              "build_proof_sha256": hashlib.sha256(proof_bytes).hexdigest(), "source_sha256": source_hashes,
              "toolchain_host": hosts[0], "release_status": "unsigned-private-candidate",
              "native_windows_execution": "NOT_RUN", "authenticode_verified": False, "nvda_verified": False}
    return {"executable_bytes": executable_bytes, "proof_bytes": proof_bytes, "report": report}


def capture_native_launcher(executable, proof, expected_sha256, source_commit, *, bundled_python):
    """Admit optional producer inputs under the current release's fixed pin."""
    options = (executable, proof, expected_sha256)
    if not any(value is not None for value in options):
        return None
    if not all(value is not None for value in options):
        raise ValueError("supply native launcher, build proof and explicit SHA256 together")
    if not bundled_python or source_commit is None:
        raise ValueError("native launcher requires bundled Python and an exact source commit")
    if expected_sha256 != NATIVE_LAUNCHER_SHA256:
        raise ValueError("native launcher differs from the fixed Windows-tested executable pin")
    return verify_native_launcher(executable, proof, expected_sha256, source_commit)


def build_bundle(wheel, wheelhouse, lock, output, python_archive=None,
                 model_dir=None, model_manifest=None, sbom=None, source_commit=None, ocr_dir=None,
                 ocr_source_archive=None, native_launcher=None, native_launcher_proof=None,
                 native_launcher_sha256=None):
    wheel, wheelhouse, lock, output = map(Path, (wheel, wheelhouse, lock, output))
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory")
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be a full lowercase Git SHA")
    native_evidence = capture_native_launcher(
        native_launcher, native_launcher_proof, native_launcher_sha256, source_commit,
        bundled_python=python_archive is not None)
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("expected a dedicated wheel directory")
    pins = read_lock(lock)
    if sbom is not None:
        sbom = regular(Path(sbom))
        # Seal the very bytes whose dependency inventory was validated.
        # Do not re-open a mutable input later in the build.
        sbom_bytes = sbom.read_bytes()
        bom = json.loads(sbom_bytes.decode("utf-8"))
        expected = {(name, version) for name, (version, _hashes) in pins.items()}
        actual = {(canonicalize_name(item["name"]), item["version"])
                  for item in bom.get("components", [])}
        if (bom.get("bomFormat") != "CycloneDX" or bom.get("specVersion") != "1.5"
                or actual != expected):
            raise ValueError("runtime SBOM does not match the dependency lock")
        components = bom["components"]
        refs = [item["bom-ref"] for item in components if "bom-ref" in item]
        if len(components) != len(expected) or len(refs) != len(set(refs)):
            raise ValueError("duplicate runtime SBOM component or reference")
    selected = {}
    inventory = {}
    for path in sorted(wheelhouse.iterdir()):
        regular(path)
        if path.suffix != ".whl":
            raise ValueError("wheelhouse must contain wheels only")
        name, version, metadata = wheel_metadata(path)
        digest = sha256(path)
        if name not in pins or str(version) != pins[name][0] or digest not in pins[name][1]:
            raise ValueError(f"wheel not admitted by dependency lock: {path.name}")
        if name in selected:
            raise ValueError("more than one wheel for a dependency")
        selected[name] = (path, version, metadata)
        inventory[path.name] = digest
    if set(selected) != set(pins):
        raise ValueError("incomplete wheelhouse")
    regular(wheel)
    name, version, metadata = wheel_metadata(wheel)
    if name != "universal-research-library" or name in selected:
        raise ValueError("expected the application wheel")
    with ZipFile(wheel) as archive:
        if "carr_library/__main__.py" not in archive.namelist():
            raise ValueError("application wheel has no production entrypoint")
    source_evidence = (verify_application_source(wheel, source_commit)
                       if source_commit is not None else None)
    selected[name] = (wheel, version, metadata)
    # Retain the digest of the exact bytes inspected by the source verifier.
    # A concurrent build can replace this fixed-name wheel before copying it;
    # the existing copy-time comparison must reject that replacement.
    inventory[wheel.name] = (source_evidence["wheel_sha256"]
                            if source_evidence is not None else sha256(wheel))
    if python_archive is not None:
        python_archive = regular(Path(python_archive))
        runtime = json.loads((HERE / "windows-python.json").read_text(encoding="utf-8"))
        if sha256(python_archive) != runtime["download"]["sha256"]:
            raise ValueError("Python runtime archive checksum mismatch")
    if (model_dir is None) != (model_manifest is None):
        raise ValueError("supply both model directory and model manifest")
    if model_dir is not None:
        if "fastembed" not in selected:
            raise ValueError("model bundle requires the semantic dependency lock")
        # Use the same pinned verifier as the application, without importing the
        # optional FastEmbed engine or downloading model data during packaging.
        source_root = str(HERE.parent / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        from carr_library.semantic import verify_model_bundle
        model_dir, model_manifest = Path(model_dir), regular(Path(model_manifest))
        verify_model_bundle(model_dir, model_manifest)
    if ocr_dir is not None:
        source_root = str(HERE.parent / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        from carr_library.extract.ocr_runtime import verify_ocr_bundle
        ocr_dir = Path(ocr_dir)
        verify_ocr_bundle(ocr_dir)
        ocr_manifest = json.loads((ocr_dir / "ocr-manifest.json").read_text(encoding="utf-8"))
        if ocr_manifest.get("target") != "win_amd64":
            raise ValueError("OCR runtime does not target Windows x64")
        source_identity = ocr_manifest.get("provenance", {}).get("corresponding_source")
        if source_identity is not None:
            if ocr_source_archive is None:
                raise ValueError("OCR runtime requires its corresponding-source archive")
            ocr_source_archive = regular(Path(ocr_source_archive))
            if (not isinstance(source_identity, dict)
                    or source_identity.get("size") != ocr_source_archive.stat().st_size
                    or source_identity.get("sha256") != sha256(ocr_source_archive)):
                raise ValueError("OCR corresponding-source archive checksum mismatch")
        elif ocr_source_archive is not None:
            raise ValueError("OCR source archive is not declared in its runtime provenance")
    elif ocr_source_archive is not None:
        raise ValueError("OCR source archive requires the OCR runtime")

    # Validate active Windows requirements from every wheel. Pip performs the
    # actual offline resolution again at install time; this is a build preflight.
    verify_dependency_closure(selected)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="carr-bundle-", dir=output.parent) as scratch:
        staged = Path(scratch)
        staged_wheels = staged / "wheelhouse"
        staged_wheels.mkdir()
        install = []
        for name, (source, version, _metadata) in sorted(selected.items()):
            destination = staged_wheels / source.name
            shutil.copyfile(source, destination)
            if sha256(destination) != inventory[source.name]:
                raise ValueError("wheel changed during bundle assembly")
            install.append(f"{name}=={version} --hash=sha256:{inventory[source.name]}")
        (staged / "install.lock").write_text("\n".join(install) + "\n", encoding="utf-8")
        if python_archive is not None:
            unpacked = staged / "_python"
            with tarfile.open(python_archive, "r:gz") as archive:
                members = archive.getmembers()
                if len(members) > 50000 or sum(m.size for m in members) > 512 * 1024 * 1024:
                    raise ValueError("Python runtime archive exceeds the build limit")
                for member in members:
                    parts = Path(member.name).parts
                    if (not parts or parts[0] != "python" or ".." in parts
                            or not (member.isfile() or member.isdir())):
                        raise ValueError("unexpected Python runtime archive member")
                archive.extractall(unpacked, filter="data")
            shutil.move(str(unpacked / "python"), staged / "runtime")
            unpacked.rmdir()
            shutil.copyfile(HERE / "windows-python.json", staged / "python-provenance.json")
            shutil.copytree(HERE / "runtime-notices", staged / "notices" / "python")
        if model_dir is not None:
            destination = staged / "models" / "minilm"
            destination.mkdir(parents=True)
            for source in model_dir.iterdir():
                shutil.copyfile(source, destination / source.name)
            manifest_path = destination.parent / "minilm-manifest.json"
            shutil.copyfile(model_manifest, manifest_path)
            verify_model_bundle(destination, manifest_path)
            shutil.copyfile(HERE / "MODEL_LICENSE.txt", destination.parent / "MODEL_LICENSE.txt")
        notices_root = HERE / "wheel-notices"
        notices = json.loads((notices_root / "SOURCES.json").read_text(encoding="utf-8"))
        included = [record for record in notices if record["component"] in selected]
        if included:
            notices_destination = staged / "notices" / "wheels"
            notices_destination.mkdir(parents=True, exist_ok=True)
            for record in included:
                shutil.copyfile(regular(notices_root / record["file"]), notices_destination / record["file"])
            (notices_destination / "SOURCES.json").write_text(
                json.dumps(included, indent=2) + "\n", encoding="utf-8")
        if ocr_dir is not None:
            # The runtime verifier validates every payload file and rejects
            # links and unlisted additions. Copy only its admitted inventory.
            destination = staged / "ocr"
            destination.mkdir()
            for name in (*ocr_manifest["files"], "ocr-manifest.json"):
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(regular(ocr_dir / name), target)
            verify_ocr_bundle(destination)
            if ocr_source_archive is not None:
                source_destination = staged / "ocr-corresponding-source.tar"
                shutil.copyfile(ocr_source_archive, source_destination)
                if sha256(source_destination) != source_identity["sha256"]:
                    raise ValueError("OCR source archive changed during bundle assembly")
        for name in ("install_windows.py", "Install.cmd", "ResearchLibrary.cmd", "CheckInstallation.cmd"):
            shutil.copyfile(regular(HERE / name), staged / name)
        shutil.copyfile(HERE.parent / "docs" / "RUNNING.md", staged / "README.txt")
        shutil.copyfile(HERE.parent / "docs" / "START_UK.txt", staged / "START_UK.txt")
        # Ship the guides referenced by the installed README, including the
        # exact advanced-query syntax and complete-backup behavior.
        guides = staged / "docs"
        guides.mkdir()
        for name in ("ACADEMIC_SEARCH.md", "ADVANCED_SEARCH.md", "ACADEMIC_LIBRARY.md",
                     "ACADEMIC_WORKFLOWS.md", "CATALOG_TOOLS.md", "READING_DESK.md",
                     "RESEARCH_NOTEBOOK.md", "EPUB_READING.md", "IMPORT_CENTER.md",
                     "TRANSCRIPTION.md"):
            shutil.copyfile(regular(HERE.parent / "docs" / name), guides / name)
        if sbom is not None:
            (staged / "runtime-sbom.cdx.json").write_bytes(sbom_bytes)
        if native_evidence is not None:
            # Only verified snapshots enter the seal, never a later pathname read.
            (staged / "ResearchLibrary.exe").write_bytes(native_evidence["executable_bytes"])
            (staged / "native-launcher-build.json").write_bytes(native_evidence["proof_bytes"])
        files = {p.relative_to(staged).as_posix(): sha256(p)
                 for p in sorted(staged.rglob("*")) if p.is_file()}
        manifest = {"schema": 1, "target": TARGET, "bundled_python": python_archive is not None,
                    "bundled_model": model_dir is not None,
                    "bundled_ocr": ocr_dir is not None,
                    "files": files}
        if source_commit is not None:
            manifest["source_commit"] = source_commit
            manifest["source_tree"] = source_evidence["source_tree"]
        if native_evidence is not None:
            manifest["bundled_native_launcher"] = True
            manifest["native_launcher_sha256"] = native_launcher_sha256
            manifest["native_launcher_source_commit"] = source_commit
        (staged / "bundle-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # Reserve a fresh destination; never overwrite an existing installation.
        output.mkdir(exist_ok=False)
        for item in staged.iterdir():
            shutil.move(str(item), output / item.name)
    return {"target": TARGET, "wheels": len(selected), "output": str(output),
            "manifest_sha256": sha256(output / "bundle-manifest.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python-archive", type=Path,
                        help="Optional pinned Windows CPython archive from windows-python.json")
    parser.add_argument("--model-dir", type=Path, help="Optional approved MiniLM model directory")
    parser.add_argument("--model-manifest", type=Path, help="Approved MiniLM manifest")
    parser.add_argument("--sbom", type=Path, help="uv-generated runtime CycloneDX 1.5 inventory")
    parser.add_argument("--source-commit",
                        help="Full local Git commit; verify the application wheel against its committed package files")
    parser.add_argument("--ocr-dir", type=Path,
                        help="Verified portable Windows Tesseract runtime and language models")
    parser.add_argument("--ocr-source-archive", type=Path,
                        help="Corresponding-source archive pinned by the OCR runtime manifest")
    parser.add_argument("--native-launcher", type=Path,
                        help="Private executable matching the fixed reviewed Windows launcher pin")
    parser.add_argument("--native-launcher-proof", type=Path,
                        help="Producer build-evidence.json for the exact executable")
    parser.add_argument("--native-launcher-sha256",
                        help="Explicit SHA256; must equal the fixed reviewed launcher pin")
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.wheel, args.wheelhouse, args.lock, args.output,
                                  args.python_archive, args.model_dir, args.model_manifest,
                                  args.sbom, args.source_commit, args.ocr_dir, args.ocr_source_archive,
                                  args.native_launcher, args.native_launcher_proof, args.native_launcher_sha256)))


if __name__ == "__main__":
    main()
