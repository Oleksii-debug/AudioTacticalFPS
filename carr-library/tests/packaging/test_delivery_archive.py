"""Real synthetic ZIP/filesystem checks for the private delivery boundary."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import tarfile
import warnings
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("carr_create_delivery_test", ROOT / "packaging/create_delivery.py")
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)
COMMIT = "a" * 40


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "sealed Ω bundle"
    root.mkdir()
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w") as archive:
        info = tarfile.TarInfo("source/README.txt")
        payload = b"Synthetic corresponding sources only.\n"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    files = {
        "install.lock": b"fixture==1.0 --hash=sha256:" + b"0" * 64 + b"\n",
        "install_windows.py": b"raise AssertionError('SUPPLIED BUNDLE CODE MUST NEVER EXECUTE')\n",
        "Install.cmd": b"synthetic installer payload\n",
        "ResearchLibrary.cmd": b"synthetic application payload\n",
        "CheckInstallation.cmd": b"synthetic check payload\n",
        "README.txt": b"Synthetic sealed bundle.\n",
        "wheelhouse/universal_research_library-1.0-py3-none-any.whl": b"synthetic wheel bytes",
        "ocr/tesseract.exe": b"synthetic non-executable bytes",
        "ocr/tessdata/eng.traineddata": b"synthetic model bytes",
        "ocr/ocr-manifest.json": b'{"synthetic": true}\n',
        delivery.SOURCES: source.getvalue(),
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {"schema": 1, "source_commit": COMMIT, "target": "Windows x64 / CPython 3.11",
                "bundled_python": False, "bundled_model": False, "bundled_native_runtime": False,
                "bundled_ocr": True, "files": {name: sha(data) for name, data in files.items()}}
    (root / delivery.MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return root


def create(bundle, tmp_path, **kwargs):
    return delivery.create_delivery(bundle, COMMIT, tmp_path / "delivery", **kwargs)


def paths(report):
    output = Path(report["output"])
    return tuple(output / report["archives"][kind]["path"] for kind in ("application", "sources"))


def companion(bundle, path, *, entries=None):
    entries = entries if entries is not None else [(delivery.PREFIX + delivery.SOURCES, (bundle / delivery.SOURCES).read_bytes())]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return path


def no_staging(tmp_path):
    assert not list(tmp_path.glob(".carr-delivery-*"))


def test_real_two_archive_roundtrip_preserves_exact_manifest_and_trusted_install(bundle, tmp_path):
    original = (bundle / delivery.MANIFEST).read_bytes()
    report = create(bundle, tmp_path)
    app, source = paths(report)
    extracted = tmp_path / "fresh extraction Ω"
    with zipfile.ZipFile(app) as archive:
        assert delivery.PREFIX + delivery.SOURCES not in archive.namelist()
        assert archive.read(delivery.PREFIX + delivery.MANIFEST) == original
        archive.extractall(extracted)
    with pytest.raises(FileNotFoundError):
        delivery._trusted_verify(extracted / "Universal-Research-Library")
    with zipfile.ZipFile(source) as archive:
        assert archive.namelist() == [delivery.PREFIX + delivery.SOURCES]
        archive.extractall(extracted)
    delivery._trusted_verify(extracted / "Universal-Research-Library")
    manifest = json.loads(original)
    assert set(p.relative_to(extracted / "Universal-Research-Library").as_posix()
               for p in (extracted / "Universal-Research-Library").rglob("*") if p.is_file()) == {*manifest["files"], delivery.MANIFEST}
    for archive in (app, source):
        assert archive.stat().st_size == next(r["bytes"] for r in report["archives"].values() if r["path"] == archive.name)
    assert report["manifest_sha256"] == sha(original)
    assert report["schema"] == 1
    assert report["source_commit"] == COMMIT
    assert report["boundaries"]["both_archives_required"] is True
    assert report["boundaries"]["native_execution"] == "NOT_RUN"
    saved = json.loads((Path(report["output"]) / "delivery-verification.json").read_text())
    assert saved == {key: value for key, value in report.items() if key != "output"}
    instructions = (Path(report["output"]) / "START_HERE_UK.txt").read_text(encoding="utf-8")
    assert "ОБИДВА" in instructions and "ОДНУ Й ТУ САМУ НОВУ" in instructions
    assert app.name in instructions and source.name in instructions
    assert "Запускайте програму через ResearchLibrary.cmd" in instructions
    assert "ResearchLibrary.exe" not in instructions
    no_staging(tmp_path)


def test_sealed_native_entry_is_delivered_and_named_in_start_instructions(bundle, tmp_path):
    # Explicit synthetic bytes test archive/instruction selection, never native execution.
    payload = b"Synthetic sealed native entry, never executed."
    (bundle / "ResearchLibrary.exe").write_bytes(payload)
    path = bundle / delivery.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["files"]["ResearchLibrary.exe"] = sha(payload)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    report = create(bundle, tmp_path)
    app, _source = paths(report)
    with zipfile.ZipFile(app) as archive:
        assert archive.read(delivery.PREFIX + "ResearchLibrary.exe") == payload
    instructions = (Path(report["output"]) / "START_HERE_UK.txt").read_text(encoding="utf-8")
    assert "Запускайте програму через ResearchLibrary.exe" in instructions
    assert "виконає локальне встановлення" in instructions
    assert "не має цифрового підпису" in instructions
    assert "run ResearchLibrary.exe" in report["instructions"]
    assert report["boundaries"]["native_execution"] == "NOT_RUN"


def test_unlisted_executable_does_not_change_the_reviewed_delivery_entry(bundle, tmp_path):
    (bundle / "ResearchLibrary.exe").write_bytes(b"unlisted neighboring file")
    report = create(bundle, tmp_path)
    app, _source = paths(report)
    with zipfile.ZipFile(app) as archive:
        assert delivery.PREFIX + "ResearchLibrary.exe" not in archive.namelist()
    instructions = (Path(report["output"]) / "START_HERE_UK.txt").read_text(encoding="utf-8")
    assert "Запускайте програму через ResearchLibrary.cmd" in instructions
    assert "ResearchLibrary.exe" not in instructions


def test_unlisted_private_environments_and_tests_never_enter_archives(bundle, tmp_path):
    for name in (".venv/private.txt", "tests/private.txt", "research.sqlite3", "private-notes.txt"):
        path = bundle / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("private data must remain outside the delivery")
    report = create(bundle, tmp_path)
    for path in paths(report):
        with zipfile.ZipFile(path) as archive:
            assert not any("private" in name or ".venv" in name or "/tests/" in name or ".sqlite3" in name
                           for name in archive.namelist())
    assert (bundle / "private-notes.txt").read_text() == "private data must remain outside the delivery"


def test_archive_bytes_reproducible_and_input_bundle_unchanged(bundle, tmp_path):
    before = {p.relative_to(bundle).as_posix(): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    first = delivery.create_delivery(bundle, COMMIT, tmp_path / "first")
    second = delivery.create_delivery(bundle, COMMIT, tmp_path / "second")
    for one, two in zip(paths(first), paths(second)):
        assert one.read_bytes() == two.read_bytes()
    assert before == {p.relative_to(bundle).as_posix(): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


@pytest.mark.parametrize("expected", ["b" * 40, "A" * 40, "a" * 12, "", "../outside"])
def test_wrong_or_invalid_source_rejected_before_publication(bundle, tmp_path, expected):
    with pytest.raises(ValueError, match="source commit"):
        delivery.create_delivery(bundle, expected, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_tampered_sealed_payload_rejected_by_trusted_verifier(bundle, tmp_path):
    (bundle / "ResearchLibrary.cmd").write_text("changed")
    with pytest.raises(ValueError, match="checksum"):
        create(bundle, tmp_path)
    no_staging(tmp_path)


def test_missing_source_tar_cannot_be_replaced_by_external_companion(bundle, tmp_path):
    existing = companion(bundle, tmp_path / "sources.zip")
    (bundle / delivery.SOURCES).unlink()
    with pytest.raises(FileNotFoundError):
        create(bundle, tmp_path, source_companion=existing)
    assert existing.exists()


@pytest.mark.parametrize("names", [
    ["../private.txt"], ["/absolute.txt"], ["a//b"], ["a/./b"], ["NUL.txt"],
    ["a.txt."], ["a:stream"], ["A.txt", "a.txt"], ["Folder/a", "folder/b"],
    ["prefix", "PREFIX/child"], ["café.txt", "cafe\u0301.txt"], ["bundle-manifest.json"],
])
def test_ambiguous_manifest_paths_rejected_before_access(bundle, tmp_path, names):
    path = bundle / delivery.MANIFEST
    data = json.loads(path.read_text())
    data["files"].update({name: "0" * 64 for name in names})
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        create(bundle, tmp_path)
    no_staging(tmp_path)


def test_duplicate_manifest_keys_rejected(bundle, tmp_path):
    path = bundle / delivery.MANIFEST
    path.write_text(path.read_text().replace('"schema": 1,', '"schema": 1, "schema": 1,'))
    with pytest.raises(ValueError, match="Duplicate manifest"):
        create(bundle, tmp_path)


def test_existing_single_entry_companion_reused_without_copying_or_modification(bundle, tmp_path):
    existing = companion(bundle, tmp_path / "previous source Ω.zip")
    before, stamp = existing.read_bytes(), existing.stat()
    report = create(bundle, tmp_path, source_companion=existing)
    assert report["archives"]["sources"]["reused"] is True
    assert report["archives"]["sources"]["path"] == str(existing)
    assert report["archives"]["sources"]["sha256"] == sha(before)
    assert len(list(Path(report["output"]).glob("*.zip"))) == 1
    assert existing.read_bytes() == before
    assert existing.stat().st_mtime_ns == stamp.st_mtime_ns
    assert "попередньому місці" in (Path(report["output"]) / "START_HERE_UK.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["wrong", "missing", "extra", "duplicate", "case", "traversal", "prefix", "link", "encrypted"])
def test_invalid_source_companion_is_rejected_and_preserved(bundle, tmp_path, kind):
    name, payload = delivery.PREFIX + delivery.SOURCES, (bundle / delivery.SOURCES).read_bytes()
    entries = [(name, payload)]
    if kind == "wrong": entries = [(name, b"wrong source payload")]
    if kind == "missing": entries = []
    if kind == "extra": entries.append((delivery.PREFIX + "private.txt", b"private"))
    if kind == "duplicate": entries.append((name, payload))
    if kind == "case": entries.append((name.upper(), payload))
    if kind == "traversal": entries = [(delivery.PREFIX + "../" + delivery.SOURCES, payload)]
    if kind == "prefix": entries = [("other-root/" + delivery.SOURCES, payload)]
    if kind == "link":
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        entries = [(info, payload)]
    existing = companion(bundle, tmp_path / "existing.zip", entries=entries)
    if kind == "encrypted":
        raw = bytearray(existing.read_bytes())
        struct.pack_into("<H", raw, raw.index(b"PK\x03\x04") + 6, 1)
        struct.pack_into("<H", raw, raw.index(b"PK\x01\x02") + 8, 1)
        existing.write_bytes(raw)
    before = existing.read_bytes()
    with pytest.raises(ValueError):
        create(bundle, tmp_path, source_companion=existing)
    assert existing.read_bytes() == before
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_oversized_zip_directory_rejected_before_zipfile_allocation(bundle, tmp_path, monkeypatch):
    existing = companion(bundle, tmp_path / "existing.zip")
    raw = bytearray(existing.read_bytes())
    struct.pack_into("<I", raw, raw.rindex(b"PK\x05\x06") + 12, 64 * delivery.CHUNK)
    existing.write_bytes(raw)
    def forbidden(*args, **kwargs):
        raise AssertionError("Must reject central-directory bound before ZipFile allocates it")
    monkeypatch.setattr(delivery.zipfile, "ZipFile", forbidden)
    with pytest.raises(ValueError, match="oversized"):
        create(bundle, tmp_path, source_companion=existing)


def test_source_changed_after_trusted_check_rejected_during_streaming(bundle, tmp_path, monkeypatch):
    original = delivery._trusted_verify
    def changed(root, *, manifest_bytes=None):
        original(root, manifest_bytes=manifest_bytes)
        (root / "README.txt").write_bytes(b"replaced after verification")
    monkeypatch.setattr(delivery, "_trusted_verify", changed)
    with pytest.raises(ValueError, match="changed during archiving"):
        create(bundle, tmp_path)
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_manifest_changed_after_trusted_check_rejected(bundle, tmp_path, monkeypatch):
    original = delivery._trusted_verify
    def changed(root, *, manifest_bytes=None):
        original(root, manifest_bytes=manifest_bytes)
        path = root / delivery.MANIFEST
        path.write_bytes(path.read_bytes() + b" ")
    monkeypatch.setattr(delivery, "_trusted_verify", changed)
    with pytest.raises(ValueError, match="manifest changed"):
        create(bundle, tmp_path)
    no_staging(tmp_path)


def test_manifest_aba_cannot_make_installer_validate_a_different_inventory(bundle, tmp_path, monkeypatch):
    path = bundle / delivery.MANIFEST
    complete = path.read_bytes()
    incomplete = json.loads(complete)
    incomplete["files"] = {delivery.SOURCES: incomplete["files"][delivery.SOURCES]}
    captured = (json.dumps(incomplete) + "\n").encode()
    path.write_bytes(captured)
    original = delivery._trusted_verify
    def changed_only_during_installer(root, *, manifest_bytes=None):
        path.write_bytes(complete)
        try:
            # This is the real installer verifier, not a mocked success. The
            # bytes given to it must be the incomplete captured inventory.
            original(root, manifest_bytes=manifest_bytes)
        finally:
            path.write_bytes(captured)
    monkeypatch.setattr(delivery, "_trusted_verify", changed_only_during_installer)
    with pytest.raises(ValueError, match="incomplete installation bundle"):
        create(bundle, tmp_path)
    assert path.read_bytes() == captured
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_trusted_verifier_snapshot_preserves_default_and_supplied_byte_checks(bundle):
    raw = (bundle / delivery.MANIFEST).read_bytes()
    delivery._trusted_verify(bundle)
    delivery._trusted_verify(bundle, manifest_bytes=raw)
    changed = json.loads(raw)
    changed["files"]["README.txt"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        delivery._trusted_verify(bundle, manifest_bytes=json.dumps(changed).encode())
    delivery._trusted_verify(bundle)


@pytest.mark.parametrize("kind", ["prefix", "trailer", "archive_comment", "entry_comment", "extra_field", "gap", "orphan_local_record", "compressed_trailer"])
def test_reused_companion_rejects_unsealed_container_bytes(bundle, tmp_path, kind):
    existing = companion(bundle, tmp_path / "existing.zip")
    hidden = b"PRIVATE UNSEALED BYTES"
    if kind in ("archive_comment", "entry_comment", "extra_field"):
        info = zipfile.ZipInfo(delivery.PREFIX + delivery.SOURCES)
        info.compress_type = zipfile.ZIP_DEFLATED
        if kind == "entry_comment": info.comment = hidden
        if kind == "extra_field": info.extra = struct.pack("<HH", 0xCAFE, len(hidden)) + hidden
        with zipfile.ZipFile(existing, "w", zipfile.ZIP_DEFLATED) as archive:
            if kind == "archive_comment": archive.comment = hidden
            archive.writestr(info, (bundle / delivery.SOURCES).read_bytes())
    else:
        raw = bytearray(existing.read_bytes())
        end = raw.rindex(b"PK\x05\x06")
        central = struct.unpack_from("<I", raw, end + 16)[0]
        if kind == "prefix": raw = hidden + raw
        elif kind == "trailer": raw += hidden
        else:
            if kind == "orphan_local_record":
                orphan = io.BytesIO()
                with zipfile.ZipFile(orphan, "w") as archive:
                    archive.writestr("private.txt", hidden)
                orphan_raw = orphan.getvalue()
                hidden = orphan_raw[:orphan_raw.index(b"PK\x01\x02")]
            raw[central:central] = hidden
            struct.pack_into("<I", raw, end + len(hidden) + 16, central + len(hidden))
            if kind == "compressed_trailer":
                size = struct.unpack_from("<I", raw, 18)[0]
                struct.pack_into("<I", raw, 18, size + len(hidden))
                struct.pack_into("<I", raw, central + len(hidden) + 20, size + len(hidden))
        existing.write_bytes(raw)
    before = existing.read_bytes()
    # These archives still expose the one correct payload to the usual ZIP
    # reader: central-directory enumeration and payload hashes alone miss it.
    with zipfile.ZipFile(existing) as archive:
        assert archive.namelist() == [delivery.PREFIX + delivery.SOURCES]
        assert archive.read(archive.namelist()[0]) == (bundle / delivery.SOURCES).read_bytes()
    with pytest.raises(ValueError, match="envelope|unlisted compressed"):
        create(bundle, tmp_path, source_companion=existing)
    assert existing.read_bytes() == before
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_fresh_helper_zip64_source_companion_can_be_reused(bundle, tmp_path):
    first = delivery.create_delivery(bundle, COMMIT, tmp_path / "first")
    _app, existing = paths(first)
    original = existing.read_bytes()
    second = delivery.create_delivery(bundle, COMMIT, tmp_path / "second", source_companion=existing)
    assert second["archives"]["sources"]["reused"] is True
    assert existing.read_bytes() == original


def test_reused_companion_replaced_during_creation_is_not_published(bundle, tmp_path, monkeypatch):
    existing = companion(bundle, tmp_path / "sources.zip")
    original = delivery._write_archive
    def changed(*args, **kwargs):
        original(*args, **kwargs)
        companion(bundle, existing, entries=[(delivery.PREFIX + delivery.SOURCES, b"changed")])
    monkeypatch.setattr(delivery, "_write_archive", changed)
    with pytest.raises(ValueError, match="checksum"):
        create(bundle, tmp_path, source_companion=existing)
    assert existing.exists()
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_cancel_before_work_preserves_all_inputs(bundle, tmp_path):
    with pytest.raises(InterruptedError):
        create(bundle, tmp_path, cancelled=lambda: True)
    assert bundle.exists()
    no_staging(tmp_path)


def test_cancel_during_payload_streaming_removes_only_staging(bundle, tmp_path, monkeypatch):
    cancelled = False
    original = delivery._stream
    class CancelAfterRead:
        def __init__(self, wrapped): self.wrapped = wrapped
        def read(self, size):
            nonlocal cancelled
            result = self.wrapped.read(size)
            cancelled = True
            return result
    def stream(source, callback, *, destination=None, limit=delivery.MAX_TOTAL):
        if destination is not None:
            source = CancelAfterRead(source)
        return original(source, callback, destination=destination, limit=limit)
    monkeypatch.setattr(delivery, "_stream", stream)
    with pytest.raises(InterruptedError):
        create(bundle, tmp_path, cancelled=lambda: cancelled)
    assert (bundle / delivery.SOURCES).exists()
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_cancel_after_union_verification_preserves_reused_companion(bundle, tmp_path, monkeypatch):
    existing = companion(bundle, tmp_path / "existing.zip")
    before = existing.read_bytes()
    cancelled = False
    def stop(_commit):
        nonlocal cancelled
        cancelled = True
        return None
    monkeypatch.setattr(delivery, "_tree", stop)
    with pytest.raises(InterruptedError):
        create(bundle, tmp_path, source_companion=existing, cancelled=lambda: cancelled)
    assert existing.read_bytes() == before
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


@pytest.mark.parametrize("sentinel", [False, True])
def test_existing_output_never_overwritten_even_when_empty(bundle, tmp_path, sentinel):
    out = tmp_path / "delivery"
    out.mkdir()
    before = out.stat()
    if sentinel: (out / "keep.txt").write_text("preserve")
    with pytest.raises(FileExistsError):
        create(bundle, tmp_path)
    assert out.stat().st_ino == before.st_ino
    if sentinel: assert (out / "keep.txt").read_text() == "preserve"
    no_staging(tmp_path)


@pytest.mark.parametrize("sentinel", [False, True])
def test_output_created_at_publication_race_is_preserved(bundle, tmp_path, monkeypatch, sentinel):
    original = delivery._publish
    def raced(stage, out, parent_fd):
        out.mkdir()
        if sentinel: (out / "keep.txt").write_text("another operation")
        original(stage, out, parent_fd)
    monkeypatch.setattr(delivery, "_publish", raced)
    with pytest.raises(OSError):
        create(bundle, tmp_path)
    assert (tmp_path / "delivery").is_dir()
    if sentinel: assert (tmp_path / "delivery/keep.txt").read_text() == "another operation"
    else: assert not list((tmp_path / "delivery").iterdir())
    no_staging(tmp_path)


def test_failed_atomic_publication_cleans_own_stage_and_keeps_companion(bundle, tmp_path, monkeypatch):
    existing = companion(bundle, tmp_path / "existing.zip")
    before = existing.read_bytes()
    def fail(*args): raise OSError("synthetic publication failure")
    monkeypatch.setattr(delivery, "_publish", fail)
    with pytest.raises(OSError, match="publication failure"):
        create(bundle, tmp_path, source_companion=existing)
    assert existing.read_bytes() == before
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_cleanup_never_deletes_a_replacement_staging_directory(bundle, tmp_path, monkeypatch):
    moved = tmp_path / "moved-own-staging"
    replacement = None
    def replace(stage, output, parent_fd):
        nonlocal replacement
        stage.rename(moved)
        stage.mkdir()
        (stage / "foreign.txt").write_text("not owned by this operation")
        replacement = stage
        raise OSError("replacement race")
    monkeypatch.setattr(delivery, "_publish", replace)
    with pytest.raises(OSError, match="replacement race"):
        create(bundle, tmp_path)
    assert (replacement / "foreign.txt").read_text() == "not owned by this operation"
    assert moved.is_dir()
    assert not (tmp_path / "delivery").exists()


def test_tampered_output_after_union_verification_is_rejected(bundle, tmp_path, monkeypatch):
    def tamper(_commit):
        stage = next(tmp_path.glob(".carr-delivery-*"))
        app = next(stage.glob("*Application*.zip"))
        with app.open("ab") as stream: stream.write(b"changed after validation")
        return None
    monkeypatch.setattr(delivery, "_tree", tamper)
    with pytest.raises(ValueError, match="changed before publication"):
        create(bundle, tmp_path)
    assert not (tmp_path / "delivery").exists()
    no_staging(tmp_path)


def test_output_inside_input_bundle_rejected(bundle, tmp_path):
    with pytest.raises(ValueError, match="outside"):
        delivery.create_delivery(bundle, COMMIT, bundle / "delivery")


def test_no_giant_payload_reads(bundle, tmp_path, monkeypatch):
    original = delivery._stream
    reads = []
    class BoundedReader:
        def __init__(self, wrapped): self.wrapped = wrapped
        def read(self, size):
            assert 0 < size <= delivery.CHUNK
            reads.append(size)
            return self.wrapped.read(size)
    def stream(source, callback, *, destination=None, limit=delivery.MAX_TOTAL):
        return original(BoundedReader(source), callback, destination=destination, limit=limit)
    monkeypatch.setattr(delivery, "_stream", stream)
    create(bundle, tmp_path)
    assert reads
