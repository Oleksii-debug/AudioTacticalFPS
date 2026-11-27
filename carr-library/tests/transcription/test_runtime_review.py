"""Actual supplied-protocol children with passive installed-file review.

The synthetic peers are the existing public receipt/V5 fixtures. No model is
loaded, downloaded or acoustically qualified by this suite.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import venv

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
from carr_library.application import LibraryApp
from carr_library import transcription
from carr_library.durable_state import atomic_json, read_json
from carr_library.transcription_runtime import RUNTIME_DISTRIBUTIONS


def make_peer(root, *, protocol='receipts-v1', mode='complete'):
    root = Path(root)
    root.mkdir(parents=True)
    app = LibraryApp(root / 'library')
    workspace = app.create_workspace('Reviewed supplied protocol')
    service = app.transcription(workspace)
    environment = root / 'selected-python'
    venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(environment)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    site = (environment / 'Lib/site-packages' if os.name == 'nt'
            else next((environment / 'lib').glob('python*/site-packages')))
    payloads = {}
    for name in RUNTIME_DISTRIBUTIONS:
        label = 'synthetic_' + name.replace('-', '_') + '.py'
        payload = site / label
        payload.write_text('# Inert installed-inventory fixture; no inference.\nVALUE = 1\n', encoding='utf-8')
        info = site / (name.replace('-', '_') + '-0.synthetic.dist-info')
        info.mkdir()
        (info / 'METADATA').write_text(
            f'Metadata-Version: 2.1\nName: {name}\nVersion: 0.synthetic\n', encoding='utf-8')
        (info / 'RECORD').write_text(
            '\n'.join((label + ',,', info.name + '/METADATA,,', info.name + '/RECORD,,')) + '\n',
            encoding='utf-8')
        payloads[name] = payload
    worker = root / 'worker.py'
    fixture_path = Path(__file__).with_name(
        'test_v5_worker_profile.py' if protocol == transcription.V5_PROTOCOL
        else 'test_supplied_worker_bridge.py')
    fixture = runpy.run_path(str(fixture_path))
    source = (fixture['SYNTHETIC_V5_WORKER'] if protocol == transcription.V5_PROTOCOL
              else fixture['SYNTHETIC_WORKER'].replace('__MODE__', repr(mode)))
    worker.write_text(source, encoding='utf-8')
    models = root / 'models'
    models.mkdir()
    if protocol == transcription.V5_PROTOCOL:
        for name in transcription.V5_SOURCES[1:]:
            (root / name).write_text('# Existing V5 protocol fixture helper only.\n', encoding='utf-8')
        model = models / 'small'
        model.mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (model / name).write_bytes(('Existing public V5 synthetic identity: ' + name).encode())
    original = root / 'source.wav'
    original.write_bytes(b'Existing synthetic protocol source; never decoded as audio.')
    configured = service.configure(python, worker, models, protocol=protocol)
    return SimpleNamespace(root=root, app=app, workspace=workspace, service=service,
        environment=environment, python=python, site=site, payloads=payloads,
        worker=worker, models=models, source=original, configured=configured)


@pytest.fixture
def peer(tmp_path):
    return make_peer(tmp_path / 'peer')


def command_environment():
    roots = (Path(carr_library.__file__).resolve().parents[1],
             Path(bootstrap_hasher.__file__).resolve().parents[3])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, dict.fromkeys(roots))))


def test_inspection_is_inert_and_named_approval_is_exact(peer, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Runtime inspection executed a selected process.')
    monkeypatch.setattr(transcription, 'run_process', forbidden)
    inspected = peer.service.inspect_runtime(peer.python, peer.worker, peer.models)
    assert inspected['inference_qualified'] is False
    assert set(inspected['runtime_inventory']['runtime_distributions']) == set(RUNTIME_DISTRIBUTIONS)
    assert all(row['present'] for row in inspected['runtime_inventory']['runtime_distributions'].values())
    untrusted = peer.service.register_runtime('Not yet approved', peer.python, peer.worker, peer.models)
    assert untrusted['trusted'] is False
    with pytest.raises(ValueError, match='fingerprint'):
        peer.service.register_runtime('Stale approval', peer.python, peer.worker, peer.models,
            owner_trusted=True, expected_fingerprint='0' * 64)
    profile = peer.service.register_runtime('Reviewed local peer', peer.python, peer.worker, peer.models,
        owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    assert profile['id'] == inspected['fingerprint'][:32]
    assert profile['configuration'] == inspected
    assert profile['qualification'] == 'unqualified'
    assert peer.service.list_runtimes()[-1]['id'] == profile['id']


@pytest.mark.parametrize('changed', ['installed-file', 'record', 'record-extra-fields', 'record-size', 'pyvenv', 'path-config', 'package-removed'])
def test_installed_runtime_change_refuses_before_actual_child(peer, changed):
    identity = peer.service.submit([peer.source])
    if changed == 'installed-file':
        peer.payloads['numpy'].write_text('# Changed reviewed package bytes.\n', encoding='utf-8')
    elif changed.startswith('record'):
        path = peer.site / 'numpy-0.synthetic.dist-info' / 'RECORD'
        invalid_row = {'record': '\n',
            'record-extra-fields': 'synthetic_numpy.py,,,extra\n',
            'record-size': 'synthetic_numpy.py,,not-a-byte-count\n'}[changed]
        path.write_text(path.read_text(encoding='utf-8') + invalid_row, encoding='utf-8')
    elif changed == 'pyvenv':
        path = peer.environment / 'pyvenv.cfg'
        path.write_text(path.read_text(encoding='utf-8') + '\n# Changed reviewed configuration.\n', encoding='utf-8')
    elif changed == 'path-config':
        (peer.site / 'extra.pth').write_text('# Inert changed path configuration.\n', encoding='utf-8')
    else:
        (peer.site / 'numpy-0.synthetic.dist-info').rename(peer.site / 'numpy-removed')
    with pytest.raises(transcription.RuntimeReviewError):
        peer.service.run(identity, timeout=20)
    assert not (peer.service._path(identity) / 'job_status.json').exists()
    assert peer.service.get(identity)['done'] == 0


def test_changed_operational_base_support_refuses_without_executing_it(peer):
    suffix = 'python-guard.dll' if os.name == 'nt' else 'python-guard.zip'
    support = peer.python.parent / suffix
    support.write_bytes(b'Inert declared Python support fixture.')
    peer.service.configure(peer.python, peer.worker, peer.models)
    identity = peer.service.submit([peer.source])
    support.write_bytes(b'Changed declared Python support fixture.')
    with pytest.raises(transcription.RuntimeReviewError):
        peer.service.run(identity, timeout=20)
    assert not (peer.service._path(identity) / 'job_status.json').exists()


def test_revalidates_installed_files_after_actual_resource_admission(peer, monkeypatch):
    broker = peer.service._admission_broker(lambda: False)
    reserve = broker.reserve
    @contextmanager
    def changed_after_wait(*args, **kwargs):
        with reserve(*args, **kwargs) as lease:
            peer.payloads['av'].write_text('# Changed after the real reservation.\n', encoding='utf-8')
            yield lease
    monkeypatch.setattr(broker, 'reserve', changed_after_wait)
    identity = peer.service.submit([peer.source])
    with pytest.raises(transcription.RuntimeReviewError):
        peer.service.run(identity, timeout=20)
    assert broker.snapshot()['leases'] == []
    assert not (peer.service._path(identity) / 'job_status.json').exists()


@pytest.mark.parametrize('protocol', ['receipts-v1', transcription.V5_PROTOCOL])
@pytest.mark.parametrize('moment', ['after-receipt-validation', 'after-durable-commit'])
def test_actual_child_runtime_drift_cannot_publish_or_resurrect_cache(tmp_path, monkeypatch, protocol, moment):
    peer = make_peer(tmp_path / 'peer', protocol=protocol)
    service = peer.service
    identity = service.submit([peer.source])
    path = service._path(identity)
    original = peer.payloads['numpy'].read_bytes()
    changed = []
    def mutate():
        if not changed:
            changed.append(True)
            peer.payloads['numpy'].write_bytes(original + b'# Changed at the actual admission barrier.\n')
    if moment == 'after-receipt-validation':
        checked = service._receipts
        def after_checked(*args, **kwargs):
            result = checked(*args, **kwargs)
            mutate()
            return result
        monkeypatch.setattr(service, '_receipts', after_checked)
    else:
        saved = service._save
        def after_saved(where, value):
            result = saved(where, value)
            if value['status'] == 'completed':
                mutate()
            return result
        monkeypatch.setattr(service, '_save', after_saved)
    result = service.run(identity, timeout=30)
    assert changed and (path / 'job_status.json').exists(), 'The actual protocol child must have run.'
    assert result['status'] == 'failed' and result['done'] == 0
    assert result['receipts'] == result['artifacts'] == []
    rejected = (path / 'rejected-execution.json').read_bytes()
    assert read_json(path / 'rejected-execution.json')['job'] == identity
    # Listing a known rejected/closed attempt must not turn it into an orphan.
    # Its veto still refuses old cache, while a fresh retained-input copy works.
    observed = LibraryApp(peer.app.root).transcription(peer.workspace).get(identity)
    assert observed['status'] == 'failed' and observed['recovery_review_required'] is False
    assert (path / 'input' / result['sources'][0]['filename']).read_bytes() == peer.source.read_bytes()
    peer.payloads['numpy'].write_bytes(original)
    service.configure(peer.python, peer.worker, peer.models, protocol=protocol)
    with pytest.raises(transcription.RuntimeReviewError, match='rejected'):
        service.run(identity, timeout=30)
    with pytest.raises(transcription.RuntimeReviewError, match='rejected'):
        service.continue_job(identity)
    assert (path / 'rejected-execution.json').read_bytes() == rejected
    # Explicit recovery is a fresh ordinary job, never the rejected workspace.
    recreated = service.copy_job(identity, profile_id=service.configuration()['profile_id'])
    assert recreated['id'] != identity and recreated['status'] == 'ready'
    assert recreated['sources'][0]['original_name'] == result['sources'][0]['original_name']
    assert recreated['sources'][0]['sha256'] == result['sources'][0]['sha256']
    assert not (service._path(recreated['id']) / 'job_status.json').exists()


def test_cancel_after_actual_durable_publication_preserves_verified_result(peer, monkeypatch):
    identity = peer.service.submit([peer.source])
    stopped = threading.Event()
    saved = peer.service._save
    def after_saved(path, value):
        result = saved(path, value)
        if value['status'] == 'completed':
            stopped.set()
        return result
    monkeypatch.setattr(peer.service, '_save', after_saved)
    result = peer.service.run(identity, cancelled=stopped.is_set, timeout=30)
    assert stopped.is_set() and result['status'] == 'completed' and result['done'] == 1
    assert peer.service.verify(identity)['status'] == 'completed'
    assert not (peer.service._path(identity) / 'rejected-execution.json').exists()


def test_named_job_keeps_reviewed_profile_when_global_selection_changes(peer):
    inspection = peer.service.inspect_runtime(peer.python, peer.worker, peer.models)
    profile = peer.service.register_runtime('Named reviewed peer', peer.python, peer.worker, peer.models,
        owner_trusted=True, expected_fingerprint=inspection['fingerprint'])
    identity = peer.service.submit([peer.source], profile_id=profile['id'])
    other = peer.root / 'other-worker.py'
    old = peer.worker.read_text(encoding='utf-8')
    assert "MODE = 'complete'" in old
    other.write_text(old.replace("MODE = 'complete'", "MODE = 'network'"), encoding='utf-8')
    peer.service.configure(peer.python, other, peer.models)
    result = peer.service.run(identity, timeout=30)
    assert result['status'] == 'completed', result['last_error']
    assert result['runtime_profile_id'] == profile['id']
    assert result['run_context']['worker_sha256'] == sha256(peer.worker.read_bytes()).hexdigest()
    assert peer.service.configuration()['worker'] == str(other)


def test_legacy_configuration_is_passive_until_explicit_new_review(peer):
    old = {key: value for key, value in peer.configured.items() if key not in (
        'runtime_inventory', 'models_inventory', 'fingerprint', 'profile_id', 'execution_review_required')}
    atomic_json(peer.service.root / 'engine.json', old, replace=True)
    assert peer.service.configuration()['models_dir'] == str(peer.models)
    identity = peer.service.submit([peer.source])
    with pytest.raises(transcription.RuntimeReviewError, match='legacy'):
        peer.service.run(identity, timeout=20)
    peer.service.configure(peer.python, peer.worker, peer.models)
    assert peer.service.run(identity, timeout=30)['status'] == 'completed'

def test_failed_rejection_write_keeps_host_veto_after_actual_candidate_commit(peer, monkeypatch):
    import errno
    identity = peer.service.submit([peer.source])
    path = peer.service._path(identity)
    original = peer.payloads['numpy'].read_bytes()
    save, write = peer.service._save, transcription.atomic_json
    committed = []
    def after_candidate(where, value):
        result = save(where, value)
        if value['status'] == 'completed':
            committed.append(True)
            peer.payloads['numpy'].write_bytes(original + b'# Drift after the durable candidate.\n')
        return result
    def no_rejection_space(target, value, **kwargs):
        if Path(target).name == 'rejected-execution.json':
            raise OSError(errno.ENOSPC, 'Actual final rejection publication has no space.')
        return write(target, value, **kwargs)
    monkeypatch.setattr(peer.service, '_save', after_candidate)
    monkeypatch.setattr(transcription, 'atomic_json', no_rejection_space)
    with pytest.raises(OSError) as error:
        peer.service.run(identity, timeout=30)
    assert error.value.errno == errno.ENOSPC
    assert committed and read_json(path / 'job.json')['status'] == 'completed'
    assert not (path / 'rejected-execution.json').exists()
    veto_path = peer.service._review_path(identity)
    veto = veto_path.read_bytes()
    assert veto_path.parent.parent == peer.service.jobs_root.parent
    assert not veto_path.is_relative_to(path), 'The child cannot erase its host review veto.'
    peer.payloads['numpy'].write_bytes(original)
    reopened = LibraryApp(peer.app.root).transcription(peer.workspace)
    current = reopened.get(identity)
    assert current['status'] == 'interrupted' and current['recovery_review_required'] is True
    for operation in (reopened.verify, reopened.run, reopened.continue_job):
        with pytest.raises(transcription.RuntimeReviewError):
            operation(identity)
    acknowledged = reopened.acknowledge_interruption(identity, worker_stopped=True)
    assert acknowledged['status'] == 'failed' and acknowledged['receipts'] == acknowledged['artifacts'] == []
    copied = reopened.copy_job(identity, profile_id=peer.configured['profile_id'])
    assert copied['status'] == 'ready' and copied['id'] != identity
    assert copied['sources'][0]['sha256'] == current['sources'][0]['sha256']
    assert veto_path.read_bytes() == veto


def test_reviewing_changed_installed_files_never_upgrades_raw_worker_cache(tmp_path):
    peer = make_peer(tmp_path / 'peer', mode='pause-once')
    identity = peer.service.submit([peer.source])
    path = peer.service._path(identity)
    stopped = peer.service.run(identity, timeout=30,
        cancelled=lambda: (path / 'job_status.json').exists())
    assert stopped['status'] == 'stopped' and stopped['done'] == 0
    assert (path / 'continued').exists(), 'The actual peer must acknowledge stop before review.'
    old_status = (path / 'job_status.json').read_bytes()
    old_configuration = stopped['run_context']['configuration_fingerprint']
    peer.payloads['numpy'].write_bytes(peer.payloads['numpy'].read_bytes() + b'# Explicitly reviewed change.\n')
    reviewed = peer.service.configure(peer.python, peer.worker, peer.models)
    assert reviewed['fingerprint'] != old_configuration
    refused = peer.service.run(identity, timeout=30)
    assert refused['status'] == 'failed' and refused['done'] == 0
    assert (path / 'job_status.json').read_bytes() == old_status
    assert (path / 'rejected-execution.json').exists()
    copied = peer.service.copy_job(identity, profile_id=reviewed['profile_id'])
    assert copied['status'] == 'ready'
    assert copied['sources'][0]['sha256'] == stopped['sources'][0]['sha256']
    assert not (peer.service._path(copied['id']) / 'job_status.json').exists()

def test_late_cancellation_cannot_hide_actual_postcommit_runtime_drift(peer, monkeypatch):
    identity = peer.service.submit([peer.source])
    stopped = threading.Event()
    save = peer.service._save
    def after_candidate(path, value):
        result = save(path, value)
        if value['status'] == 'completed':
            peer.payloads['numpy'].write_bytes(peer.payloads['numpy'].read_bytes() + b'# Late drift.\n')
            stopped.set()
        return result
    monkeypatch.setattr(peer.service, '_save', after_candidate)
    result = peer.service.run(identity, cancelled=stopped.is_set, timeout=30)
    assert stopped.is_set()
    assert result['status'] == 'failed' and result['done'] == 0
    assert result['receipts'] == result['artifacts'] == []
    assert peer.service._review_path(identity).exists()
    assert (peer.service._path(identity) / 'rejected-execution.json').exists()
