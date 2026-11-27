"""Portable verdict tests; these never claim a Windows installation succeeded."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest


PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'qualify_standard_library.py'
SPEC = importlib.util.spec_from_file_location('installed_library_qualification_tested', PATH)
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


def report(path, module):
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite')
    for name, count in qualification.EXPECTED[module].items():
        for index in range(count):
            ET.SubElement(suite, 'testcase', classname=module,
                          name=name + ('[' + str(index) + ']' if count > 1 else ''))
    ET.ElementTree(root).write(path, encoding='utf-8')
    return root


def snapshot():
    return {
        'source_commit': 'a' * 40, 'source_tree': 'b' * 40,
        'manifest_sha256': 'c' * 64, 'wheel_sha256': 'd' * 64,
        'package_hashes': {'carr_library/__init__.py': 'e' * 64},
        'origins': {'carr_library': {'path': 'installed/__init__.py', 'sha256': 'e' * 64}},
        'source_hashes': {'tests/e2e/test_semantic_runtime.py': 'f' * 64},
        'versions': {'universal-research-library': '0.1.0', 'onnxruntime': '1.23.2'},
    }


def with_test_dependencies(before):
    after = deepcopy(before)
    after['versions'].update(qualification.TEST_VERSIONS, pluggy='1.6.0')
    return after


def test_exact_six_workflow_reports_require_all_fifty_cases(tmp_path):
    observed = {}
    for index, module in enumerate(qualification.EXPECTED):
        path = tmp_path / (str(index) + '.xml')
        report(path, module)
        observed[module] = qualification._cases(path, module, 0)
    assert observed == qualification.EXPECTED
    assert sum(sum(counts.values()) for counts in observed.values()) == 50
    assert len(qualification.SUITES) == len(observed) == 6


@pytest.mark.parametrize('fault', ('failure', 'error', 'skipped', 'missing',
                                  'duplicate', 'foreign', 'nonzero'))
def test_installed_workflow_negative_verdicts_cannot_be_reported_as_success(tmp_path, fault):
    module = 'tests.e2e.test_import_worker'
    path = tmp_path / 'junit.xml'
    root = report(path, module)
    suite = root.find('testsuite')
    case = suite.find('testcase')
    if fault in ('failure', 'error', 'skipped'):
        ET.SubElement(case, fault)
    elif fault == 'missing':
        suite.remove(case)
    elif fault == 'duplicate':
        suite.append(deepcopy(case))
    elif fault == 'foreign':
        case.set('classname', 'tests.e2e.test_unapproved')
    ET.ElementTree(root).write(path, encoding='utf-8')
    with pytest.raises(AssertionError):
        qualification._cases(path, module, 1 if fault == 'nonzero' else 0)


@pytest.mark.parametrize('field', ('source_commit', 'source_tree', 'manifest_sha256',
                                  'wheel_sha256', 'package_hashes', 'origins', 'source_hashes'))
def test_resealed_or_changed_source_identity_does_not_pass_after_tests(field):
    before = snapshot()
    after = with_test_dependencies(before)
    after[field] = 'different protected identity'
    with pytest.raises(AssertionError, match='protected identity'):
        qualification._preserved(before, after)


@pytest.mark.parametrize('fault', ('changed-production', 'missing-production', 'unapproved-addition',
                                  'wrong-pytest', 'missing-reportlab'))
def test_test_dependency_install_cannot_replace_production_or_change_the_declared_closure(fault):
    before = snapshot()
    after = with_test_dependencies(before)
    if fault == 'changed-production':
        after['versions']['onnxruntime'] = '999.0'
    elif fault == 'missing-production':
        del after['versions']['onnxruntime']
    elif fault == 'unapproved-addition':
        after['versions']['undeclared-package'] = '1.0'
    elif fault == 'wrong-pytest':
        after['versions']['pytest'] = '9.0.1'
    elif fault == 'missing-reportlab':
        del after['versions']['reportlab']
    with pytest.raises(AssertionError):
        qualification._preserved(before, after)


def test_only_exact_test_additions_preserve_the_original_production_snapshot():
    before = snapshot()
    unchanged = deepcopy(before)
    qualification._preserved(before, with_test_dependencies(before))
    assert before == unchanged


@pytest.mark.parametrize('fault', ('none', 'changed-bytes', 'checkout-origin', 'extra-checkout-module'))
def test_actual_origin_files_must_be_installed_and_match_the_wheel_bytes(tmp_path, monkeypatch, fault):
    bundle = tmp_path / 'generation'
    package = bundle / '.venv' / 'Lib' / 'site-packages' / 'carr_library'
    package.mkdir(parents=True)
    installed = package / '__init__.py'
    installed.write_bytes(b'INSTALLED = True\n')
    digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    foreign = checkout / '__init__.py'
    foreign.write_bytes(installed.read_bytes())
    selected = foreign if fault == 'checkout-origin' else installed
    module = SimpleNamespace(__file__=str(selected))
    modules = {'carr_library': module}
    if fault == 'extra-checkout-module':
        modules['carr_library.foreign'] = SimpleNamespace(__file__=str(foreign))
    if fault == 'changed-bytes':
        installed.write_bytes(b'INSTALLED = False\n')
    monkeypatch.setattr(qualification, 'MODULES', ('carr_library',))
    monkeypatch.setattr(qualification, 'sys', SimpleNamespace(modules=modules))
    monkeypatch.setattr(qualification.importlib, 'import_module', lambda name: module)
    monkeypatch.setattr(qualification, '_sha',
                        lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest())
    if fault == 'none':
        actual = qualification._origins(bundle, {'carr_library/__init__.py': digest})
        assert actual == {'carr_library': {'path': str(installed.resolve()), 'sha256': digest}}
    else:
        with pytest.raises(AssertionError, match='origin|bytes|escaped'):
            qualification._origins(bundle, {'carr_library/__init__.py': digest})


def test_public_evidence_is_exclusive_and_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'result.json'
    qualification._write(path, {'status': 'synthetic-verifier-fixture'})
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        qualification._write(path, {'status': 'must-not-replace'})
    assert path.read_bytes() == original
    monkeypatch.setattr(qualification, 'JSON_LIMIT', 16)
    other = tmp_path / 'oversized.json'
    with pytest.raises(ValueError, match='byte budget'):
        qualification._write(other, {'status': 'too large for the bounded fixture'})
    assert not other.exists()
