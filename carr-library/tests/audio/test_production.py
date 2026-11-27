"""Audio pipeline regressions. ToneEngine is explicitly synthetic speech."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import wave
import struct
import math
import os
import shutil
from unittest.mock import patch

from carr_library.audio.production import parse_script, render_project, speech_segments
from carr_library.audio.models import ProductionPlan, Effect, Role, Line, Scene, Pause
from carr_library.audio.jobs import RenderCancelled
from carr_library.audio.ffmpeg import wav_info


FFMPEG = Path(os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg') or '/missing-ffmpeg')


class ToneEngine:
    """A deterministic fixture, never a proof of real speech quality."""
    def __init__(self):
        self.calls = []

    def cache_identity(self, voice):
        return {'fixture': 'synthetic-tone-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        self.calls.append(text)
        rate, frames = 8000, 1600
        freq = 330 + (sum(map(ord, text)) % 200)
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(7000*math.sin(i*2*math.pi*freq/rate))) for i in range(frames)))
        return output


class ProductionModelTests(unittest.TestCase):
    def test_round_trip_preserves_source_and_typed_directives(self):
        script = '# One\n[role Alice pan=left voice="local:One"]\n[effect Alice equalizer frequency=500 gain_db=2 q=1]\n[music mood=calm tempo=90 seed=4]\nAlice: Exact words.\n[pause 2]\n# Two\nNarrator: End.'
        plan = parse_script(script)
        restored = ProductionPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
        self.assertEqual(plan, restored)
        self.assertEqual(plan.original_script, script)
        self.assertEqual(plan.roles[1].pan, -1)
        self.assertEqual(len(plan.scenes), 2)

    def test_rejects_unsafe_and_unknown_directives(self):
        for script in ('[shell rm -rf /]', '[pause nan]', '[sound ../outside.wav]', '[role X pan=inf]',
                       '[output format=hls]', '[music tempo=9999]', '[effect Narrator arbitrary command=1]'):
            with self.subTest(script=script), self.assertRaises(ValueError):
                parse_script(script)

    def test_rejects_plan_nan_bool_ranges_and_unknown_fields(self):
        plan = parse_script('Hello').to_dict()
        for value in (float('nan'), True, 2):
            bad = json.loads(json.dumps(plan))
            bad['roles'][0]['pan'] = value
            with self.assertRaises(ValueError):
                ProductionPlan.from_dict(bad)
        with self.assertRaises(ValueError):
            ProductionPlan.from_dict({**plan, 'execute': 'anything'})

    def test_multi_hour_duration_is_not_rejected(self):
        plan = parse_script('[output sample_rate=8000 stems=false]\n[pause 36000]')
        self.assertEqual(plan.scenes[0].events[0].seconds, 36000)

    def test_late_role_changes_do_not_rewrite_previous_speech(self):
        for script in ('Alice: First.\n[role Alice pan=left]\nAlice: Second.',
                       'Narrator: First.\n[effect Narrator highpass frequency=80]',
                       'Narrator: First.\n[music mood=suspense]'):
            with self.assertRaisesRegex(ValueError, 'before'):
                parse_script(script)

    def test_bounded_speech_segments_preserve_every_character(self):
        for content in ('A sentence. ' * 2000, 'Довгесловобезпробілу' * 700, 'Word' + ' ' * 16000):
            pieces = speech_segments(content)
            self.assertEqual(''.join(pieces), content)
            self.assertTrue(all(0 < len(piece) <= 7000 for piece in pieces))


@unittest.skipUnless(FFMPEG.is_file(), 'Requires an installed FFmpeg executable')
class ProductionRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = ToneEngine()
        self.plan = parse_script('[output sample_rate=8000]\n# First\n[role Alice pan=left]\nAlice: One.\n[pause 0.1]\n# Second\n[role Bob pan=right]\nBob: Two.')

    def tearDown(self):
        self.temp.cleanup()

    def run_plan(self, plan=None, **kwargs):
        return render_project(plan or self.plan, self.root, engine=self.engine, ffmpeg=FFMPEG, **kwargs)

    def test_real_ffmpeg_multiscene_output_stems_subtitles_and_cached_resume(self):
        first = self.run_plan()
        self.assertEqual(len(self.engine.calls), 2)
        info = wav_info(first['output'])
        self.assertEqual((info.frames, info.channels, info.rate), (4000, 2, 8000))
        self.assertEqual(len(first['chapters']), 2)
        self.assertFalse(first['measurements']['clipped'])
        self.assertIn('Alice: One.', Path(first['subtitles']).read_text('utf-8'))
        for stem in first['stems']:
            self.assertEqual(wav_info(stem).frames, info.frames)
        second = self.run_plan()
        self.assertEqual(second['output'], first['output'])
        self.assertEqual(len(self.engine.calls), 2)

    def test_cancel_then_restart_reuses_sealed_speech(self):
        cancel = [False]
        def progress(message):
            if 'saved an audio step' in message:
                cancel[0] = True
        with self.assertRaises(RenderCancelled):
            self.run_plan(cancelled=lambda: cancel[0], on_status=progress)
        self.assertEqual(len(self.engine.calls), 1)
        journal = json.loads((self.root / 'render-journal.json').read_text('utf-8'))
        self.assertEqual(journal['state'], 'cancelled')
        self.run_plan()
        self.assertEqual(len(self.engine.calls), 2)

    def test_only_changed_scene_speech_is_regenerated(self):
        first = self.run_plan()
        original_first = first['files']
        changed = parse_script(self.plan.original_script.replace('Two.', 'Three.'))
        second = self.run_plan(changed)
        self.assertEqual(self.engine.calls, ['One.', 'Two.', 'Three.'])
        a = next(x['sha256'] for x in original_first if x['name'] == 'chapter-000001.wav')
        b = next(x['sha256'] for x in second['files'] if x['name'] == 'chapter-000001.wav')
        self.assertEqual(a, b)

    def test_corrupted_speech_checkpoint_is_regenerated(self):
        first = self.run_plan()
        for item in (self.root / 'cache').glob('*.json'):
            data = json.loads(item.read_text('utf-8'))
            if data.get('provenance', {}).get('kind') == 'speech':
                item.with_suffix('.wav').write_bytes(b'corrupt')
                break
        # Remove only the completion marker to force inspection of the DAG.
        Path(first['manifest']).unlink()
        self.run_plan()
        self.assertEqual(len(self.engine.calls), 3)
        self.assertTrue((self.root / 'cache' / 'quarantine').is_dir())

    def test_output_corruption_is_not_returned_as_success(self):
        first = self.run_plan()
        Path(first['output']).write_bytes(b'broken')
        second = self.run_plan()
        self.assertEqual(wav_info(second['output']).frames, 4000)
        self.assertEqual(len(self.engine.calls), 2)

    def test_editing_export_cannot_damage_sealed_scene(self):
        from carr_library.audio import jobs
        completed = self.run_plan()
        hashes = {path: jobs.sha256_file(path) for path in (self.root / 'cache').glob('*.wav')}
        chapter = Path(completed['output']).parent / 'chapter-000001.wav'
        chapter.write_bytes(b'user changed exported chapter')
        self.assertTrue(all(jobs.sha256_file(path) == checksum for path, checksum in hashes.items()))
        repaired = self.run_plan()
        self.assertEqual(wav_info(Path(repaired['output']).parent / chapter.name).frames,
                         completed['chapters'][0]['end_frame'])
        self.assertEqual(len(self.engine.calls), 2)

    def test_new_pipeline_does_not_reuse_legacy_aliased_complete_output(self):
        from carr_library.audio import jobs, production
        with patch.object(production, 'PIPELINE_VERSION', 1):
            legacy = self.run_plan()
        legacy_chapter = Path(legacy['output']).parent / 'chapter-000001.wav'
        checksum = jobs.sha256_file(legacy_chapter)
        old_cache = set((self.root / 'cache').glob('*.wav'))
        sealed = next(path for path in old_cache if jobs.sha256_file(path) == checksum)
        legacy_chapter.unlink()
        os.link(sealed, legacy_chapter)
        self.assertTrue(sealed.samefile(legacy_chapter))

        current = self.run_plan()
        self.assertNotEqual(current['run_key'], legacy['run_key'])
        new_cache = set((self.root / 'cache').glob('*.wav')) - old_cache
        self.assertTrue(new_cache)
        checksums = {path: jobs.sha256_file(path) for path in new_cache}
        legacy_chapter.write_bytes(b'Edited legacy exported chapter')
        self.assertTrue(all(jobs.sha256_file(path) == value for path, value in checksums.items()))
        self.assertEqual(self.run_plan()['output'], current['output'])
        self.assertEqual(len(self.engine.calls), 4)

    def test_retained_project_payload_exhausts_budget_before_speech_starts(self):
        previous = self.root / 'music-jobs' / 'retained'
        previous.mkdir(parents=True)
        (previous / 'old.wav').write_bytes(b'x' * (1024 ** 2))
        plan = replace(self.plan, disk_budget_bytes=1024 ** 2)
        with self.assertRaisesRegex(RuntimeError, 'disk budget'):
            self.run_plan(plan)
        self.assertEqual(self.engine.calls, [])
        self.assertFalse(list((self.root / 'cache').glob('*.wav')))

    def test_symlink_project_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            link = self.root / 'alias'
            try:
                link.symlink_to(other, target_is_directory=True)
            except OSError:
                self.skipTest('OS does not permit symlinks')
            with self.assertRaises(ValueError):
                render_project(self.plan, link, engine=self.engine, ffmpeg=FFMPEG)

    def test_changed_sound_asset_invalidates_completed_render(self):
        assets = self.root / 'assets'
        assets.mkdir()
        self.engine.synthesize('Door one', assets / 'door.wav')
        plan = parse_script('[output sample_rate=8000 stems=false]\n[sound door.wav]')
        first = self.run_plan(plan)
        self.engine.synthesize('Door two', assets / 'door.wav')
        second = self.run_plan(plan)
        self.assertNotEqual(first['run_key'], second['run_key'])
        self.assertNotEqual(Path(first['output']).read_bytes(), Path(second['output']).read_bytes())

    def test_untrusted_completion_output_path_is_not_returned(self):
        result = self.run_plan()
        path = Path(result['manifest'])
        data = json.loads(path.read_text('utf-8'))
        data['output_name'] = '../../outside.wav'
        path.write_text(json.dumps(data), encoding='utf-8')
        repaired = self.run_plan()
        self.assertEqual(Path(repaired['output']).name, 'master.wav')

    def test_unused_default_voice_is_not_resolved(self):
        class Restricted(ToneEngine):
            def cache_identity(self, voice):
                if voice == 'sapi5:default':
                    raise RuntimeError('Not installed')
                return super().cache_identity(voice)
        self.engine = Restricted()
        plan = parse_script('[role Alice voice=fixture:voice]\nAlice: Hello.')
        self.run_plan(plan)

    def test_long_line_uses_bounded_cached_requests_and_complete_subtitle(self):
        content = 'One word. ' * 1800
        plan = parse_script(content)
        result = self.run_plan(plan)
        self.assertGreater(len(self.engine.calls), 1)
        self.assertTrue(all(len(call) <= 7000 for call in self.engine.calls))
        # Identical chunks may share synthesis, so compare the documented split.
        self.assertEqual(''.join(speech_segments(content)), content)
        self.assertIn(content, Path(result['subtitles']).read_text('utf-8'))

    def test_generated_music_companions_survive_complete_render(self):
        plan = parse_script('[output sample_rate=8000 stems=true]\n[music mood=calm tempo=90 seed=7]\nNarrator: Music fixture.\n[pause 0.3]')
        result = self.run_plan(plan)
        names = {item['name'] for item in result['files']}
        self.assertIn('music-000001.mid', names)
        self.assertIn('music-000001.music.json', names)
        self.assertTrue((self.root / 'music-jobs').is_dir())
        self.assertEqual(self.run_plan(plan)['run_key'], result['run_key'])

    def test_music_cancellation_retains_engine_chunks_for_next_run(self):
        from carr_library.audio import music
        plan = parse_script('[output sample_rate=8000 stems=false]\n[music mood=calm tempo=240]\n[pause 10]')
        stopped = [False]
        def progress(message):
            if message.startswith('Music chunk 1 of '):
                stopped[0] = True
        with self.assertRaises((InterruptedError, RenderCancelled)):
            self.run_plan(plan, cancelled=lambda: stopped[0], on_status=progress)
        states = list((self.root / 'music-jobs').glob('*/cue.music-work/state.json'))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text('utf-8'))
        self.assertEqual(len(state['chunks']), 1)
        accepted = states[0].parent / 'part-00000000.wav'
        checksum = hashlib.sha256(accepted.read_bytes()).hexdigest()
        with patch.object(music, '_render_procedural_chunk', wraps=music._render_procedural_chunk) as generate:
            result = self.run_plan(plan)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(hashlib.sha256(accepted.read_bytes()).hexdigest(), checksum)
        self.assertEqual(result['frames'], 80000)


if __name__ == '__main__':
    unittest.main()
