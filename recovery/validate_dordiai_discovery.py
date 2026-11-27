"""Execute the unchanged real-child cancellation regression on pinned public sources."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'carr-library'
OUT = ROOT / 'ci-dordiai-results'
SHARED = '8c981c7e947f8d144c8706b5ea022990517c7dd9'
EXPECTED = {
    "carr-library/src/carr_library/integrations/dordiai_speech.py": "b042c9f31e65aaae4819a1ec40422895cf8be9ff",
    "carr-library/src/carr_library/speech/dordiai.py": "3d151356b58c0a7f3b3cfc711f427afdc6638a68",
    "carr-library/src/carr_library/runtime_settings.py": "1f5687e3f65a804634772d93f10f7cfbdc0a957a",
    "carr-library/src/carr_library/speech/runtime.py": "9214d398a237b583a71e0cdf86d50b018ac26423",
    "carr-library/tests/integrations/test_dordiai_speech.py": "a428c53632dd9246553cb9412938fb5cb7f10165",
    "carr-library/tests/integrations/test_dordiai_canonical_convergence.py": "55c732d757b60518e29f235b2509e1d18d8b4acf",
    "recovery/dordiai-discovery-baseline.py": "ece549fa8ff660fa2285eb8a8ea6c3a5151bdb7f",
    "carr-library/src/carr_library/integrations/dordiai_voices.py": "f68b6f9a8abe90640a05235c68b53dff3a158eae",
    "carr-library/tests/integrations/test_dordiai_voices.py": "f58db339a72a4e57a63e9a9950098c41083680c2",
    "carr-library/tests/integrations/test_dordiai_probe_cancellation.py": "52db2c928f9f51f19f90e9de8859fd5bdf9b0c7e"
}
SUITES = (
    'tests/integrations/test_dordiai_canonical_convergence.py',
    'tests/integrations/test_dordiai_speech.py',
    'tests/integrations/test_dordiai_voices.py',
    'tests/integrations/test_dordiai_probe_cancellation.py',
)
TARGET = APP / 'src/carr_library/integrations/dordiai_speech.py'

def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()

def blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()

def snapshot():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((APP / 'src').rglob('*')) if p.is_file()
            and '__pycache__' not in p.parts and not p.name.endswith('.pyc')}

def cases(path):
    return list(ET.parse(path).iter('testcase'))

def run(label, selectors, timeout):
    output = OUT / (label + '.xml')
    command = [sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider',
        '-q', '--import-mode=importlib', '-o', 'xfail_strict=true',
        '--junitxml=' + str(output), *selectors]
    env = dict(os.environ, PYTHONPATH=str(APP / 'src'), PYTHONDONTWRITEBYTECODE='1',
        PYTHONUTF8='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    env.pop('CARR_TEST_SPEECH_RUNTIME_SOURCE', None)
    started = time.monotonic()
    with (OUT / (label + '.log')).open('wb') as log:
        result = subprocess.run(command, cwd=APP, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout)
    return result.returncode, time.monotonic() - started, cases(output)

def main():
    OUT.mkdir()
    identities = []
    for path, expected in EXPECTED.items():
        raw = (ROOT / path).read_bytes()
        actual = blob(raw)
        if actual != expected:
            raise ValueError('Selected public source changed: ' + path)
        if path.endswith('.py'):
            compile(raw, path, 'exec', dont_inherit=True)
        identities.append(dict(path=path, git_blob=actual, sha256=hashlib.sha256(raw).hexdigest()))
    io_path = 'carr-library/src/carr_library/speech/_io.py'
    assert git('rev-parse', SHARED + ':' + io_path) == blob((ROOT / io_path).read_bytes())
    evidence = dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'),
        shared_parent=SHARED, files=identities, python=sys.version, executable=sys.executable,
        installed='NOT_RUN', nvda='NOT_RUN', real_voice_or_model='NOT_RUN',
        scope='Actual synthetic-worker processes through the existing public provider')
    (OUT / 'source-identity.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    before = snapshot()
    fixed = TARGET.read_bytes()
    baseline = (ROOT / 'recovery/dordiai-discovery-baseline.py').read_bytes()
    try:
        TARGET.write_bytes(baseline)
        assert blob(TARGET.read_bytes()) == EXPECTED['recovery/dordiai-discovery-baseline.py']
        status, elapsed, tests = run('baseline',
            [SUITES[0] + '::test_provider_cold_discovery_cancellation_closes_owned_child'], 180)
        caller = [t for t in tests if t.get('name', '').endswith('[caller]')]
        provider = [t for t in tests if t.get('name', '').endswith('[provider]')]
        assert status == 1 and len(tests) == 2 and len(caller) == len(provider) == 1
        assert not any(t.find(tag) is not None for t in tests for tag in ('error', 'skipped'))
        assert caller[0].find('failure') is None
        failure = provider[0].find('failure')
        assert failure is not None
        detail = failure.text or ''
        assert 'DordiaiTimeout' in detail and 'InterruptedError' in detail
        evidence['baseline'] = dict(status='EXPECTED_NEGATIVE_CONFIRMED', passed=1,
                                     failed=1, seconds=elapsed)
        print(json.dumps(evidence['baseline']), flush=True)
    finally:
        TARGET.write_bytes(fixed)
        assert TARGET.read_bytes() == fixed
    assert snapshot() == before
    status, elapsed, tests = run('fixed', SUITES, 600)
    names = [Path(p).stem for p in SUITES]
    assert status == 0 and tests
    assert not any(t.find(tag) is not None for t in tests for tag in ('failure', 'error', 'skipped'))
    assert all(any(t.get('classname', '').endswith(name) for t in tests) for name in names)
    assert snapshot() == before
    evidence['fixed'] = dict(status='PASS', cases=len(tests), suites=names,
                             seconds=elapsed, source_unchanged=True)
    (OUT / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(evidence['fixed']), flush=True)

if __name__ == '__main__':
    main()
