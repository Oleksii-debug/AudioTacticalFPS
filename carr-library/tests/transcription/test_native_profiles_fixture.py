"""Real child proof for the finite native ASR pause/restart fixture barrier."""
import importlib.util
import json
from pathlib import Path
import time

from carr_library.audio.models import AudioBudget


_spec = importlib.util.spec_from_file_location(
    'native_profile_barrier_fixture', Path(__file__).with_name('fixture_runtime.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def _setup(tmp_path, options):
    runtime = _fixture.make_runtime(tmp_path / 'runtime', options=options)
    service = _fixture.make_service(
        tmp_path / 'library/state/transcription',
        budget=AudioBudget(timeout=30, reserve_disk_bytes=0, max_log_bytes=65536),
        stop_grace=.15)
    profile = _fixture.register(service, runtime)
    sources = [tmp_path / 'first.wav', tmp_path / 'second.wav']
    for number, source in enumerate(sources):
        source.write_bytes(b'Synthetic source identity only: ' + str(number).encode('ascii'))
    created = service.create_job(profile['id'], sources)
    return service, runtime, created


def _lines(runtime, name):
    with runtime.worker.with_name(name).open('rb') as stream:
        raw = stream.read(4097)
    assert len(raw) <= 4096
    return raw.decode('utf-8').splitlines()


def test_actual_stop_barrier_waits_past_old_hold_and_resume_starts_only_unfinished_source(tmp_path):
    service, runtime, created = _setup(tmp_path, {
        'wait_for_stop_after': 1, 'hold_after_checkpoint': 3.0})
    observations = []

    def progress(event):
        if (event['phase'].startswith('Transcribing locally')
                and event['done'] == 1 and not observations):
            before = _lines(runtime, 'fixture-sources.log')
            # Deliberately exceed the old native fixture's three-second hold.
            # Only the actual host Pause below may release this first child.
            time.sleep(3.2)
            after = _lines(runtime, 'fixture-sources.log')
            observations.append((before, after))
            service.pause(created['id'])

    paused = service.run(created['id'], progress=progress)
    assert paused['status'] == 'paused' and paused['done'] == 1 and paused['total'] == 2, paused
    assert len(observations) == 1, observations
    before, after = observations[0]
    assert before == after and len(before) == 1, observations
    assert _lines(runtime, 'fixture-starts.log') == ['started']
    retained = dict(paused['results'][0])
    assert not service.host._broker.snapshot()['leases']
    runtime.options.write_text(json.dumps({}), encoding='utf-8')
    complete = service.resume(created['id'])
    assert complete['status'] == 'complete' and complete['done'] == complete['total'] == 2, complete
    assert retained in complete['results']
    starts, processed = _lines(runtime, 'fixture-starts.log'), _lines(runtime, 'fixture-sources.log')
    assert starts == ['started', 'started'], (starts, processed)
    assert len(processed) == 2 and processed.count(before[0]) == 1, processed
    assert len(set(processed)) == 2
    assert not service.host._broker.snapshot()['leases']


def test_actual_stop_barrier_expires_without_silent_second_source_or_complete_status(tmp_path):
    service, runtime, created = _setup(tmp_path, {
        'wait_for_stop_after': 1, 'checkpoint_wait_timeout': .2})
    partial = service.run(created['id'])
    assert partial['status'] == 'partial' and partial['done'] == 1 and partial['total'] == 2, partial
    assert 'Synthetic checkpoint stop barrier expired without a host stop signal.' in partial['error'], partial
    assert _lines(runtime, 'fixture-starts.log') == ['started']
    assert len(_lines(runtime, 'fixture-sources.log')) == 1
    worker = service._job_root(created['id']) / 'worker'
    assert not (worker / 'stop.flag').exists()
    assert len(list((worker / 'logs/cache').rglob('*.cache.json'))) == 1
    assert not service.host._broker.snapshot()['leases']
