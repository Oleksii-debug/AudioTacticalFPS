"""Real process controls and bounded intake on the single supplied-worker service."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.models import AudioCancelled
from carr_library.durable_state import read_json

_helpers = runpy.run_path(str(Path(__file__).with_name('test_runtime_review.py')))
make_peer = _helpers['make_peer']
command_environment = _helpers['command_environment']


def wait_for(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Actual process barrier was not reached.')
        time.sleep(.02)


@pytest.mark.parametrize('action,terminal', [('pause', 'paused'), ('cancel', 'cancelled')])
def test_actual_other_process_controls_live_worker_then_explicit_resume(tmp_path, action, terminal):
    peer = make_peer(tmp_path / 'peer', mode='pause-once')
    service = peer.service
    identity = service.submit([peer.source])
    path = service._path(identity)
    stop = threading.Event()
    output, errors = [], []
    def run():
        try:
            output.append(service.run(identity, timeout=40, cancelled=stop.is_set))
        except BaseException as exc:
            errors.append(exc)
    owner = threading.Thread(target=run)
    owner.start()
    try:
        wait_for(lambda: (path / 'job_status.json').exists())
        assert read_json(path / 'job_status.json')['state'] == 'running'
        with pytest.raises(RuntimeError, match='another operation'):
            service.acknowledge_interruption(identity, worker_stopped=True)
        code = (
            "import json,sys\n"
            "from carr_library.application import LibraryApp\n"
            "s=LibraryApp(sys.argv[1]).transcription(sys.argv[2])\n"
            "r=getattr(s,sys.argv[4])(sys.argv[3])\n"
            "print(json.dumps(r))\n")
        requested = subprocess.run([sys.executable, '-B', '-s', '-P', '-X', 'utf8', '-c', code,
            str(peer.app.root), peer.workspace, identity, action],
            env=command_environment(), capture_output=True, text=True, encoding='utf-8', timeout=25)
        assert requested.returncode == 0, requested.stdout + requested.stderr
        # The child may acknowledge before the controller reacquires the job
        # lock. Both an in-flight request and its confirmed terminal state are valid.
        assert json.loads(requested.stdout)['status'] in (
            'pausing' if action == 'pause' else 'cancelling', terminal)
        owner.join(12)
        assert not owner.is_alive() and not errors
        assert output[0]['status'] == terminal and output[0]['done'] == 0
        assert output[0]['receipts'] == output[0]['artifacts'] == []
        assert (path / 'continued').exists(), 'The unchanged actual child acknowledged stop.flag.'
        assert service._admission_broker(lambda: False).snapshot()['leases'] == []
        reopened = LibraryApp(peer.app.root).transcription(peer.workspace)
        assert reopened.run(identity, timeout=30)['status'] == terminal
        result = reopened.resume(identity, timeout=30)
        assert result['status'] == 'completed' and result['done'] == 1
        assert (path / 'input' / result['sources'][0]['filename']).read_bytes() == peer.source.read_bytes()
    finally:
        stop.set()
        owner.join(12)
        assert not owner.is_alive(), 'The actual child/owner must close before fixture cleanup.'


def test_kernel_released_orphan_lock_requires_explicit_acknowledgement(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    identity = peer.service.submit([peer.source])
    code = (
        "import os,sys\n"
        "from carr_library.application import LibraryApp\n"
        "from carr_library.durable_state import state_lock\n"
        "s=LibraryApp(sys.argv[1]).transcription(sys.argv[2]); p=s._path(sys.argv[3])\n"
        "with state_lock(p,'transcribe'):\n"
        " j=s.get(sys.argv[3]); j['status']='running'; s._save(p,j)\n"
        " os._exit(23)\n")
    prior = subprocess.run([sys.executable, '-B', '-s', '-P', '-X', 'utf8', '-c', code,
        str(peer.app.root), peer.workspace, identity], env=command_environment(),
        capture_output=True, timeout=25)
    assert prior.returncode == 23
    job = peer.service.get(identity)
    assert job['status'] == 'interrupted' and job['recovery_review_required'] is True
    with pytest.raises(RuntimeError, match='Confirm'):
        peer.service.run(identity, timeout=30)
    with pytest.raises(ValueError, match='Explicitly confirm'):
        peer.service.acknowledge_interruption(identity)
    peer.service.acknowledge_interruption(identity, worker_stopped=True)
    assert peer.service.resume(identity, timeout=30)['status'] == 'completed'


def test_mixed_intake_optional_recursion_keeps_sources_and_exclusion_counts(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    folder = tmp_path / 'chosen'
    folder.mkdir()
    (folder / 'top.wav').write_bytes(b'Top-level retained source.')
    (folder / 'ignored.txt').write_text('Not selected media.', encoding='utf-8')
    child = folder / 'nested'
    child.mkdir()
    (child / 'deep.wav').write_bytes(b'Nested retained source.')
    one = peer.service.submit_sources([peer.source, folder], recursive=False)
    assert one['files'] == 2 and one['excluded_files'] == 1 and one['excluded_folders'] == 1
    two = peer.service.submit_sources([peer.source, folder], recursive=True)
    assert two['files'] == 3 and two['excluded_files'] == 1 and two['excluded_folders'] == 0
    for receipt in (one, two):
        job = peer.service.get(receipt['id'])
        names = [row['filename'] for row in job['sources']]
        assert len(names) == len(set(names)) == receipt['files']
        assert job['status'] == 'ready' and job['run_context'] is None
        assert not (peer.service._path(job['id']) / 'job_status.json').exists()


def test_mixed_intake_cancelled_and_linked_selections_publish_no_job(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    with pytest.raises(AudioCancelled, match='cancel'):
        peer.service.submit_sources([peer.source], cancelled=lambda: True)
    assert peer.service.list() == ()
    duplicate = peer.root / 'hard-linked.wav'
    os.link(peer.source, duplicate)
    with pytest.raises(ValueError, match='[Ll]ink'):
        peer.service.submit_sources([duplicate])
    assert peer.service.list() == ()


def test_export_subset_preserves_every_original_internal_format(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    identity = peer.service.submit([peer.source])
    result = peer.service.run(identity, timeout=30)
    assert result['status'] == 'completed', result['last_error']
    before = {row['path']: (peer.service._path(identity) / 'results' / row['path']).read_bytes()
              for row in result['artifacts']}
    assert {row['format'] for row in result['artifacts']} == {
        'txt', 'srt', 'vtt', 'json', 'words.srt', 'words.vtt'}
    exported = peer.service.export(identity, tmp_path / 'exported', formats=('txt', 'json'))
    manifest = read_json(exported / 'transcription-manifest.json')
    assert manifest['export_formats'] == ['txt', 'json']
    assert {Path(row['filename']).suffix for row in manifest['artifacts']} == {'.txt', '.json'}
    assert peer.service.verify(identity)['artifacts'] == result['artifacts']
    assert all((peer.service._path(identity) / 'results' / name).read_bytes() == raw
               for name, raw in before.items())
    with pytest.raises(ValueError, match='distinct'):
        peer.service.export(identity, tmp_path / 'invalid-export', formats=('txt', 'txt'))
    assert not (tmp_path / 'invalid-export').exists()


def test_idle_controls_persist_without_starting_a_worker(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    identity = peer.service.submit([peer.source])
    assert peer.service.pause(identity)['status'] == 'paused'
    assert peer.service.get(identity)['status'] == 'paused'
    assert peer.service.cancel(identity)['status'] == 'cancelled'
    assert peer.service.get(identity)['status'] == 'cancelled'
    assert peer.service.continue_job(identity)['status'] == 'ready'
    assert peer.service.get(identity)['status'] == 'ready'
    assert not (peer.service._path(identity) / 'job_status.json').exists()


@pytest.mark.parametrize('existing', ['none', 'state', 'profiles'])
def test_fresh_runtime_inventory_stays_empty_without_creating_state(tmp_path, monkeypatch, existing):
    import carr_library.transcription as transcription
    app = LibraryApp(tmp_path / 'fresh-library')
    workspace = app.create_workspace('Fresh runtime inventory')
    service = app.transcription(workspace)
    if existing == 'state':
        service.root.mkdir(parents=True)
    elif existing == 'profiles':
        (service.root / 'profiles').mkdir(parents=True)
    before = tuple(sorted(service.root.rglob('*'))) if service.root.exists() else None

    def forbidden(*args, **kwargs):
        pytest.fail('Passive profile listing inspected or started a runtime.')

    monkeypatch.setattr(transcription, 'inspect_python', forbidden)
    monkeypatch.setattr(transcription, 'inspect_models', forbidden)
    monkeypatch.setattr(transcription, 'run_process', forbidden)
    assert service.list_runtimes() == ()
    assert service.list() == ()
    after = tuple(sorted(service.root.rglob('*'))) if service.root.exists() else None
    assert after == before
    assert not (service.root / 'engine.json').exists()


@pytest.mark.parametrize('collision', ['state', 'profiles'])
def test_passive_runtime_inventory_rechecks_non_directory_state(tmp_path, collision):
    app = LibraryApp(tmp_path / 'fresh-library')
    workspace = app.create_workspace('Profile path boundary')
    service = app.transcription(workspace)
    target = service.root if collision == 'state' else service.root / 'profiles'
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b'Owner data is not a profile directory.'
    target.write_bytes(original)
    with pytest.raises(ValueError, match='regular local files'):
        service.list_runtimes()
    assert target.read_bytes() == original
