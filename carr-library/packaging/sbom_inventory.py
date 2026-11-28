"""Strict dependency and declared-data inventory for the Windows bundle.

A CycloneDX file is not a Python distribution. Only the reviewed OCR model is
admitted as a file component, and its bytes are verified without importing it.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import stat
from zipfile import ZipFile

MODEL_NAME = "py3langid/data/model.npz.xz"
MANIFEST = Path(__file__).resolve().parents[1] / "src/carr_library/extract/ocr-language-detector.json"
QUALIFICATION = ("Packaged model identity verified; model confidence is not OCR accuracy "
                 "or language-quality qualification.")
MAX_SBOM_BYTES = 1024 * 1024


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate runtime SBOM JSON key")
        result[key] = value
    return result


def _json(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_SBOM_BYTES:
        raise ValueError("runtime SBOM exceeds its byte bound")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid runtime SBOM JSON") from exc
    if type(result) is not dict:
        raise ValueError("invalid runtime SBOM object")
    return result


def _normalize(name):
    if type(name) is not str or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", name) is None:
        raise ValueError("invalid runtime dependency name")
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(pins):
    if type(pins) is not dict or not pins:
        raise ValueError("empty runtime dependency lock")
    result = {}
    for name, version in pins.items():
        name = _normalize(name)
        if name in result:
            raise ValueError("duplicate normalized dependency pin")
        if type(version) is not str or not version or re.search(r"\s", version):
            raise ValueError("invalid runtime dependency version")
        result[name] = version
    return result


def _model_manifest():
    raw = MANIFEST.read_bytes()
    value = _json(raw)
    try:
        record = value["files"][MODEL_NAME]
        wheel = value["wheel"]
        valid = (
            value["schema"] == 1 and value["package"] == "py3langid"
            and value["version"] == "0.4.0" and value["license"] == "BSD-3-Clause"
            and type(record["size"]) is int and 0 < record["size"] <= 8 * 1024 * 1024
            and re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            and re.fullmatch(r"[0-9a-f]{40}", record["git_blob"])
            and type(wheel["size"]) is int and 0 < wheel["size"] <= 8 * 1024 * 1024
            and re.fullmatch(r"[0-9a-f]{64}", wheel["sha256"])
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError("invalid reviewed OCR model manifest")
    return value


def _model_component(manifest):
    record = manifest["files"][MODEL_NAME]
    return {
        "type": "file",
        "bom-ref": "carr-ocr-py3langid-model@" + manifest["version"],
        "name": MODEL_NAME,
        "version": manifest["version"],
        "hashes": [{"alg": "SHA-256", "content": record["sha256"]}],
        "licenses": [{"license": {"id": manifest["license"]}}],
        "properties": [
            {"name": "carr:upstream-git-blob", "value": record["git_blob"]},
            {"name": "carr:size", "value": str(record["size"])},
            {"name": "carr:qualification", "value": QUALIFICATION},
        ],
    }


def validate(raw, pins):
    """Return the exact typed inventory; unknown or missing components refuse."""
    pins = _pins(pins)
    bom = _json(raw)
    if bom.get("bomFormat") != "CycloneDX" or bom.get("specVersion") != "1.5":
        raise ValueError("runtime SBOM does not match the dependency lock")
    components = bom.get("components")
    if type(components) is not list or len(components) > len(pins) + 1:
        raise ValueError("duplicate or extra runtime SBOM component")
    manifest = _model_manifest() if "py3langid" in pins else None
    expected_model = _model_component(manifest) if manifest is not None else None
    if manifest is not None and pins["py3langid"] != manifest["version"]:
        raise ValueError("runtime SBOM model version does not match the dependency lock")
    libraries, files, refs = {}, [], set()
    for item in components:
        if type(item) is not dict:
            raise ValueError("invalid runtime SBOM component")
        if "bom-ref" in item:
            ref = item["bom-ref"]
            if type(ref) is not str or not ref:
                raise ValueError("invalid runtime SBOM reference")
            if ref in refs:
                raise ValueError("duplicate runtime SBOM reference")
            refs.add(ref)
        if item.get("type") == "library":
            name = _normalize(item.get("name"))
            if name in libraries:
                raise ValueError("duplicate runtime SBOM library")
            version = item.get("version")
            if type(version) is not str:
                raise ValueError("invalid runtime SBOM library version")
            libraries[name] = version
        elif item.get("type") == "file":
            if expected_model is None or item != expected_model or files:
                raise ValueError("unapproved, changed or duplicate runtime SBOM file")
            files.append(item)
        else:
            raise ValueError("unknown runtime SBOM component type")
    if libraries != pins:
        raise ValueError("runtime SBOM does not match the dependency lock")
    if bool(files) != (manifest is not None):
        raise ValueError("runtime SBOM is missing its reviewed OCR model")
    return {"bom": bom, "libraries": libraries, "files": files, "model": manifest}


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("expected a regular runtime model file")
    return info


def _hash_stream(stream, size, expected):
    digest, total = hashlib.sha256(), 0
    while block := stream.read(min(1024 * 1024, size - total + 1)):
        total += len(block)
        if total > size:
            raise ValueError("runtime model size differs from its reviewed identity")
        digest.update(block)
    if total != size or digest.hexdigest() != expected:
        raise ValueError("runtime model checksum differs from its reviewed identity")


def _verify_file(path, size, expected):
    path = Path(path)
    before = _regular(path)
    if before.st_size != size:
        raise ValueError("runtime model size differs from its reviewed identity")
    with path.open("rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("runtime model changed before verification")
        _hash_stream(stream, size, expected)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("runtime model changed during verification")
    if _stamp(_regular(path)) != _stamp(before):
        raise ValueError("runtime model changed during verification")


def verify_wheel(inventory, wheel):
    """Match both the reviewed distribution archive and its declared model."""
    manifest = inventory["model"]
    if manifest is None:
        return
    wheel_record = manifest["wheel"]
    _verify_file(wheel, wheel_record["size"], wheel_record["sha256"])
    record = manifest["files"][MODEL_NAME]
    with ZipFile(wheel) as archive:
        entries = [item for item in archive.infolist() if item.filename == MODEL_NAME]
        if len(entries) != 1:
            raise ValueError("missing or duplicate runtime model in its wheel")
        item = entries[0]
        if item.is_dir() or item.file_size != record["size"] or item.flag_bits & 1:
            raise ValueError("invalid runtime model wheel entry")
        mode = item.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise ValueError("runtime model wheel entry is not a regular file")
        with archive.open(item) as stream:
            _hash_stream(stream, record["size"], record["sha256"])


def verify_installed(raw, pins):
    """Verify installed versions and model bytes without importing package code."""
    inventory = validate(raw, pins)
    actual = {name: metadata.version(name) for name in pins}
    if actual != pins:
        raise ValueError("installed dependencies do not match the dependency lock")
    manifest = inventory["model"]
    if manifest is not None:
        distribution = metadata.distribution("py3langid")
        if distribution.version != manifest["version"]:
            raise ValueError("installed runtime model distribution version differs")
        record = manifest["files"][MODEL_NAME]
        _verify_file(distribution.locate_file(MODEL_NAME), record["size"], record["sha256"])
    return actual
