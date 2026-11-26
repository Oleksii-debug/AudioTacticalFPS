"""Actual installed local speech + FFmpeg + restart + backup acceptance.

Run directly with the installed interpreter, including -I. The music fixture is
a labelled synthetic tone, not a neural-generation or listening-quality claim.
Temporary outputs are never uploaded by the Windows evidence workflow.
"""
from array import array
from dataclasses import replace
import json
import math
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import wave


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    from carr_library.audio import FFmpegAudio
    from carr_library.speech import discover_voices
    from carr_library.studio import Clip, Line, Project, ProjectStore, Role, Scene
    from carr_library.studio.backup import backup_project, restore_project
    from carr_library.studio.director import Director, RenderPaused
    from carr_library.studio.store import digest_file
    voices = [voice for voice in discover_voices() if voice.engine == 'sapi5']
    if not voices:
        raise RuntimeError('No actual installed SAPI5 voice. Native synthesis acceptance is not complete.')
    selected = [voices[index % len(voices)] for index in range(3)]
    with TemporaryDirectory(prefix='carr-actual-studio-') as temporary:
        root = Path(temporary)
        assets = root / 'selected assets'
        assets.mkdir()
        music = assets / 'synthetic tone.wav'
        with wave.open(str(music), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
            output.writeframes(array('h', (round(1800 * math.sin(2 * math.pi * 180 * n / 24000))
                                          for n in range(24000 * 4))).tobytes())
        original_music = digest_file(music)
        authored = 'Left: The original words remain unchanged.\nNarrator: Two seconds have passed.\nRight: The second speaker is on the right.'
        project = Project('Čítanie — український проєкт', (
            Role('left', selected[0].id, routing='left'),
            Role('narrator', selected[1].id, routing='center', gain_db=-3),
            Role('right', selected[2].id, routing='right')),
            (Scene('opening', 'Opening', (
                Line('a', 'left', 'The original words remain unchanged.', pause_after=2),
                Line('b', 'narrator', 'Two seconds have passed.'),
                Line('c', 'right', 'The second speaker is on the right.'))),
             Scene('closing', 'Closing', (Line('d', 'narrator', 'This scene uses a quiet synthetic music fixture.'),),
                   clips=(Clip('bed', str(music), role='music', gain_db=-18),))),
            authored_script=authored)
        store = ProjectStore(root / 'Український проєкт')
        pause = [False]
        def progress(value):
            if value['completed_chunks'] >= 1:
                pause[0] = True
        try:
            Director(store).render(project, allowed_audio_roots=(assets,),
                                   paused=lambda: pause[0], progress=progress)
        except RenderPaused:
            pass
        else:
            raise AssertionError('The actual render did not pause at a retained speech checkpoint.')
        assert store.jobs()[0]['state'] == 'paused'
        reopened = ProjectStore(store.root)
        result = Director(reopened).render(reopened.load(), allowed_audio_roots=(assets,))
        assert result['reused_chunks'] >= 1 and len(result['chapters']) == 2
        assert len(result['captions']) == 4
        first, second = result['captions'][:2]
        assert abs(second['start'] - first['end'] - 2) <= 1 / project.sample_rate
        with wave.open(result['master']['path'], 'rb') as audio:
            samples = array('h', audio.readframes(audio.getnframes()))
            rate = audio.getframerate()
            frames = audio.getnframes()
        start, end = round(first['start'] * rate), round(first['end'] * rate)
        assert max(abs(n) for n in samples[start*2:end*2:2]) > 100
        assert not any(samples[start*2+1:end*2:2]), 'Left route leaked into right channel.'
        pause_start = round(first['end'] * rate)
        pause_end = round(second['start'] * rate)
        assert not any(samples[pause_start*2:pause_end*2]), 'The authored pause was shortened or filled.'
        assert frames == sum(round(chapter['duration_seconds'] * rate) for chapter in result['chapters'])
        same = Director(reopened).render(project, allowed_audio_roots=(assets,))
        assert same['completed_chunks'] == 0 and same['master']['sha256'] == result['master']['sha256']
        changed = replace(project, scenes=(replace(project.scenes[0], lines=(
            replace(project.scenes[0].lines[0], text='This one line is deliberately revised.'),
            *project.scenes[0].lines[1:])), project.scenes[1]))
        revised = Director(reopened).render(changed, allowed_audio_roots=(assets,))
        assert revised['completed_chunks'] == 1
        assert revised['chapters'][1]['sha256'] == result['chapters'][1]['sha256']
        digest = backup_project(reopened, root / 'backup')
        restored = restore_project(root / 'backup', root / 'restored', expected_manifest_sha256=digest)
        restored_result = Director(restored).render(restored.load(), allowed_audio_roots=(assets,))
        assert restored_result['completed_chunks'] == 0
        assert restored_result['master']['sha256'] == revised['master']['sha256']
        assert restored.load().authored_script == authored
        assert digest_file(music) == original_music
        loudness = FFmpegAudio().measure_loudness(Path(revised['master']['path']))
        report = dict(status='PASS', platform=platform.platform(), installed_sapi_entries=len(voices),
                      selected_voice_ids=[v.id for v in selected], distinct_voice_ids=len({v.id for v in selected}),
                      actual_synthesis='PASS', actual_ffmpeg_mixing='PASS', three_role_routing='PASS',
                      exact_two_second_pause='PASS', paused_restart='PASS', cached_second_render='PASS',
                      changed_line_only='PASS', unchanged_scene='PASS', backup_restore='PASS',
                      authored_script_unchanged='PASS', input_audio_unchanged='PASS',
                      duration_seconds=revised['master']['duration_seconds'],
                      sha256=revised['master']['sha256'], measured_lufs=loudness.integrated_lufs,
                      true_peak_dbfs=loudness.true_peak_dbfs, music='SYNTHETIC_TONE_FIXTURE',
                      actual_musicgen='NOT_RUN', listening_quality='NOT_RUN', nvda='NOT_RUN')
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
