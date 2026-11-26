import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('offline_pdf_smoke', ROOT / 'packaging' / 'offline_pdf_smoke.py')
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class OfflineSmokeTests(unittest.TestCase):
    def test_pdf_fts_without_socket_access(self):
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network denied')):
            result = SMOKE.probe()
        self.assertEqual(result['fts_page'], 2)
        self.assertEqual(result['synthetic_pdf_pages'], 2)

    def test_optional_ocr_absent(self):
        with patch.object(SMOKE.shutil, 'which', return_value=None):
            self.assertFalse(SMOKE.probe()['optional_tesseract_found'])

    def test_install_refuses_global_python(self):
        with patch.object(sys, 'prefix', sys.base_prefix), patch.object(SMOKE.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                SMOKE.offline_install(Path('.'))
            run.assert_not_called()

    def test_install_rejects_missing_directory(self):
        with tempfile.TemporaryDirectory() as root, patch.object(sys, 'prefix', 'synthetic-venv'), patch.object(SMOKE.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                SMOKE.offline_install(Path(root) / 'missing')
            run.assert_not_called()

    def test_pip_rejects_tampered_wheel(self):
        with tempfile.TemporaryDirectory(prefix='Wheel house Ž ') as root:
            wheelhouse = Path(root)
            (wheelhouse / 'pypdf-5.9.0-py3-none-any.whl').write_bytes(b'not a genuine wheel')
            completed = subprocess.run([
                sys.executable, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check',
                'install', '--dry-run', '--ignore-installed', '--no-index', '--no-deps',
                '--require-hashes', '--only-binary=:all:', '--no-cache-dir', '--find-links', root,
                '-r', str(SMOKE.LOCK),
            ], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn('DO NOT MATCH THE HASHES', completed.stderr.upper())


if __name__ == '__main__':
    unittest.main()
