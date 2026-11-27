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


    def test_optional_profile_preserves_canonical_core_and_semantic_locks(self):
        core = builder.read_lock(ROOT / "packaging/windows-cp311.lock")
        semantic = builder.read_lock(ROOT / "packaging/windows-semantic-cp311.lock")
        optional = builder.read_lock(ROOT / "packaging/windows-semantic-trusted-cp311.lock")
        trust = builder.read_lock(ROOT / "packaging/trusted-client.lock")
        self.assertEqual((len(core), len(semantic), len(optional)), (15, 51, 56))
        self.assertEqual(set(optional) - set(semantic),
                         {"cryptography", "cffi", "pycparser", "tuf", "securesystemslib"})
        for name, pin in semantic.items():
            self.assertEqual(optional[name], pin)
        # Core and semantic are independently reviewed profiles. The optional
        # profile extends semantic exactly; it cannot silently substitute the
        # different core NumPy release or weaken either profile's pin.
        core_differences = {name: (version, optional[name][0])
                            for name, (version, _hashes) in core.items()
                            if optional[name][0] != version}
        self.assertEqual(core_differences, {"numpy": ("2.4.6", "2.3.5")})
        for filename, pins in (("windows-sbom.cdx.json", core),
                               ("windows-semantic-sbom.cdx.json", semantic),
                               ("windows-semantic-trusted-sbom.cdx.json", optional)):
            sbom = json.loads((ROOT / "packaging" / filename).read_bytes())
            self.assertEqual({item["name"]: item["version"] for item in sbom["components"]},
                             {name: pin[0] for name, pin in pins.items()})
        for name, (version, hashes) in trust.items():
            self.assertEqual(optional[name][0], version)
            self.assertTrue(hashes.issubset(optional[name][1]))
        self.assertEqual(optional["urllib3"], semantic["urllib3"])

    def test_optional_sbom_keeps_every_existing_component_and_reviewed_addition(self):
        base = json.loads((ROOT / "packaging/windows-semantic-sbom.cdx.json").read_bytes())
        full = json.loads((ROOT / "packaging/windows-semantic-trusted-sbom.cdx.json").read_bytes())
        review = json.loads((ROOT / "packaging/trusted-client-dependencies.json").read_bytes())
        components = {item["name"]: item for item in full["components"]}
        self.assertEqual(len(full["components"]), 56)
        self.assertEqual(len(components), 56)
        for item in base["components"]:
            self.assertEqual(components[item["name"]], item)
        base_names = {item["name"] for item in base["components"]}
        for item in review["dependencies"]:
            component = components[item["name"]]
            self.assertEqual(component["version"], item["version"])
            if item["name"] not in base_names:
                self.assertIn({"alg": "SHA-256", "content": item["sha256"]},
                              component["hashes"])
                self.assertTrue(component["licenses"])
        refs = {item["bom-ref"] for item in full["components"]}
        root = next(item for item in full["dependencies"]
                    if item["ref"] == full["metadata"]["component"]["bom-ref"])
        self.assertEqual(set(root["dependsOn"]), refs)
        for item in full["dependencies"]:
            self.assertTrue(set(item.get("dependsOn", ())).issubset(refs))

    def test_optional_profile_cannot_certify_an_unrelated_dependency_lock(self):
        with self.assertRaisesRegex(ValueError, "exact full optional dependency lock"):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                 sbom=self.sbom(), trusted_client=True)
        self.assertFalse(self.output.exists())

    def test_optional_profile_requires_its_complete_sbom_before_copy(self):
        with self.assertRaisesRegex(ValueError, "full optional runtime SBOM"):
            builder.build_bundle(self.app, self.wheels,
                                 ROOT / "packaging/windows-semantic-trusted-cp311.lock",
                                 self.output, trusted_client=True)
        self.assertFalse(self.output.exists())

    def test_optional_profile_and_review_bytes_are_sealed_without_executing_payload(self):
        # Substitute only the admitted lock catalogue with an inert fixture.
        # The actual file copying, manifest and installed verifier remain real;
        # the separate catalogue test above preserves all 56 release pins.
        read_lock = builder.read_lock
        expected = read_lock(self.lock)
        def catalogue(path):
            if Path(path) == builder.HERE / "windows-semantic-trusted-cp311.lock":
                return expected
            return read_lock(path)
        with patch.object(builder, "read_lock", side_effect=catalogue):
            builder.build_bundle(self.app, self.wheels, self.lock, self.output,
                                 sbom=self.sbom(), trusted_client=True)
        manifest = json.loads((self.output / "bundle-manifest.json").read_bytes())
        self.assertIs(manifest["bundled_trusted_client"], True)
        review = (ROOT / "packaging/trusted-client-dependencies.json").read_bytes()
        self.assertEqual((self.output / "trusted-client-dependencies.json").read_bytes(), review)
        self.assertEqual(manifest["files"]["trusted-client-dependencies.json"],
                         hashlib.sha256(review).hexdigest())
        installer.verify(self.output)
        (self.output / "trusted-client-dependencies.json").write_bytes(b"changed review")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.output)

    def test_optional_flag_cannot_hide_missing_inventory_or_use_truthy_text(self):
        self.build()
        target = self.output / "bundle-manifest.json"
        manifest = json.loads(target.read_bytes())
        for value, message in ((True, "incomplete trusted-client"), ("yes", "invalid trusted-client")):
            manifest["bundled_trusted_client"] = value
            target.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, message):
                installer.verify(self.output)


if __name__ == "__main__":
    unittest.main()
