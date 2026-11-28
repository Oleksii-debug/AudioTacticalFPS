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


def launcher_bytes(path, expected_sha256):
    """Capture the explicitly selected unsigned launcher without executing it."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("native launcher requires its expected lowercase SHA256")
    path = regular(Path(path))
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("native launcher exceeds its size budget")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("native launcher checksum mismatch")
    return raw


def inspect_launcher(path):
    # Parse the copied bytes with the existing trusted PE probe. Never load or
    # execute the supplied executable during bundle assembly.
    spec = importlib.util.spec_from_file_location(
        "carr_bundle_launcher_pe", HERE / "native-launcher" / "probe_pe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    try:
        return probe.inspect_pe(path)
    except (ValueError, probe.struct.error, IndexError, UnicodeDecodeError) as exc:
        raise ValueError("invalid unsigned native launcher PE: " + str(exc)) from exc


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


def verify_portable_entry_sources(wheel):
    """Bind the opt-in owner contract to the exact wheel and reviewed native build."""
    import ast
    package = HERE.parent / "src" / "carr_library"
    entry = regular(package / "portable_entry.py").read_bytes()
    syntax = ast.parse(entry)
    declared = [node.value for node in syntax.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "OWNED_SOURCES"
                        for target in node.targets)]
    if len(declared) != 1:
        raise ValueError("portable ownership source inventory is ambiguous")
    sources = ast.literal_eval(declared[0])
    if (type(sources) is not tuple or not 1 <= len(sources) <= 64
            or len(sources) != len(set(sources))
            or any(not isinstance(item, str) or not item.endswith(".py")
                   or item.startswith("/") or "\\" in item or ".." in item.split("/")
                   for item in sources)):
        raise ValueError("invalid portable ownership source inventory")
    with ZipFile(wheel) as archive:
        for relative in sources:
            current = regular(package / relative).read_bytes()
            name = "carr_library/" + relative
            names = [info for info in archive.infolist() if info.filename.casefold() == name.casefold()]
            if (len(names) != 1 or names[0].filename != name
                    or names[0].file_size != len(current) or archive.read(names[0]) != current):
                raise ValueError("portable ownership code differs from the exact application wheel")
    return tuple(sources)


def reviewed_portable_launcher(native_launcher, build_evidence, native_probe, source_commit):
    """Reuse the existing exact source/build/native-process evidence verifier."""
    spec = importlib.util.spec_from_file_location(
        "carr_portable_launcher_admission", HERE / "native-launcher" / "verify_launcher.py")
    if spec is None or spec.loader is None:
        raise ValueError("reviewed native launcher admission helper is unavailable")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier.verify_launcher(native_launcher, build_evidence, native_probe, source_commit)


VENDORED_NUM2WORDS_COMPONENT = json.loads(r'''{
  "type": "library",
  "bom-ref": "carr-vendored-num2words-sk@0.5.14",
  "name": "num2words (vendored Slovak runtime closure)",
  "version": "0.5.14",
  "licenses": [
    {
      "expression": "LGPL-2.1-or-later"
    }
  ],
  "externalReferences": [
    {
      "type": "vcs",
      "url": "https://github.com/savoirfairelinux/num2words/tree/6abe2c64714a38e41de23fe20fa319d0ad2fd845"
    }
  ],
  "properties": [
    {
      "name": "carr:scope",
      "value": "Unmodified lang_SK.py/base.py/utils.py/compat.py/currency.py; full COPYING and per-file Git identities in reader/_vendor_num2words/UPSTREAM.json; local inert initializer. No upstream eager initializer, CLI or docopt dependency."
    }
  ]
}''')
VENDORED_NUM2WORDS_FILES = {
    "__init__.py": "4b98330587fd681df4bfa61767ba22e12816d2e2",
    "UPSTREAM.json": "579a9b92808377a671e88d1c1c2f467468283f03",
    "lang_SK.py": "35ece024f13cf90999d3ca4954229f1d4994a4ba",
    "base.py": "243045ffc701ce2986f12f02101f2cbca92960a5",
    "utils.py": "b5e4611be5fafe19324a00594b2648d75bd17bd8",
    "compat.py": "321cd2e84e11335163afc5902b1b136196fee889",
    "currency.py": "4982251ec4f8a15c286dab82388154daf26b706d",
    "COPYING": "a7b7406f02f769167539316e5093cd58f609dce5"
}
VENDORED_NUM2WORDS_PREFIX = "carr_library/reader/_vendor_num2words/"
VENDORED_SOURCE_LIMIT = 512 * 1024


def _verify_vendored_sources(files):
    """Validate the exact reviewed source closure, without importing its code."""
    if not files:
        return False
    if set(files) != set(VENDORED_NUM2WORDS_FILES):
        raise ValueError("vendored num2words source inventory differs from its reviewed closure")
    for name, expected in VENDORED_NUM2WORDS_FILES.items():
        raw = files[name]
        if not isinstance(raw, bytes) or len(raw) > VENDORED_SOURCE_LIMIT:
            raise ValueError("vendored num2words source exceeds its byte boundary")
        identity = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw,
                                usedforsecurity=False).hexdigest()
        if identity != expected:
            raise ValueError("vendored num2words source identity differs: " + name)
    return True


def vendored_sources_from_wheel(wheel):
    """Bind reviewed vendor bytes to the same wheel digest later sealed by the caller."""
    wheel = regular(Path(wheel))
    digest = sha256(wheel)
    files = {}
    with ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name.casefold().startswith(VENDORED_NUM2WORDS_PREFIX.casefold()):
                continue
            if info.is_dir():
                if name != VENDORED_NUM2WORDS_PREFIX:
                    raise ValueError("unexpected vendored num2words archive directory")
                continue
            relative = name[len(VENDORED_NUM2WORDS_PREFIX):]
            if (name != VENDORED_NUM2WORDS_PREFIX + relative
                    or relative not in VENDORED_NUM2WORDS_FILES or relative in files
                    or not 0 <= info.file_size <= VENDORED_SOURCE_LIMIT):
                raise ValueError("unexpected or duplicate vendored num2words archive member")
            with archive.open(info) as stream:
                raw = stream.read(VENDORED_SOURCE_LIMIT + 1)
            if len(raw) != info.file_size:
                raise ValueError("vendored num2words archive member size differs")
            files[relative] = raw
    _verify_vendored_sources(files)
    if sha256(wheel) != digest:
        raise ValueError("application wheel changed during vendored source review")
    return files, digest


def vendored_sources_from_package(package_root):
    """Read the installed source/notice closure; compiled caches are not source components."""
    root = Path(package_root) / "reader" / "_vendor_num2words"
    if not root.exists() and not root.is_symlink():
        return {}
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or root.is_symlink()
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("expected the installed vendored num2words directory")
    files = {}
    for path in root.iterdir():
        if path.name == "__pycache__" and path.is_dir() and not path.is_symlink():
            continue
        if path.name not in VENDORED_NUM2WORDS_FILES:
            raise ValueError("unexpected installed vendored num2words source member")
        regular(path)
        with path.open("rb") as stream:
            files[path.name] = stream.read(VENDORED_SOURCE_LIMIT + 1)
    _verify_vendored_sources(files)
    return files


def runtime_sbom_components(bom, *, vendor_files):
    """Separate exact wheel distributions from the one reviewed embedded source component."""
    if (not isinstance(bom, dict) or bom.get("bomFormat") != "CycloneDX"
            or bom.get("specVersion") != "1.5" or not isinstance(bom.get("components"), list)):
        raise ValueError("runtime SBOM does not match the dependency lock format")
    vendor_present = _verify_vendored_sources(vendor_files)
    distributions, references, vendor_count = {}, set(), 0
    for component in bom["components"]:
        if not isinstance(component, dict):
            raise ValueError("invalid runtime SBOM component")
        reference = component.get("bom-ref")
        if "bom-ref" in component:
            if not isinstance(reference, str) or not reference or reference in references:
                raise ValueError("duplicate runtime SBOM component or reference")
            references.add(reference)
        if reference == VENDORED_NUM2WORDS_COMPONENT["bom-ref"]:
            if component != VENDORED_NUM2WORDS_COMPONENT:
                raise ValueError("vendored num2words SBOM identity or notice differs")
            vendor_count += 1
            continue
        name, version = component.get("name"), component.get("version")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
                or not isinstance(version, str) or not version):
            raise ValueError("invalid runtime SBOM distribution identity")
        name = canonicalize_name(name)
        if name in distributions:
            raise ValueError("duplicate runtime SBOM component or reference")
        distributions[name] = component
    if vendor_count != int(vendor_present):
        raise ValueError("runtime SBOM omits or invents the reviewed vendored num2words component")
    if vendor_present:
        metadata = bom.get("metadata")
        root = metadata.get("component") if isinstance(metadata, dict) else None
        root_ref = root.get("bom-ref") if isinstance(root, dict) else None
        dependencies = bom.get("dependencies")
        if not isinstance(root_ref, str) or not isinstance(dependencies, list):
            raise ValueError("vendored num2words SBOM dependency binding is missing")
        root_edges = [item for item in dependencies if isinstance(item, dict) and item.get("ref") == root_ref]
        vendor_edges = [item for item in dependencies if isinstance(item, dict)
                        and item.get("ref") == VENDORED_NUM2WORDS_COMPONENT["bom-ref"]]
        if (len(root_edges) != 1 or len(vendor_edges) != 1
                or not isinstance(root_edges[0].get("dependsOn"), list)
                or root_edges[0]["dependsOn"].count(VENDORED_NUM2WORDS_COMPONENT["bom-ref"]) != 1
                or vendor_edges[0].get("dependsOn", []) != []):
            raise ValueError("vendored num2words SBOM dependency binding differs")
    return distributions


def verify_runtime_sbom(bom, pins, *, vendor_files):
    components = runtime_sbom_components(bom, vendor_files=vendor_files)
    actual = {(name, item["version"]) for name, item in components.items()}
    expected = {(name, version) for name, (version, _hashes) in pins.items()}
    if actual != expected or len(components) != len(pins):
        raise ValueError("runtime SBOM does not match the dependency lock")
    return components


def build_bundle(wheel, wheelhouse, lock, output, python_archive=None,
                 model_dir=None, model_manifest=None, sbom=None, source_commit=None, ocr_dir=None,
                 ocr_source_archive=None, native_launcher=None, native_launcher_sha256=None,
                 *, portable_updates=False, native_launcher_build=None, native_launcher_probe=None):
    wheel, wheelhouse, lock, output = map(Path, (wheel, wheelhouse, lock, output))
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory")
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be a full lowercase Git SHA")
    if (native_launcher is None) != (native_launcher_sha256 is None):
        raise ValueError("supply both native launcher and its expected SHA256")
    native_bytes = (launcher_bytes(native_launcher, native_launcher_sha256)
                    if native_launcher is not None else None)
    if native_bytes is not None and (python_archive is None or source_commit is None):
        raise ValueError("native launcher requires bundled Python and an exact source commit")
    if type(portable_updates) is not bool:
        raise ValueError("portable_updates must be an explicit boolean")
    if portable_updates:
        if native_bytes is None or native_launcher_build is None or native_launcher_probe is None:
            raise ValueError("portable updates require the exact launcher and build/native-process evidence")
        verify_portable_entry_sources(wheel)
        reviewed = reviewed_portable_launcher(
            native_launcher, native_launcher_build, native_launcher_probe, source_commit)
        if reviewed != native_bytes:
            raise ValueError("native launcher changed between capture and portable admission")
    elif native_launcher_build is not None or native_launcher_probe is not None:
        raise ValueError("portable launcher evidence requires --portable-updates")
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("expected a dedicated wheel directory")
    pins = read_lock(lock)
    if sbom is not None:
        sbom = regular(Path(sbom))
        # Seal the very bytes whose dependency inventory was validated.
        # Do not re-open a mutable input later in the build.
        sbom_bytes = sbom.read_bytes()
        bom = json.loads(sbom_bytes.decode("utf-8"))
        vendor_files, sbom_wheel_sha256 = vendored_sources_from_wheel(wheel)
        verify_runtime_sbom(bom, pins, vendor_files=vendor_files)
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
    if sbom is not None and inventory[wheel.name] != sbom_wheel_sha256:
        raise ValueError("application wheel changed after vendored SBOM review")
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
        if native_bytes is not None:
            launcher = staged / "ResearchLibrary.exe"
            launcher.write_bytes(native_bytes)
            if inspect_launcher(launcher)["sha256"] != native_launcher_sha256:
                raise ValueError("native launcher changed during bundle assembly")
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
        # Setup helpers are source only. A user may explicitly acquire and
        # prepare the pinned runtime on their own machine; transfer archives
        # never include that runtime. Copy only this reviewed file inventory.
        shutil.copyfile(regular(HERE / "SetupLocalAudio.cmd"), staged / "SetupLocalAudio.cmd")
        for relative in (
            "setup_local_audio.py",
            "acquire_audio_runtime.py",
            "prepare_audio_runtime.py",
            "audio-runtime/README.txt",
            "audio-runtime/licenses/BUILD_RECIPES_LICENSE",
            "audio-runtime/licenses/COPYING.GPLv2",
            "audio-runtime/licenses/COPYING.GPLv3",
            "audio-runtime/licenses/COPYING.LGPLv2.1",
            "audio-runtime/licenses/COPYING.LGPLv3",
            "audio-runtime/licenses/LICENSE.md",
            "audio-runtime/provenance/build-configuration.txt",
            "audio-runtime/provenance/checksums.sha256",
            "audio-runtime/provenance/dependency-source-index.json",
            "audio-runtime/source-lock.json",
        ):
            target = staged / "tools" / "audio-setup" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(regular(HERE / relative), target)
        shutil.copyfile(HERE.parent / "docs" / "RUNNING.md", staged / "README.txt")
        shutil.copyfile(HERE.parent / "docs" / "START_UK.txt", staged / "START_UK.txt")
        shutil.copyfile(HERE.parent / "docs" / "START_V3_UK.txt", staged / "START_V3_UK.txt")
        if sbom is not None:
            (staged / "runtime-sbom.cdx.json").write_bytes(sbom_bytes)
        if source_evidence is not None:
            # Retain the verifier's exact commit/tree/wheel binding inside the
            # sealed payload, so installed acceptance can attest those same
            # bytes. Use deterministic LF bytes on both build platforms.
            (staged / "application-source.json").write_bytes(
                (json.dumps(source_evidence, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        if portable_updates:
            (staged / "portable-update.ini").write_bytes(
                b"[Resonance portable updates]\nProtocol=2\nOwner=windows-job-receipt-v1\n")
        files = {p.relative_to(staged).as_posix(): sha256(p)
                 for p in sorted(staged.rglob("*")) if p.is_file()}
        if native_bytes is not None and files["ResearchLibrary.exe"] != native_launcher_sha256:
            raise ValueError("native launcher changed before bundle sealing")
        manifest = {"schema": 1, "target": TARGET, "bundled_python": python_archive is not None,
                    "bundled_model": model_dir is not None,
                    "bundled_ocr": ocr_dir is not None,
                    "files": files}
        if portable_updates:
            manifest["bundled_launcher"] = True
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
    parser.add_argument("--native-launcher", type=Path,
                        help="Optional explicitly built unsigned ResearchLibrary.exe")
    parser.add_argument("--native-launcher-sha256",
                        help="Expected SHA256 from this launcher's build and native process evidence")
    parser.add_argument("--portable-updates", action="store_true",
                        help="Seal reviewed portable child ownership; requires matching launcher evidence")
    parser.add_argument("--native-launcher-build", type=Path)
    parser.add_argument("--native-launcher-probe", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.wheel, args.wheelhouse, args.lock, args.output,
                                  args.python_archive, args.model_dir, args.model_manifest,
                                  args.sbom, args.source_commit, args.ocr_dir, args.ocr_source_archive,
                                  args.native_launcher, args.native_launcher_sha256,
                                  portable_updates=args.portable_updates,
                                  native_launcher_build=args.native_launcher_build,
                                  native_launcher_probe=args.native_launcher_probe)))


if __name__ == "__main__":
    main()
