"""Require installed synthetic protocol tests with only numeric loopback sockets.

The workflow also blocks non-loopback traffic for both selected Python binaries,
including subprocesses. These fixtures do not use external accounts or models.
"""
import argparse
from collections import Counter
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import xml.etree.ElementTree as ET


SUITES = (
    'tests/assistant/test_local_planning_loopback.py',
    'tests/assistant/test_providers.py',
    'tests/assistant/test_translation_v3_loopback.py',
)
EXPECTED_CASES = {
    'test_local_planning_loopback': 12,
    'test_providers': 28,
    'test_translation_v3_loopback': 4,
}
MODULES = (
    'bootstrap', 'carr_library', 'carr_library.assistant.llm',
    'carr_library.assistant.service', 'carr_library.assistant.profiles',
    'carr_library.assistant.providers', 'carr_library.assistant.translation',
    'carr_library.assistant.translation_cli',
)


def loopback_only():
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def address(value):
        if not isinstance(value, tuple) or len(value) < 2 or value[0] != '127.0.0.1':
            raise OSError('Only the owned numeric 127.0.0.1 fixture is allowed')

    def guarded_connect(stream, value):
        address(value)
        return connect(stream, value)

    def guarded_connect_ex(stream, value):
        address(value)
        return connect_ex(stream, value)

    def guarded_resolve(host, port, *args, **kwargs):
        address((host, port))
        return getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_resolve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--junit', required=True, type=Path)
    args = parser.parse_args()
    root = args.bundle.resolve(strict=True)
    checkout = Path.cwd().resolve()
    for name in SUITES:
        if not (checkout / name).is_file():
            raise RuntimeError('Required protocol suite is missing: ' + name)
    manifest_bytes = (root / 'bundle-manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    assert all(manifest.get('bundled_' + name) is True
               for name in ('native_runtime', 'native_launcher', 'audio', 'citation_runtime'))
    assert Path(sys.executable).resolve().is_relative_to(root / '.venv')
    from carr_library.native_runtime import activate_bundled_runtime
    runtime_handle = activate_bundled_runtime(bundle_root=root)
    assert runtime_handle is not None

    def origins():
        result = {}
        for name in MODULES:
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve(strict=True)
            assert path.is_relative_to(root / '.venv') and not path.is_relative_to(checkout), (name, path)
            result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
        return result

    before = origins()
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ.pop('PYTEST_ADDOPTS', None)
    os.environ.pop('PYTEST_PLUGINS', None)
    loopback_only()
    import pytest

    class RejectExpectedFailures:
        seen = False

        def pytest_runtest_logreport(self, report):
            if hasattr(report, 'wasxfail'):
                self.seen = True

    policy = RejectExpectedFailures()
    args.junit.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = pytest.main([
            '-q', '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
            '--import-mode=importlib', '--junitxml=' + str(args.junit), *SUITES,
        ], plugins=[policy])
        cases = list(ET.parse(args.junit).iter('testcase')) if args.junit.exists() else []
        failed = sum(any(case.find(tag) is not None for tag in ('failure', 'error')) for case in cases)
        skipped = sum(case.find('skipped') is not None for case in cases)
        coverage = Counter(case.get('classname', '').rsplit('.', 1)[-1] for case in cases)
        after = origins()
        ok = (status == 0 and cases and failed == skipped == 0 and not policy.seen
              and dict(coverage) == EXPECTED_CASES
              and before == after and (root / 'bundle-manifest.json').read_bytes() == manifest_bytes)
        result = dict(schema=1, status='PASS' if ok else 'FAIL', passed=len(cases) - failed - skipped,
                      failed=failed, skipped=skipped, pytest_exit=int(status),
                      expected_failures=policy.seen, suites=dict(coverage),
                      expected_suite_cases=EXPECTED_CASES, origins=after,
                      source_commit=manifest['source_commit'],
                      bundle_manifest_sha256=sha256(manifest_bytes).hexdigest(),
                      network='Owned numeric 127.0.0.1 synthetic fixtures only',
                      external_provider='NOT_RUN', actual_model='NOT_RUN', nvda='NOT_RUN')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result), flush=True)
        return 0 if ok else 1
    finally:
        runtime_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
