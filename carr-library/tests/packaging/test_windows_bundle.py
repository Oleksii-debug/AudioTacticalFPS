"""Offline distribution boundaries, using synthetic wheel bytes and real files."""
import base64
import csv
import hashlib
import io
import os
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import tomllib
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

    def test_complete_bundle_verifies_without_network(self):
        result = self.build()
        self.assertEqual(result["wheels"], 2)
        installer.verify(self.output)
        self.assertIn("universal-research-library==1.0", (self.output / "install.lock").read_text())
        self.assertEqual((self.output / "wheelhouse" / self.app.name).read_bytes(), self.app.read_bytes())
        manifest = json.loads((self.output / "bundle-manifest.json").read_bytes())
        self.assertNotIn("source_tree", manifest)
        self.assertFalse((self.output / "application-source.json").exists())
        self.assertEqual((self.output / "START_V3_UK.txt").read_bytes(),
                         (ROOT / "docs/START_V3_UK.txt").read_bytes())

    def test_actual_sealed_builder_manifest_reaches_native_installation_checks(self):
        # Build and verify the real bundle format with public synthetic wheels.
        # Only extension loading is substituted; the manifest is never filtered,
        # reconstructed, or allowed to bypass native_checks._manifest.
        from types import SimpleNamespace
        from carr_library import native_checks

        self.build()
        installer.verify(self.output)
        path = self.output / "bundle-manifest.json"
        raw = path.read_bytes()
        manifest = json.loads(raw)
        self.assertIs(manifest["bundled_launcher"], False)
        self.assertIs(manifest["bundled_audio_runtime"], False)
        self.assertEqual(native_checks._manifest(self.output), manifest)
        loaded = []

        def load(name):
            loaded.append(name)
            if name == "carr_library.native_runtime":
                return SimpleNamespace(activate_bundled_runtime=lambda *, bundle_root: None)
            self.assertIn(name, [module for _, module in native_checks.NATIVE_MODULES])
            return SimpleNamespace()

        with patch.object(native_checks, "import_module", side_effect=load):
            report = native_checks.check_native_dependencies(self.output)
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(loaded, ["carr_library.native_runtime",
                                 *[module for _, module in native_checks.NATIVE_MODULES]])
        self.assertEqual(report["semantic"]["status"], "NOT_INCLUDED")
        self.assertEqual(report["ocr"]["status"], "NOT_INCLUDED")
        self.assertEqual(path.read_bytes(), raw)

    def test_core_seals_source_only_local_audio_setup_and_keeps_transfer_gate(self):
        self.build()
        manifest = json.loads((self.output / "bundle-manifest.json").read_text())
        expected = {
            "SetupLocalAudio.cmd": ROOT / "packaging" / "SetupLocalAudio.cmd",
            "tools/audio-setup/setup_local_audio.py": ROOT / "packaging" / "setup_local_audio.py",
            "tools/audio-setup/prepare_ffmpeg_runtime.py": ROOT / "packaging" / "prepare_ffmpeg_runtime.py",
            "tools/audio-setup/audio-runtime/source-lock.json": ROOT / "packaging" / "audio-runtime" / "source-lock.json",
            "LOCAL_AUDIO_SETUP.txt": ROOT / "docs" / "LOCAL_AUDIO_SETUP.md",
        }
        for relative, source in expected.items():
            actual = (self.output / relative).read_bytes()
            self.assertEqual(actual, source.read_bytes())
            self.assertEqual(manifest["files"][relative], hashlib.sha256(actual).hexdigest())
        self.assertIs(manifest["bundled_audio_runtime"], False)
        self.assertFalse(any(name.split("/", 1)[0].casefold() in ("audio", "audio-runtime")
                             for name in manifest["files"]))
        module("create_delivery")._require_transferable_audio_inventory(manifest)
        installer.verify(self.output)
        (self.output / "tools/audio-setup/setup_local_audio.py").write_bytes(b"changed helper")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.output)

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
        from carr_library.native_checks import _manifest
        self.assertEqual(_manifest(self.output)['source_tree'], tree)
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


class OfflineInstallerInventoryTests(unittest.TestCase):
    def inventories(self):
        for prefix, extras in (("windows", ("desktop", "sync")),
                               ("windows-semantic", ("desktop", "sync", "semantic"))):
            yield prefix, extras, builder.read_lock(ROOT / "packaging" / (prefix + "-cp311.lock"))

    def test_installer_roots_and_locks_include_declared_application_features(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

        def requirements(rows):
            parsed = [builder.Requirement(row) for row in rows]
            return {builder.canonicalize_name(row.name): next(iter(row.specifier)).version for row in parsed}

        for prefix, extras, pins in self.inventories():
            with self.subTest(installer=prefix):
                expected = list(project["dependencies"])
                for extra in extras:
                    expected.extend(project["optional-dependencies"][extra])
                roots = (ROOT / "packaging" / (prefix + "-roots.in")).read_text(encoding="utf-8").splitlines()
                roots = [row.strip() for row in roots if row.strip() and not row.lstrip().startswith("#")]
                self.assertEqual(requirements(roots), requirements(expected))
                self.assertEqual(len(roots), len(requirements(roots)))
                for name, version in requirements(expected).items():
                    self.assertEqual(pins[name][0], version)

    def test_sync_download_pins_are_identical_in_each_offline_installer(self):
        sync = builder.read_lock(ROOT / "packaging/windows-sync-cp311.lock")
        for prefix, _extras, pins in self.inventories():
            with self.subTest(installer=prefix):
                self.assertEqual({name: pins[name] for name in sync}, sync)

    def test_installer_sbom_covers_the_exact_inventory_and_sync_dependency_graph(self):
        sync = builder.read_lock(ROOT / "packaging/windows-sync-cp311.lock")
        for prefix, _extras, pins in self.inventories():
            with self.subTest(installer=prefix):
                bom = json.loads((ROOT / "packaging" / (prefix + "-sbom.cdx.json")).read_text(encoding="utf-8"))
                components = {builder.canonicalize_name(row["name"]): row for row in bom["components"]}
                self.assertEqual(len(components), len(bom["components"]))
                self.assertEqual({name: row["version"] for name, row in components.items()},
                                 {name: pin[0] for name, pin in pins.items()})
                refs = {row["bom-ref"] for row in components.values()}
                self.assertEqual(len(refs), len(components))
                root = bom["metadata"]["component"]["bom-ref"]
                graph = {row["ref"]: set(row.get("dependsOn", ())) for row in bom["dependencies"]}
                self.assertEqual(len(graph), len(bom["dependencies"]))
                self.assertLessEqual(set(graph), refs | {root})
                self.assertTrue(all(targets <= refs for targets in graph.values()))
                crypto, cffi, parser = (components[name]["bom-ref"] for name in ("cryptography", "cffi", "pycparser"))
                self.assertIn(crypto, graph[root])
                self.assertIn(cffi, graph[crypto])
                self.assertIn(parser, graph[cffi])
                for name, (_version, hashes) in sync.items():
                    self.assertEqual({row["content"] for row in components[name]["hashes"]
                                      if row["alg"] == "SHA-256"}, hashes)
                    self.assertTrue(components[name]["licenses"])


if __name__ == "__main__":
    unittest.main()
