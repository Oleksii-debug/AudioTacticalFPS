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
    return verifier.verify_application_wheel(wheel, HERE.parent.parent, source_commit)


def build_bundle(wheel, wheelhouse, lock, output, python_archive=None,
                 model_dir=None, model_manifest=None, sbom=None, source_commit=None, ocr_dir=None,
                 ocr_source_archive=None):
    wheel, wheelhouse, lock, output = map(Path, (wheel, wheelhouse, lock, output))
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory")
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be a full lowercase Git SHA")
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
        shutil.copyfile(HERE.parent / "docs" / "TRANSCRIPTION.md", staged / "TRANSCRIPTION.txt")
        if sbom is not None:
            (staged / "runtime-sbom.cdx.json").write_bytes(sbom_bytes)
        files = {p.relative_to(staged).as_posix(): sha256(p)
                 for p in sorted(staged.rglob("*")) if p.is_file()}
        manifest = {"schema": 1, "target": TARGET, "bundled_python": python_archive is not None,
                    "bundled_model": model_dir is not None,
                    "bundled_ocr": ocr_dir is not None,
                    "files": files}
        if source_commit is not None:
            manifest["source_commit"] = source_commit
            manifest["source_tree"] = source_evidence["source_tree"]
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
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.wheel, args.wheelhouse, args.lock, args.output,
                                  args.python_archive, args.model_dir, args.model_manifest,
                                  args.sbom, args.source_commit, args.ocr_dir, args.ocr_source_archive)))


if __name__ == "__main__":
    main()
