"""Exact update envelope production over the existing standard builder.

The fixture supplies inert executable bytes only after ordinary sealed-bundle
preparation. This is not a compilation or native setup result.
"""
from hashlib import sha256
import json
from pathlib import Path
import runpy
import zipfile

import pytest

from carr_library.security.standard_handoff import prepare_signed_setup
from carr_library.security.updates import LocalRepositoryClient

BASE = runpy.run_path(str(Path(__file__).with_name('test_standard_installer.py')))
bundle = BASE['bundle']
native_bundle = BASE['native_bundle']
installer = BASE['installer']
COMMIT = BASE['COMMIT']


@pytest.fixture
def compiled_record(native_bundle, tmp_path):
    report = BASE['prepare'](native_bundle, tmp_path)
    stage = Path(report.pop('output'))
    raw = (stage / 'payload/bundle-manifest.json').read_bytes()
    setup = stage / 'dist' / ('Resonance-Setup-' + COMMIT[:12] + '.exe')
    setup.parent.mkdir()
    setup.write_bytes(b'INERT SYNTHETIC COMPILED-RECORD FIXTURE; NEVER EXECUTE\n')
    report.update(status='compiled', compiler_sha256='b' * 64,
        installer=dict(path=setup.relative_to(stage).as_posix(),
                       sha256=sha256(setup.read_bytes()).hexdigest(), bytes=setup.stat().st_size))
    return stage, report, raw, setup


def test_exact_existing_builder_envelope_is_consumed_only_after_real_tuf_authentication(compiled_record, tmp_path):
    stage, report, manifest, setup = compiled_record
    before = setup.read_bytes(), manifest
    result = installer._update_envelope(stage, report, manifest, lambda: False)
    archive = stage / result['path']
    assert result['sha256'] == sha256(archive.read_bytes()).hexdigest()
    assert result['bytes'] == archive.stat().st_size and result['authority'].startswith('none;')
    with zipfile.ZipFile(archive) as opened:
        assert set(opened.namelist()) == {'ResonanceSetup.exe', 'installer-build.json', 'bundle-manifest.json'}
        assert opened.read('ResonanceSetup.exe') == before[0]
        assert opened.read('bundle-manifest.json') == before[1]
        assert json.loads(opened.read('installer-build.json')) == report
    fixtures = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'security/conftest.py'))
    repository = fixtures['Repository'](tmp_path / 'signed-source')
    repository.publish(archive.read_bytes(), custom={'source_commit': COMMIT})
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    release = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)
    accepted, captured = prepare_signed_setup(package, repository.policy(), tmp_path / 'accepted')
    assert accepted == report and captured == manifest
    assert (tmp_path / 'accepted/ResonanceSetup.exe').read_bytes() == before[0]
    assert setup.read_bytes() == before[0]
    assert (stage / 'payload/bundle-manifest.json').read_bytes() == before[1]


def test_only_actual_compilation_path_may_request_an_update_envelope(native_bundle, tmp_path):
    with pytest.raises(ValueError, match='native compilation'):
        BASE['prepare'](native_bundle, tmp_path, update_envelope=True)
    assert not (tmp_path / 'installer').exists()


def test_prepared_report_does_not_claim_executable_publication(compiled_record):
    stage, report, raw, _setup = compiled_record
    report['status'] = 'prepared'
    with pytest.raises(ValueError, match='compiled'):
        installer._update_envelope(stage, report, raw, lambda: False)
    assert not list(stage.glob('Resonance-Update-*.zip'))


@pytest.mark.parametrize('mutation', ['bytes', 'length', 'digest'])
def test_changed_compiler_output_does_not_receive_an_accepted_envelope(compiled_record, mutation):
    stage, report, raw, setup = compiled_record
    if mutation == 'bytes':
        setup.write_bytes(b'x' * setup.stat().st_size)
    elif mutation == 'length':
        report['installer']['bytes'] += 1
    else:
        report['installer']['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='changed'):
        installer._update_envelope(stage, report, raw, lambda: False)
    # Partial private builder staging is never a completed builder result.
    assert setup.exists() and json.loads(raw)['source_commit'] == COMMIT


def test_cancellation_prevents_returning_any_update_receipt(compiled_record):
    stage, report, raw, setup = compiled_record
    before = setup.read_bytes()
    with pytest.raises(InterruptedError):
        installer._update_envelope(stage, report, raw, lambda: True)
    assert setup.read_bytes() == before


def test_standard_builder_refuses_portable_lifetime_marker_even_when_inventory_hashes_it(native_bundle, tmp_path):
    marker = native_bundle / 'portable-update.ini'
    from carr_library.portable_entry import PORTABLE_CONTRACT
    marker.write_bytes(PORTABLE_CONTRACT)
    manifest_path = native_bundle / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['files'][marker.name] = sha256(PORTABLE_CONTRACT).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    before = manifest_path.read_bytes()
    with pytest.raises(ValueError, match='portable ownership'):
        BASE['prepare'](native_bundle, tmp_path)
    assert manifest_path.read_bytes() == before and marker.read_bytes() == PORTABLE_CONTRACT
    assert not (tmp_path / 'installer').exists()
