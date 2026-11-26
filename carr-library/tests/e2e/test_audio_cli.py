"""Headless entrypoint uses the same typed local production model."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from carr_library.__main__ import main


class AudioCliTests(unittest.TestCase):
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

    def test_audio_cannot_accidentally_run_with_online_acquisition(self):
        for audio in (['--audio-voices'], ['--audio-script', 'script.txt', '--audio-preview'],
                      ['--audio-plan', 'plan.json', '--audio-preview']):
            with self.subTest(audio=audio), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main([*audio, '--lookup-doi', '10.1234/synthetic', '--allow-online-doi',
                      '--workspace', 'synthetic-library'])
            self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
