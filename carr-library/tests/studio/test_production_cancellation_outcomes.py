"""Real local render receipts distinguish late stop requests from failures.

Only publication/verification boundaries are wrapped to inject failures or stops.
Speech, mixing, outputs, and their hashes use the actual installed engines.
"""
from hashlib import sha256
from pathlib import Path
import shutil
import sys

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.speech import FliteProvider, NativeSapiProvider, VoiceRegistry
from carr_library.studio.render import ProductionService


@pytest.fixture(scope='module')
def voices():
    ffmpeg = shutil.which('ffmpeg')
    if sys.platform == 'win32':
        assert ffmpeg and shutil.which('ffprobe'), 'The strict Windows render gate requires FFmpeg and ffprobe.'
        registry = VoiceRegistry([NativeSapiProvider()])
        installed = registry.voices()
        assert installed, 'The strict Windows render gate requires installed native SAPI: ' + '; '.join(registry.notices)
        selected = next((voice for voice in installed if voice.language.casefold().startswith('en')), installed[0])
        return registry, selected.id
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Actual render verification requires installed FFmpeg and ffprobe.')
    registry = VoiceRegistry([FliteProvider(ffmpeg)])
    if 'flite:slt' not in {voice.id for voice in registry.voices()}:
        pytest.skip('Actual render verification requires the installed Flite slt voice.')
    return registry, 'flite:slt'


def production(tmp_path, voices):
    registry, selected_voice = voices
    service = ProductionService(tmp_path / 'state', voices=registry)
    project = service.create('Keep the saved audio.', voice_ids=(selected_voice,))
    output = tmp_path / 'output'
    output.mkdir()
    return service, service.submit(project, output, stems=False)


@pytest.mark.parametrize('stop_kind', ['pause', 'cancel', 'predicate'])
def test_late_stop_does_not_hide_error_after_verified_outputs(tmp_path, voices, monkeypatch, stop_kind):
    service, job = production(tmp_path, voices)
    verify = service._verify_artifacts
    stopping, accepted = [False], {}

    def verify_then_fail(state, *, cancelled=lambda: False):
        verify(state, cancelled=cancelled)
        assert state['status'] == 'complete' and state['artifacts']
        for artifact in state['artifacts']:
            path = Path(artifact['path'])
            accepted[str(path)] = (path.stat().st_size, sha256(path.read_bytes()).hexdigest())
        if stop_kind == 'pause':
            service.request_pause(job)
        elif stop_kind == 'cancel':
            service.cancel(job)
        else:
            stopping[0] = True
        raise OSError('Synthetic final receipt publication failure')

    monkeypatch.setattr(service, '_verify_artifacts', verify_then_fail)
    with pytest.raises(OSError, match='final receipt publication'):
        service.run(job, cancelled=lambda: stopping[0])
    saved = service.get(job)
    assert saved['status'] == 'failed'
    assert saved['error'] == 'Synthetic final receipt publication failure'
    assert saved['artifacts'] and accepted
    for filename, before in accepted.items():
        path = Path(filename)
        assert (path.stat().st_size, sha256(path.read_bytes()).hexdigest()) == before
    verify(saved)  # The genuine error does not delete or rewrite accepted files.


@pytest.mark.parametrize(('stop_kind', 'expected'), [('pause', 'paused'), ('cancel', 'cancelled')])
def test_actual_dsp_cancellation_retains_typed_paused_or_cancelled_receipt(tmp_path, voices, stop_kind, expected):
    service, job = production(tmp_path, voices)
    requested = []

    def before_meter(message):
        if message == 'Measuring the actual completed master':
            requested.append(message)
            if stop_kind == 'pause':
                service.request_pause(job)
            else:
                service.cancel(job)

    with pytest.raises(AudioCancelled):
        service.run(job, progress=before_meter)
    assert len(requested) == 1
    saved = service.get(job)
    assert saved['status'] == expected
    assert saved['artifacts']
    service._verify_artifacts(saved)


def test_stop_during_staged_master_validation_prevents_publication_and_resumes(tmp_path, voices, monkeypatch):
    from carr_library.studio import render
    service, job = production(tmp_path, voices)
    output = Path(service.get(job)['destination'])
    digest = render.digest_file
    stopping = False
    observed = []

    def digest_then_stop(path, *args, **kwargs):
        nonlocal stopping
        result = digest(path, *args, **kwargs)
        path = Path(path)
        if path.parent == output and path.name.startswith('.pending-') and not observed:
            observed.append(path)
            stopping = True
        return result

    monkeypatch.setattr(render, 'digest_file', digest_then_stop)
    with pytest.raises(InterruptedError, match='completed chunks are retained'):
        service.run(job, cancelled=lambda: stopping)
    assert len(observed) == 1
    saved = service.get(job)
    assert saved['status'] == 'paused'
    assert any(Path(row['path']) == output / 'master.wav' for row in saved['artifacts'])
    assert not (output / 'master.wav').exists()
    assert not observed[0].exists()

    stopping = False
    completed = service.run(job, cancelled=lambda: stopping)
    assert completed['status'] == 'complete'
    service._verify_artifacts(completed)
    assert (output / 'master.wav').is_file()


def test_retained_inventory_cancels_during_enumeration_and_closes_iterator(tmp_path, monkeypatch):
    from carr_library.studio import render
    root = tmp_path / 'private-state'
    root.mkdir()
    files = [root / f'retained-{index}.bin' for index in range(8)]
    for path in files:
        path.write_bytes(path.name.encode('ascii'))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    original = render.os.scandir
    stopping, yielded, closed = [False], [], []

    class ObservedEntries:
        def __init__(self, path):
            self.stream = original(path)

        def __enter__(self):
            return self

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.stream)
            yielded.append(entry.name)
            if len(yielded) == 2:
                stopping[0] = True
            return entry

        def __exit__(self, *args):
            self.stream.close()
            closed.append(True)

    monkeypatch.setattr(render.os, 'scandir', ObservedEntries)
    with pytest.raises(InterruptedError, match='disk accounting cancelled'):
        render._retained_bytes((root,), lambda: stopping[0])
    assert len(yielded) == 2 and closed == [True]
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before


def test_retained_inventory_refuses_entry_budget_before_descending(tmp_path, monkeypatch):
    from carr_library.studio import render
    root = tmp_path / 'private-state'
    root.mkdir()
    files = [root / f'retained-{index}.bin' for index in range(8)]
    for path in files:
        path.write_bytes(path.name.encode('ascii'))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    original, inspected = Path.lstat, []

    def observed(path, *args, **kwargs):
        inspected.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(render, '_MAX_RETAINED_ENTRIES', 3)
    monkeypatch.setattr(Path, 'lstat', observed)
    with pytest.raises(ValueError, match='entry budget'):
        render._retained_bytes((root,), lambda: False)
    assert inspected == [root], 'The bounded pending list must reject before any child descent.'
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before

@pytest.fixture
def submission(tmp_path):
    # Only queue creation is under test; no voice synthesis is invoked.
    from types import SimpleNamespace
    registry = SimpleNamespace(voices=lambda: (SimpleNamespace(id='test:voice'),))
    service = ProductionService(tmp_path / 'state', voices=registry)
    project = service.create('Synthetic submission.', voice_ids=('test:voice',))
    output = tmp_path / 'output'
    output.mkdir()
    return service, project, output


@pytest.mark.parametrize('committed', [False, True])
def test_submission_retries_same_operation_after_receipt_write_failure(submission, monkeypatch, committed):
    from carr_library.studio import render
    service, project, output = submission
    job = 'receipt-replay'
    directory = service.jobs_root / job
    write = render.atomic_json

    def fail_receipt(path, value, **kwargs):
        if Path(path) == directory / 'job.json':
            if committed:
                write(path, value, **kwargs)
            else:
                # Simulate a process interruption before publishing the receipt.
                (directory / '.pending-interrupted').write_bytes(b'partial receipt')
            raise OSError('Injected receipt write interruption')
        return write(path, value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(render, 'atomic_json', fail_receipt)
        with pytest.raises(OSError, match='Injected receipt'):
            service.submit(project, output, operation_id=job)
    assert (directory / 'job.json').exists() is committed
    reopened = ProductionService(service.root, voices=service.voices)
    assert reopened.submit(project, output, operation_id=job) == job
    receipt = reopened.get(job)
    assert receipt['status'] == 'pending'
    assert receipt['fingerprint'] == project.fingerprint
    assert len(reopened.jobs()) == 1
    assert reopened.submit(project, output, operation_id=job) == job
    assert reopened.get(job) == receipt
    with pytest.raises(ValueError, match='different production plan'):
        reopened.submit(project, output, operation_id=job, stems=False)
    if not committed:
        assert (directory / '.pending-interrupted').read_bytes() == b'partial receipt'


def test_submission_does_not_adopt_unreceipted_recovery_data(submission):
    service, project, output = submission
    directory = service.jobs_root / 'missing-receipt'
    directory.mkdir()
    recovery = directory / 'timeline.json'
    recovery.write_bytes(b'preserve existing recovery data')
    with pytest.raises(ValueError, match='recovery data but no receipt'):
        service.submit(project, output, operation_id=directory.name)
    assert recovery.read_bytes() == b'preserve existing recovery data'
    assert not (directory / 'job.json').exists()


@pytest.mark.parametrize('legacy', [False, True])
def test_submission_does_not_adopt_existing_unreceipted_output(submission, legacy):
    from carr_library.studio.render import _output_name, _production_output_name
    service, project, output = submission
    job = 'existing-output'
    name = (f'{_output_name(project.title)}-{job[:12]}' if legacy
            else _production_output_name(project.title, job))
    existing = output / name
    existing.mkdir()
    master = existing / 'master.wav'
    master.write_bytes(b'preserve existing output')
    with pytest.raises(FileExistsError, match='without a matching job receipt'):
        service.submit(project, output, operation_id=job)
    assert master.read_bytes() == b'preserve existing output'
    assert not (service.jobs_root / job / 'job.json').exists()


def test_submission_respects_existing_render_lock(submission):
    from carr_library.durable_state import state_lock
    service, project, output = submission
    job = 'locked-submit'
    with state_lock(service.jobs_root / job, 'render'):
        with pytest.raises(RuntimeError, match='already being changed'):
            service.submit(project, output, operation_id=job)
        assert not (service.jobs_root / job / 'job.json').exists()
    assert service.submit(project, output, operation_id=job) == job


@pytest.mark.parametrize('phase', ['before-receipt', 'after-receipt'])
def test_submission_recovers_actual_process_exit_without_rewriting_receipt(submission, phase):
    import subprocess
    import carr_library

    service, project, output = submission
    job = 'child-exit-receipt'
    directory = service.jobs_root / job
    receipt_path = directory / 'job.json'
    # Select exactly the package already imported by this gate. In an installed
    # lane this remains the installed package; no checkout path is substituted.
    package_file = Path(carr_library.__file__).resolve()
    child = r'''
import os
from pathlib import Path
import sys
from types import SimpleNamespace

expected = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(expected.parent.parent))
import carr_library
assert Path(carr_library.__file__).resolve() == expected
assert sys.flags.isolated and sys.dont_write_bytecode
from carr_library import durable_state
from carr_library.studio import render

registry = SimpleNamespace(voices=lambda: (SimpleNamespace(id='test:voice'),))
service = render.ProductionService(Path(sys.argv[2]), voices=registry)
project = service.projects.load(sys.argv[3])
output, job, phase = Path(sys.argv[4]), sys.argv[5], sys.argv[6]
receipt = service.jobs_root / job / 'job.json'
original_link = durable_state.os.link
original_json = render.atomic_json

def interrupted_link(source, destination, *args, **kwargs):
    if Path(destination) == receipt and phase == 'before-receipt':
        # The real writer has flushed its temporary bytes, but has not linked
        # the receipt. Immediate process exit bypasses both cleanup and unlock.
        os._exit(73)
    return original_link(source, destination, *args, **kwargs)

def interrupted_acknowledgement(path, value, **kwargs):
    result = original_json(path, value, **kwargs)
    if Path(path) == receipt and phase == 'after-receipt':
        # A fully published durable receipt exists; the caller got no reply.
        os._exit(73)
    return result

durable_state.os.link = interrupted_link
render.atomic_json = interrupted_acknowledgement
service.submit(project, output, operation_id=job)
raise AssertionError('The requested receipt boundary was never reached.')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', child,
                             str(package_file), str(service.root), project.id,
                             str(output), job, phase],
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)
    assert receipt_path.exists() is (phase == 'after-receipt')
    before = receipt_path.read_bytes() if receipt_path.exists() else None
    pending = {path: path.read_bytes() for path in directory.glob('.pending-*')}
    assert len(pending) == (1 if phase == 'before-receipt' else 0)

    reopened = ProductionService(service.root, voices=service.voices)
    assert reopened.submit(project, output, operation_id=job) == job
    receipt = reopened.get(job)
    assert receipt['status'] == 'pending' and receipt['fingerprint'] == project.fingerprint
    assert len(reopened.jobs()) == 1
    assert not Path(receipt['destination']).exists(), 'Submission must not adopt or publish audio.'
    if before is not None:
        assert receipt_path.read_bytes() == before
    assert {path: path.read_bytes() for path in directory.glob('.pending-*')} == pending
    committed = receipt_path.read_bytes()
    assert reopened.submit(project, output, operation_id=job) == job
    assert receipt_path.read_bytes() == committed


@pytest.mark.parametrize('kind', ['directory', 'hardlink'])
def test_submission_rejects_nonregular_pending_recovery_entries(submission, kind):
    service, project, output = submission
    directory = service.jobs_root / 'unsafe-pending'
    directory.mkdir()
    pending = directory / '.pending-uncommitted'
    retained = output / 'independent-data.txt'
    retained.write_bytes(b'Independent data must remain untouched.')
    if kind == 'directory':
        pending.mkdir()
    else:
        pending.hardlink_to(retained)
    with pytest.raises(ValueError, match='regular local files'):
        service.submit(project, output, operation_id=directory.name)
    assert not (directory / 'job.json').exists()
    assert retained.read_bytes() == b'Independent data must remain untouched.'
    if kind == 'directory':
        assert pending.is_dir() and list(pending.iterdir()) == []
    else:
        assert pending.samefile(retained)


def test_completed_replay_requires_full_inventory_and_owner(tmp_path, voices, monkeypatch):
    from copy import deepcopy
    from carr_library.durable_state import atomic_json, read_json
    service, job = production(tmp_path, voices)
    complete = service.run(job)
    output = Path(complete['destination'])
    original = {row['path']: row['sha256'] for row in complete['artifacts']}
    receipt = service.jobs_root / job / 'job.json'
    marker = output / 'production-owner.json'
    owner = read_json(marker)

    def forbid_render():
        pytest.fail('Committed replay must not invoke the audio runtime.')

    monkeypatch.setattr(service, '_runtime', forbid_render)
    assert service.run(job) == complete
    for missing in ('master.wav', 'script.vtt', 'script.srt', 'timeline.json', 'project.json', 'chapter'):
        damaged = deepcopy(complete)
        damaged['artifacts'] = [
            row for row in damaged['artifacts']
            if (Path(row['path']).parent.name != 'chapters' if missing == 'chapter'
                else Path(row['path']).name != missing)
        ]
        assert len(damaged['artifacts']) < len(complete['artifacts'])
        atomic_json(receipt, damaged, replace=True)
        with pytest.raises(ValueError, match='missing required artifacts'):
            service.run(job)
        assert read_json(receipt) == damaged

    damaged = deepcopy(complete)
    damaged['artifacts'] = []
    atomic_json(receipt, damaged, replace=True)
    with pytest.raises(ValueError, match='missing required artifacts'):
        service.run(job)

    damaged = deepcopy(complete)
    damaged['artifacts'].append(damaged['artifacts'][0])
    atomic_json(receipt, damaged, replace=True)
    with pytest.raises(ValueError, match='Duplicate production artifact'):
        service.run(job)

    atomic_json(receipt, complete, replace=True)
    atomic_json(marker, {**owner, 'job': 'another-job'}, replace=True)
    with pytest.raises(ValueError, match='owner receipt changed'):
        service.run(job)
    atomic_json(marker, owner, replace=True)
    assert service.run(job) == complete
    assert {name: sha256(Path(name).read_bytes()).hexdigest() for name in original} == original


def test_pending_operation_options_are_bound(submission):
    from carr_library.durable_state import atomic_json, read_json
    service, project, output = submission
    job = service.submit(project, output, format='flac', stems=True)
    receipt = service.jobs_root / job / 'job.json'
    saved = read_json(receipt)
    for options in ({'format': 'wav'}, {'stems': False}, {'stems': 1}):
        damaged = {**saved, **options}
        atomic_json(receipt, damaged, replace=True)
        with pytest.raises(ValueError, match='production options|operation options'):
            service.get(job)
        assert read_json(receipt) == damaged
    atomic_json(receipt, saved, replace=True)
    assert service.get(job) == saved


def test_completed_operation_options_cannot_hide_missing_outputs(tmp_path, voices, monkeypatch):
    from copy import deepcopy
    from carr_library.durable_state import atomic_json, read_json
    from carr_library.studio.render import _operation_sha256
    registry, selected_voice = voices
    service = ProductionService(tmp_path / 'state', voices=registry)
    project = service.create('Synthetic options test.', voice_ids=(selected_voice,))
    output = tmp_path / 'output'
    output.mkdir()
    job = service.submit(project, output, format='flac', stems=True)
    complete = service.run(job)
    receipt = service.jobs_root / job / 'job.json'
    marker = Path(complete['destination']) / 'production-owner.json'
    owner = read_json(marker)
    assert owner['operation_sha256'] == complete['operation_sha256']
    assert any(Path(a['path']).name == 'audiobook.flac' for a in complete['artifacts'])
    assert any(Path(a['path']).parent.name == 'stems' for a in complete['artifacts'])
    monkeypatch.setattr(service, '_runtime', lambda: pytest.fail('Must not render committed output.'))
    damaged = deepcopy(complete)
    damaged['format'], damaged['stems'] = 'wav', False
    damaged['artifacts'] = [a for a in complete['artifacts']
                            if Path(a['path']).name != 'audiobook.flac'
                            and Path(a['path']).parent.name != 'stems']
    atomic_json(receipt, damaged, replace=True)
    with pytest.raises(ValueError, match='operation options changed'):
        service.run(job)
    # The separately published owner receipt rejects a recalculated identity
    # or a missing new field; neither is treated as a legacy owner.
    damaged['operation_sha256'] = _operation_sha256(damaged)
    atomic_json(receipt, damaged, replace=True)
    with pytest.raises(ValueError, match='owner receipt changed'):
        service.run(job)
    del damaged['operation_sha256']
    atomic_json(receipt, damaged, replace=True)
    with pytest.raises(ValueError, match='owner receipt changed'):
        service.run(job)
    assert read_json(marker) == owner
    atomic_json(receipt, complete, replace=True)
    assert service.run(job) == complete

    # Legacy jobs have no separate option identity. Keep them readable and
    # verify the complete inventory; never silently backfill historical proof.
    legacy = {k: v for k, v in complete.items() if k != 'operation_sha256'}
    legacy_owner = {k: v for k, v in owner.items() if k != 'operation_sha256'}
    atomic_json(receipt, legacy, replace=True)
    atomic_json(marker, legacy_owner, replace=True)
    assert service.run(job) == legacy
    assert read_json(receipt) == legacy
    assert read_json(marker) == legacy_owner



def test_production_distinct_full_operation_ids_do_not_share_output(submission):
    service, project, output = submission
    first = 'sameprefix12-first'
    second = 'sameprefix12-second'
    assert first[:12] == second[:12]
    assert service.submit(project, output, operation_id=first) == first
    assert service.submit(project, output, operation_id=second) == second
    saved_first, saved_second = service.get(first), service.get(second)
    assert saved_first['destination'] != saved_second['destination']
    assert Path(saved_first['destination']).parent == output
    assert Path(saved_second['destination']).parent == output
    reopened = ProductionService(service.root, voices=service.voices)
    assert reopened.submit(project, output, operation_id=first) == first
    assert reopened.submit(project, output, operation_id=second) == second
    assert reopened.get(first) == saved_first
    assert reopened.get(second) == saved_second


def test_production_preserves_legacy_destination_on_replay(submission):
    from carr_library.durable_state import atomic_json, read_json
    from carr_library.studio.render import _output_name, _owner_receipt
    service, project, output = submission
    first, second = 'sameprefix12-first', 'sameprefix12-second'
    service.submit(project, output, operation_id=first)
    path = service.jobs_root / first / 'job.json'
    state = service.get(first)
    legacy = output / f'{_output_name(project.title)}-{first[:12]}'
    state['destination'] = str(legacy)
    atomic_json(path, state, replace=True)
    legacy.mkdir()
    marker = legacy / 'production-owner.json'
    atomic_json(marker, _owner_receipt(state))
    original = legacy / 'master.wav'
    original.write_bytes(b'synthetic retained output')
    raw, owner_raw = path.read_bytes(), marker.read_bytes()

    # Replay of a prior receipt keeps its original location and bytes.
    assert service.submit(project, output, operation_id=first) == first
    assert service.get(first) == state
    assert service.submit(project, output, operation_id=second) == second
    assert Path(service.get(second)['destination']) != legacy
    assert path.read_bytes() == raw and marker.read_bytes() == owner_raw
    assert original.read_bytes() == b'synthetic retained output'
    assert read_json(marker) == _owner_receipt(state)
    # An orphaned legacy output of this operation still cannot be adopted.
    path.unlink()
    with pytest.raises(FileExistsError, match='without a matching job receipt'):
        service.submit(project, output, operation_id=first)
    assert original.read_bytes() == b'synthetic retained output'


def test_production_output_component_stays_bounded():
    from carr_library.studio.render import _production_output_name
    name = _production_output_name('Український' * 100, 'a' * 80)
    assert len(name) <= 83
    assert len(name.encode('utf-8')) <= 255
    assert name != _production_output_name('Український' * 100, 'a' * 79 + 'b')
