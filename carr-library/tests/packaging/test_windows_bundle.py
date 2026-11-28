"""Offline distribution boundaries, using synthetic wheel bytes and real files."""
import hashlib
import os
import csv
import base64
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import subprocess
import struct
import tarfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


builder = module("build_windows_bundle")
installer = module("install_windows")


def synthetic_runtime(folder):
    """Inert archive for packaging tests; never a runnable Python fixture."""
    path = folder / "synthetic-python.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        payload = b"Synthetic runtime; never executed."
        member = tarfile.TarInfo("python/python.exe")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return path


def wheel(folder, name, version="1.0", *, tag="py3-none-any", requires=(), entrypoint=False):
    path = folder / f"{name}-{version}-{tag}.whl"
    metadata = f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {item}\n" for item in requires)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
        if entrypoint:
            archive.writestr("carr_library/__main__.py", "print('synthetic test wheel')\n")
    return path


class WindowsBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bundle Ω ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.dep = wheel(self.wheels, "fixture")
        self.lock = self.root / "windows.lock"
        self.lock.write_text(f"fixture==1.0 --hash=sha256:{builder.sha256(self.dep)}\n")
        self.app = wheel(self.root, "universal_research_library", requires=["fixture==1.0"], entrypoint=True)
        self.output = self.root / "offline"

    def build(self):
        return builder.build_bundle(self.app, self.wheels, self.lock, self.output)

    def build_native(self, **kwargs):
        runtime = synthetic_runtime(self.root)
        expected = json.loads((ROOT / "packaging/windows-python.json").read_text(encoding="utf-8"))["download"]["sha256"]
        actual_sha = builder.sha256
        with patch.object(builder, "sha256", side_effect=lambda path: expected if Path(path) == runtime else actual_sha(path)), \
                patch.object(builder, "verify_application_source", side_effect=lambda path, commit: {"wheel_sha256": actual_sha(path), "source_commit": commit, "source_tree": "b" * 40}):
            return builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                        python_archive=runtime, source_commit="a" * 40, **kwargs)

    def test_complete_bundle_verifies_without_network(self):
        result = self.build()
        self.assertEqual(result["wheels"], 2)
        installer.verify(self.output)
        self.assertIn("universal-research-library==1.0", (self.output / "install.lock").read_text())
        self.assertEqual((self.output / "wheelhouse" / self.app.name).read_bytes(), self.app.read_bytes())
        self.assertFalse((self.output / "ResearchLibrary.exe").exists())

    def native_launcher(self):
        """Synthetic PE metadata for inspection only; never a runnable fixture."""
        data = bytearray(0x600)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 0x80)
        data[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<HHIIIHH", data, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0x22)
        optional = 0x98
        struct.pack_into("<H", data, optional, 0x20B)
        struct.pack_into("<HH", data, optional + 68, 2, 0x160)
        struct.pack_into("<II", data, optional + 120, 0x1000, 40)
        struct.pack_into("<II", data, optional + 128, 0x1100, 16)
        section = optional + 0xF0
        data[section:section + 8] = b".rdata\0\0"
        struct.pack_into("<IIII", data, section + 8, 0x400, 0x1000, 0x400, 0x200)
        struct.pack_into("<IIIII", data, 0x200, 0, 0, 0, 0x1060, 0)
        data[0x260:0x26D] = b"kernel32.dll\0"
        manifest = b'<requestedExecutionLevel level="asInvoker" uiAccess="false"/><longPathAware>true</longPathAware>'
        data[0x300:0x300 + len(manifest)] = manifest
        path = self.root / "ResearchLibrary.exe"
        path.write_bytes(data)
        return path

    def test_native_launcher_exact_selected_bytes_are_sealed_without_execution(self):
        launcher = self.native_launcher()
        original, expected = launcher.read_bytes(), builder.sha256(launcher)
        with patch.object(subprocess, "Popen", side_effect=AssertionError("Bundle assembly must not execute a launcher")):
            self.build_native(native_launcher=launcher, native_launcher_sha256=expected)
        sealed = self.output / "ResearchLibrary.exe"
        self.assertEqual(sealed.read_bytes(), original)
        self.assertTrue((self.output / "ResearchLibrary.cmd").is_file())
        manifest = json.loads((self.output / "bundle-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["ResearchLibrary.exe"], expected)
        installer.verify(self.output)
        sealed.write_bytes(b"changed after assembly")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.output)
        self.assertEqual(launcher.read_bytes(), original)

    def test_native_launcher_requires_paired_hash_and_valid_bounded_pe_before_publication(self):
        launcher = self.native_launcher()
        digest = builder.sha256(launcher)
        invalid = self.root / "invalid.exe"
        invalid.write_bytes(b"not a PE")
        oversized = self.root / "oversized.exe"
        oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        for selected, expected, message in (
                (launcher, None, "supply both"),
                (None, digest, "supply both"),
                (launcher, "0" * 64, "checksum"),
                (invalid, builder.sha256(invalid), "invalid unsigned native launcher"),
                (oversized, builder.sha256(oversized), "size budget")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.build_native(native_launcher=selected, native_launcher_sha256=expected)
                self.assertFalse(self.output.exists())

    def test_native_launcher_input_replacement_cannot_substitute_captured_bytes(self):
        launcher = self.native_launcher()
        original, expected = launcher.read_bytes(), builder.sha256(launcher)
        original_copy = builder.shutil.copyfile
        changed = []

        def replace_input(source, destination, *args, **kwargs):
            if Path(source) == self.dep and not changed:
                launcher.write_bytes(b"concurrent unselected executable")
                changed.append(True)
            return original_copy(source, destination, *args, **kwargs)

        with patch.object(builder.shutil, "copyfile", side_effect=replace_input):
            self.build_native(native_launcher=launcher, native_launcher_sha256=expected)
        self.assertEqual(changed, [True])
        self.assertNotEqual(launcher.read_bytes(), original)
        self.assertEqual((self.output / "ResearchLibrary.exe").read_bytes(), original)
        installer.verify(self.output)

    def test_native_launcher_stage_change_is_rejected_before_sealing(self):
        launcher = self.native_launcher()
        expected = builder.sha256(launcher)
        original_copy = builder.shutil.copyfile

        def change_stage(source, destination, *args, **kwargs):
            if Path(source).name == "ResearchLibrary.cmd":
                (Path(destination).parent / "ResearchLibrary.exe").write_bytes(b"changed staged launcher")
            return original_copy(source, destination, *args, **kwargs)

        with patch.object(builder.shutil, "copyfile", side_effect=change_stage):
            with self.assertRaisesRegex(ValueError, "changed before bundle sealing"):
                self.build_native(native_launcher=launcher, native_launcher_sha256=expected)
        self.assertFalse(self.output.exists())

    def sbom(self, version="1.0"):
        path = self.root / "input-sbom.json"
        path.write_text(json.dumps({
            "bomFormat": "CycloneDX", "specVersion": "1.5",
            "components": [{"name": "fixture", "version": version}],
        }, indent=2) + "\n", encoding="utf-8")
        return path

    def test_sbom_exact_bytes_are_sealed_and_tampering_is_rejected(self):
        sbom = self.sbom()
        expected = sbom.read_bytes()
        builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        sealed = self.output / "runtime-sbom.cdx.json"
        self.assertEqual(sealed.read_bytes(), expected)
        manifest = json.loads((self.output / "bundle-manifest.json").read_text())
        self.assertEqual(manifest["files"]["runtime-sbom.cdx.json"],
                         hashlib.sha256(expected).hexdigest())
        installer.verify(self.output)
        sealed.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.output)

    def test_sbom_initial_mismatch_rejected(self):
        sbom = self.sbom("9.9")
        with self.assertRaisesRegex(ValueError, "SBOM does not match"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertFalse(self.output.exists())

    def test_duplicate_sbom_component_rejected(self):
        sbom = self.sbom()
        bom = json.loads(sbom.read_text(encoding="utf-8"))
        bom["components"][0]["bom-ref"] = "pkg:pypi/fixture@1.0"
        bom["components"].append(dict(bom["components"][0]))
        sbom.write_text(json.dumps(bom), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            builder.build_bundle(self.app, self.wheels, self.lock,
                                 self.output, sbom=sbom)
        self.assertFalse(self.output.exists())

    def vendored_app(self, overrides=None, missing=()):
        """Actual reviewed source bytes in an inert application wheel; never imported."""
        prefix = "carr_library/reader/_vendor_num2words/"
        with ZipFile(self.app) as archive:
            payload = {info.filename: archive.read(info)
                       for info in archive.infolist() if not info.filename.startswith(prefix)}
        source = ROOT / "src/carr_library/reader/_vendor_num2words"
        files = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}
        self.assertEqual(len(files), 8)
        files.update(overrides or {})
        for name in missing:
            files.pop(name)
        payload.update({prefix + name: raw for name, raw in files.items()})
        with ZipFile(self.app, "w", ZIP_DEFLATED) as archive:
            for name, raw in payload.items():
                archive.writestr(name, raw)
        return self.app

    def vendored_sbom(self):
        path = self.sbom()
        bom = json.loads(path.read_bytes())
        actual = json.loads((ROOT / "packaging/windows-sbom.cdx.json").read_bytes())
        component = next(item for item in actual["components"]
                         if item.get("bom-ref") == "carr-vendored-num2words-sk@0.5.14")
        bom["components"].append(component)
        bom["metadata"] = {"component": {"bom-ref": "synthetic-application"}}
        bom["dependencies"] = [
            {"ref": "synthetic-application", "dependsOn": [component["bom-ref"]]},
            {"ref": component["bom-ref"]},
        ]
        path.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
        return path

    def test_reviewed_vendor_and_locked_distribution_are_sealed_together(self):
        self.vendored_app()
        sbom = self.vendored_sbom()
        expected = sbom.read_bytes()
        app_bytes = self.app.read_bytes()
        builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertEqual((self.output / "runtime-sbom.cdx.json").read_bytes(), expected)
        self.assertEqual((self.output / "wheelhouse" / self.app.name).read_bytes(), app_bytes)
        installer.verify(self.output)
        sealed = json.loads(expected)
        self.assertEqual(len(sealed["components"]), 2)
        self.assertEqual(sealed["components"][1]["licenses"], [{"expression": "LGPL-2.1-or-later"}])

    def test_vendor_cannot_be_omitted_or_invented_in_runtime_sbom(self):
        sbom = self.vendored_sbom()
        with self.assertRaisesRegex(ValueError, "omits or invents"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertFalse(self.output.exists())
        self.vendored_app()
        sbom = self.sbom()
        with self.assertRaisesRegex(ValueError, "omits or invents"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertFalse(self.output.exists())

    def test_vendor_descriptor_and_dependency_edges_are_exact(self):
        self.vendored_app()
        sbom = self.vendored_sbom()
        original = sbom.read_bytes()

        def change_license(bom):
            bom["components"][1]["licenses"] = [{"expression": "MIT"}]

        def change_version(bom):
            bom["components"][1]["version"] = "0.5.15"

        def change_provenance(bom):
            bom["components"][1]["externalReferences"][0]["url"] += "-changed"

        def duplicate(bom):
            bom["components"].append(dict(bom["components"][1]))

        def omit_edge(bom):
            bom["dependencies"][0]["dependsOn"] = []

        def duplicate_edge(bom):
            bom["dependencies"][0]["dependsOn"] *= 2

        def add_dependency(bom):
            bom["dependencies"][1]["dependsOn"] = ["unreviewed"]

        for change in (change_license, change_version, change_provenance, duplicate,
                       omit_edge, duplicate_edge, add_dependency):
            with self.subTest(change=change.__name__):
                bom = json.loads(original)
                change(bom)
                sbom.write_text(json.dumps(bom), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "vendored|duplicate"):
                    builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
                self.assertFalse(self.output.exists())

    def test_vendor_sources_not_only_the_bom_label_are_verified(self):
        sbom = self.vendored_sbom()
        for name in ("lang_SK.py", "UPSTREAM.json", "COPYING", "__init__.py"):
            with self.subTest(member=name):
                self.vendored_app({name: b"unreviewed replacement\n"})
                with self.assertRaisesRegex(ValueError, "source identity differs"):
                    builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
                self.assertFalse(self.output.exists())
        self.vendored_app(missing=("COPYING",))
        with self.assertRaisesRegex(ValueError, "source inventory differs"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertFalse(self.output.exists())
        self.vendored_app({"unexpected.py": b"unreviewed extra source\n"})
        with self.assertRaisesRegex(ValueError, "unexpected or duplicate"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertFalse(self.output.exists())

    def test_vendor_wheel_replacement_after_review_is_rejected(self):
        self.vendored_app()
        sbom = self.vendored_sbom()
        original = builder.verify_runtime_sbom
        reviewed = []

        def replace_after_review(*args, **kwargs):
            result = original(*args, **kwargs)
            reviewed.append(True)
            self.vendored_app({"lang_SK.py": b"changed after the exact vendor review\n"})
            return result

        with patch.object(builder, "verify_runtime_sbom", side_effect=replace_after_review):
            with self.assertRaisesRegex(ValueError, "changed after vendored SBOM review"):
                builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertEqual(reviewed, [True])
        self.assertFalse(self.output.exists())

    def test_both_real_runtime_manifests_keep_exact_pins_and_vendor_sources(self):
        files = builder.vendored_sources_from_package(ROOT / "src/carr_library")
        self.assertEqual(len(files), 8)
        for stem in ("windows", "windows-semantic"):
            with self.subTest(profile=stem):
                pins = builder.read_lock(ROOT / "packaging" / (stem + "-cp311.lock"))
                bom = json.loads((ROOT / "packaging" / (stem + "-sbom.cdx.json")).read_bytes())
                components = builder.verify_runtime_sbom(bom, pins, vendor_files=files)
                self.assertEqual(set(components), set(pins))
                self.assertEqual(len(bom["components"]), len(pins) + 1)

    def test_sbom_replaced_after_validation_cannot_change_sealed_bytes(self):
        sbom = self.sbom()
        expected = sbom.read_bytes()
        original_copy = builder.shutil.copyfile
        replacements = []

        def copy_after_replacement(source, destination, *args, **kwargs):
            if Path(source) == self.dep and not replacements:
                replacement = sbom.with_suffix(".replacement")
                replacement.write_bytes(expected.replace(b'"1.0"', b'"9.9"'))
                replacement.replace(sbom)
                replacements.append(True)
            return original_copy(source, destination, *args, **kwargs)

        with patch.object(builder.shutil, "copyfile", side_effect=copy_after_replacement):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output, sbom=sbom)
        self.assertEqual(replacements, [True])
        self.assertNotEqual(sbom.read_bytes(), expected)
        self.assertEqual((self.output / "runtime-sbom.cdx.json").read_bytes(), expected)
        installer.verify(self.output)

    def test_changed_wheel_rejected_before_output(self):
        with ZipFile(self.dep, "a") as archive:
            archive.writestr("unapproved.txt", "changed after lock generation")
        with self.assertRaisesRegex(ValueError, "not admitted"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_missing_or_extra_wheel_rejected(self):
        self.dep.unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.build()
        wheel(self.wheels, "other")
        with self.assertRaisesRegex(ValueError, "not admitted"):
            self.build()

    def test_platform_and_dependency_contracts_rejected(self):
        self.dep.unlink()
        self.dep = wheel(self.wheels, "fixture", tag="cp311-cp311-manylinux_2_17_x86_64")
        self.lock.write_text(f"fixture==1.0 --hash=sha256:{builder.sha256(self.dep)}\n")
        with self.assertRaisesRegex(ValueError, "does not target"):
            self.build()
        self.dep.unlink()
        self.dep = wheel(self.wheels, "fixture")
        self.lock.write_text(f"fixture==1.0 --hash=sha256:{builder.sha256(self.dep)}\n")
        self.app.unlink()
        self.app = wheel(self.root, "universal_research_library", requires=["missing==1.0"], entrypoint=True)
        with self.assertRaisesRegex(ValueError, "unsatisfied"):
            self.build()

    def test_application_without_entrypoint_rejected(self):
        self.app.unlink()
        self.app = wheel(self.root, "universal_research_library")
        with self.assertRaisesRegex(ValueError, "production entrypoint"):
            self.build()

    def test_source_commit_does_not_certify_an_unrelated_application_wheel(self):
        commit = subprocess.check_output(['git', '-C', str(ROOT.parent), 'rev-parse', 'HEAD'],
                                         text=True).strip()
        # This wheel is intentionally synthetic, despite having the right name
        # and an entrypoint. It must not be labelled as the real source commit.
        with self.assertRaises(ValueError):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                 source_commit=commit)
        self.assertFalse(self.output.exists())

    def test_exact_verified_historical_tree_is_preserved_in_bundle(self):
        repo = self.root / 'source repository'
        package = repo / 'carr-library/src/carr_library'
        package.mkdir(parents=True)
        (repo / 'carr-library/pyproject.toml').write_text('''[project]
name = "universal-research-library"
version = "1.0"
dependencies = ["fixture==1.0"]
[tool.setuptools]
packages = ["carr_library"]
package-dir = {"" = "src"}
''', encoding='utf-8')
        main = b"print('synthetic source-bound application')\n"
        (package / '__main__.py').write_bytes(main)
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_AUTHOR_NAME='Synthetic Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                           GIT_COMMITTER_NAME='Synthetic Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], env=environment,
                                           text=True).strip()
        git('init', '-q')
        git('add', '.')
        git('commit', '-qm', 'Synthetic release source')
        release = git('rev-parse', 'HEAD')
        tree = git('rev-parse', release + '^{tree}')
        (repo / 'later.txt').write_text('Unrelated later commit.\n', encoding='utf-8')
        git('add', '.')
        git('commit', '-qm', 'Later source checkout')
        self.assertNotEqual(tree, git('rev-parse', 'HEAD^{tree}'))

        metadata = 'universal_research_library-1.0.dist-info/'
        payload = {'carr_library/__main__.py': main,
                   metadata + 'METADATA': b'Metadata-Version: 2.4\nName: universal-research-library\nVersion: 1.0\nRequires-Dist: fixture==1.0\n',
                   metadata + 'WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
        rows = [[name, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip('='),
                 str(len(raw))] for name, raw in payload.items()]
        rows.append([metadata + 'RECORD', '', ''])
        record = io.StringIO(newline='')
        csv.writer(record, lineterminator='\n').writerows(rows)
        with ZipFile(self.app, 'w', ZIP_DEFLATED) as archive:
            for name, raw in payload.items():
                archive.writestr(name, raw)
            archive.writestr(metadata + 'RECORD', record.getvalue().encode())

        verifier = module('source_wheel')
        def verify(path, source_commit):
            return verifier.verify_application_wheel(path, repo, source_commit)
        with patch.object(builder, 'verify_application_source', side_effect=verify):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                 source_commit=release)
        manifest = json.loads((self.output / 'bundle-manifest.json').read_text())
        self.assertEqual(manifest['source_commit'], release)
        self.assertEqual(manifest['source_tree'], tree)
        proof_path = self.output / 'application-source.json'
        proof_bytes = proof_path.read_bytes()
        proof = json.loads(proof_bytes)
        self.assertEqual(proof['source_commit'], release)
        self.assertEqual(proof['source_tree'], tree)
        self.assertEqual(proof['wheel_sha256'], builder.sha256(self.app))
        self.assertEqual(proof['project_name'], 'universal-research-library')
        self.assertEqual(manifest['files']['application-source.json'],
                         hashlib.sha256(proof_bytes).hexdigest())
        self.assertNotIn(b'\r', proof_bytes)
        installer.verify(self.output)
        proof_path.write_bytes(proof_bytes.replace(release.encode(), b'0' * 40))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            installer.verify(self.output)

    def test_application_replaced_after_source_verification_is_rejected(self):
        verified_digest = builder.sha256(self.app)

        def replace_after_verification(path, source_commit):
            with ZipFile(path, "a") as archive:
                archive.writestr("carr_library/later_build.py", "changed = True\n")
            return {"wheel_sha256": verified_digest}

        with patch.object(builder, "verify_application_source",
                          side_effect=replace_after_verification):
            with self.assertRaisesRegex(ValueError, "changed during bundle assembly"):
                builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                     source_commit="a" * 40)
        self.assertFalse(self.output.exists())

    def test_existing_installation_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "preserve.txt"
        sentinel.write_text("user installation")
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.build()
        self.assertEqual(sentinel.read_text(), "user installation")

    def test_installer_detects_tampering_and_extra_wheels(self):
        self.build()
        extra = self.output / "wheelhouse" / "extra.whl"
        extra.write_bytes(b"not approved")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            installer.verify(self.output)
        extra.unlink()
        (self.output / "ResearchLibrary.cmd").write_text("changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.output)

    def test_manifest_path_escape_rejected(self):
        self.build()
        path = self.output / "bundle-manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["../outside.txt"] = "0" * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "manifest path"):
            installer.verify(self.output)

    def test_lock_rejects_network_options_and_missing_hashes(self):
        for text in ("fixture==1.0\n", "--index-url https://example.invalid\n",
                     "fixture @ https://example.invalid/file.whl\n"):
            self.lock.write_text(text)
            with self.assertRaises(ValueError):
                self.build()


if __name__ == "__main__":
    unittest.main()
