"""Real signed setup envelopes and complete library backups.

Only Windows setup/registration are replaced by the explicitly synthetic
generation adapter below. It still runs the real generated program for health,
uses the actual installer verifier, and never claims native installation proof.
"""
from contextlib import contextmanager
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from carr_library.security import standard_handoff as module
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.anchors import Anchor
from carr_library.security.installation import ActivationCleanupError
from carr_library.security.updates import LocalRepositoryClient, UpdateCancelled
from carr_library import portable_entry, installation_lease
from .conftest import bundle, installer_verify
from .test_installation import setup_update, assert_preserved


def source_id(version):
    return str('ABC'.index(version) + 1) * 40


def payload(version):
    source = source_id(version)
    with zipfile.ZipFile(io.BytesIO(bundle(version))) as archive:
        files = {name: archive.read(name) for name in archive.namelist() if name != 'bundle-manifest.json'}
    files.update({'runtime/python.exe': b'synthetic runtime, never executed',
                  'ResearchLibrary.exe': b'synthetic native launcher, never executed'})
    raw = json_bytes(dict(schema=1, bundled_python=True, source_commit=source,
                          files={name: sha256(data).hexdigest() for name, data in files.items()}))
    files['bundle-manifest.json'] = raw
    return files, raw


def setup_bytes(version):
    return b'synthetic-standard-setup:' + version.encode('ascii')


def envelope(version='B', *, change=None, extra=None):
    files, manifest = payload(version)
    source = source_id(version)
    setup = setup_bytes(version)
    report = dict(schema=1, kind='standard-windows-installer', source_commit=source,
                  manifest_sha256=sha256(manifest).hexdigest(), payload_files=len(files) - 1,
                  status='compiled', recipe_sha256='a' * 64, compiler_sha256='b' * 64,
                  native_install_repair_uninstall='NOT_RUN', authenticode_verified=False,
                  nvda_verified=False, update_or_entitlement_authority=False, automatic_network=False,
                  installer=dict(path='dist/Resonance-Setup-' + source[:12] + '.exe',
                                 sha256=sha256(setup).hexdigest(), bytes=len(setup)))
    if change:
        change(report)
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('ResonanceSetup.exe', setup)
        archive.writestr('installer-build.json', json_bytes(report))
        archive.writestr('bundle-manifest.json', manifest)
        if extra is not None:
            archive.writestr(extra, b'not admitted')
    return result.getvalue()


class SyntheticStandard(module.StandardSetupAdapter):
    def __init__(self):
        super().__init__(installer_verify())
        self.selected = None
        self.calls = []
        self.fail = False
        self.uncertain = False
        self.after_setup = None

    def install(self, generation, version):
        files, _raw = payload(version)
        generation.mkdir()
        for name, raw in files.items():
            target = generation / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        (generation / 'maintenance-setup.exe').write_bytes(setup_bytes(version))
        (generation / '.venv').mkdir()
        (generation / '.venv/.installation-complete').touch()
        self.selected = generation
        return generation

    def run_setup(self, executable, root, work, *, timeout=None):
        value = Path(executable).read_bytes()
        assert value in tuple(setup_bytes(version) for version in 'ABC')
        version = value[-1:].decode('ascii')
        source = source_id(version)
        for index in range(1, 100000):
            generation = root / 'versions' / (source[:12] + '-' + str(index))
            if not generation.exists():
                break
        self.install(generation, version)
        self.calls.append((version, generation))
        if self.uncertain:
            raise ActivationCleanupError('Synthetic unconfirmed native setup owner')
        if self.after_setup:
            self.after_setup(generation)
        return 0

    def generation_health(self, generation, manifest_raw, version):
        self.verify_bundle(generation, manifest_bytes=manifest_raw)
        answer = subprocess.check_output([sys.executable, '-I', '-B', str(generation / 'program.py')], timeout=10)
        expected = 'ABC'[int(json.loads(manifest_raw)['source_commit'][0]) - 1].encode('ascii')
        return answer.strip() == expected and not (self.fail and expected == b'B')

    def registered(self, root, generation, work):
        if self.selected != generation:
            raise TrustError('Synthetic current setup selection differs')
        assert generation.parent == root / 'versions'
        return True


@pytest.fixture
def ready(setup_update, repository, tmp_path, clock):
    original = setup_update
    root = tmp_path / 'standard-program'
    (root / 'versions').mkdir(parents=True)
    (root / 'install-layout-v1.txt').write_bytes(module.LAYOUT)
    adapter = SyntheticStandard()
    installed = adapter.install(root / 'versions' / ('1' * 12 + '-1'), 'A')
    (installed / 'unowned-recording.wav').write_bytes(b'preserve this unknown prior file')
    _, manifest = payload('A')
    repository.advance(); repository.publish(envelope())
    client = LocalRepositoryClient(tmp_path / 'standard-cache', repository.policy())
    release = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)
    anchor = original[-2]
    anchor.value = Anchor(1, '1' * 64, clock.now)
    helper = module.StandardUpdateHandoff(original[0], installed, tmp_path / 'standard-updates',
        repository.policy(), anchor, installer_verify(),
        installed_manifest_sha256=sha256(manifest).hexdigest(),
        installed_setup_sha256=sha256(setup_bytes('A')).hexdigest(),
        installed_version='0.1.0', clock=clock, adapter=adapter)
    return helper, package, adapter, original


def test_signed_current_setup_updates_selection_retains_old_generation_and_real_library(ready):
    helper, package, adapter, original = ready
    before = {p.relative_to(helper.program): p.read_bytes() for p in helper.program.rglob('*') if p.is_file()}
    result = helper.apply(package)
    assert result['status'] == 'complete' and result['completion_confirmed'] is True
    assert Path(result['program']) == adapter.selected and adapter.selected != helper.program
    assert (adapter.selected / 'version.txt').read_bytes() == b'B'
    assert {p.relative_to(helper.program): p.read_bytes() for p in helper.program.rglob('*') if p.is_file()} == before
    assert [version for version, _ in adapter.calls] == ['B']
    assert helper.anchor.load().sequence == 2
    assert Path(result['backup']).is_dir() and len(result['backup_sha256']) == 64
    assert helper.recover(package)['status'] == 'complete'
    assert_preserved(original)
    with pytest.raises(TrustError, match='rollback|replay'):
        helper.apply(package)


def test_failed_b_health_repairs_with_exact_old_setup_without_deleting_b(ready):
    helper, package, adapter, original = ready
    adapter.fail = True
    with pytest.raises(TrustError, match='health'):
        helper.apply(package)
    assert [version for version, _ in adapter.calls] == ['B', 'A']
    assert adapter.selected != helper.program
    assert (adapter.selected / 'version.txt').read_bytes() == b'A'
    assert (adapter.calls[0][1] / 'version.txt').read_bytes() == b'B'
    assert helper.anchor.load().sequence == 1
    record = json.loads(helper.journal.read_bytes())
    assert record['phase'] == 'rolled-back' and record['restored'] == adapter.selected.name
    assert helper.recover(package)['program'] == str(adapter.selected)
    assert_preserved(original)


@pytest.mark.parametrize('phase', ['setup-complete', 'health-checked'])
def test_interrupted_acknowledgement_recovers_using_signed_package_and_pinned_a(ready, monkeypatch, phase):
    helper, package, adapter, original = ready
    write = helper._write
    class InterruptedOwner(BaseException):
        pass
    def interrupt(record, selected):
        write(record, selected)
        if selected == phase:
            raise InterruptedOwner()
    with monkeypatch.context() as patch:
        patch.setattr(helper, '_write', interrupt)
        with pytest.raises(InterruptedOwner):
            helper.apply(package)
    assert helper.anchor.load().sequence == 1
    assert helper.recover(package)['status'] == 'rolled-back'
    assert [version for version, _ in adapter.calls] == ['B', 'A']
    assert_preserved(original)


@pytest.mark.parametrize('boundary', ['journal', 'lock-exit', 'owner-release', 'anchor-after'])
@pytest.mark.parametrize('error', [OSError, KeyboardInterrupt])
def test_protected_standard_commit_survives_late_acknowledgement_errors(ready, monkeypatch, boundary, error):
    helper, package, adapter, original = ready
    if boundary == 'journal':
        write = helper._write
        def fail(record, phase):
            if phase == 'complete':
                raise error('late journal')
            return write(record, phase)
        monkeypatch.setattr(helper, '_write', fail)
    elif boundary == 'lock-exit':
        actual = module.locked
        @contextmanager
        def fail(*args, **kwargs):
            with actual(*args, **kwargs):
                yield
            raise error('late lock release')
        monkeypatch.setattr(module, 'locked', fail)
    elif boundary == 'owner-release':
        from carr_library.security import handoff
        def fail(receipt):
            raise error('late owner receipt cleanup')
        monkeypatch.setattr(handoff, '_clear_owner', fail)
    else:
        compare = helper.anchor.compare_and_set
        def fail(*args, **kwargs):
            compare(*args, **kwargs); raise error('protected commit acknowledged late')
        monkeypatch.setattr(helper.anchor, 'compare_and_set', fail)
    result = helper.apply(package)
    assert result['status'] == 'complete' and result['completion_confirmed'] is False
    assert result['warning'] and helper.anchor.load().digest == package.release.sha256
    assert [version for version, _ in adapter.calls] == ['B']
    assert (helper.program / 'version.txt').read_bytes() == b'A'
    assert_preserved(original)


def test_uncertain_native_setup_keeps_owner_veto_and_never_rolls_back_registration(ready):
    helper, package, adapter, original = ready
    adapter.uncertain = True
    with pytest.raises(ActivationCleanupError):
        helper.apply(package)
    assert [version for version, _ in adapter.calls] == ['B']
    assert helper.anchor.load().sequence == 1
    control = portable_entry.control_root(helper.root)
    receipts = list(control.glob('owner-*.json'))
    assert len(receipts) == 1
    before = receipts[0].read_bytes()
    with pytest.raises(RuntimeError, match='complete cleanup'):
        helper.recover(package)
    assert receipts[0].read_bytes() == before
    assert_preserved(original)


@pytest.mark.parametrize('field', ['original', 'candidate', 'setup_before', 'release'])
def test_mutable_standard_journal_cannot_retarget_original_or_signed_candidate(ready, monkeypatch, field):
    helper, package, adapter, original = ready
    write = helper._write
    class InterruptedOwner(BaseException):
        pass
    def fail(record, phase):
        write(record, phase)
        if phase == 'setup-complete':
            raise InterruptedOwner()
    with monkeypatch.context() as patch:
        patch.setattr(helper, '_write', fail)
        with pytest.raises(InterruptedOwner):
            helper.apply(package)
    value = json.loads(helper.journal.read_bytes())
    value[field] = 'foreign'
    helper.journal.write_bytes(json_bytes(value))
    with pytest.raises(TrustError, match='journal'):
        helper.recover(package)
    assert [version for version, _ in adapter.calls] == ['B']
    assert_preserved(original)


def test_cancel_before_native_setup_has_no_install_or_reselection(ready):
    helper, package, adapter, original = ready
    with pytest.raises(UpdateCancelled):
        helper.apply(package, cancelled=lambda: True)
    assert adapter.calls == [] and adapter.selected == helper.program
    assert_preserved(original)


@pytest.mark.parametrize('change', [
    lambda report: report.update(source_commit='3' * 40),
    lambda report: report.update(manifest_sha256='0' * 64),
    lambda report: report.update(status='prepared'),
    lambda report: report.update(update_or_entitlement_authority=True),
    lambda report: report['installer'].update(path='../foreign.exe'),
    lambda report: report['installer'].update(sha256='0' * 64),
    lambda report: report['installer'].update(bytes=1),
])
def test_even_signed_setup_envelope_requires_exact_current_builder_contract(repository, tmp_path, change):
    repository.publish(envelope(change=change))
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    release = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)
    with pytest.raises(TrustError):
        module.prepare_signed_setup(package, repository.policy(), tmp_path / 'prepared')


@pytest.mark.parametrize('extra', ['../outside', 'extra.txt', 'ResonanceSetup.exe'])
def test_setup_envelope_rejects_extra_traversal_and_duplicate_members(repository, tmp_path, extra):
    repository.publish(envelope(extra=extra))
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    release = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)
    with pytest.raises(TrustError):
        module.prepare_signed_setup(package, repository.policy(), tmp_path / 'prepared')
    assert not (tmp_path / 'outside').exists()


def test_foreign_postcommit_generation_is_not_adopted_as_completed(ready, monkeypatch):
    helper, package, adapter, original = ready
    write = helper._write
    def fail(record, phase):
        if phase == 'complete':
            (adapter.selected / 'version.txt').write_bytes(b'foreign')
            raise OSError('late foreign replacement')
        return write(record, phase)
    monkeypatch.setattr(helper, '_write', fail)
    with pytest.raises((OSError, ValueError)):
        helper.apply(package)
    assert helper.anchor.load().sequence == 2
    assert_preserved(original)


def configured_current(helper, adapter, generation, *, build, version):
    raw = (generation / 'bundle-manifest.json').read_bytes()
    from dataclasses import replace
    return module.StandardUpdateHandoff(helper.app, generation, helper.work,
        replace(helper.policy, current_build=build), helper.anchor, installer_verify(),
        installed_manifest_sha256=sha256(raw).hexdigest(),
        installed_setup_sha256=sha256((generation / 'maintenance-setup.exe').read_bytes()).hexdigest(),
        installed_version=version, clock=helper.clock, adapter=adapter)


def third_package(repository, tmp_path):
    repository.advance(); repository.publish(envelope('C'), build=3)
    client = LocalRepositoryClient(tmp_path / 'third-release-cache', repository.policy(current_build=2))
    release = client.check(repository.root, repository.target_path)
    return client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)


def test_two_real_signed_updates_share_workspace_and_preserve_each_terminal_journal(ready, repository, tmp_path):
    helper, package, adapter, original = ready
    first = helper.apply(package)
    old_raw = helper.journal.read_bytes()
    old_record = json.loads(old_raw)
    second_helper = configured_current(helper, adapter, Path(first['program']), build=2, version=first['version'])
    second = second_helper.apply(third_package(repository, tmp_path))
    assert second['status'] == 'complete' and second['completion_confirmed'] is True
    assert helper.anchor.load().sequence == 3
    assert [version for version, _ in adapter.calls] == ['B', 'C']
    assert (helper.work / ('history-' + old_record['transaction'] + '.json')).read_bytes() == old_raw
    assert json.loads(helper.journal.read_bytes())['transaction'] != old_record['transaction']
    assert first['backup'] != second['backup']
    assert Path(first['backup']).is_dir() and Path(second['backup']).is_dir()
    assert (helper.program / 'version.txt').read_bytes() == b'A'
    assert (Path(first['program']) / 'version.txt').read_bytes() == b'B'
    assert (Path(second['program']) / 'version.txt').read_bytes() == b'C'
    assert_preserved(original)


def test_verified_rollback_history_is_retained_before_retry_in_same_workspace(ready):
    helper, package, adapter, original = ready
    adapter.fail = True
    with pytest.raises(TrustError, match='health'):
        helper.apply(package)
    raw = helper.journal.read_bytes()
    record = json.loads(raw)
    restored = adapter.selected
    adapter.fail = False
    next_helper = configured_current(helper, adapter, restored, build=1, version='0.1.0')
    result = next_helper.apply(package)
    assert result['status'] == 'complete' and helper.anchor.load().sequence == 2
    assert [version for version, _ in adapter.calls] == ['B', 'A', 'B']
    assert (helper.work / ('history-' + record['transaction'] + '.json')).read_bytes() == raw
    assert (restored / 'version.txt').read_bytes() == b'A'
    assert adapter.calls[0][1] != adapter.calls[-1][1]
    assert_preserved(original)


@pytest.mark.parametrize('fault', ['unfinished', 'anchor', 'archive', 'selection'])
def test_next_update_cannot_use_mutable_history_as_current_installation_authority(
        ready, repository, tmp_path, fault):
    helper, package, adapter, original = ready
    first = helper.apply(package)
    next_helper = configured_current(helper, adapter, Path(first['program']), build=2, version=first['version'])
    third = third_package(repository, tmp_path)
    record = json.loads(helper.journal.read_bytes())
    if fault == 'unfinished':
        record['phase'] = 'health-checked'
        helper.journal.write_bytes(json_bytes(record))
    elif fault == 'anchor':
        current = helper.anchor.load()
        helper.anchor.value = Anchor(current.sequence, 'f' * 64, current.max_utc)
    elif fault == 'archive':
        (helper.work / ('history-' + record['transaction'] + '.json')).write_bytes(b'different retained evidence')
    else:
        adapter.selected = helper.program
    before = helper.journal.read_bytes()
    with pytest.raises(TrustError):
        next_helper.apply(third)
    assert helper.journal.read_bytes() == before
    assert [version for version, _ in adapter.calls] == ['B']
    if fault == 'archive':
        assert (helper.work / ('history-' + record['transaction'] + '.json')).read_bytes() == b'different retained evidence'
    assert_preserved(original)


def test_late_history_write_failure_retains_old_journal_and_supports_exact_retry(
        ready, repository, tmp_path, monkeypatch):
    from carr_library import durable_state
    helper, package, adapter, original = ready
    first = helper.apply(package)
    next_helper = configured_current(helper, adapter, Path(first['program']), build=2, version=first['version'])
    third = third_package(repository, tmp_path)
    raw = helper.journal.read_bytes()
    record = json.loads(raw)
    actual = durable_state.atomic_bytes
    def late(path, *args, **kwargs):
        actual(path, *args, **kwargs)
        if Path(path).name.startswith('history-'):
            raise OSError('actual history bytes saved, acknowledgement lost')
    with monkeypatch.context() as patch:
        patch.setattr(durable_state, 'atomic_bytes', late)
        with pytest.raises(OSError, match='acknowledgement'):
            next_helper.apply(third)
    assert helper.journal.read_bytes() == raw and helper.anchor.load().sequence == 2
    assert (helper.work / ('history-' + record['transaction'] + '.json')).read_bytes() == raw
    assert next_helper.apply(third)['status'] == 'complete'
    assert [version for version, _ in adapter.calls] == ['B', 'C']
    assert_preserved(original)


def test_actual_recovery_repair_uses_existing_exclusive_library_writer(ready, monkeypatch):
    from carr_library.ingest import writer_guard
    helper, package, adapter, original = ready
    write = helper._write
    class InterruptedOwner(BaseException):
        pass
    def interrupt(record, phase):
        write(record, phase)
        if phase == 'setup-complete':
            raise InterruptedOwner()
    with monkeypatch.context() as patch:
        patch.setattr(helper, '_write', interrupt)
        with pytest.raises(InterruptedOwner):
            helper.apply(package)
    entered = []
    actual_lock = writer_guard.exclusive_writer
    actual_setup = adapter.run_setup
    @contextmanager
    def observed_lock(root, *args, **kwargs):
        with actual_lock(root, *args, **kwargs):
            entered.append(root)
            try:
                yield
            finally:
                entered.pop()
    def repair(*args, **kwargs):
        assert entered == [helper.app.root]
        return actual_setup(*args, **kwargs)
    monkeypatch.setattr(writer_guard, 'exclusive_writer', observed_lock)
    monkeypatch.setattr(adapter, 'run_setup', repair)
    assert helper.recover(package)['status'] == 'rolled-back'
    assert entered == [] and [version for version, _ in adapter.calls] == ['B', 'A']
    assert_preserved(original)
