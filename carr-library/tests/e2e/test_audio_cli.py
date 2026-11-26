"""Headless entrypoint uses the same typed local production model."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

from carr_library.__main__ import main
from carr_library.audio.engines import EngineUnavailable, FliteEngine


class AudioCliTests(unittest.TestCase):
    def test_selected_off_path_ffmpeg_lists_and_renders_its_actual_voice(self):
        binary = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
        if not binary:
            self.skipTest('No local FFmpeg build is installed.')
        binary = Path(binary).resolve()
        try:
            voice = FliteEngine(binary).voices()[0].id
        except EngineUnavailable:
            self.skipTest('This installed FFmpeg does not include libflite.')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'script.txt'
            original = 'An explicitly selected local voice reads this sentence.\n'
            source.write_text(original, encoding='utf-8')
            listed, rendered = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {'PATH': str(root / 'no-tools')}):
                self.assertIsNone(shutil.which('ffmpeg'))
                with redirect_stdout(listed):
                    status = main(['--audio-voices', '--ffmpeg', str(binary)])
                self.assertEqual(status, 0)
                self.assertIn(voice, {item['id'] for item in json.loads(listed.getvalue())['voices']})
                with redirect_stdout(rendered), redirect_stderr(io.StringIO()):
                    status = main(['--audio-script', str(source), '--voice', voice,
                                   '--audio-output', str(root / 'output'), '--ffmpeg', str(binary)])
                self.assertEqual(status, 0)
            result = json.loads(rendered.getvalue())
            self.assertEqual(result['status'], 'complete')
            with wave.open(result['output'], 'rb') as output:
                self.assertGreater(output.getnframes(), 2400)
                self.assertTrue(any(output.readframes(output.getnframes())))
            self.assertEqual(source.read_text(encoding='utf-8'), original)


    def test_preview_preserves_unicode_source_and_never_renders(self):
        script = '# Розділ\nОлексій: Добрий день.\n[pause 2]\n'
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'script.txt'
            source.write_bytes(script.encode('utf-8'))
            output = io.StringIO()
            with redirect_stdout(output):
                status = main(['--audio-script', str(source), '--audio-preview', '--language', 'uk'])
            self.assertEqual(status, 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan['original_script'], script)
            self.assertEqual(plan['roles'][0]['language'], 'uk')
            self.assertEqual(list(Path(temp).iterdir()), [source])

    def test_render_requires_explicit_destination(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--audio-script', 'input.txt'])
        self.assertEqual(raised.exception.code, 2)

    def test_audio_cannot_accidentally_run_with_backup(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--audio-voices', '--backup', 'backup'])

    def test_audio_does_not_combine_with_network_or_format_operations(self):
        cases = (['--document-formats'],
                 ['--lookup-doi', '10.1000/synthetic', '--allow-online-doi', '--workspace', 'synthetic'])
        for arguments in cases:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), \
                    patch('carr_library.application.LibraryApp') as library, \
                    patch('carr_library.audio_cli.run_audio') as render:
                with self.assertRaises(SystemExit) as raised:
                    main(['--audio-voices', *arguments])
                self.assertEqual(raised.exception.code, 2)
                library.assert_not_called()
                render.assert_not_called()


if __name__ == '__main__':
    unittest.main()
