"""Bounded native launcher admission and real immutable bundle assembly.

PE bytes and the tiny Python archive are synthetic structural fixtures. They are
never executed. Native Windows execution is covered by the separate CI probe.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_module("native_launcher_admission_test", ROOT / "packaging/native_launcher.py")
builder = load_module("native_launcher_bundle_test", ROOT / "packaging/build_windows_bundle.py")
installer = load_module("native_launcher_installer_test", ROOT / "packaging/install_windows.py")
probe = load_module("native_launcher_pe_test", ROOT / "packaging/native-launcher/probe_pe.py")
native_checks = load_module("native_launcher_app_check_test", ROOT / "src/carr_library/native_checks.py")


def pe_fixture():
    data = bytearray(1536)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x8664, 1, 0, 0, 0, 240, 0)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<HH", data, optional + 68, 2, 0x160)
    struct.pack_into("<II", data, optional + 112 + 8, 0x1000, 40)
    struct.pack_into("<II", data, optional + 112 + 16, 0x1100, 128)
    struct.pack_into("<IIII", data, optional + 240 + 8, 1024, 0x1000, 1024, 0x200)
    struct.pack_into("<IIIII", data, 0x200, 1, 0, 0, 0x1050, 1)
    data[0x250:0x250 + 13] = b"kernel32.dll\0"
    manifest = b'<requestedExecutionLevel level="asInvoker" uiAccess="false"/><longPathAware>true</longPathAware>'
    data[0x300:0x300 + len(manifest)] = manifest
    return bytes(data)


def wheel(path, name, *, application=False):
    filename = path / (name + "-1.0-py3-none-any.whl")
    metadata = "Metadata-Version: 2.3\nName: " + name + "\nVersion: 1.0\n"
    if application:
        metadata += "Requires-Dist: fixture==1.0\n"
    with ZipFile(filename, "w") as archive:
        archive.writestr(name + "-1.0.dist-info/METADATA", metadata)
        if application:
            archive.writestr("carr_library/__main__.py", "# Synthetic package; never executed.\n")
    return filename


class NativeLauncherAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native launcher Київ ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root / "ResearchLibrary.exe"
        self.executable.write_bytes(pe_fixture())
        self.evidence = self.root / "build-evidence.json"
        self.sources = self.root / "sources"
        self.sources.mkdir()
        for name in capture.SOURCE_SETS["Windows x64 / MSVC"]:
            shutil.copyfile(capture.SOURCE / name, self.sources / name)
        self.report = {
            "schema": 1, "target": "Windows x64 / MSVC",
            "toolchain": {name: {"sha256": "a" * 64, "version": "synthetic-test-version"}
                          for name in ("cl.exe", "link.exe", "rc.exe")},
            "source_sha256": {name: hashlib.sha256((self.sources / name).read_bytes()).hexdigest()
                              for name in capture.SOURCE_SETS["Windows x64 / MSVC"]},
            "pe": probe.inspect_pe(self.executable),
            "release_status": "unsigned-private-candidate",
        }
        self.write_report()

    def write_report(self):
        self.evidence.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")

    def admit(self):
        with patch.object(capture, "SOURCE", self.sources):
            return capture.capture_native_launcher(self.executable, self.evidence)

    def test_exact_executable_and_evidence_snapshots_are_returned(self):
        image, report = self.admit()
        self.assertEqual(image, self.executable.read_bytes())
        self.assertEqual(report, self.evidence.read_bytes())
        self.assertFalse(json.loads(report)["pe"]["windows_execution_verified"])
        self.assertFalse(json.loads(report)["pe"]["authenticode_signed"])

    def test_different_current_source_is_rejected(self):
        source = self.sources / "launcher.c"
        source.write_bytes(source.read_bytes() + b"\n/* changed source */\n")
        with self.assertRaisesRegex(ValueError, "different source"):
            self.admit()

    def test_changed_executable_is_not_certified_by_old_evidence(self):
        image = bytearray(self.executable.read_bytes())
        image[-1] = 1
        self.executable.write_bytes(image)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.admit()

    def test_duplicate_json_fields_are_rejected(self):
        self.evidence.write_text('{"schema":1,' + self.evidence.read_text(encoding="utf-8").lstrip()[1:],
                                 encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.admit()

    def test_missing_or_unknown_source_fields_are_rejected(self):
        original = dict(self.report["source_sha256"])
        for sources in ({key: value for key, value in original.items() if key != "launcher.c"},
                        {**original, "../outside.c": "a" * 64}):
            with self.subTest(sources=list(sources)):
                self.report["source_sha256"] = sources
                self.write_report()
                with self.assertRaisesRegex(ValueError, "source inventory"):
                    self.admit()

    def test_invalid_toolchain_claims_are_rejected(self):
        for toolchain in ({}, {"cl.exe": {"sha256": "a" * 64, "version": "fixture"}},
                          {name: {"sha256": "bad", "version": "fixture"}
                           for name in ("cl.exe", "link.exe", "rc.exe")}):
            with self.subTest(toolchain=toolchain):
                self.report["toolchain"] = toolchain
                self.write_report()
                with self.assertRaisesRegex(ValueError, "toolchain"):
                    self.admit()

    def test_wrong_pe_subsystem_is_rejected(self):
        image = bytearray(self.executable.read_bytes())
        struct.pack_into("<H", image, 0x98 + 68, 3)
        self.executable.write_bytes(image)
        with self.assertRaisesRegex(ValueError, "Invalid native launcher PE"):
            self.admit()

    def test_oversized_executable_and_report_are_rejected(self):
        for target, limit in ((self.executable, capture.MAX_PE_BYTES),
                              (self.evidence, capture.MAX_REPORT_BYTES)):
            with self.subTest(file=target.name):
                original = target.read_bytes()
                with target.open("wb") as stream:
                    stream.truncate(limit + 1)
                with self.assertRaisesRegex(ValueError, "bounded regular"):
                    self.admit()
                target.write_bytes(original)

    def test_actual_mutation_during_capture_is_rejected(self):
        actual_fstat = os.fstat
        calls = []

        def replace_after_read(fd):
            result = actual_fstat(fd)
            calls.append(fd)
            if len(calls) == 2:
                with self.executable.open("ab") as writer:
                    writer.write(b"changed")
            return result

        with patch.object(capture.os, "fstat", side_effect=replace_after_read):
            with self.assertRaisesRegex(ValueError, "changed during"):
                self.admit()
        self.assertEqual(len(calls), 2)

    def test_directory_instead_of_native_executable_is_rejected(self):
        self.executable.unlink()
        self.executable.mkdir()
        with self.assertRaisesRegex(ValueError, "bounded regular"):
            self.admit()
        self.assertTrue(self.executable.is_dir())

    def test_other_executable_name_is_rejected(self):
        renamed = self.root / "unrelated.exe"
        shutil.copyfile(self.executable, renamed)
        with self.assertRaisesRegex(ValueError, "Expected ResearchLibrary.exe"):
            capture.capture_native_launcher(renamed, self.evidence)


class NativeLauncherBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native bundle Київ ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wheels = self.root / "wheelhouse"
        self.wheels.mkdir()
        self.dependency = wheel(self.wheels, "fixture")
        self.application = wheel(self.root, "universal_research_library", application=True)
        self.lock = self.root / "runtime.lock"
        self.lock.write_text("fixture==1.0 --hash=sha256:" + builder.sha256(self.dependency) + "\n",
                             encoding="utf-8")
        self.output = self.root / "bundle"
        self.packaging = self.root / "source/packaging"
        self.packaging.mkdir(parents=True)
        native = self.packaging / "native-launcher"
        native.mkdir()
        for name in capture.SOURCE_SETS["Windows x64 / MSVC"]:
            shutil.copyfile(capture.SOURCE / name, native / name)
        for name in ("native_launcher.py", "install_windows.py", "Install.cmd",
                     "ResearchLibrary.cmd", "CheckInstallation.cmd", "SetupLocalAudio.cmd",
                     "setup_local_audio.py", "prepare_ffmpeg_runtime.py"):
            shutil.copyfile(ROOT / "packaging" / name, self.packaging / name)
        notices = self.packaging / "wheel-notices"
        notices.mkdir()
        (notices / "SOURCES.json").write_text("[]", encoding="utf-8")
        runtime_notices = self.packaging / "runtime-notices"
        runtime_notices.mkdir()
        (runtime_notices / "SYNTHETIC.txt").write_text("Test fixture only.", encoding="utf-8")
        docs = self.packaging.parent / "docs"
        docs.mkdir()
        for name in ("RUNNING.md", "START_UK.txt"):
            (docs / name).write_text("Synthetic bundle test.", encoding="utf-8")
        self.runtime = self.root / "python.tar.gz"
        runtime_bytes = b"Synthetic Python placeholder; never executed."
        with tarfile.open(self.runtime, "w:gz") as archive:
            info = tarfile.TarInfo("python/python.exe")
            info.size = len(runtime_bytes)
            archive.addfile(info, io.BytesIO(runtime_bytes))
        (self.packaging / "windows-python.json").write_text(
            json.dumps({"download": {"sha256": builder.sha256(self.runtime)}}), encoding="utf-8")
        self.launcher = self.root / "ResearchLibrary.exe"
        self.launcher.write_bytes(pe_fixture())
        self.evidence = self.root / "build-evidence.json"
        report = {
            "schema": 1, "target": "Windows x64 / MSVC",
            "toolchain": {name: {"sha256": "a" * 64, "version": "synthetic-test-version"}
                          for name in ("cl.exe", "link.exe", "rc.exe")},
            "source_sha256": {name: hashlib.sha256((native / name).read_bytes()).hexdigest()
                              for name in capture.SOURCE_SETS["Windows x64 / MSVC"]},
            "pe": probe.inspect_pe(self.launcher),
            "release_status": "unsigned-private-candidate",
        }
        self.evidence.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def build(self, *, native=True, python=True, **overrides):
        options = {}
        if python:
            options["python_archive"] = self.runtime
        if native:
            options.update(native_launcher=self.launcher, native_launcher_evidence=self.evidence)
        options.update(overrides)
        with patch.object(builder, "HERE", self.packaging):
            return builder.build_bundle(self.application, self.wheels, self.lock, self.output, **options)

    def manifest(self):
        return json.loads((self.output / "bundle-manifest.json").read_text(encoding="utf-8"))

    def write_manifest(self, manifest):
        (self.output / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_launcher_and_evidence_must_be_supplied_together(self):
        for options in ({"native_launcher": self.launcher}, {"native_launcher_evidence": self.evidence}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "supply both"):
                    self.build(native=False, **options)
                self.assertFalse(self.output.exists())

    def test_native_launcher_requires_bundled_python(self):
        with self.assertRaisesRegex(ValueError, "requires bundled Python"):
            self.build(python=False)
        self.assertFalse(self.output.exists())

    def test_bundle_seals_both_exact_files_and_rejects_tampering(self):
        image, report = self.launcher.read_bytes(), self.evidence.read_bytes()
        self.build()
        manifest = self.manifest()
        self.assertIs(manifest["bundled_native_launcher"], True)
        self.assertEqual((self.output / "ResearchLibrary.exe").read_bytes(), image)
        self.assertEqual((self.output / "native-launcher-build.json").read_bytes(), report)
        self.assertEqual(manifest["files"]["ResearchLibrary.exe"], hashlib.sha256(image).hexdigest())
        self.assertEqual(manifest["files"]["native-launcher-build.json"], hashlib.sha256(report).hexdigest())
        # Exercise both production readers on the assembler's real manifest;
        # hand-written permissive flag fixtures cannot prove this interoperability.
        self.assertEqual(native_checks._manifest(self.output), manifest)
        installer.verify(self.output)
        (self.output / "ResearchLibrary.exe").write_bytes(image + b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.output)

    def test_input_replacement_after_capture_cannot_change_sealed_bytes(self):
        image, report = self.launcher.read_bytes(), self.evidence.read_bytes()
        copy_file = shutil.copyfile
        replaced = []

        def replace_after_capture(source, destination, *args, **kwargs):
            if Path(source) == self.dependency and not replaced:
                self.launcher.write_bytes(b"later unreviewed executable")
                self.evidence.write_text("{}", encoding="utf-8")
                replaced.append(True)
            return copy_file(source, destination, *args, **kwargs)

        with patch.object(builder.shutil, "copyfile", side_effect=replace_after_capture):
            self.build()
        self.assertEqual(replaced, [True])
        self.assertEqual((self.output / "ResearchLibrary.exe").read_bytes(), image)
        self.assertEqual((self.output / "native-launcher-build.json").read_bytes(), report)
        installer.verify(self.output)

    def test_existing_installation_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "preserve.txt"
        sentinel.write_bytes(b"existing installation")
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.build()
        self.assertEqual(sentinel.read_bytes(), b"existing installation")

    def test_unannounced_native_files_are_rejected(self):
        self.build(native=False, python=False)
        installer.verify(self.output)
        for name in ("ResearchLibrary.exe", "native-launcher-build.json"):
            with self.subTest(file=name):
                path = self.output / name
                path.write_bytes(b"unannounced")
                with self.assertRaisesRegex(ValueError, "unannounced native launcher"):
                    installer.verify(self.output)
                path.unlink()

    def test_invalid_native_launcher_flag_is_rejected(self):
        self.build(native=False, python=False)
        manifest = self.manifest()
        for value in (1, "true", []):
            with self.subTest(flag=value):
                manifest["bundled_native_launcher"] = value
                self.write_manifest(manifest)
                with self.assertRaisesRegex(ValueError, "invalid native launcher"):
                    installer.verify(self.output)

    def test_announced_launcher_requires_complete_manifest_and_python(self):
        self.build(native=False, python=False)
        manifest = self.manifest()
        manifest["bundled_native_launcher"] = True
        self.write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "requires bundled Python"):
            installer.verify(self.output)


if __name__ == "__main__":
    unittest.main()
