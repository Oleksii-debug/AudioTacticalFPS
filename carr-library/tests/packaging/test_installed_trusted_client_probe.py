"""Real pytest collection/JUnit identity independent of an external config root.

These fixtures exercise the reviewed runner and collector plugin in a real
isolated interpreter. They do not substitute for the probe's installed Carr
module-origin checks or its native installation acceptance.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'installed_trusted_client_probe.py'
CI_SCRIPT = ROOT / 'scripts' / 'ci_check.py'
HELPER = runpy.run_path(str(SCRIPT))
VERIFY = HELPER['verify_required_report']
SUITE = HELPER['SUITE_PROPERTY']
CASE = HELPER['CASE_PROPERTY']
TRUST_UI = HELPER['TRUST_UI']

CHILD = r"""
import json
from pathlib import Path
import runpy
import sys
import pytest
script, ci, root, required, evidence = sys.argv[1:6]
required = json.loads(required)
helper = runpy.run_path(script)
membership = helper['required_suite_plugin'](Path(root), required)
real_main = pytest.main
def checked_main(*args, **kwargs):
    kwargs['plugins'] = [*kwargs.get('plugins', ()), membership]
    result = real_main(*args, **kwargs)
    Path(evidence).write_text(json.dumps(dict(
        collected=membership.collected, pytest_exit=int(result))), encoding='utf-8')
    return result
pytest.main = checked_main
sys.argv = ['required-membership', *sys.argv[6:]]
# Execute the actual offline and XFAIL/XPASS policy, not a test replica.
exec(compile(runpy.run_path(ci)['PYTEST_CHILD'], ci, 'exec'))
"""


def run_case(root, required, *, selected=None, extra=()):
    root = root.resolve()
    config_root = root.parent / (root.name + ' external config')
    config_root.mkdir()
    config = config_root / 'pytest.ini'
    config.write_text('[pytest]\n', encoding='utf-8')
    report = config_root / 'report.xml'
    evidence = config_root / 'collector.json'
    environment = {key: value for key, value in os.environ.items()
        if key.upper() not in ('PYTHONPATH', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS',
                               'CARR_CI_ALLOW_LOOPBACK')}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD,
        str(SCRIPT), str(CI_SCRIPT), str(root), json.dumps(list(required)), str(evidence),
        '-c', str(config), '-o', 'addopts=', '-o', 'pythonpath=',
        '-o', 'xfail_strict=true', '--import-mode=importlib', '-p', 'no:cacheprovider',
        '--junitxml', str(report), *extra,
        *[str(root / path) for path in (selected if selected is not None else required)]],
        cwd=root, env=environment, capture_output=True, timeout=60, check=False)
    diagnostic = (result.stdout + result.stderr).decode('utf-8', errors='replace')
    proof = json.loads(evidence.read_text(encoding='utf-8')) if evidence.exists() else None
    return dict(result=result, diagnostic=diagnostic, report=report, proof=proof,
                root=root, config=config)


def write_source(root, name, source):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding='utf-8')


@pytest.fixture(scope='module')
def real_pass(tmp_path_factory):
    root = tmp_path_factory.mktemp('required suite source')
    required = ('tests/first/test_same.py', 'tests/second/test_same.py', TRUST_UI)
    write_source(root, required[0], '''import socket
import pytest

def test_shared(record_property):
    record_property('fixture.note', 'preserved')
    assert True

@pytest.mark.parametrize('value', ['plain', '\\u03a9'], ids=['plain', 'unicode'])
def test_parameter(value):
    assert value

class TestGroup:
    def test_shared(self):
        assert True

def test_offline_policy():
    for action in (lambda: socket.create_connection(('127.0.0.1', 9)),
                   lambda: socket.getaddrinfo('example.invalid', 443),
                   lambda: socket.socket().connect(('127.0.0.1', 9))):
        with pytest.raises(OSError, match='disabled during offline verification'):
            action()
''')
    write_source(root, required[1], 'def test_shared():\n    assert True\n')
    write_source(root, TRUST_UI, '''import pytest
@pytest.mark.parametrize('index', range(11))
def test_required_ui_case(index):
    assert 0 <= index < 11
''')
    outcome = run_case(root, required)
    assert outcome['result'].returncode == 0, outcome['diagnostic']
    assert outcome['proof'] is not None, outcome['diagnostic']
    outcome.update(required=required,
                   counts={required[0]: 5, required[1]: 1, TRUST_UI: 11},
                   xml=outcome['report'].read_bytes())
    return outcome


def xml_copy(real_pass):
    return ET.ElementTree(ET.fromstring(real_pass['xml']))


def remove_case(tree, case):
    for parent in tree.iter():
        if case in list(parent):
            parent.remove(case)
            return
    raise AssertionError('The real JUnit case must have a parent.')


def test_real_external_config_and_same_named_modules_have_exact_suite_membership(real_pass):
    assert not real_pass['config'].is_relative_to(real_pass['root'])
    counts = VERIFY(real_pass['report'], real_pass['required'], real_pass['proof']['collected'])
    assert counts == real_pass['counts']
    assert sum(counts.values()) == 17
    cases = list(ET.fromstring(real_pass['xml']).iter('testcase'))
    # The former classname-suffix algorithm cannot establish this mapping.
    legacy = {name: sum(case.get('classname', '').rsplit('.', 1)[-1] == Path(name).stem
                        for case in cases) for name in real_pass['required']}
    assert legacy != counts
    assert len({row['identity'] for row in real_pass['proof']['collected']}) == 17
    assert any(prop.get('name') == 'fixture.note' and prop.get('value') == 'preserved'
               for case in cases for prop in case.findall('./properties/property'))


def test_display_classname_and_name_are_not_suite_authority(real_pass, tmp_path):
    tree = xml_copy(real_pass)
    for case in tree.iter('testcase'):
        case.set('classname', 'external.configuration.directory')
        case.set('name', 'untrusted display name')
    report = tmp_path / 'display-only.xml'
    tree.write(report, encoding='utf-8', xml_declaration=True)
    assert VERIFY(report, real_pass['required'], real_pass['proof']['collected']) == real_pass['counts']


@pytest.mark.parametrize('mutation', [
    'missing-suite', 'duplicate-suite', 'unknown-suite', 'mismatched-suite',
    'missing-case-label', 'duplicate-case-label', 'unknown-case-label',
    'extra-reserved-label', 'duplicate-case', 'omitted-case',
    'failure', 'error', 'skipped',
])
def test_real_report_rejects_missing_foreign_duplicate_or_unsuccessful_cases(real_pass, tmp_path, mutation):
    tree = xml_copy(real_pass)
    case = next(tree.iter('testcase'))
    properties = case.find('properties')
    assert properties is not None
    suite = next(prop for prop in properties if prop.get('name') == SUITE)
    identity = next(prop for prop in properties if prop.get('name') == CASE)
    if mutation == 'missing-suite':
        properties.remove(suite)
    elif mutation == 'duplicate-suite':
        properties.append(deepcopy(suite))
    elif mutation == 'unknown-suite':
        suite.set('value', 'tests/foreign/test_same.py')
    elif mutation == 'mismatched-suite':
        suite.set('value', TRUST_UI if suite.get('value') != TRUST_UI else real_pass['required'][0])
    elif mutation == 'missing-case-label':
        properties.remove(identity)
    elif mutation == 'duplicate-case-label':
        properties.append(deepcopy(identity))
    elif mutation == 'unknown-case-label':
        identity.set('value', 'f' * 64)
    elif mutation == 'extra-reserved-label':
        ET.SubElement(properties, 'property', name='carr.required_unreviewed', value='extra')
    elif mutation == 'duplicate-case':
        next(tree.iter('testsuite')).append(deepcopy(case))
    elif mutation == 'omitted-case':
        remove_case(tree, case)
    else:
        ET.SubElement(case, mutation, message='Actual outcome must remain a failure.')
    report = tmp_path / 'rejected.xml'
    tree.write(report, encoding='utf-8', xml_declaration=True)
    with pytest.raises(RuntimeError):
        VERIFY(report, real_pass['required'], real_pass['proof']['collected'])


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown-suite', 'changed-case', 'missing-case', 'missing-suite'])
def test_report_cannot_replace_missing_or_forged_collector_evidence(real_pass, mutation):
    rows = deepcopy(real_pass['proof']['collected'])
    if mutation == 'duplicate':
        rows.append(deepcopy(rows[0]))
    elif mutation == 'unknown-suite':
        rows[0]['suite'] = 'tests/foreign/test_same.py'
    elif mutation == 'changed-case':
        rows[0]['case'] += '[not-collected]'
    elif mutation == 'missing-case':
        rows.pop()
    else:
        rows = [row for row in rows if row['suite'] != TRUST_UI]
    with pytest.raises(RuntimeError):
        VERIFY(real_pass['report'], real_pass['required'], rows)


def test_exact_eleven_trust_ui_cases_remain_required(real_pass, tmp_path):
    tree = xml_copy(real_pass)
    rows = deepcopy(real_pass['proof']['collected'])
    missing = next(row for row in rows if row['suite'] == TRUST_UI)
    rows.remove(missing)
    case = next(case for case in tree.iter('testcase')
                if any(prop.get('name') == CASE and prop.get('value') == missing['identity']
                       for prop in case.findall('./properties/property')))
    remove_case(tree, case)
    report = tmp_path / 'only-ten-ui.xml'
    tree.write(report, encoding='utf-8', xml_declaration=True)
    # The report and actual collector agree exactly; the independent UI guard
    # still rejects the otherwise passing ten-case result.
    with pytest.raises(RuntimeError, match='suite counts'):
        VERIFY(report, real_pass['required'], rows)
    counts = {**real_pass['counts'], TRUST_UI: 10}
    assert VERIFY(report, real_pass['required'], rows, required_counts=counts) == counts


@pytest.mark.parametrize('mode', ['foreign', 'duplicate', 'empty-required'])
def test_real_collection_refuses_foreign_duplicate_or_empty_suite_before_execution(tmp_path, mode):
    root = tmp_path / 'source'
    marker = tmp_path / 'test-body-ran.txt'
    first, second = 'tests/test_first.py', 'tests/test_second.py'
    body = ('from pathlib import Path\n'
            'def test_marker():\n'
            '    Path(' + repr(str(marker)) + ').write_text("ran", encoding="utf-8")\n')
    write_source(root, first, body)
    required, selected, extra = [first], [first], []
    if mode == 'foreign':
        write_source(root, second, body)
        selected.append(second)
    elif mode == 'duplicate':
        selected.append(first)
        extra.append('--keep-duplicates')
    else:
        write_source(root, second, '# The declared required suite has no cases.\n')
        required.append(second)
        selected.append(second)
    outcome = run_case(root, required, selected=selected, extra=extra)
    assert outcome['result'].returncode != 0, outcome['diagnostic']
    assert not marker.exists(), outcome['diagnostic']
    expected = {'foreign': 'outside the exact required suites',
                'duplicate': 'collected more than once',
                'empty-required': 'did not collect any tests'}[mode]
    assert expected in outcome['diagnostic']


@pytest.mark.parametrize('mode', ['xfail', 'xpass', 'skip', 'spoof'])
def test_actual_offline_child_keeps_expected_failure_and_reserved_evidence_policy(tmp_path, mode):
    root = tmp_path / 'source'
    name = 'tests/test_policy.py'
    sources = {
        'xfail': "import pytest\n@pytest.mark.xfail(reason='negative control')\ndef test_policy(): assert False\n",
        'xpass': "import pytest\n@pytest.mark.xfail(reason='negative control', strict=False)\ndef test_policy(): assert True\n",
        'skip': "import pytest\n@pytest.mark.skip(reason='negative control')\ndef test_policy(): assert True\n",
        'spoof': "def test_policy(record_property):\n    record_property('carr.required_suite', 'forged')\n",
    }
    write_source(root, name, sources[mode])
    outcome = run_case(root, [name])
    if mode in ('xfail', 'xpass'):
        assert outcome['result'].returncode != 0, outcome['diagnostic']
        assert 'pytest reported XFAIL or XPASS' in outcome['diagnostic']
    elif mode == 'spoof':
        assert outcome['result'].returncode != 0, outcome['diagnostic']
        assert 'replace reserved required-suite evidence' in outcome['diagnostic']
    else:
        assert outcome['result'].returncode == 0, outcome['diagnostic']
        with pytest.raises(RuntimeError, match='was skipped'):
            VERIFY(outcome['report'], [name], outcome['proof']['collected'],
                   required_counts={name: 1})


@pytest.mark.parametrize('names', [
    ['../test_a.py'], ['./tests/test_a.py'], ['tests//test_a.py'],
    ['tests/../test_a.py'], ['tests/test_a.py', 'tests/TEST_A.py'], [],
])
def test_required_suite_paths_reject_aliases_before_collection(tmp_path, names):
    write_source(tmp_path, 'tests/test_a.py', 'def test_ok(): pass\n')
    with pytest.raises(ValueError):
        HELPER['required_suite_plugin'](tmp_path, names)
