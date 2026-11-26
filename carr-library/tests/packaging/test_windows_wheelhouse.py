"""Actual uv subprocess tests, synthetic wheel metadata; no Windows binary execution."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('windows_wheelhouse', Path(__file__).resolve().parents[2] / 'packaging/windows_wheelhouse.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def wheel(root, name, tag, dependencies=()):
    # Minimal test-only metadata; no executable payload or external source.
    dist = name + '-1.0.dist-info'
    path = root / (name + '-1.0-' + tag + '.whl')
    meta = 'Metadata-Version: 2.1\nName: ' + name.replace('_', '-') + '\nVersion: 1.0\n'
    meta += ''.join('Requires-Dist: ' + d + '\n' for d in dependencies)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(dist + '/METADATA', meta)
        z.writestr(dist + '/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: ' + tag + '\n')
        z.writestr(dist + '/RECORD', '')
    return path


class TargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='Carr ž test ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wheels = self.root / 'wheelhouse'
        self.wheels.mkdir()
        self.req = self.root / 'requirements.in'
        self.req.write_text('carr-root==1.0\n', encoding='utf-8')
        self.out = self.root / 'windows.lock'
        self.manifest = self.root / 'approved.json'
        wheel(self.wheels, 'carr_root', 'py3-none-any', ["carr_native==1.0; sys_platform == 'win32'", "carr_linux==1.0; sys_platform == 'linux'"])

    def approve_fixture(self):
        # Synthetic fixtures only. Production approval must be independently sourced.
        self.manifest.write_text(json.dumps({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in self.wheels.iterdir()}), encoding='utf-8')

    def run_resolve(self):
        if not self.manifest.exists():
            self.approve_fixture()
        mod.resolve(self.req, self.wheels, self.out, self.manifest)

    def test_reject_modified_valid_wheel(self):
        native = wheel(self.wheels, 'carr_native', 'cp311-cp311-win_amd64')
        self.approve_fixture()
        with zipfile.ZipFile(native, 'a') as z:
            z.writestr('modified.txt', 'changed after approval')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_reject_extra_wheel(self):
        self.approve_fixture()
        wheel(self.wheels, 'carr_native', 'cp311-cp311-win_amd64')
        with self.assertRaisesRegex(ValueError, 'approved inventory'):
            self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_reject_missing_wheel(self):
        native = wheel(self.wheels, 'carr_native', 'cp311-cp311-win_amd64')
        self.approve_fixture()
        native.unlink()
        with self.assertRaisesRegex(ValueError, 'approved inventory'):
            self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_reject_invalid_manifest(self):
        for payload in ('{}', '[]', '{"x-1.0-py3-none-any.whl": "not-a-hash"}',
                        '{"x": 1, "x": 2}', '{"../x.whl": "' + '0'*64 + '"}',
                        json.dumps({'x-1.0-py3-none-any.whl': '0'*64,
                                    'X-1.0-py3-none-any.whl': '1'*64})):
            with self.subTest(payload=payload):
                self.manifest.write_text(payload, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.run_resolve()
                self.assertFalse(self.out.exists())

    def test_reject_mutation_after_dry_run(self):
        native = wheel(self.wheels, 'carr_native', 'cp311-cp311-win_amd64')
        self.approve_fixture()
        real_run = subprocess.run
        def change_after_run(cmd, *args, **kwargs):
            result = real_run(cmd, *args, **kwargs)
            if '--dry-run' in cmd:
                with zipfile.ZipFile(native, 'a') as z:
                    z.writestr('modified.txt', 'changed during staging')
            return result
        with patch.object(mod.subprocess, 'run', side_effect=change_after_run):
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_windows_marker_and_hash(self):
        native = wheel(self.wheels, 'carr_native', 'cp311-cp311-win_amd64')
        self.run_resolve()
        lock = self.out.read_text(encoding='utf-8')
        self.assertIn('carr-native==1.0', lock)
        self.assertNotIn('carr-linux', lock)
        self.assertIn(hashlib.sha256(native.read_bytes()).hexdigest(), lock)

    def test_reject_linux_only(self):
        wheel(self.wheels, 'carr_native', 'cp311-cp311-manylinux_2_17_x86_64')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_reject_wrong_python(self):
        wheel(self.wheels, 'carr_native', 'cp313-cp313-win_amd64')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_reject_missing_dependency(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_reject_direct_url(self):
        self.req.write_text('carr-root @ https://example.invalid/root.whl\n')
        with self.assertRaises(ValueError):
            self.run_resolve()
        self.assertFalse(self.out.exists())

    def test_preserve_existing(self):
        self.out.write_bytes(b'old lock')
        with self.assertRaises(FileExistsError):
            self.run_resolve()
        self.assertEqual(self.out.read_bytes(), b'old lock')

    def test_real_universal_wheel(self):
        # A separately acquired publisher-verified wheel; tests never download.
        # The environment override makes this gate portable across Linux/Windows.
        provided = os.environ.get('CARR_TEST_PYPDF_WHEEL')
        fixture = Path(provided) if provided else Path('/usr/share/python-wheels/pip-25.1.1-py3-none-any.whl')
        if not fixture.is_file():
            self.skipTest('set CARR_TEST_PYPDF_WHEEL to the approved pypdf 7.1.1 wheel')
        if provided:
            self.assertEqual(fixture.name, 'pypdf-7.1.1-py3-none-any.whl')
            requirement = 'pypdf==7.1.1'
            expected = '04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1'
        else:
            requirement = 'pip==25.1.1'
            expected = '20568e5d750393b2a331d6b261886cbe5d62aca3f54d49a39815b7474d34cd9c'
        shutil.copyfile(fixture, self.wheels / fixture.name)
        self.req.write_text(requirement + '\n', encoding='utf-8')
        self.approve_fixture()
        # Independently recorded publisher/distro digest, not self-approved bytes.
        approved = json.loads(self.manifest.read_text(encoding='utf-8'))
        approved[fixture.name] = expected
        self.manifest.write_text(json.dumps(approved), encoding='utf-8')
        self.run_resolve()
        lock = self.out.read_text(encoding='utf-8')
        self.assertIn(requirement, lock)
        self.assertIn(hashlib.sha256(fixture.read_bytes()).hexdigest(), lock)


if __name__ == '__main__':
    unittest.main()
