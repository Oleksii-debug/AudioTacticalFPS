"""Qualify the installed, transferable library bundle without audio payload.

This gate supplements the complete local audio integration gate. It runs only
with the real Windows bundle interpreter and never substitutes source modules.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

SUITES = (
    'tests/e2e/test_semantic_runtime.py',
    'tests/e2e/test_import_worker.py',
    'tests/e2e/test_import_batches_ocr.py',
    'tests/e2e/test_notebook_research_workflow.py',
    'tests/e2e/test_epub_import.py',
    'tests/e2e/test_extended_document_workflows.py',
    'tests/reader/test_ebook_reader.py',
    'tests/e2e/test_catalog_insights_workflow.py',
    'tests/e2e/test_academic_workflows.py',
    'tests/e2e/test_academic_workflow.py',
    'tests/e2e/test_academic_document_export.py',
    'tests/e2e/test_result_presentation.py',
    'tests/e2e/test_advanced_research_search.py',
    'tests/search/test_advanced_search.py',
    'tests/ingest/test_import_metadata_shape.py',
)
MODULES = (
    'carr_library', 'carr_library.application', 'carr_library.semantic',
    'carr_library.extract.ocr_runtime', 'carr_library.search.advanced',
    'carr_library.runtime_settings', 'carr_library.audio_runtime',
)
MANIFESTS = (
    'bundle-manifest.json', 'models/minilm-manifest.json',
    'ocr/ocr-manifest.json', 'native-runtime/native-manifest.json',
)

def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def _origins(root):
    import importlib
    base = (root / '.venv' / 'Lib' / 'site-packages').resolve()
    source = Path(__file__).resolve().parents[1] / 'src'
    origins = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve()
        expected = base.joinpath(*name.split('.'))
        if path.name == '__init__.py':
            expected = expected / '__init__.py'
        else:
            expected = expected.with_suffix('.py')
        if path != expected:
            raise AssertionError('Module did not come from the installed package: ' + name)
        reviewed = source.joinpath(*name.split('.'))
        reviewed = (reviewed / '__init__.py' if path.name == '__init__.py'
                    else reviewed.with_suffix('.py'))
        if path.read_bytes() != reviewed.read_bytes():
            raise AssertionError('Installed module bytes differ from the reviewed source: ' + name)
        origins[name] = str(path)
    return origins

def _private_dlls(root):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    paths = {}
    for name in ('msvcp140.dll', 'msvcp140_1.dll'):
        module = kernel.GetModuleHandleW(name)
        if not module:
            raise AssertionError(name + ' was not exercised by the installed workflows.')
        actual = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(module, actual, len(actual))
        expected = root / 'native-runtime' / 'bin' / name
        if not 0 < length < len(actual) or not Path(actual.value).samefile(expected):
            raise AssertionError('Native dependency loaded outside the verified bundle: ' + name)
        paths[name] = str(expected)
    return paths

def run(bundle, evidence):
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Requires native Windows',
                          'manual_nvda': 'NOT_RUN'}), flush=True)
        return 77
    root = bundle.resolve(strict=True)
    source = Path(__file__).resolve().parents[1]
    evidence.mkdir(parents=True, exist_ok=True)
    interpreter = root / '.venv' / 'Scripts' / 'python.exe'
    if not Path(sys.executable).samefile(interpreter):
        raise AssertionError('Run with this bundle installed interpreter.')
    installer_path = source / 'packaging' / 'install_windows.py'
    if (root / 'install_windows.py').read_bytes() != installer_path.read_bytes():
        raise AssertionError('Bundle installer differs from the committed source.')
    installer = _module(installer_path, 'trusted_transfer_installer')
    transfer = _module(source / 'packaging' / 'create_delivery.py', 'trusted_transfer_policy')
    manifest_path = root / 'bundle-manifest.json'
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    transfer._require_transferable_audio_inventory(manifest)
    if not all(manifest.get(key) is True for key in
               ('bundled_python', 'bundled_model', 'bundled_ocr', 'bundled_native_runtime')):
        raise AssertionError('Required transferable components are missing.')
    installer.verify(root)
    from carr_library.audio_runtime import bundled_audio_tools
    from carr_library.native_runtime import activate_bundled_runtime
    if bundled_audio_tools(root) is not None:
        raise AssertionError('The transferable package contains an audio runtime.')
    handle = activate_bundled_runtime(bundle_root=root)
    if handle is None:
        raise AssertionError('The verified native runtime was not activated.')
    try:
        origins = _origins(root)
        import os
        import pytest
        os.environ['CARR_TEST_MODEL_DIR'] = str(root / 'models' / 'minilm')
        os.environ['CARR_OCR_DIR'] = str(root / 'ocr')
        os.environ['TESSDATA_PREFIX'] = str(root / 'ocr' / 'tessdata')
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        os.environ['PATH'] = os.pathsep.join((str(root / 'ocr'), os.environ.get('PATH', '')))

        class RejectExpectedFailures:
            expected_failures = False
            def pytest_runtest_logreport(self, report):
                if hasattr(report, 'wasxfail'):
                    self.expected_failures = True

        policy = RejectExpectedFailures()
        xml = evidence / 'transferable-bundle.xml'
        status = pytest.main(['-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                              '--import-mode=importlib', '--junitxml=' + str(xml), *SUITES],
                             plugins=[policy])
        cases = list(ET.parse(xml).iter('testcase'))
        failed = sum(any(c.find(kind) is not None for kind in ('failure', 'error')) for c in cases)
        skipped = sum(c.find('skipped') is not None for c in cases)
        expected = {Path(suite).stem for suite in SUITES}
        memberships = [{name for name in expected if name in c.get('classname', '').split('.')}
                       for c in cases]
        actual = set().union(*memberships) if memberships else set()
        if status != 0 or not cases or failed or skipped or policy.expected_failures or actual != expected or any(len(names) != 1 for names in memberships):
            raise AssertionError('Transferable installed workflows did not pass every declared suite without skips.')
        names = {c.get('name', '') for c in cases}
        required = {
            'test_actual_multilingual_ranker_and_complete_token_coverage',
            'test_scanned_pdf_requires_explicit_review_for_each_exact_page_before_publication',
            'test_notebook_search_fresh_navigation_and_backup_restore',
            'test_academic_reference_roundtrip_preserves_source_evidence',
            'test_academic_import_is_atomic_and_exports_preserve_metadata',
        }
        if not required <= names:
            raise AssertionError('Required actual model, OCR and source workflows are absent.')
        if _origins(root) != origins:
            raise AssertionError('Installed module origins changed during acceptance.')
        private_dlls = _private_dlls(root)
        installer.verify(root)
        if manifest_path.read_bytes() != manifest_raw or bundled_audio_tools(root) is not None:
            raise AssertionError('Transferable bundle identity changed during acceptance.')
        wheels = [name for name in manifest['files']
                  if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
        if len(wheels) != 1:
            raise AssertionError('Expected one sealed application wheel.')
        verifier = _module(source / 'packaging' / 'source_wheel.py', 'trusted_transfer_source')
        proof = verifier.verify_application_wheel(root / wheels[0], source.parent, manifest['source_commit'])
        if proof != json.loads((root / 'application-source.json').read_bytes()):
            raise AssertionError('Installed source proof differs from the committed wheel verification.')
        if proof['source_tree'] != manifest['source_tree'] or proof['wheel_sha256'] != manifest['files'][wheels[0]]:
            raise AssertionError('Wheel, source tree and bundle identity disagree.')
        destination = evidence / 'transferable-manifests'
        destination.mkdir(exist_ok=False)
        for name in MANIFESTS:
            shutil.copyfile(root / name, destination / Path(name).name)
        shutil.copyfile(root / 'application-source.json', destination / 'application-source.json')
        report = {
            'status': 'PASS', 'source_commit': manifest['source_commit'],
            'source_tree': manifest['source_tree'], 'bundle_manifest_sha256': _sha(manifest_path),
            'wheel': Path(wheels[0]).name, 'wheel_sha256': proof['wheel_sha256'],
            'passed': len(cases), 'failed': 0, 'skipped': 0, 'expected_failures': False,
            'suites': list(SUITES), 'origins': origins, 'installed_source_bytes_verified': True,
            'private_native_dlls': private_dlls,
            'manifest_unchanged': True, 'sealed_files_verified': True,
            'audio_payload': False, 'transfer_policy': 'PASS', 'manual_nvda': 'NOT_RUN',
        }
        (evidence / 'transferable-bundle.json').write_text(
            json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print(json.dumps(report, sort_keys=True), flush=True)
        return 0
    finally:
        handle.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    return run(args.bundle, args.evidence)

if __name__ == '__main__':
    raise SystemExit(main())
