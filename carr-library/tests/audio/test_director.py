"""Actual local speech and FFmpeg production, restart and cache-integrity tests."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import struct
import threading
import wave

import pytest

from carr_library.audio.projects import ProjectStore, Settings, parse_script
from carr_library.audio.director import ProductionDirector, file_sha256
from carr_library.audio.voices import VoiceRegistry
from carr_library.audio.render import probe_audio

SCRIPT='# Arrival\n@music calm 80\nNarrator: We are in the library.\nLeft: Welcome.\n@pause 0.4\n# Reply\nRight: The original source stays unchanged.\n'

@pytest.fixture
def registry():
    registry=VoiceRegistry()
    available={voice.id for voice in registry.list_voices()}
    assert {'flite:kal','flite:slt','flite:rms'}<=available, 'Install reviewed full FFmpeg for actual audio acceptance.'
    return registry

def create(root,registry,script=SCRIPT,*,stems=True):
    project=parse_script(script,['flite:kal','flite:slt','flite:rms'],title='Synthetic production',
        settings=Settings(sample_rate=8000,channels=2,stems=stems,chapter_gap_seconds=0.25))
    roles=tuple(replace(role,pan=-1 if role.name=='Left' else 1 if role.name=='Right' else 0) for role in project.roles)
    project=replace(project,roles=roles)
    store=ProjectStore(root)
    store.create(project)
    return store,project,ProductionDirector(root,registry=registry)

def channel_energy(path):
    with wave.open(str(path),'rb') as stream:
        assert stream.getnchannels()==2 and stream.getsampwidth()==2
        frames=stream.readframes(stream.getnframes())
    samples=struct.unpack('<'+'h'*(len(frames)//2),frames)
    return sum(value*value for value in samples[0::2]),sum(value*value for value in samples[1::2])

def test_actual_three_voice_music_production_preserves_text_stereo_chapters_and_outputs(tmp_path,registry,monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('Production attempted Python network.'))
    store,project,director=create(tmp_path/'data',registry)
    before=project.to_dict()
    result=director.render(project.id,tmp_path/'output')
    assert result['state']=='complete' and result['duration_seconds']>3
    assert store.load(project.id).to_dict()==before
    output=Path(result['output_dir'])
    report=json.loads((output/'production.json').read_text())
    assert report['mode']=='LOCAL_ONLY' and len(report['chapters'])==2
    assert report['script_sha256']==sha256(SCRIPT.encode()).hexdigest()
    assert len(report['timings'])==3
    assert 'We are in the library.' in (output/'narration.srt').read_text()
    for role in project.roles:
        path=output/('stem-'+role.id+'.wav')
        assert probe_audio(path).frames==probe_audio(output/'master.wav').frames
        left,right=channel_energy(path)
        if role.name=='Left':
            assert left>1000 and right==0
        elif role.name=='Right':
            assert right>1000 and left==0
        else:
            assert left>1000 and right>1000
    for row in result['outputs']:
        assert file_sha256(output/row['file'])==row['sha256']
    monkeypatch.setattr(registry,'synthesize',lambda *a,**k:pytest.fail('Completed output was regenerated.'))
    assert director.render(project.id,job_id=result['job_id'])==result

def test_pause_reopen_and_resume_reuses_accepted_chunks(tmp_path,registry):
    store,project,director=create(tmp_path/'data',registry,stems=False)
    stop=threading.Event()
    def progress(event):
        if event['message'].startswith('Scene 1 saved'):
            stop.set()
    with pytest.raises(InterruptedError):
        director.render(project.id,tmp_path/'output',cancelled=stop.is_set,progress=progress)
    job=director.jobs(project.id)[0]
    assert job['state']=='paused'
    accepted={p.name:file_sha256(p) for p in (store.path(project.id)/'cache').glob('*.wav')}
    stop.clear()
    result=ProductionDirector(store.root,registry=registry).render(project.id,job_id=job['id'])
    assert result['state']=='complete' and result['reused_steps']>=4
    assert all(file_sha256(store.path(project.id)/'cache'/name)==value for name,value in accepted.items())

def test_change_one_scene_preserves_other_scene_audio(tmp_path,registry):
    store,project,director=create(tmp_path/'data',registry,stems=False)
    first=director.render(project.id,tmp_path/'first')
    new=parse_script(SCRIPT.replace('The original source stays unchanged.','A different final sentence.'),
        registry.list_voices(),title=project.title,previous=project)
    store.save(new)
    second=director.render(project.id,tmp_path/'second')
    assert file_sha256(Path(first['output_dir'])/'chapter-0001.wav')==file_sha256(Path(second['output_dir'])/'chapter-0001.wav')
    assert file_sha256(Path(first['output_dir'])/'chapter-0002.wav')!=file_sha256(Path(second['output_dir'])/'chapter-0002.wav')
    assert second['reused_steps']>0 and second['generated_steps']<first['generated_steps']

def test_more_than_128_sequential_lines_render_with_bounded_graph_and_keep_pan(tmp_path,registry):
    script='\n'.join('Left: Yes.' for _ in range(140))
    store,project,director=create(tmp_path/'data',registry,script,stems=False)
    result=director.render(project.id,tmp_path/'long-chapter')
    assert result['state']=='complete' and result['reused_steps']>=139
    left,right=channel_energy(Path(result['master']))
    assert left>1000 and right==0
    report=json.loads((Path(result['output_dir'])/'production.json').read_text())
    assert len(report['timings'])==140 and report['frames']==probe_audio(result['master']).frames

def test_cache_corruption_regenerates_chunk_without_overwriting_published_output(tmp_path,registry):
    store,project,director=create(tmp_path/'data',registry,'Narrator: A short exact source.',stems=False)
    first=director.render(project.id,tmp_path/'first')
    original=file_sha256(first['master'])
    cache=store.path(project.id)/'cache'
    speech_meta=next(p for p in cache.glob('*.json') if json.loads(p.read_text())['kind']=='speech')
    speech_meta.with_suffix('.wav').write_bytes(b'broken audio')
    second=director.render(project.id,tmp_path/'second')
    assert second['generated_steps']>=1 and file_sha256(first['master'])==original
    assert any('.invalid-' in p.name for p in cache.iterdir())

def test_completed_output_tampering_is_reported_without_overwrite(tmp_path,registry):
    store,project,director=create(tmp_path/'data',registry,'Narrator: Keep this source.',stems=False)
    result=director.render(project.id,tmp_path/'output')
    target=Path(result['master'])
    target.write_bytes(b'User changed this output')
    with pytest.raises(ValueError,match='changed or removed'):
        director.render(project.id,job_id=result['job_id'])
    assert target.read_bytes()==b'User changed this output'

def test_unavailable_voice_refuses_before_render_or_network(tmp_path,registry):
    store,project,director=create(tmp_path/'data',registry,'Text',stems=False)
    updated=replace(project,roles=(replace(project.roles[0],voice_id='unknown-private-voice'),),revision=2)
    store.save(updated)
    with pytest.raises(ValueError,match='unavailable'):
        director.render(project.id,tmp_path/'output')
    assert not (tmp_path/'output').exists()

def test_disk_budget_stops_before_final_output_but_preserves_project(tmp_path,registry):
    store,project,director=create(tmp_path/'data',registry,'Narrator: Small disk budget.',stems=False)
    limited=replace(project,settings=replace(project.settings,disk_budget_bytes=1),revision=2)
    store.save(limited)
    with pytest.raises(ValueError,match='disk budget'):
        director.render(project.id,tmp_path/'output')
    assert store.load(project.id)==limited and not (tmp_path/'output'/'master.wav').exists()

@pytest.mark.parametrize('seconds',[0.02,120.02])
def test_music_tail_below_generator_minimum_keeps_exact_scene_length(tmp_path,registry,seconds):
    store,project,director=create(tmp_path/'data',registry,f'@music calm 80\n@pause {seconds}',stems=False)
    result=director.render(project.id,tmp_path/'output')
    assert result['state']=='complete'
    assert probe_audio(result['master']).frames==round(seconds*project.settings.sample_rate)
