"""Actual uv subprocess tests, synthetic wheel metadata; no Windows binary execution."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
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
        wheel(self.wheels, 'carr_root', 'py3-none-any', ["carr_native==1.0; sys_platform == 'win32'", "carr_linux==1.0; sys_platform == 'linux'"])

    def run_resolve(self):
        mod.resolve(self.req, self.wheels, self.out)

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
        # Explicitly supplied distro wheel, not downloaded or executed.
        fixture = Path('/usr/share/python-wheels/pip-25.1.1-py3-none-any.whl')
        if not fixture.is_file():
            self.skipTest('local distro pip wheel not available')
        shutil.copyfile(fixture, self.wheels / fixture.name)
        self.req.write_text('pip==25.1.1\n', encoding='utf-8')
        self.run_resolve()
        lock = self.out.read_text(encoding='utf-8')
        self.assertIn('pip==25.1.1', lock)
        self.assertIn(hashlib.sha256(fixture.read_bytes()).hexdigest(), lock)


if __name__ == '__main__':
    unittest.main()
