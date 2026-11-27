"""Real local speech/music/DSP production, restart and receipt integrity.

No synthetic speech mocks substitute for successful audio rendering here.
The source scripts and spoken text are purpose-written test fixtures.
"""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.speech import FliteProvider, VoiceRegistry
from carr_library.studio.project import Cue, Project, ProjectStore, parse_script
from carr_library.studio.render import ProductionService


@pytest.fixture(scope='module')
def actual_voices():
    executable = shutil.which('ffmpeg')
    if not executable or not shutil.which('ffprobe'):
        pytest.skip('This actual production acceptance requires installed FFmpeg and ffprobe.')
    registry = VoiceRegistry([FliteProvider(executable)])
    selected = ('flite:awb', 'flite:rms', 'flite:slt')
    if not set(selected) <= {voice.id for voice in registry.voices()}:
        pytest.skip('This actual production acceptance requires three installed Flite voices.')
    return registry


SCRIPT = '''[Scene: The archive]
[Music: suspense; tempo=80; gain=-24; seed=4]
Narrator [center]: Three readers entered the quiet archive.
Alice [left]: I have found the original letter.
[Pause: 2]
Bob [right]: We will preserve every original word.
[Scene: The conclusion]
Narrator [center]: Their work was saved locally.
'''


def pcm(path):
    with wave.open(str(path), 'rb') as audio:
        frames, rate, channels, width = audio.getnframes(), audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
        assert width == 2
        raw = audio.readframes(frames)
    values = struct.unpack('<' + 'h' * (len(raw) // 2), raw)
    return [values[index::channels] for index in range(channels)], rate, frames


def energy(values):
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


@pytest.fixture(scope='module')
def rendered(tmp_path_factory, actual_voices):
    root = tmp_path_factory.mktemp('actual-production')
    outputs = root / 'Готові аудіофайли'
    outputs.mkdir()
    service = ProductionService(root / 'private-state', voices=actual_voices)
    project = service.create(SCRIPT, title='Synthetic three-role production',
                             voice_ids=('flite:awb', 'flite:rms', 'flite:slt'), mode='authored')
    original = project.to_dict()
    job = service.submit(project, outputs, operation_id='three-role-acceptance')
    complete = service.run(job)
    return service, project, original, complete


def test_actual_three_role_music_chapters_stems_and_text_invariance(rendered):
    service, project, original, complete = rendered
    assert complete['status'] == 'complete'
    assert project.to_dict() == original
    assert service.projects.load(project.id).to_dict() == original
    target = Path(complete['destination'])
    assert Project.from_dict(json.loads((target / 'project.json').read_text())).original_script == SCRIPT
    assert complete['result']['source_sha256'] == sha256(SCRIPT.encode()).hexdigest()
    assert complete['result']['local_only'] is True
    assert len(complete['result']['chapters']) == 2
    assert len(list((target / 'chapters').glob('*.wav'))) == 2
    assert {path.name for path in (target / 'stems').glob('*.wav')} == {'role-1.wav', 'role-2.wav', 'role-3.wav', 'music.wav'}
    channels, rate, frames = pcm(target / 'master.wav')
    assert len(channels) == 2 and rate == project.sample_rate
    assert abs(frames / rate - complete['result']['duration']) <= 1 / rate
    assert max(abs(value) for channel in channels for value in channel) <= 31200
    assert energy(channels[0]) > 50 and energy(channels[1]) > 50
    assert not complete['result']['loudness']['silent']
    assert complete['result']['loudness']['integrated_lufs'] is not None
    assert complete['result']['loudness']['true_peak_dbfs'] < 0
    for artifact in complete['artifacts']:
        path = Path(artifact['path'])
        assert path.is_relative_to(target)
        assert artifact['sha256'] == sha256(path.read_bytes()).hexdigest()


def test_actual_left_center_right_routing_and_two_second_pause(rendered):
    _, project, _, complete = rendered
    target = Path(complete['destination'])
    timeline = complete['result']['lines']
    roles = {role.name: role for role in project.roles}
    for name, side in (('Alice', 'left'), ('Bob', 'right'), ('Narrator', 'center')):
        role = roles[name]
        channels, rate, frames = pcm(target / 'stems' / (role.id + '.wav'))
        assert frames > 0 and len(channels) == 2
        left, right = energy(channels[0]), energy(channels[1])
        if side == 'left':
            assert left > 50 and right < 1
        elif side == 'right':
            assert right > 50 and left < 1
        else:
            assert left > 25 and abs(left - right) < 1
    alice = next(line for line in timeline if line['role'] == roles['Alice'].id)
    bob = next(line for line in timeline if line['role'] == roles['Bob'].id)
    assert bob['start'] - alice['end'] >= 2.299
    for role in roles.values():
        channels, rate, _ = pcm(target / 'stems' / (role.id + '.wav'))
        first = round((alice['end'] + .5) * rate)
        last = round((bob['start'] - .1) * rate)
        assert max(abs(value) for channel in channels for value in channel[first:last]) == 0


def test_measured_line_subtitles_and_music_stem_alignment(rendered):
    _, _, _, complete = rendered
    target = Path(complete['destination'])
    vtt = (target / 'script.vtt').read_text()
    srt = (target / 'script.srt').read_text()
    assert vtt.startswith('WEBVTT\n')
    assert vtt.count(' --> ') == 4 and srt.count(' --> ') == 4
    lines = complete['result']['lines']
    for line in lines:
        assert line['text'] in vtt and line['text'] in srt
        assert line['end'] > line['start']
        assert 'word timings unavailable' in line['timing']
    music, rate, frames = pcm(target / 'stems' / 'music.wav')
    master, master_rate, master_frames = pcm(target / 'master.wav')
    assert (rate, frames) == (master_rate, master_frames)
    boundary = round(complete['result']['chapters'][1]['start'] * rate)
    assert energy(music[0][:boundary]) > 10
    assert max(abs(value) for channel in music for value in channel[boundary:]) == 0


def test_actual_music_ducking_recovers_during_deliberate_silence(rendered):
    _, project, _, complete = rendered
    target = Path(complete['destination'])
    master, rate, _ = pcm(target / 'master.wav')
    music, _, _ = pcm(target / 'stems' / 'music.wav')
    stems = [pcm(target / 'stems' / (role.id + '.wav'))[0] for role in project.roles]
    mixed_voice = [sum(stem[0][index] for stem in stems) for index in range(len(master[0]))]
    residual = [value - voice for value, voice in zip(master[0], mixed_voice)]
    roles = {role.name: role.id for role in project.roles}
    lines = complete['result']['lines']
    alice = next(line for line in lines if line['role'] == roles['Alice'])
    bob = next(line for line in lines if line['role'] == roles['Bob'])
    quiet = range(round((alice['end'] + 1) * rate), round((bob['start'] - .2) * rate))
    first_end = round(complete['result']['chapters'][0]['end'] * rate)
    active = [index for index in range(first_end) if abs(mixed_voice[index]) > 2500]
    assert len(active) > rate / 20
    quiet_ratio = energy([residual[i] for i in quiet]) / energy([music[0][i] for i in quiet])
    active_ratio = energy([residual[i] for i in active]) / energy([music[0][i] for i in active])
    assert .055 < quiet_ratio < .07, (quiet_ratio, active_ratio)
    assert active_ratio < .85 * quiet_ratio, (quiet_ratio, active_ratio)


def test_completed_operation_is_idempotent_and_outputs_are_reverified(rendered):
    service, project, _, complete = rendered
    repeated = service.submit(project, Path(complete['destination']).parent, operation_id=complete['id'])
    assert repeated == complete['id']
    before = {artifact['path']: Path(artifact['path']).stat().st_mtime_ns for artifact in complete['artifacts']}
    assert service.run(repeated)['status'] == 'complete'
    assert before == {path: Path(path).stat().st_mtime_ns for path in before}
    changed = replace(project, title='Another production', revision=project.revision + 1)
    with pytest.raises(ValueError, match='different production'):
        service.submit(changed, Path(complete['destination']).parent, operation_id=complete['id'])


def test_pause_restart_and_one_corrupt_speech_chunk_repaired(tmp_path, actual_voices):
    service = ProductionService(tmp_path / 'state', voices=actual_voices)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    text = 'Alice [left]: First verified line.\nBob [right]: Second retained line.\nNarrator [center]: Last line.'
    project = service.create(text, voice_ids=('flite:awb', 'flite:rms', 'flite:slt'), mode='authored')
    job = service.submit(project, outputs, stems=False)
    speech_started = 0
    def pause_before_third(message):
        nonlocal speech_started
        if message == 'speech: rendering local audio':
            speech_started += 1
            if speech_started == 3:
                service.request_pause(job)
    with pytest.raises((InterruptedError, RuntimeError)):
        service.run(job, progress=pause_before_third)
    paused = service.get(job)
    assert paused['status'] == 'paused'
    speech_nodes = [(key, value) for key, value in paused['nodes'].items() if value['kind'] == 'speech']
    assert len(speech_nodes) == 2
    bad_key, bad_node = speech_nodes[0]
    good_key, good_node = speech_nodes[1]
    bad_receipt = json.loads((service.cache / (bad_key + '.json')).read_text())
    corrupted = service.cache / bad_receipt['file']
    corrupted.write_bytes(b'corrupted cache chunk, never acceptable as speech')
    reopened = ProductionService(service.root, voices=actual_voices)
    complete = reopened.run(job)
    assert complete['status'] == 'complete'
    assert complete['nodes'][bad_key]['reused'] is False
    assert complete['nodes'][good_key]['reused'] is True
    assert complete['nodes'][good_key]['sha256'] == good_node['sha256']
    replacement = json.loads((service.cache / (bad_key + '.json')).read_text())
    assert replacement['file'] != bad_receipt['file']
    assert replacement['sha256'] == bad_node['sha256']
    assert len(list(outputs.iterdir())) == 1


def test_edit_one_scene_reuses_unchanged_speech_and_source_original(tmp_path, actual_voices):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = ProductionService(tmp_path / 'state', voices=actual_voices)
    text = '[Scene: First]\nNarrator: First scene.\n[Scene: Second]\nNarrator: The unchanged second scene.'
    project = service.create(text, voice_ids=('flite:slt',), mode='authored')
    first = service.run(service.submit(project, outputs, stems=False))
    speech = {key: value for key, value in first['nodes'].items() if value['kind'] == 'speech'}
    line = replace(project.scenes[0].lines[0], text='Only the first scene is revised.')
    scene = replace(project.scenes[0], lines=(line,))
    changed = replace(project, scenes=(scene, project.scenes[1]), revision=2)
    service.projects.save(changed, expected_revision=1)
    second = service.run(service.submit(changed, outputs, stems=False))
    reused = [key for key, node in second['nodes'].items() if node['kind'] == 'speech' and node['reused']]
    assert len(reused) == 1 and reused[0] in speech
    assert second['project']['original_script'] == text
    assert first['destination'] != second['destination']
    assert Path(first['destination'], 'master.wav').is_file()
    assert len(list(service.projects.directory(project.id).joinpath('revisions').glob('*.json'))) == 1


def test_completed_output_tampering_is_not_silently_overwritten(tmp_path, actual_voices):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = ProductionService(tmp_path / 'state', voices=actual_voices)
    project = service.create('A short output-integrity check.', voice_ids=('flite:slt',))
    job = service.submit(project, outputs, stems=False)
    complete = service.run(job)
    output = Path(complete['destination']) / 'master.wav'
    output.write_bytes(b'user modified this file')
    with pytest.raises(ValueError, match='checksum'):
        service.run(job)
    assert output.read_bytes() == b'user modified this file'


def test_corrupt_receipt_is_recoverable_like_corrupt_audio(tmp_path, actual_voices):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = ProductionService(tmp_path / 'state', voices=actual_voices)
    project = service.create('Narrator: A recoverable line.\nNarrator: A second line.',
                             voice_ids=('flite:slt',), mode='authored')
    job = service.submit(project, outputs, stems=False)
    def stop_after_first(message):
        if message == 'voice placement: rendering local audio':
            service.request_pause(job)
    with pytest.raises((InterruptedError, RuntimeError)):
        service.run(job, progress=stop_after_first)
    state = service.get(job)
    key = next(key for key, node in state['nodes'].items() if node['kind'] == 'speech')
    (service.cache / (key + '.json')).write_bytes(b'{partial receipt')
    complete = ProductionService(service.root, voices=actual_voices).run(job)
    assert complete['status'] == 'complete'
    assert complete['nodes'][key]['reused'] is False


def test_actual_selected_local_sfx_routing_and_source_audio_invariance(tmp_path, actual_voices):
    inputs = tmp_path / 'selected-assets'
    inputs.mkdir()
    effect = inputs / 'synthetic-door-click.wav'
    sample_rate = 24000
    # Purpose-written fixture, not an assertion that this is a generated neural
    # door sound. Real PCM must pass through the production routing/mix path.
    samples = [round(10000 * math.sin(2 * math.pi * 220 * i / sample_rate)
                     * math.exp(-i / 1600)) for i in range(6000)]
    with wave.open(str(effect), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    original_hash = sha256(effect.read_bytes()).hexdigest()
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = ProductionService(tmp_path / 'state', voices=actual_voices, input_roots=(inputs,))
    base = parse_script('Narrator: Hello.', voice_ids=('flite:slt',), mode='authored')
    scene = replace(base.scenes[0], cues=(Cue('door', str(effect), start=3, gain_db=0, pan=1),))
    project = replace(base, scenes=(scene,))
    complete = service.run(service.submit(project, outputs, stems=False))
    channels, rate, frames = pcm(Path(complete['destination']) / 'master.wav')
    assert frames == round(3.25 * rate)
    assert max(abs(value) for value in channels[0][3 * rate:]) == 0
    assert max(abs(value) for value in channels[1][3 * rate:]) > 5000
    assert sha256(effect.read_bytes()).hexdigest() == original_hash


def test_unselected_cue_directory_is_denied_before_publication(tmp_path, actual_voices):
    outside = tmp_path / 'not-selected.wav'
    with wave.open(str(outside), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\x00\x00' * 2400)
    service = ProductionService(tmp_path / 'state', voices=actual_voices)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    base = parse_script('Narrator: Local only.', voice_ids=('flite:slt',), mode='authored')
    project = replace(base, scenes=(replace(base.scenes[0], cues=(Cue('outside', str(outside)),)),))
    job = service.submit(project, outputs, stems=False)
    with pytest.raises(ValueError, match='selected input'):
        service.run(job)
    assert not list(outputs.iterdir())
    assert service.get(job)['status'] == 'failed'
