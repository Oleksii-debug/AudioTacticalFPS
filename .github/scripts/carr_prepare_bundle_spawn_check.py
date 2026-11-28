"""Prepare a bounded CI-only command-argument and bundled-spawn regression.

The original full-media wrapper is transported as data, not executed here.
The ordinary MAIN job remains the full-media/OCR/model acceptance gate.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import pathlib
import runpy
import sys
import xml.etree.ElementTree as ET

MAX_WORKFLOW_BYTES = 200 * 1024
COMMAND_LIMIT_UTF16 = 32767
ARGUMENT_RESERVE_UTF16 = 2048
OPEN = "          $runInstalledTests = @'\n"
CLOSE = "\n          '@"
ENTRY = r"""source_wrapper = r'''__CARR_SOURCE_WRAPPER__'''
import hashlib, os, pathlib, types
checker_path = pathlib.Path(os.environ['CARR_BUNDLE_SPAWN_CHECKER'])
checker_bytes = checker_path.read_bytes()
assert len(checker_bytes) <= 64 * 1024
assert hashlib.sha256(checker_bytes).hexdigest() == os.environ['CARR_BUNDLE_SPAWN_CHECKER_SHA256']
checker = types.ModuleType('carr_bundle_spawn_receiving')
checker.__file__ = str(checker_path)
exec(compile(checker_bytes, str(checker_path), 'exec'), checker.__dict__)
checker.run_focused(source_wrapper)
"""


def run_focused(source_wrapper: str) -> None:
    report = pathlib.Path(os.environ['CARR_BUNDLE_SPAWN_REPORT'])
    proof = dict(status='FAILED', scope='CORE bundled interpreter and installed broker spawn entry',
                 full_media_wrapper_executed=False, model_inference='NOT_RUN',
                 ocr='NOT_RUN', audio_devices='NOT_RUN', nvda='NOT_RUN')
    def save():
        report.write_text(json.dumps(proof, sort_keys=True, indent=2) + '\n', encoding='utf-8')

    def run():
        import carr_library
        from carr_library.operations import ResourceBroker
        from multiprocessing.spawn import get_preparation_data
        assert sys.platform == 'win32' and sys.flags.isolated and sys.dont_write_bytecode
        assert sys.flags.optimize == 0
        assert sys.argv[0] == '-c'
        at = sys.orig_argv.index('-c')
        assert sys.orig_argv[1:at] == ['-I', '-B']
        code = sys.orig_argv[at + 1]
        assert hashlib.sha256(code.encode('utf-8')).hexdigest() == os.environ['CARR_BUNDLE_SPAWN_CODE_SHA256']
        assert hashlib.sha256(source_wrapper.encode('utf-8')).hexdigest() == os.environ['CARR_BUNDLE_SPAWN_WRAPPER_SHA256']
        assert len(source_wrapper.encode('utf-16-le')) // 2 == int(os.environ['CARR_BUNDLE_SPAWN_WRAPPER_UNITS'])
        root = pathlib.Path(sys.argv[1]).resolve(strict=True)
        checkout = pathlib.Path(os.environ['GITHUB_WORKSPACE']).resolve(strict=True) / 'carr-library'
        assert pathlib.Path.cwd().resolve(strict=True) == checkout
        assert pathlib.Path(sys.executable).resolve(strict=True) == root / '.venv' / 'Scripts' / 'python.exe'
        assert pathlib.Path(sys._base_executable).resolve(strict=True) == root / 'runtime' / 'python.exe'
        assert pathlib.Path(sys.prefix).resolve(strict=True) == root / '.venv'
        package = pathlib.Path(carr_library.__file__).resolve(strict=True)
        package_root = package.parent
        assert package.is_relative_to(root / '.venv') and not package.is_relative_to(checkout)
        owners = [dist for dist in metadata.distributions()
                  if (dist.metadata.get('Name') or '').lower().replace('_', '-') == 'universal-research-library'
                  and pathlib.Path(dist.locate_file('carr_library/__init__.py')).resolve() == package]
        assert len(owners) == 1
        record = {str(item): item for item in owners[0].files or ()}
        def inventory():
            source = {item.relative_to(checkout / 'src' / 'carr_library').as_posix():
                      hashlib.sha256(item.read_bytes()).hexdigest()
                      for item in (checkout / 'src' / 'carr_library').rglob('*.py')}
            installed = {}
            for item in package_root.rglob('*.py'):
                assert not item.is_symlink() and item.resolve(strict=True).is_relative_to(package_root)
                name = item.relative_to(package_root).as_posix()
                raw = item.read_bytes()
                digest = hashlib.sha256(raw)
                declared = record['carr_library/' + name]
                assert declared.hash is not None and declared.hash.mode == 'sha256'
                assert declared.hash.value == base64.urlsafe_b64encode(digest.digest()).rstrip(b'=').decode('ascii')
                installed[name] = digest.hexdigest()
            assert source and installed == source
            return installed
        before = inventory()
        broker_path = pathlib.Path(sys.modules[ResourceBroker.__module__].__file__).resolve(strict=True)
        assert broker_path.is_relative_to(package_root)
        assert broker_path.relative_to(package_root).as_posix() in before
        raw_manifest = (root / 'bundle-manifest.json').read_bytes()
        manifest_sha = hashlib.sha256(raw_manifest).hexdigest()
        assert manifest_sha == os.environ['CARR_BUNDLE_SPAWN_MANIFEST_SHA256']
        manifest = json.loads(raw_manifest)
        assert manifest['source_commit'] == os.environ['GITHUB_SHA']
        assert manifest['bundled_python'] is True and manifest['bundled_native_runtime'] is True
        assert all(manifest.get(name) is False for name in ('bundled_model', 'bundled_ocr', 'bundled_audio'))
        assert hashlib.sha256((root / 'runtime' / 'python.exe').read_bytes()).hexdigest() == manifest['files']['runtime/python.exe']
        spec = importlib.util.spec_from_file_location('spawn_bundle_verifier', root / 'install_windows.py')
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        verifier.verify(root)
        assert (root / '.venv' / '.installation-complete').is_file()
        assert getattr(sys.modules['__main__'], '__file__', None) is None
        preparation = get_preparation_data('bundle-spawn-receiving')
        assert 'init_main_from_path' not in preparation and 'init_main_from_name' not in preparation
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCommandLineW.argtypes = []
        kernel.GetCommandLineW.restype = wintypes.LPWSTR
        actual_units = len(kernel.GetCommandLineW().encode('utf-16-le')) // 2 + 1
        assert actual_units <= 32767
        names = ('test_actual_spawned_process_crash_releases_without_pid_reaping',
                 'test_actual_process_contention_has_no_overlapping_admission')
        expected = ['-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                    '--import-mode=importlib', '--junitxml=' + os.environ['CARR_BUNDLE_SPAWN_JUNIT'],
                    *['tests/operations/test_resources.py::' + name for name in names]]
        assert sys.argv[2:] == expected
        assert metadata.version('pytest') == '9.0.2'
        proof.update(phase='before-original-resource-cases', source_commit=manifest['source_commit'],
                     bundled_python=sys.executable, base_interpreter=sys._base_executable,
                     package_origin=str(package),
                     broker_origin=str(pathlib.Path(sys.modules[ResourceBroker.__module__].__file__).resolve()),
                     python_version=sys.version, wrapper_data_sha256=os.environ['CARR_BUNDLE_SPAWN_WRAPPER_SHA256'],
                     wrapper_data_utf16_units=int(os.environ['CARR_BUNDLE_SPAWN_WRAPPER_UNITS']),
                     code_sha256=os.environ['CARR_BUNDLE_SPAWN_CODE_SHA256'],
                     command_line_utf16_units_including_nul=actual_units,
                     checker_sha256=os.environ['CARR_BUNDLE_SPAWN_CHECKER_SHA256'],
                     powershell_conservative_command_units=int(os.environ['CARR_BUNDLE_SPAWN_BOUND']),
                     powershell_version=os.environ['CARR_BUNDLE_SPAWN_PS_VERSION'],
                     powershell_argument_mode=os.environ['CARR_BUNDLE_SPAWN_PS_ARGUMENT_MODE'],
                     argv_bundle_index=1, pytest_argv_start=2, no_main_path_reconstruction=True,
                     application_python_modules=len(before), bundle_manifest_sha256=manifest_sha)
        source_files = ('tests/operations/test_resources.py',
                        'tests/operations/carr_resource_process_fixture.py')
        source_hashes = {}
        for name in source_files:
            path = checkout / name
            assert path.stat().st_size <= 128 * 1024
            source_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        proof['unchanged_case_and_spawn_helper_sha256'] = source_hashes
        save()
        policy_path = checkout / 'scripts' / 'ci_check.py'
        assert policy_path.stat().st_size <= 128 * 1024
        policy = runpy.run_path(str(policy_path))['PYTEST_CHILD']
        assert isinstance(policy, str) and policy
        proof['offline_and_xfail_policy_sha256'] = hashlib.sha256(policy.encode('utf-8')).hexdigest()
        save()
        assert getattr(sys.modules['__main__'], '__file__', None) is None
        original_argv = list(sys.argv)
        scope = {'__name__': '__main__'}
        try:
            # The unchanged existing policy consumes pytest arguments from argv[1:].
            # The actual native invocation and its argv[1]/argv[2:] proof above stay intact.
            sys.argv = [original_argv[0], *original_argv[2:]]
            try:
                exec(compile(policy, str(policy_path) + ':PYTEST_CHILD', 'exec'), scope)
            except SystemExit as stopped:
                status = stopped.code
            else:
                raise AssertionError('The existing pytest policy did not return an exit status.')
        finally:
            sys.argv = original_argv
        cases = list(ET.parse(os.environ['CARR_BUNDLE_SPAWN_JUNIT']).iter('testcase'))
        assert status == 0 and scope['policy'].seen is False
        assert len(cases) == 2 and {case.get('name') for case in cases} == set(names)
        assert all(case.get('classname', '').rsplit('.', 1)[-1] == 'test_resources' for case in cases)
        assert all(not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases)
        assert getattr(sys.modules['__main__'], '__file__', None) is None
        assert inventory() == before
        assert {name: hashlib.sha256((checkout / name).read_bytes()).hexdigest()
                for name in source_files} == source_hashes
        assert hashlib.sha256((root / 'bundle-manifest.json').read_bytes()).hexdigest() == manifest_sha
        verifier.verify(root)
        report.with_name('bundle-spawn-python-inventory.json').write_text(
            json.dumps(before, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        proof.update(status='PASS', phase='completed', original_real_process_cases=list(names),
                     passed=2, failed=0, errors=0, skipped=0, expected_failures=False,
                     application_namespace_record_and_source_unchanged=True, sealed_files_verified_after=True)
    save()
    try:
        run()
    except BaseException as error:
        proof.update(status='FAILED', error_type=type(error).__name__, error=str(error))
        save()
        raise
    save()
    print(json.dumps(proof, sort_keys=True, ensure_ascii=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow', type=Path, required=True)
    parser.add_argument('--code', type=Path, required=True)
    parser.add_argument('--literal', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    with args.workflow.open('rb') as stream:
        raw = stream.read(MAX_WORKFLOW_BYTES + 1)
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise ValueError('Workflow exceeds the reviewed source bound.')
    text = raw.decode('utf-8')
    if '\r' in text or text.count(OPEN) != 1:
        raise ValueError('Expected the exact LF source wrapper declaration.')
    body = text.split(OPEN, 1)[1].split(CLOSE, 1)[0]
    lines = body.split('\n')
    if any(line and not line.startswith('          ') for line in lines):
        raise ValueError('Source wrapper indentation changed.')
    wrapper = '\n'.join(line[10:] if line else '' for line in lines)
    invocation = '& $python -I -B -c $runInstalledTests $bundle '
    if text.count(invocation) != 1 or '$runInstalledTests | & $python' in text:
        raise ValueError('MAIN must use its reviewed direct command-string entry.')
    if ('"' in wrapper or "\0" in wrapper or "'''" in wrapper
            or any(line.strip() == "'@" for line in wrapper.splitlines())):
        raise ValueError('Source wrapper no longer has the reviewed quoting shape.')
    if 'root = pathlib.Path(sys.argv[1]).resolve()' not in wrapper:
        raise ValueError('Bundle argv contract changed.')
    if 'pytest.main([*sys.argv[2:], *contents_suites], plugins=[ImmediateFailures()])' not in wrapper:
        raise ValueError('Pytest argv contract changed.')
    code = ENTRY.replace('__CARR_SOURCE_WRAPPER__', wrapper)
    code_units = len(code.encode('utf-16-le')) // 2
    if '"' in code or '\0' in code or code_units + ARGUMENT_RESERVE_UTF16 > COMMAND_LIMIT_UTF16:
        raise ValueError('Focused argument exceeds its conservative Windows command bound.')
    compile(wrapper, '<unchanged-main-wrapper-data>', 'exec')
    compile(code, '<bundle-spawn-focused-entry>', 'exec')
    literal = "$runInstalledTests = @'\n" + wrapper + "\n'@\n"
    with args.code.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(code)
    with args.literal.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(literal)
    result = dict(status='PREPARED_NOT_EXECUTED',
                  workflow_sha256=hashlib.sha256(raw).hexdigest(),
                  checker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  wrapper_sha256=hashlib.sha256(wrapper.encode('utf-8')).hexdigest(),
                  wrapper_utf16_units=len(wrapper.encode('utf-16-le')) // 2,
                  wrapper_representation='Deindented source body; neither opening nor closing separator newline.',
                  code_sha256=hashlib.sha256(code.encode('utf-8')).hexdigest(),
                  code_utf16_units=code_units, reserved_other_argument_units=ARGUMENT_RESERVE_UTF16,
                  command_limit_including_nul=COMMAND_LIMIT_UTF16,
                  full_media_wrapper_executed=False)
    with args.report.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps(result, sort_keys=True))

if __name__ == '__main__':
    main()
