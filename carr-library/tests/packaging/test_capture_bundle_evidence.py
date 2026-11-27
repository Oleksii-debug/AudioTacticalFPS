"""Evidence collection uses real source/wheel/native verifiers, never Windows.

The reports below are synthetic inputs to the collector contract. They do not
represent execution of the inert PE fixtures on this test host.
"""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'evidence_native_fixture_support', Path(__file__).with_name('test_native_launcher_bundle.py'))
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
spec.loader.exec_module(support)
candidate = support.candidate
refresh_support = support.module(
    'evidence_refresh_fixture_support', Path(__file__).with_name('test_native_launcher_refresh.py'))


@pytest.fixture
def evidence(candidate, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'packaging'))
    collector = support.module('bundle_evidence_collector', ROOT / 'packaging/capture_bundle_evidence.py')
    monkeypatch.setattr(collector, 'builder', candidate.builder)
    refresh = refresh_support.prepare(candidate, monkeypatch)
    refresh_support.refresh(refresh)
    candidate.output = refresh.output
    process = candidate.output.with_name('synthetic-process-report.json')
    process.write_text(json.dumps({
        'schema': 1, 'status': 'passed', 'launcher_sha256': candidate.pin,
        'checks': sorted(collector.PROCESS_CHECKS), 'check_count': len(collector.PROCESS_CHECKS),
        'native_launcher_execution_verified': True, 'actual_python_installation_verified': False,
        'nvda_verified': False, 'authenticode_verified': False,
    }))
    installed = candidate.output.with_name('synthetic-installed-report.json')
    installed.write_text(json.dumps({
        'schema': 1, 'status': 'passed', 'launcher_sha256': candidate.pin,
        'checks': ['--check-installation', '--self-test'],
        'actual_installed_app_self_test_verified': True,
        'actual_fresh_installation_verified': False, 'gui_verified': False,
        'nvda_verified': False, 'authenticode_verified': False,
    }))
    return SimpleNamespace(value=candidate, collector=collector, process=process,
                           installed=installed, output=candidate.output.with_name('accepted'))


def capture(value, **options):
    arguments = dict(bundle=value.value.output, wheel=value.value.app,
                     source_commit=value.value.commit, output=value.output,
                     process_probe=value.process, installed_probe=value.installed)
    arguments.update(options)
    return value.collector.capture(**arguments)


def reseal(value, name, raw):
    path = value.value.output / name
    path.write_bytes(raw)
    manifest = value.value.output / 'bundle-manifest.json'
    data = json.loads(manifest.read_bytes())
    data['files'][name] = sha256(raw).hexdigest()
    manifest.write_text(json.dumps(data))


def test_only_exact_source_wheel_and_sealed_text_are_retained(evidence):
    value = evidence.value
    before = (value.output / 'bundle-manifest.json').read_bytes()
    report = capture(evidence)
    files = {p.relative_to(evidence.output).as_posix()
             for p in evidence.output.rglob('*') if p.is_file()}
    assert len(files) == 10
    assert all(name.endswith(('.json', '.whl')) for name in files)
    assert {name for name in files if name.endswith('.whl')} == {
        'tested-application-wheel/' + value.app.name}
    assert (evidence.output / 'tested-application-wheel' / value.app.name).read_bytes() == value.app.read_bytes()
    assert (evidence.output / 'tested-native-launcher/build-evidence.json').read_bytes() == value.proof.read_bytes()
    assert (evidence.output / 'sealed-bundle/bundle-manifest.json').read_bytes() == before
    proof = json.loads((evidence.output / 'tested-application-wheel/source-wheel-proof.json').read_bytes())
    assert proof['source_commit'] == report['source_commit'] == value.commit
    assert proof['source_tree'] == report['source_tree'] == support.git(value.repo, 'rev-parse', 'HEAD^{tree}')
    assert proof['wheel_sha256'] == report['wheel_sha256'] == sha256(value.app.read_bytes()).hexdigest()
    assert report['runtime_payload_included'] is False
    assert not report['nvda_verified'] and not report['authenticode_verified']
    assert (value.output / 'bundle-manifest.json').read_bytes() == before


@pytest.mark.parametrize('field,value', [
    ('source_commit', 'b' * 40), ('source_tree', 'b' * 40), ('bundled_native_launcher', False),
])
def test_wrong_sealed_source_or_native_declaration_never_publishes(evidence, field, value):
    manifest = evidence.value.output / 'bundle-manifest.json'
    data = json.loads(manifest.read_bytes())
    data[field] = value
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        capture(evidence)
    assert not evidence.output.exists()


def test_different_valid_committed_wheel_cannot_be_substituted_for_installed_bytes(evidence):
    value = evidence.value
    # Changing only metadata ordering preserves a valid, source-identical wheel
    # but produces bytes that were never installed in the sealed bundle.
    old = value.app.read_bytes()
    support.write_wheel(value.app, 'universal_research_library',
                        {'carr_library/__main__.py': b'raise SystemExit(0)\n',
                         'carr_library/__init__.py': b''}, requires=('fixture==1.0',))
    assert value.app.read_bytes() != old
    value.builder.verify_application_source(value.app, value.commit)
    with pytest.raises(ValueError, match='sealed application proof|retained wheel'):
        capture(evidence)
    assert not evidence.output.exists()


def test_resealed_fabricated_application_proof_is_rejected(evidence):
    source = json.loads((evidence.value.output / 'application-source.json').read_bytes())
    source['payload_files'] += 1
    reseal(evidence, 'application-source.json', json.dumps(source).encode())
    with pytest.raises(ValueError, match='application proof'):
        capture(evidence)
    assert not evidence.output.exists()


@pytest.mark.parametrize('which,field,value', [
    ('process', 'launcher_sha256', 'c' * 64),
    ('installed', 'launcher_sha256', 'c' * 64),
    ('process', 'native_launcher_execution_verified', False),
    ('process', 'checks', ['read-only-ready-probe']),
    ('installed', 'checks', ['--self-test']),
    ('installed', 'actual_installed_app_self_test_verified', False),
    ('installed', 'authenticode_verified', True),
])
def test_foreign_or_incomplete_windows_report_never_publishes(evidence, which, field, value):
    path = getattr(evidence, which)
    data = json.loads(path.read_bytes())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='report|probe'):
        capture(evidence)
    assert not evidence.output.exists()


def test_duplicate_json_and_existing_output_are_rejected(evidence):
    raw = evidence.process.read_bytes()
    evidence.process.write_bytes(raw[:-1] + b',"status":"passed"}')
    with pytest.raises(ValueError, match='Duplicate'):
        capture(evidence)
    assert not evidence.output.exists()
    evidence.process.write_bytes(raw)
    evidence.output.mkdir()
    sentinel = evidence.output / 'keep.txt'
    sentinel.write_bytes(b'existing evidence')
    with pytest.raises(ValueError, match='new evidence'):
        capture(evidence)
    assert sentinel.read_bytes() == b'existing evidence'


def test_mutation_during_capture_cannot_replace_the_published_wheel(evidence, monkeypatch):
    original = evidence.collector._publish_directory
    exact = evidence.value.app.read_bytes()

    def replace_after_capture(stage, output):
        evidence.value.app.write_bytes(b'Changed after the immutable source capture.')
        return original(stage, output)

    monkeypatch.setattr(evidence.collector, '_publish_directory', replace_after_capture)
    capture(evidence)
    retained = evidence.output / 'tested-application-wheel' / evidence.value.app.name
    assert retained.read_bytes() == exact
