"""Offline distribution boundaries, using synthetic wheel bytes and real files."""
import hashlib
import base64
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
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


    def test_native_launcher_is_sealed_and_tampering_is_rejected(self):
        # Synthetic bytes test packaging boundaries only; native execution is
        # independently required by the Windows launcher's real process probe.
        launcher = self.root / "ResearchLibrary.exe"
        approved = b"synthetic native launcher packaging fixture"
        launcher.write_bytes(approved)
        digest = hashlib.sha256(approved).hexdigest()
        with patch.object(builder, "NATIVE_LAUNCHER_SHA256", digest):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                 native_launcher=launcher)
        self.assertEqual((self.output / "ResearchLibrary.exe").read_bytes(), approved)
        manifest = json.loads((self.output / "bundle-manifest.json").read_text())
        self.assertEqual(manifest["files"]["ResearchLibrary.exe"], digest)
        installer.verify(self.output)
        (self.output / "ResearchLibrary.exe").write_bytes(b"changed executable")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.output)

    def test_native_launcher_wrong_identity_fails_before_output(self):
        launcher = self.root / "ResearchLibrary.exe"
        launcher.write_bytes(b"an unapproved executable")
        with self.assertRaisesRegex(ValueError, "Windows-tested"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                 native_launcher=launcher)
        self.assertFalse(self.output.exists())

    def test_native_launcher_replaced_during_copy_is_rejected(self):
        launcher = self.root / "ResearchLibrary.exe"
        launcher.write_bytes(b"approved synthetic bytes")
        digest = builder.sha256(launcher)
        original_copy = builder.shutil.copyfile

        def replace_before_copy(source, destination, *args, **kwargs):
            if Path(source) == launcher:
                launcher.write_bytes(b"replacement after verification")
            return original_copy(source, destination, *args, **kwargs)

        with patch.object(builder, "NATIVE_LAUNCHER_SHA256", digest):
            with patch.object(builder.shutil, "copyfile", side_effect=replace_before_copy):
                with self.assertRaisesRegex(ValueError, "changed during bundle assembly"):
                    builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                         native_launcher=launcher)
        self.assertFalse(self.output.exists())

    def test_native_launcher_directory_is_rejected(self):
        launcher = self.root / "ResearchLibrary.exe"
        launcher.mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                 native_launcher=launcher)
        self.assertFalse(self.output.exists())

    def sbom(self, version="1.0"):
        path = self.root / "input-sbom.json"
        path.write_text(json.dumps({
            "bomFormat": "CycloneDX", "specVersion": "1.5",
            "components": [{"type": "library", "name": "fixture", "version": version}],
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



class SbomInventoryTests(unittest.TestCase):
    """Current release profiles and real bytes, without importing model code."""

    def setUp(self):
        self.validator = module("sbom_inventory")
        self.temp = tempfile.TemporaryDirectory(prefix="sbom model Ω ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def profile(self, semantic=False):
        prefix = "windows-semantic" if semantic else "windows"
        lock = ROOT / "packaging" / (prefix + "-cp311.lock")
        pins = {name: version for name, (version, _hashes) in builder.read_lock(lock).items()}
        raw = (ROOT / "packaging" / (prefix + "-sbom.cdx.json")).read_bytes()
        return pins, raw

    def test_current_base_profile_has_22_libraries_and_exact_model(self):
        pins, raw = self.profile()
        result = self.validator.validate(raw, pins)
        self.assertEqual(len(result["libraries"]), 22)
        self.assertEqual(result["libraries"], pins)
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["name"], "py3langid/data/model.npz.xz")
        self.assertEqual(result["files"][0]["hashes"], [{
            "alg": "SHA-256",
            "content": "f4f4a2c3465ca1f081541037f9cac23021d68151c2f709c55ae5eedfca522963",
        }])

    def test_current_semantic_profile_has_57_libraries_and_exact_model(self):
        pins, raw = self.profile(semantic=True)
        result = self.validator.validate(raw, pins)
        self.assertEqual(len(result["libraries"]), 57)
        self.assertEqual(result["libraries"], pins)
        self.assertEqual(len(result["files"]), 1)
        base_pins, base_raw = self.profile()
        self.assertEqual(result["files"], self.validator.validate(base_raw, base_pins)["files"])
        self.assertEqual(pins["numpy"], "2.3.5")
        self.assertEqual(base_pins["numpy"], "2.4.6")

    def test_current_profiles_refuse_missing_extra_changed_and_duplicate_libraries(self):
        for semantic in (False, True):
            for change in ("missing", "extra", "duplicate", "normalized_duplicate",
                           "version", "unknown_type", "missing_type", "duplicate_ref"):
                with self.subTest(semantic=semantic, change=change):
                    pins, raw = self.profile(semantic)
                    bom = json.loads(raw)
                    item = next(value for value in bom["components"] if value["type"] == "library")
                    if change == "missing":
                        bom["components"].remove(item)
                    elif change == "extra":
                        bom["components"].append({"type": "library", "name": "unexpected", "version": "1"})
                    elif change == "duplicate":
                        bom["components"].append(dict(item))
                    elif change == "normalized_duplicate":
                        item = next(value for value in bom["components"]
                                    if "-" in value["name"] and value["type"] == "library")
                        extra = dict(item, name=item["name"].upper().replace("-", "_"))
                        extra.pop("bom-ref", None)
                        bom["components"].append(extra)
                    elif change == "version":
                        item["version"] = "0.invalid"
                    elif change == "unknown_type":
                        item["type"] = "operating-system"
                    elif change == "missing_type":
                        del item["type"]
                    else:
                        other = next(value for value in bom["components"]
                                     if value is not item and "bom-ref" in value)
                        other["bom-ref"] = item["bom-ref"]
                    with self.assertRaises(ValueError):
                        self.validator.validate(json.dumps(bom).encode(), pins)

    def test_model_missing_duplicate_or_extra_file_is_not_ignored(self):
        for semantic in (False, True):
            for change in ("missing", "duplicate", "extra"):
                with self.subTest(semantic=semantic, change=change):
                    pins, raw = self.profile(semantic)
                    bom = json.loads(raw)
                    item = next(value for value in bom["components"] if value["type"] == "file")
                    if change == "missing":
                        bom["components"].remove(item)
                    else:
                        extra = json.loads(json.dumps(item))
                        if change == "extra":
                            extra["name"] = "py3langid/data/unreviewed.bin"
                            extra["bom-ref"] = "unreviewed"
                        bom["components"].append(extra)
                    with self.assertRaises(ValueError):
                        self.validator.validate(json.dumps(bom).encode(), pins)

    def test_model_identity_license_size_and_provenance_are_not_silently_filtered(self):
        pins, raw = self.profile()
        for change in ("name", "version", "ref", "hash", "size", "git_blob", "license",
                       "qualification", "extra_property", "extra_field", "wrong_type"):
            with self.subTest(change=change):
                bom = json.loads(raw)
                item = next(value for value in bom["components"] if value["type"] == "file")
                if change == "name":
                    item["name"] = "../model.npz.xz"
                elif change == "version":
                    item["version"] = "0.4.1"
                elif change == "ref":
                    item["bom-ref"] += "-different"
                elif change == "hash":
                    item["hashes"][0]["content"] = "0" * 64
                elif change == "size":
                    item["properties"][1]["value"] = "1"
                elif change == "git_blob":
                    item["properties"][0]["value"] = "0" * 40
                elif change == "license":
                    item["licenses"][0]["license"]["id"] = "Unreviewed"
                elif change == "qualification":
                    item["properties"][2]["value"] = "All languages qualified"
                elif change == "extra_property":
                    item["properties"].append(dict(item["properties"][0]))
                elif change == "extra_field":
                    item["additional_file"] = "anything"
                else:
                    item["type"] = "library"
                with self.assertRaises(ValueError):
                    self.validator.validate(json.dumps(bom).encode(), pins)

    def test_unknown_file_cannot_be_added_to_a_library_only_profile(self):
        raw = json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [
            {"type": "library", "name": "fixture", "version": "1"},
            {"type": "file", "name": "arbitrary.bin", "version": "1"},
        ]}).encode()
        with self.assertRaisesRegex(ValueError, "unapproved"):
            self.validator.validate(raw, {"fixture": "1"})

    def test_duplicate_json_keys_and_normalized_pins_refuse(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.validator.validate(b'{"bomFormat":"CycloneDX","bomFormat":"CycloneDX"}',
                                    {"fixture": "1"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.validator.validate(b"{}", {"some-name": "1", "SOME_name": "1"})

    def synthetic(self, payload=b"reviewed synthetic model bytes", *, contents=None, duplicate=False):
        manifest = json.loads(self.validator.MANIFEST.read_bytes())
        record = manifest["files"][self.validator.MODEL_NAME]
        record["size"] = len(payload)
        record["sha256"] = hashlib.sha256(payload).hexdigest()
        wheel_path = self.root / "py3langid-0.4.0-py3-none-any.whl"
        with ZipFile(wheel_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("py3langid-0.4.0.dist-info/METADATA",
                             "Metadata-Version: 2.3\nName: py3langid\nVersion: 0.4.0\n")
            if contents is not None:
                for name, data in contents:
                    archive.writestr(name, data)
            else:
                archive.writestr(self.validator.MODEL_NAME, payload)
            if duplicate:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(self.validator.MODEL_NAME, payload)
        manifest["wheel"]["size"] = wheel_path.stat().st_size
        manifest["wheel"]["sha256"] = builder.sha256(wheel_path)
        raw = json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [
            {"type": "library", "name": "py3langid", "version": "0.4.0"},
            self.validator._model_component(manifest),
        ]}).encode()
        return manifest, raw, wheel_path

    def test_real_model_wheel_bytes_are_verified_without_importing_the_package(self):
        import builtins
        original_import = builtins.__import__

        def no_model_code(name, *args, **kwargs):
            if name.split(".")[0] in {"py3langid", "numpy"}:
                self.fail("model or NumPy code was imported during byte validation")
            return original_import(name, *args, **kwargs)

        manifest, raw, wheel_path = self.synthetic()
        with patch.object(self.validator, "_model_manifest", return_value=manifest):
            inventory = self.validator.validate(raw, {"py3langid": "0.4.0"})
        with patch("builtins.__import__", side_effect=no_model_code):
            self.validator.verify_wheel(inventory, wheel_path)

    def test_real_wheel_rejects_missing_duplicate_and_changed_model_bytes(self):
        for change in ("missing", "duplicate", "changed", "extra_bytes"):
            with self.subTest(change=change):
                payload = b"reviewed synthetic model bytes"
                contents = None
                if change == "missing":
                    contents = [("py3langid/data/other.bin", payload)]
                elif change == "changed":
                    contents = [(self.validator.MODEL_NAME, b"X" * len(payload))]
                elif change == "extra_bytes":
                    contents = [(self.validator.MODEL_NAME, payload + b"X")]
                manifest, raw, wheel_path = self.synthetic(
                    payload, contents=contents, duplicate=change == "duplicate")
                with patch.object(self.validator, "_model_manifest", return_value=manifest):
                    inventory = self.validator.validate(raw, {"py3langid": "0.4.0"})
                # The archive itself is admitted; its declared inner model still must match.
                with self.assertRaisesRegex(ValueError, "model"):
                    self.validator.verify_wheel(inventory, wheel_path)

    def installed(self):
        manifest, raw, _wheel = self.synthetic()
        path = self.root / self.validator.MODEL_NAME
        path.parent.mkdir(parents=True)
        path.write_bytes(b"reviewed synthetic model bytes")

        class Distribution:
            version = "0.4.0"

            def locate_file(inner, name):
                self.assertEqual(name, self.validator.MODEL_NAME)
                return self.root / name

        return manifest, raw, path, Distribution()

    def test_real_installed_model_matches_before_any_model_import(self):
        import builtins
        manifest, raw, _path, distribution = self.installed()
        original_import = builtins.__import__

        def no_model_code(name, *args, **kwargs):
            if name.split(".")[0] in {"py3langid", "numpy"}:
                self.fail("model code imported by installed inventory validation")
            return original_import(name, *args, **kwargs)

        with patch.object(self.validator, "_model_manifest", return_value=manifest):
            with patch.object(self.validator.metadata, "version", return_value="0.4.0"):
                with patch.object(self.validator.metadata, "distribution", return_value=distribution):
                    with patch("builtins.__import__", side_effect=no_model_code):
                        self.assertEqual(
                            self.validator.verify_installed(raw, {"py3langid": "0.4.0"}),
                            {"py3langid": "0.4.0"})

    def test_installed_version_mismatch_refuses_before_opening_model(self):
        manifest, raw, _path, _distribution = self.installed()
        with patch.object(self.validator, "_model_manifest", return_value=manifest):
            with patch.object(self.validator.metadata, "version", return_value="0.4.1"):
                with patch.object(self.validator.metadata, "distribution",
                                  side_effect=AssertionError("model opened after version mismatch")):
                    with self.assertRaisesRegex(ValueError, "installed dependencies"):
                        self.validator.verify_installed(raw, {"py3langid": "0.4.0"})

    def test_real_installed_same_size_model_tampering_refuses(self):
        manifest, raw, path, distribution = self.installed()
        path.write_bytes(b"X" * path.stat().st_size)
        with patch.object(self.validator, "_model_manifest", return_value=manifest):
            with patch.object(self.validator.metadata, "version", return_value="0.4.0"):
                with patch.object(self.validator.metadata, "distribution", return_value=distribution):
                    with self.assertRaisesRegex(ValueError, "checksum"):
                        self.validator.verify_installed(raw, {"py3langid": "0.4.0"})

    def test_model_changed_after_genuine_hash_is_rejected(self):
        payload = b"reviewed synthetic model bytes"
        path = self.root / "model.npz.xz"
        path.write_bytes(payload)
        genuine_hash = self.validator._hash_stream

        def changed_after_hash(stream, size, expected):
            genuine_hash(stream, size, expected)
            # Append after the real hash. The open descriptor's size change
            # must be observed before verification can report success.
            with path.open("ab") as writer:
                writer.write(b"changed")

        with patch.object(self.validator, "_hash_stream", side_effect=changed_after_hash):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.validator._verify_file(path, len(payload), hashlib.sha256(payload).hexdigest())

    def test_builder_seals_typed_model_and_libraries_as_exact_reviewed_bytes(self):
        manifest, raw, model_wheel = self.synthetic()
        wheels = self.root / "wheels"
        wheels.mkdir()
        destination = wheels / model_wheel.name
        destination.write_bytes(model_wheel.read_bytes())
        app = wheel(self.root, "universal_research_library",
                    requires=["py3langid==0.4.0"], entrypoint=True)
        lock = self.root / "windows.lock"
        lock.write_text("py3langid==0.4.0 --hash=sha256:" + builder.sha256(destination) + "\n")
        sbom = self.root / "runtime.json"
        sbom.write_bytes(raw)
        output = self.root / "bundle"
        original_read = Path.read_bytes

        def reviewed_fixture(path):
            if path == self.validator.MANIFEST:
                return json.dumps(manifest).encode()
            return original_read(path)

        with patch.object(Path, "read_bytes", reviewed_fixture):
            builder.build_bundle(app, wheels, lock, output, sbom=sbom)
        self.assertEqual((output / "runtime-sbom.cdx.json").read_bytes(), raw)
        self.assertEqual((output / "wheelhouse" / model_wheel.name).read_bytes(),
                         destination.read_bytes())
        installer.verify(output)
        self.assertFalse((output / "py3langid").exists())

    def test_builder_rejects_declared_model_mismatch_before_output(self):
        manifest, raw, model_wheel = self.synthetic(
            contents=[(self.validator.MODEL_NAME, b"X" * len(b"reviewed synthetic model bytes"))])
        wheels = self.root / "wheels"
        wheels.mkdir()
        (wheels / model_wheel.name).write_bytes(model_wheel.read_bytes())
        app = wheel(self.root, "universal_research_library",
                    requires=["py3langid==0.4.0"], entrypoint=True)
        lock = self.root / "windows.lock"
        lock.write_text("py3langid==0.4.0 --hash=sha256:" + builder.sha256(model_wheel) + "\n")
        sbom = self.root / "runtime.json"
        sbom.write_bytes(raw)
        output = self.root / "bundle"
        original_read = Path.read_bytes

        def reviewed_fixture(path):
            if path == self.validator.MANIFEST:
                return json.dumps(manifest).encode()
            return original_read(path)

        with patch.object(Path, "read_bytes", reviewed_fixture):
            with self.assertRaisesRegex(ValueError, "checksum"):
                builder.build_bundle(app, wheels, lock, output, sbom=sbom)
        self.assertFalse(output.exists())

if __name__ == "__main__":
    unittest.main()
