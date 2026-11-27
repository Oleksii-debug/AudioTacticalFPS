"""Owner-pinned review commands reuse TUF and existing maintenance authority."""
from hashlib import sha256
import json
from pathlib import Path
import sys

import pytest

from carr_library.security import maintenance as module
from carr_library.security.__main__ import main
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.standard_handoff import StandardUpdateHandoff
from .test_standard_update_handoff import ready
from .test_installation import assert_preserved
from .conftest import installer_verify


@pytest.fixture
def selected(ready, repository, tmp_path):
    helper, package, adapter, original = ready
    bootstrap = tmp_path / 'independent-root.json'
    bootstrap.write_bytes(repository.bootstrap)
    verifier = Path(installer_verify().__code__.co_filename).resolve()
    value = {
        'schema': 'carr-update-owner/1', 'product': 'carr-test', 'channel': 'stable', 'current_build': 1,
        'bootstrap': {'path': str(bootstrap), 'sha256': sha256(repository.bootstrap).hexdigest()},
        'repository': {'kind': 'local', 'path': str(repository.root)},
        'target': repository.target_path, 'cache': str(tmp_path / 'maintenance-cache'),
        'installation': {'kind': 'standard', 'program': str(helper.program), 'work': str(helper.work),
                         'manifest_sha256': helper.before, 'setup_sha256': helper.old_setup,
                         'version': helper.version},
        'anchor': {'profile_id': 'synthetic-update-owner',
                   'reference': 'carr/provider/synthetic-update-owner/' + 'a' * 32, 'binding': 'b' * 64},
        'verifier': {'path': str(verifier), 'sha256': sha256(verifier.read_bytes()).hexdigest()},
        'helper_python': None,
    }
    path = tmp_path / 'owner-reviewed.json'
    raw = json_bytes(value); path.write_bytes(raw)
    config = module.UpdateConfiguration.read(path, sha256(raw).hexdigest())
    return config, value, helper, package, adapter, original


def session(selected, monkeypatch):
    config, _value, helper, _package, adapter, _original = selected
    class FixtureStandard(StandardUpdateHandoff):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, adapter=adapter, clock=helper.clock)
    monkeypatch.setattr(module, 'StandardUpdateHandoff', FixtureStandard)
    return module.MaintenanceSession(helper.app, config, anchor=helper.anchor)


def test_pinned_review_is_passive_and_does_not_provision_trust_or_contact_source(selected, monkeypatch):
    config, value, helper, _package, adapter, original = selected
    def forbidden(*args, **kwargs):
        pytest.fail('Review activated a runtime, network client or protected store.')
    monkeypatch.setattr(module, 'LocalRepositoryClient', forbidden)
    monkeypatch.setattr(module, 'HTTPSRepositoryClient', forbidden)
    monkeypatch.setattr(module, 'WindowsVaultAnchor', forbidden)
    observed = module.MaintenanceSession(helper.app, config)
    assert observed.release is None and observed.package is None
    assert 'Current program:' in config.description()
    assert not Path(value['cache']).exists()
    assert adapter.calls == []
    assert_preserved(original)


def test_actual_signed_check_download_apply_and_recovery_remain_separate(selected, monkeypatch):
    current = session(selected, monkeypatch)
    config, _value, helper, package, adapter, original = selected
    release = current.check()
    assert release == package.release and adapter.calls == []
    with pytest.raises(TrustError, match='Download'):
        current.apply(accepted=True)
    current.download(release)
    assert adapter.calls == []
    with pytest.raises(TrustError, match='explicitly accept'):
        current.apply()
    command = current.command()
    assert 'apply-update' in command and '--reviewed-sha256' in command and '--accept' in command
    result = current.apply(accepted=True)
    assert result['status'] == 'complete' and helper.anchor.load().sequence == 2
    assert [version for version, _ in adapter.calls] == ['B']
    assert current.last_result == result
    assert current.recover(accepted=True)['status'] == 'complete'
    assert config.path.read_bytes() == config.raw
    assert_preserved(original)


def test_mutated_owner_configuration_blocks_before_apply_or_new_requests(selected, monkeypatch):
    current = session(selected, monkeypatch)
    config, value, _helper, _package, adapter, original = selected
    release = current.check(); current.download(release)
    value['target'] = 'different-release.zip'
    config.path.write_bytes(json_bytes(value))
    with pytest.raises(TrustError, match='changed after review'):
        current.apply(accepted=True)
    assert adapter.calls == []
    assert_preserved(original)


def test_fresh_tuf_authorization_is_required_at_the_actual_apply_boundary(selected, repository, monkeypatch):
    current = session(selected, monkeypatch)
    _config, _value, helper, _package, adapter, original = selected
    release = current.check(); current.download(release)
    # Cache still contains the previously valid exact package. A forged current
    # metadata response must nevertheless prevent a new activation.
    timestamp = repository.root / 'metadata/timestamp.json'
    value = json.loads(timestamp.read_bytes())
    value['signed']['version'] += 1
    timestamp.write_bytes(json_bytes(value))
    with pytest.raises(Exception):
        current.apply(accepted=True)
    assert helper.anchor.load().sequence == 1 and adapter.calls == []
    assert_preserved(original)


def test_check_failure_invalidates_old_download_and_never_keeps_hidden_apply_authority(selected, monkeypatch):
    current = session(selected, monkeypatch)
    config, _value, _helper, _package, adapter, _original = selected
    release = current.check(); current.download(release)
    config.path.write_bytes(b'changed')
    with pytest.raises(TrustError):
        current.check()
    assert current.release is None and current.package is None and adapter.calls == []


def test_selected_verifier_bytes_are_rechecked_before_loading_any_code(selected, tmp_path, monkeypatch):
    config, value, helper, _package, adapter, original = selected
    path = tmp_path / 'install_windows.py'
    path.write_bytes(b'raise AssertionError("must not execute an unreviewed verifier")\n')
    value['verifier']['path'] = str(path)
    raw = json_bytes(value); config.path.write_bytes(raw)
    current = module.MaintenanceSession(helper.app,
        module.UpdateConfiguration.read(config.path, sha256(raw).hexdigest()), anchor=helper.anchor)
    release = current.check(); current.download(release)
    with pytest.raises(TrustError, match='verifier changed'):
        current.apply(accepted=True)
    assert adapter.calls == []
    assert_preserved(original)


def test_missing_protected_anchor_never_becomes_private_permission_or_reset(selected, monkeypatch):
    config, _value, helper, _package, adapter, original = selected
    class Absent:
        def load(self):
            raise TrustError('protected anchor is absent')
        def compare_and_set(self, *args, **kwargs):
            pytest.fail('Missing anchor was provisioned.')
    monkeypatch.setattr(module, 'WindowsVaultAnchor', lambda *args, **kwargs: Absent())
    current = module.MaintenanceSession(helper.app, config)
    release = current.check(); current.download(release)
    with pytest.raises(TrustError, match='absent'):
        current.apply(accepted=True)
    assert adapter.calls == [] and helper.anchor.load().sequence == 1
    assert_preserved(original)


@pytest.mark.parametrize('field', ['schema', 'unknown', 'anchor', 'bootstrap', 'verifier'])
def test_owner_configuration_exact_fields_are_fail_closed(selected, field):
    config, value, _helper, _package, _adapter, _original = selected
    value[field] = 'unreviewed'
    raw = json_bytes(value); config.path.write_bytes(raw)
    with pytest.raises((ValueError, TypeError, PermissionError)):
        module.UpdateConfiguration.read(config.path, sha256(raw).hexdigest())


def test_direct_configuration_constructor_cannot_bypass_independent_byte_identity(selected):
    config = selected[0]
    with pytest.raises(TrustError, match='reviewed identity'):
        module.UpdateConfiguration(config.path, '0' * 64, config.raw)


def test_research_and_installer_roots_cannot_be_shared_for_update_scratch(selected):
    config, value, helper, _package, _adapter, _original = selected
    value['installation']['work'] = str(helper.app.root / 'unexpected-update-work')
    raw = json_bytes(value); config.path.write_bytes(raw)
    config = module.UpdateConfiguration.read(config.path, sha256(raw).hexdigest())
    with pytest.raises(TrustError, match='overlap'):
        module.MaintenanceSession(helper.app, config)
    assert not Path(value['installation']['work']).exists()


def test_cli_review_check_download_and_explicit_accept_boundaries(selected, monkeypatch, capsys):
    config, _value, helper, _package, adapter, original = selected
    common = ['--configuration', str(config.path), '--configuration-sha256', config.digest]
    assert main(['review-configuration', *common]) == 0
    assert 'Explicit owner configuration' in capsys.readouterr().out
    assert main(['check-configured-update', *common, '--data-dir', str(helper.app.root)]) == 0
    release = json.loads(capsys.readouterr().out)
    assert release['build'] == 2 and adapter.calls == []
    args = [*common, '--data-dir', str(helper.app.root), '--reviewed-sha256', release['sha256']]
    assert main(['apply-update', *args]) == 2
    assert adapter.calls == []
    assert main(['download-configured-update', *args]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared['activated'] is False and 'apply-update' in prepared['maintenance_command']
    assert Path(prepared['package']).is_file()
    assert_preserved(original)


def test_cli_https_source_is_explicit_and_bad_port_never_constructs_transport(tmp_path, capsys):
    bootstrap = tmp_path / 'root.json'; bootstrap.write_bytes(b'{}')
    assert main(['check-update', '--repository-url', 'https://example.invalid:0/',
        '--bootstrap', str(bootstrap), '--bootstrap-sha256', sha256(b'{}').hexdigest(),
        '--product', 'carr-test', '--channel', 'stable', '--current-build', '1',
        '--cache', str(tmp_path / 'cache'), '--target', 'release.zip']) == 2
    assert not (tmp_path / 'cache').exists()
    assert 'Verification failed' in capsys.readouterr().err
