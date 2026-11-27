"""Private runtime inventory/activation tests; synthetic DLLs are never loaded."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from carr_library import native_runtime as native


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / 'bundle'
        self.runtime = self.root / native.DIRECTORY
        self.runtime.mkdir(parents=True)
        self.addCleanup(patch.stopall)
        binary = bytearray(134)
        binary[:2] = b'MZ'
        struct.pack_into('<I', binary, 60, 128)
        binary[128:] = b'PE\0\0\x64\x86'
        self.binary = bytes(binary)
        pins = {name: (len(binary), hashlib.sha256(binary).hexdigest()) for name in native.DLLS}
        patch.object(native, 'DLLS', pins).start()
        source = {'selected_original_files': {f'Library/bin/{name}': {
            'destination': f'bin/{name}', 'size': size, 'sha256': sha}
            for name, (size, sha) in pins.items()}}
        for name in native.FILES:
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.binary if name.startswith('bin/') else b'Synthetic fixture\n')
        (self.runtime / 'provenance/source-lock.json').write_text(json.dumps(source))
        patch.object(native, 'SOURCE_LOCK_SHA256', digest(self.runtime / 'provenance/source-lock.json')).start()
        (self.root / 'README.txt').write_text('Synthetic installation fixture\n')
        self.write_inner()
        self.write_outer()

    def write_inner(self):
        self.inner = {'schema': 1, 'target': 'Windows x64',
                      'use_scope': 'private-local-installation',
                      'source_lock_sha256': native.SOURCE_LOCK_SHA256,
                      'files': {name: digest(self.runtime / name) for name in native.FILES}}
        (self.runtime / native.MANIFEST).write_text(json.dumps(self.inner))

    def write_outer(self, included=True, *, omit_flag=False):
        self.outer = {'schema': 1, 'target': 'Windows x64 / CPython 3.11',
                      'bundled_native_runtime': included,
                      'files': {'README.txt': digest(self.root / 'README.txt')}}
        if self.runtime.exists():
            self.outer['files'].update({p.relative_to(self.root).as_posix(): digest(p)
                                        for p in self.runtime.rglob('*') if p.is_file()})
        if omit_flag:
            del self.outer['bundled_native_runtime']
        (self.root / 'bundle-manifest.json').write_text(json.dumps(self.outer))

    def test_prepare_manifest_uses_lf_bytes(self):
        from io import BytesIO
        import importlib.util
        import tarfile
        from zipfile import ZipFile
        import zstandard

        spec = importlib.util.spec_from_file_location('prepare_native_runtime_lf_test',
            Path(__file__).resolve().parents[2] / 'packaging/prepare_native_runtime.py')
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        here = self.parent / 'producer'
        metadata = here / 'native-runtime'
        metadata.mkdir(parents=True)
        selected, buffer = {}, BytesIO()
        with tarfile.open(fileobj=buffer, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for destination in sorted(native.FILES):
                if not destination.startswith(('bin/', 'licenses/')):
                    continue
                data = (self.runtime / destination).read_bytes()
                original = 'Library/' + destination
                selected[original] = {'destination': destination, 'size': len(data),
                    'sha256': hashlib.sha256(data).hexdigest()}
                info = tarfile.TarInfo(original)
                info.size = len(data)
                archive.addfile(info, BytesIO(data))
        package = self.parent / 'synthetic.conda'
        with ZipFile(package, 'w') as archive:
            archive.writestr('pkg-synthetic.tar.zst',
                zstandard.ZstdCompressor().compress(buffer.getvalue()))
        lock = {'package': {'size': package.stat().st_size, 'sha256': digest(package)},
                'selected_original_files': selected}
        lock_path = metadata / 'source-lock.json'
        lock_path.write_bytes((json.dumps(lock, indent=2) + '\n').encode('utf-8'))
        (metadata / 'README.txt').write_bytes((self.runtime / 'README.txt').read_bytes())
        (metadata / 'pe-compatibility.json').write_bytes(b'{"fixture": true}\n')
        output = self.parent / 'prepared-native'
        with patch.object(producer, 'HERE', here), \
                patch.object(native, 'SOURCE_LOCK_SHA256', digest(lock_path)), \
                patch.object(sys, 'path', list(sys.path)):
            report = producer.prepare(package, output)
        raw = (output / native.MANIFEST).read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        self.assertNotIn(b'\r', raw)
        self.assertEqual(set(json.loads(raw)['files']), native.FILES)
        self.assertEqual(report['native_execution'], 'NOT_RUN')
        self.assertEqual(report['manifest_sha256'], hashlib.sha256(raw).hexdigest())

    def test_standalone_verification_reports_no_native_execution(self):
        report = native.verify_native_runtime(self.runtime)
        self.assertEqual(report['dlls'], ['msvcp140.dll', 'msvcp140_1.dll'])
        self.assertEqual(report['native_execution'], 'NOT_RUN')

    def test_source_mode_ignores_current_directory(self):
        prefix = self.parent / 'unbundled-python'
        prefix.mkdir()
        with patch.object(sys, 'prefix', str(prefix)), patch.object(sys, 'base_prefix', str(prefix)), \
                patch.object(os, 'getcwd', side_effect=AssertionError('cwd must not be used')):
            self.assertIsNone(native.activate_bundled_runtime())

    def test_declared_absence_and_legacy_absence_are_allowed(self):
        shutil.rmtree(self.runtime)
        for omit in (False, True):
            with self.subTest(legacy=omit):
                self.write_outer(False, omit_flag=omit)
                self.assertIsNone(native.activate_bundled_runtime(self.root))

    def test_declared_missing_runtime_fails(self):
        shutil.rmtree(self.runtime)
        with self.assertRaisesRegex(ValueError, 'missing'):
            native.activate_bundled_runtime(self.root)

    def test_false_flag_with_present_directory_fails(self):
        self.write_outer(False)
        with self.assertRaisesRegex(ValueError, 'Unannounced'):
            native.activate_bundled_runtime(self.root)

    def test_tamper_cannot_be_hidden_by_regenerating_manifests(self):
        path = self.runtime / 'bin/msvcp140.dll'
        changed = bytearray(path.read_bytes())
        changed[70] = 1
        path.write_bytes(changed)
        self.write_inner()
        self.write_outer()
        with self.assertRaisesRegex(ValueError, 'reviewed source'):
            native.activate_bundled_runtime(self.root)

    def test_unknown_dll_is_rejected(self):
        (self.runtime / 'bin/unknown.dll').write_bytes(self.binary)
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            native.verify_native_runtime(self.runtime)

    def test_changed_source_lock_is_rejected(self):
        (self.runtime / 'provenance/source-lock.json').write_text('{}')
        self.write_inner()
        self.write_outer()
        with self.assertRaisesRegex(ValueError, 'source lock changed'):
            native.activate_bundled_runtime(self.root)

    def test_symlinked_ancestor_is_rejected(self):
        alias = self.parent / 'linked-bundle'
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Host does not permit directory symlinks: ' + str(exc))
        with self.assertRaisesRegex(ValueError, 'ordinary'):
            native.verify_native_runtime(alias / native.DIRECTORY)

    def test_relative_and_parent_traversal_paths_are_rejected(self):
        for path in ('native-runtime', self.runtime / '..' / native.DIRECTORY):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'absolute'):
                    native.verify_native_runtime(path)

    def test_outer_manifest_binds_exact_inner_identity(self):
        self.outer['files'][native.DIRECTORY + '/' + native.MANIFEST] = 'a' * 64
        (self.root / 'bundle-manifest.json').write_text(json.dumps(self.outer))
        with self.assertRaisesRegex(ValueError, 'enclosing installation'):
            native.activate_bundled_runtime(self.root)

    def test_duplicate_keys_and_bool_schema_are_rejected(self):
        path = self.root / 'bundle-manifest.json'
        raw = path.read_text()
        path.write_text('{"schema": 1,' + raw[1:])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            native.activate_bundled_runtime(self.root)
        self.outer['schema'] = True
        path.write_text(json.dumps(self.outer))
        with self.assertRaisesRegex(ValueError, 'manifest'):
            native.activate_bundled_runtime(self.root)

    def test_discovery_uses_prefix_ancestors(self):
        prefix = self.root / '.venv'
        base = self.root / 'runtime'
        with patch.object(sys, 'prefix', str(prefix)), patch.object(sys, 'base_prefix', str(base)):
            self.assertEqual(native._discover(), self.root)

    def test_discovery_ignores_distant_ancestor_manifest(self):
        prefix = self.root / 'unrelated/deeper/python'
        with patch.object(sys, 'prefix', str(prefix)), patch.object(sys, 'base_prefix', str(prefix)):
            self.assertIsNone(native._discover())

    def test_mock_windows_loader_handles_are_independent(self):
        desktop_handle, diagnostic_handle = Mock(), Mock()
        loader = Mock(side_effect=[desktop_handle, diagnostic_handle])
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(native, '_check_host'), patch.object(native, '_check_loaded_host'), \
                patch.object(os, 'add_dll_directory', loader, create=True):
            self.assertIs(native.activate_bundled_runtime(self.root), desktop_handle)
            self.assertIs(native.activate_bundled_runtime(self.root), diagnostic_handle)
            diagnostic_handle.close()
            diagnostic_handle.close.assert_called_once()
            desktop_handle.close.assert_not_called()
            desktop_handle.close()
            desktop_handle.close.assert_called_once()
            self.assertEqual(loader.call_count, 2)
            loader.assert_called_with(str(self.runtime / 'bin'))

    def test_pinned_host_runtime_is_required_without_overwriting_it(self):
        host = self.root / 'runtime'
        host.mkdir()
        pins = {}
        for name in native.HOST_DLLS:
            path = host / name
            path.write_bytes(('Host fixture ' + name).encode())
            pins[name] = digest(path)
        with patch.object(native, 'HOST_DLLS', pins), patch.object(sys, 'base_prefix', str(host)):
            native._check_host(self.root)
            (host / 'vcruntime140.dll').write_bytes(b'Changed host')
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                native._check_host(self.root)


if __name__ == '__main__':
    unittest.main()
