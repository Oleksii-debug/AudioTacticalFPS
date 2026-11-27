"""Run the same narrow Windows helper regressions from source and installation."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
SELECTOR = 'tests/security/test_standard_system_helper.py'
MODULE = 'tests.security.test_standard_system_helper'
BOOTSTRAP = r"""
from hashlib import sha256
import importlib,json,pathlib,sys
project=pathlib.Path(sys.argv[1]).resolve()
mode,report=sys.argv[2:4]
if mode=='source':
    sys.path.insert(0,str(project/'src'))
import carr_library
package=pathlib.Path(carr_library.__file__).resolve().parent
if mode=='source':
    assert package==project/'src'/'carr_library', package
else:
    assert package.is_relative_to(pathlib.Path(sys.prefix).resolve()), package
    assert not package.is_relative_to(project/'src'), package
names=('carr_library.security.standard_windows','carr_library.security._files')
expected={}
for name in names:
    module=importlib.import_module(name)
    path=pathlib.Path(module.__file__).resolve()
    source=project/'src'/pathlib.Path(*name.split('.')).with_suffix('.py')
    assert path.is_relative_to(package), (name,path,package)
    expected[name]=sha256(source.read_bytes()).hexdigest()
    assert sha256(path.read_bytes()).hexdigest()==expected[name], name
import pytest
status=pytest.main(['-q','--import-mode=importlib','-p','no:cacheprovider',
    '-o','pythonpath=','--junitxml='+report,'tests/security/test_standard_system_helper.py'])
for name,digest in expected.items():
    path=pathlib.Path(sys.modules[name].__file__).resolve()
    assert path.is_relative_to(package), (name,path,package)
    assert sha256(path.read_bytes()).hexdigest()==digest, name
for name,module in tuple(sys.modules.items()):
    if (name=='carr_library' or name.startswith('carr_library.')) and getattr(module,'__file__',None):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(package), name
print(json.dumps({'mode':mode,'package_origin':str(package),'tested_modules_sha256':expected}),flush=True)
raise SystemExit(status)
"""


def main():
    results = PROJECT / 'ci-results'
    results.mkdir(exist_ok=True)
    oracle = json.loads((PROJECT / 'scripts/v3_acceptance_oracles.json').read_text(encoding='utf-8'))
    expected = Counter(oracle['source'][MODULE])
    if expected != Counter(oracle['installed'][MODULE]) or sum(expected.values()) != 16:
        raise AssertionError('The exact protected helper oracle changed.')
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=PROJECT, text=True).strip()
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=PROJECT, text=True).strip()
    observed = {}
    for mode in ('source', 'installed'):
        report = results / ('standard-system-helper-' + mode + '.xml')
        process = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c',
            BOOTSTRAP, str(PROJECT), mode, str(report)], cwd=PROJECT, timeout=180, check=False)
        if process.returncode:
            raise RuntimeError('The exact ' + mode + ' helper regression process failed.')
        root = ET.parse(report).getroot()
        cases = list(root.iter('testcase'))
        actual = Counter(case.attrib['name'].split('[', 1)[0] for case in cases)
        if (actual != expected or len(cases) != 16
                or any(list(root.iter(tag)) for tag in ('failure', 'error', 'skipped'))):
            raise AssertionError('A helper regression is missing, failed or skipped: ' + mode)
        observed[mode] = {'passed': len(cases), 'failed': 0, 'skipped': 0,
            'functions': dict(actual), 'junit_sha256': sha256(report.read_bytes()).hexdigest()}
    value = {'schema': 'carr-system-helper-regressions/1', 'status': 'PASS',
        'source_commit': source, 'source_tree': tree, 'results': observed,
        'native_activation': 'NOT_RUN_BY_THIS_GATE', 'private_file_policy': 'UNCHANGED_SINGLE_LINK'}
    raw = json.dumps(value, sort_keys=True, indent=2) + '\n'
    (results / 'standard-system-helper-regressions.json').write_text(raw, encoding='utf-8')
    print(raw, flush=True)


if __name__ == '__main__':
    main()
