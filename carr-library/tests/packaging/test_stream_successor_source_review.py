"""Small source-only checks for the private streaming assembler.

All payloads are synthetic bytes. These checks do not build a release, execute
Windows software, load models, or claim native acceptance.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


ASSEMBLER = module("review_stream_successor", "build_v3_stream_successor.py")
DELIVERY = module("review_production_delivery", "create_delivery.py")


class StreamingSuccessorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.payloads = {
            "wheelhouse/previous.whl": b"synthetic previous wheel; never installed",
            "runtime/unchanged.dat": bytes(range(256)) * 1024,
            "docs/читання.txt": "Синтетична інструкція читання.\n".encode("utf-8"),
            DELIVERY.SOURCES: b"synthetic corresponding source; no executable content",
        }
        for name, raw in self.payloads.items():
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.previous = {
            "schema": 1, "source_commit": "a" * 40,
            "target": "Windows x64 / CPython 3.11", "bundled_ocr": True,
            "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in self.payloads.items()},
        }
        self.previous_raw = ASSEMBLER.json_bytes(self.previous)
        self.parent = self.root / "parent.zip"
        self.companion = self.root / "sources.zip"
        DELIVERY._write_archive(self.parent, self.bundle,
            (set(self.payloads) - {DELIVERY.SOURCES}) | {DELIVERY.MANIFEST},
            self.previous["files"], self.previous_raw, lambda: False)
        DELIVERY._write_archive(self.companion, self.bundle, {DELIVERY.SOURCES},
            self.previous["files"], self.previous_raw, lambda: False)
        DELIVERY.verify_archives(self.parent, self.companion, self.previous_raw, "a" * 40)
        self.parent_sha = ASSEMBLER.digest(self.parent)
        self.companion_sha = ASSEMBLER.digest(self.companion)

        self.replacements = {}
        self.files = dict(self.previous["files"])
        self.files.pop("wheelhouse/previous.whl")
        for name, raw in {
            "wheelhouse/current.whl": b"synthetic current wheel; never installed",
            "application-source.json": ASSEMBLER.json_bytes({"source_commit": "b" * 40}),
        }.items():
            path = self.root / ("overlay-" + Path(name).name)
            path.write_bytes(raw)
            self.replacements[name] = path
            self.files[name] = hashlib.sha256(raw).hexdigest()
        self.current = dict(self.previous, source_commit="b" * 40, files=self.files,
                            parent_source_commit="a" * 40,
                            parent_manifest_sha256=hashlib.sha256(self.previous_raw).hexdigest())
        self.current_raw = ASSEMBLER.json_bytes(self.current)
        DELIVERY._manifest(self.current_raw, "b" * 40)

    def tearDown(self):
        self.assertEqual(ASSEMBLER.digest(self.parent), self.parent_sha)
        self.assertEqual(ASSEMBLER.digest(self.companion), self.companion_sha)
        self.temporary.cleanup()

    def stream(self, name="successor.zip"):
        destination = self.root / name
        records = ASSEMBLER.stream_application(
            self.parent, destination, self.files, self.replacements, self.current_raw, DELIVERY)
        return destination, records

    def test_exact_union_replaces_application_and_preserves_unicode_runtime(self):
        destination, records = self.stream()
        result = DELIVERY.verify_archives(destination, self.companion, self.current_raw, "b" * 40)
        self.assertEqual(result["application"]["entries"], len(self.files))
        self.assertEqual(result["sources"]["sha256"], self.companion_sha)
        with ZipFile(destination) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertNotIn(DELIVERY.PREFIX + "wheelhouse/previous.whl", names)
            self.assertNotIn(DELIVERY.PREFIX + DELIVERY.SOURCES, names)
            for name in ("runtime/unchanged.dat", "docs/читання.txt"):
                self.assertEqual(archive.read(DELIVERY.PREFIX + name), self.payloads[name])
                self.assertEqual(records[name]["origin"], "verified-parent")
            source = json.loads(archive.read(DELIVERY.PREFIX + "application-source.json"))
            self.assertEqual(source["source_commit"], "b" * 40)
            for info in archive.infolist():
                record = records[info.filename[len(DELIVERY.PREFIX):]]
                self.assertEqual((record["crc32"], record["size"]), (info.CRC, info.file_size))

    def test_mutated_overlay_is_rejected_instead_of_resealed(self):
        self.replacements["wheelhouse/current.whl"].write_bytes(b"changed after its approved hash")
        with self.assertRaisesRegex(ValueError, "SHA256/CRC32/size mismatch"):
            self.stream("rejected.zip")

    def test_existing_destination_remains_untouched(self):
        destination = self.root / "successor.zip"
        destination.write_bytes(b"existing output must not be replaced")
        with self.assertRaises(FileExistsError):
            self.stream()
        self.assertEqual(destination.read_bytes(), b"existing output must not be replaced")

    def test_appended_envelope_bytes_are_rejected_by_production_verifier(self):
        destination, _records = self.stream()
        with destination.open("ab") as output:
            output.write(b"unlisted trailing bytes")
        with self.assertRaisesRegex(ValueError, "ZIP envelope"):
            DELIVERY.verify_archives(destination, self.companion, self.current_raw, "b" * 40)

    def test_wrong_source_companion_is_rejected(self):
        destination, _records = self.stream()
        wrong = self.root / "wrong-sources.zip"
        with ZipFile(wrong, "w") as archive:
            archive.writestr(DELIVERY.PREFIX + DELIVERY.SOURCES, b"different source")
        with self.assertRaisesRegex(ValueError, "checksum"):
            DELIVERY.verify_archives(destination, wrong, self.current_raw, "b" * 40)

    def test_manifest_case_collision_is_rejected_before_assembly(self):
        conflicting = dict(self.current, files=dict(self.files, **{"Runtime/unchanged.dat": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "[Cc]ase|collid|[Dd]uplicate"):
            DELIVERY._manifest(ASSEMBLER.json_bytes(conflicting), "b" * 40)

    def test_current_source_runtime_pins_can_be_read_without_loading_engines(self):
        source = PACKAGING.parent / "src" / "carr_library"
        model = ASSEMBLER.constant(source / "semantic.py", "APPROVED_MANIFEST")
        self.assertEqual(len(model["hashes"]), 9)
        self.assertEqual(len(ASSEMBLER.constant(source / "native_runtime.py", "DLLS")), 2)
        self.assertEqual(len(ASSEMBLER.constant(source / "native_runtime.py", "HOST_DLLS")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
