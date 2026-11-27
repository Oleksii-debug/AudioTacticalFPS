"""Require the existing canonical planner to advertise only its registered tools."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'carr-library'
OUT = ROOT / 'ci-planner-transcription-results'
CANONICAL = 'e69ba2403515f21509686d66f4bc8b3673756d81'
HERE = Path(__file__).resolve().parent
TARGET = APP / 'src/carr_library/assistant/llm.py'
TEST = 'tests/assistant/test_supplied_transcription.py'
POSITIVE = TEST + '::test_local_planner_advertises_registered_transcription_and_only_proposes'
SUITES = (TEST, 'tests/assistant/test_local_planning.py',
          'tests/assistant/test_local_planning_loopback.py')
EXPECTED_SHA256 = {
    'src/carr_library/assistant/llm.py': 'ad72db3072db609be5671d80166aa5ad7dc47e29aead559ca175e8fd83a42644',
    TEST: 'b8d0906ec7ce9fbface4244d8811400998d808beb2f77d4cc6f20ea7989dce3c',
}
BASELINE_BLOB = 'ee73d023bc8bb8932239a4e569f6a5e812dcf1a5'
UNCHANGED = ('src/carr_library/assistant/models.py',
             'src/carr_library/assistant/service.py',
             'src/carr_library/assistant/planner.py',
             'src/carr_library/integrations/supplied_transcription.py',
             'tests/assistant/test_local_planning.py',
             'tests/assistant/test_local_planning_loopback.py')

def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()

def blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()

def snapshot():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((APP / 'src').rglob('*')) if p.is_file()
            and '__pycache__' not in p.parts and not p.name.endswith('.pyc')}

def run(label, selectors, timeout):
    log = OUT / (label + '.log')
    xml = OUT / (label + '.xml')
    command = [sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
               '--import-mode=importlib', '-o', 'xfail_strict=true',
               '--junitxml=' + str(xml), *selectors]
    env = dict(os.environ, PYTHONPATH=str(APP / 'src'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    started = time.monotonic()
    with log.open('wb') as stream:
        result = subprocess.run(command, cwd=APP, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    # This proof contains only public synthetic fixtures; retain a bounded
    # diagnostic in the job log as well as the complete artifact.
    if result.returncode:
        print(log.read_text(encoding='utf-8', errors='replace')[-18000:], flush=True)
    return result.returncode, time.monotonic() - started, list(ET.parse(xml).iter('testcase'))

def main():
    OUT.mkdir()
    identities = {}
    for relative, expected in EXPECTED_SHA256.items():
        raw = (APP / relative).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected, relative
        compile(raw, relative, 'exec', dont_inherit=True)
        identities[relative] = dict(sha256=expected, git_blob=blob(raw))
    for relative in UNCHANGED:
        raw = (APP / relative).read_bytes()
        expected = git('rev-parse', CANONICAL + ':carr-library/' + relative)
        assert blob(raw) == expected, 'Existing authority contract changed: ' + relative
        identities[relative] = dict(git_blob=expected, sha256=hashlib.sha256(raw).hexdigest())
    baseline = (HERE / 'baseline-llm.py').read_bytes()
    assert blob(baseline) == BASELINE_BLOB
    evidence = dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'),
        canonical=CANONICAL, files=identities, python=sys.version,
        executable=sys.executable, real_engine='NOT_RUN', installed='NOT_RUN', nvda='NOT_RUN')
    (OUT / 'source-identity.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    before = snapshot()
    fixed = TARGET.read_bytes()
    try:
        TARGET.write_bytes(baseline)
        code, elapsed, cases = run('baseline', [POSITIVE], 90)
        assert code == 1 and len(cases) == 1
        assert not any(c.find(tag) is not None for c in cases for tag in ('error', 'skipped'))
        failure = cases[0].find('failure')
        assert failure is not None
        detail = failure.text or ''
        assert 'audio.transcribe' in detail and 'advertised == TOOLS' in detail
        evidence['baseline'] = dict(status='EXPECTED_NEGATIVE_CONFIRMED', failed=1, seconds=elapsed)
        print(json.dumps(evidence['baseline']), flush=True)
    finally:
        TARGET.write_bytes(fixed)
        assert TARGET.read_bytes() == fixed
    assert snapshot() == before
    code, elapsed, cases = run('fixed', SUITES, 300)
    assert code == 0 and cases
    assert not any(c.find(tag) is not None for c in cases for tag in ('failure', 'error', 'skipped'))
    assert all(any(c.get('classname', '').endswith(Path(s).stem) for c in cases) for s in SUITES)
    assert len([c for c in cases if c.get('name', '').startswith(
        'test_local_transcription_proposal_retains_existing_authority_boundaries[')]) == 5
    assert snapshot() == before
    evidence['fixed'] = dict(status='PASS', cases=len(cases), suites=list(SUITES),
                             seconds=elapsed, source_unchanged=True)
    (OUT / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(evidence['fixed']), flush=True)

if __name__ == '__main__':
    main()
