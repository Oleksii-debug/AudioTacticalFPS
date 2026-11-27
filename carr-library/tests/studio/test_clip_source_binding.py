"""Actual FFmpeg/PCM tests for a cue's admitted source identity.

No voice or model is loaded. Mutations occur through real files at the existing
progress, native mix, and cache-inspection seams. A valid earlier scene remains
reusable; a changed source never becomes an accepted new mix or final output.
"""
from array import array
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.audio.render import tool_path
from carr_library.speech import VoiceRegistry
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


MESSAGE = 'A selected production input changed after resource admission'


def write_tone(path, frequency):
    rate, frames = 24000, 6000
    samples = array('h', (round(8000 * math.sin(2 * math.pi * frequency * index / rate))
                         for index in range(frames)))
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, rate, 0, 'NONE', 'PCM'))
        writer.writeframes(samples.tobytes())


def pcm(path):
    with wave.open(str(path), 'rb') as reader:
        rate, channels, frames = reader.getframerate(), reader.getnchannels(), reader.getnframes()
        assert reader.getsampwidth() == 2 and reader.getcomptype() == 'NONE'
        raw = reader.readframes(frames)
        assert len(raw) == frames * channels * 2
    values = array('h', raw)
    if sys.byteorder != 'little':
        values.byteswap()
    return rate, channels, frames, values


def energy(path, frequency):
    rate, channels, _, values = pcm(path)
    samples = values[::channels]
    real = sum(value * math.cos(2 * math.pi * frequency * index / rate)
               for index, value in enumerate(samples))
    imaginary = sum(value * math.sin(2 * math.pi * frequency * index / rate)
                    for index, value in enumerate(samples))
    return (real * real + imaginary * imaginary) / len(samples)**2


@pytest.fixture
def production(tmp_path):
    inputs, outputs = tmp_path / 'selected-inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'external-tone.wav'
    write_tone(source, 440)
    voices = VoiceRegistry([])
    hooks = SimpleNamespace(mix=None, inspect=None)
    runtimes = []

    def runtime_factory(directory, *, input_roots):
        runtime = AudioRuntime(directory, input_roots=input_roots,
            ffmpeg=tool_path('ffmpeg'), ffprobe=tool_path('ffprobe'),
            budget=AudioBudget(timeout=30, threads=1, max_output_bytes=16 * 1024**2,
                               reserve_disk_bytes=4 * 1024**2))
        actual_mix, actual_inspect = runtime.mix, runtime.inspect

        def mix(tracks, target, **options):
            if hooks.mix is not None:
                hooks.mix('before', tracks, Path(target))
            result = actual_mix(tracks, target, **options)
            if hooks.mix is not None:
                hooks.mix('after', tracks, Path(target))
            return result

        def inspect(path, **options):
            if hooks.inspect is not None:
                hooks.inspect(Path(path))
            return actual_inspect(path, **options)

        runtime.mix, runtime.inspect = mix, inspect
        runtimes.append(runtime)
        return runtime

    service = ProductionService(tmp_path / 'library' / 'state' / 'studio',
        voices=voices, input_roots=(inputs,), runtime_factory=runtime_factory)
    project = Project('source-binding', 'Admitted clip identity', (),
        (Scene('earlier', 'Earlier silence', (), minimum_duration=1),
         Scene('later', 'External clip', (), cues=(
             Cue('external', str(source), gain_db=0, role='microphone',
                 duration=.25, title='Purpose-written external PCM'),))),
        original_script='[Scene: Earlier silence]\n[Duration: 1]\n[Scene: External clip]',
        sample_rate=24000, schema=2)
    value = SimpleNamespace(service=service, project=project, source=source, outputs=outputs,
        original=source.read_bytes(), hooks=hooks, runtimes=runtimes, prior={})
    try:
        yield value
    finally:
        service.close()
        voices.close()


def retain_earlier_scene(production, job_id, message):
    if not message.startswith('Scene 2 of 2:'):
        return
    state = production.service.get(job_id)
    for key, node in state['nodes'].items():
        if node['kind'] != 'scene mix':
            continue
        receipt = production.service.cache / (key + '.json')
        declaration = json.loads(receipt.read_text(encoding='utf-8'))
        path = production.service.cache / declaration['file']
        rate, channels, frames, values = pcm(path)
        assert (rate, channels, frames) == (24000, 2, 24000) and not any(values)
        production.prior[key] = (receipt.read_bytes(), path, path.read_bytes())


def assert_failed_before_output(production, job_id):
    state = production.service.get(job_id)
    assert state['status'] == 'failed' and MESSAGE in state['error']
    assert state['artifacts'] == [] and not Path(state['destination']).exists()
    assert production.source.is_file()
    assert len(production.prior) == 1
    for key, (receipt, path, data) in production.prior.items():
        assert state['nodes'][key]['kind'] == 'scene mix'
        assert (production.service.cache / (key + '.json')).read_bytes() == receipt
        assert path.read_bytes() == data
        assert state['nodes'][key]['sha256'] == sha256(data).hexdigest()
    return state


@pytest.mark.parametrize('mutation', ['content', 'mtime-only'])
def test_external_cue_changed_after_full_admission_is_rejected_at_consumption(production, mutation):
    service, source = production.service, production.source
    job_id = service.submit(production.project, production.outputs, stems=True)
    changed = []

    def progress(message):
        if message.startswith('Scene 1 of 2:'):
            state = service.get(job_id)
            assert state['status'] == 'running'
            assert state['resource_plan']['source_sha256'][str(source)] == sha256(production.original).hexdigest()
            before = source.stat()
            if mutation == 'content':
                write_tone(source, 880)
                assert source.read_bytes() != production.original
            else:
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                assert source.read_bytes() == production.original
                assert source.stat().st_mtime_ns != before.st_mtime_ns
            changed.append((source.read_bytes(), source.stat().st_mtime_ns))
        retain_earlier_scene(production, job_id, message)

    with pytest.raises(ValueError, match=MESSAGE):
        service.run(job_id, progress=progress)
    state = assert_failed_before_output(production, job_id)
    assert len(changed) == 1
    assert (source.read_bytes(), source.stat().st_mtime_ns) == changed[0]
    assert sum(node['kind'] == 'scene mix' for node in state['nodes'].values()) == 1
    assert not any(node['kind'] == 'scene clip stem' for node in state['nodes'].values())


@pytest.mark.parametrize('kind', ['scene mix', 'scene clip stem'])
@pytest.mark.parametrize('when', ['before', 'after'])
def test_cue_mutation_around_actual_mix_cannot_commit_a_cache_receipt(production, kind, when):
    service, source = production.service, production.source
    job_id = service.submit(production.project, production.outputs, stems=True)
    observed = {}

    def at_mix(phase, tracks, target):
        selected = [track for track in tracks if Path(track.path) == source]
        if not selected:
            return
        actual_kind = 'scene clip stem' if len(tracks) == 1 else 'scene mix'
        if actual_kind != kind:
            return
        if phase == 'before':
            assert not observed, 'An unaccepted mix must not be silently retried.'
            observed['path'] = target
            if when == 'before':
                write_tone(source, 880)
        else:
            assert target == observed['path'] and target.is_file()
            expected = 880 if when == 'before' else 440
            other = 440 if when == 'before' else 880
            assert energy(target, expected) > 100 * max(1, energy(target, other))
            observed['actual_pcm_sha256'] = sha256(target.read_bytes()).hexdigest()
            if when == 'after':
                write_tone(source, 880)
            observed['changed_source'] = source.read_bytes()

    production.hooks.mix = at_mix
    with pytest.raises(ValueError, match=MESSAGE):
        service.run(job_id, progress=lambda message: retain_earlier_scene(production, job_id, message))
    state = assert_failed_before_output(production, job_id)
    assert observed.get('actual_pcm_sha256'), 'The original FFmpeg mix must actually complete.'
    assert source.read_bytes() == observed['changed_source'] != production.original
    key = observed['path'].name.split('-', 1)[0]
    assert key not in state['nodes']
    assert not (service.cache / (key + '.json')).exists()
    # A completed but unaccepted private attempt may remain for review. It must
    # never become a reusable node or overwrite the prior valid scene cache.
    for node_key, node in state['nodes'].items():
        receipt = json.loads((service.cache / (node_key + '.json')).read_text(encoding='utf-8'))
        assert receipt['file'] != observed['path'].name


@pytest.mark.parametrize('kind', ['scene mix', 'scene clip stem'])
def test_cached_mix_inspection_cannot_reuse_after_its_external_source_changes(production, kind):
    service, source = production.service, production.source
    first_id = service.submit(production.project, production.outputs, stems=True)
    first = service.run(first_id)
    assert first['status'] == 'complete'
    first_files = {Path(item['path']): Path(item['path']).read_bytes() for item in first['artifacts']}
    assert energy(Path(first['destination']) / 'chapters' / '0002-External-clip.wav', 440) > 1000
    candidates = []
    for key, node in first['nodes'].items():
        if node['kind'] != kind:
            continue
        receipt_path = service.cache / (key + '.json')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        candidate = service.cache / receipt['file']
        if any(pcm(candidate)[3]):
            candidates.append((key, candidate, receipt_path.read_bytes(), candidate.read_bytes()))
    assert len(candidates) == 1
    key, candidate, old_receipt, old_pcm = candidates[0]
    second_id = service.submit(production.project, production.outputs, stems=True)
    fired = []

    def at_inspection(path):
        if path != candidate or fired:
            return
        state = service.get(second_id)
        assert state['status'] == 'running'
        assert state['resource_plan']['source_sha256'][str(source)] == sha256(production.original).hexdigest()
        write_tone(source, 880)
        fired.append(source.read_bytes())

    production.hooks.inspect = at_inspection
    with pytest.raises(ValueError, match=MESSAGE):
        service.run(second_id, progress=lambda message: retain_earlier_scene(
            production, second_id, message))
    second = assert_failed_before_output(production, second_id)
    assert len(fired) == 1 and source.read_bytes() == fired[0] != production.original
    assert key not in second['nodes'], 'The changed candidate cannot be accepted as a cache hit.'
    assert (service.cache / (key + '.json')).read_bytes() == old_receipt
    assert candidate.read_bytes() == old_pcm
    assert service.get(first_id)['status'] == 'complete'
    assert all(path.read_bytes() == data for path, data in first_files.items())
