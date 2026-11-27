"""Headless entrypoint uses the same typed local production model."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pytest

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


def _environment():
    import carr_library.audio_cli as module
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(module.__file__).resolve().parents[1])
    environment['PYTHONIOENCODING'] = 'utf-8'
    return environment


def _ffmpeg():
    binary = os.environ.get('CARR_AUDIO_FFMPEG') or shutil.which('ffmpeg')
    if not binary:
        pytest.fail('Actual FFmpeg is required for the audio CLI production tests.')
    return str(Path(binary).resolve())


def _pause_plan(path, *, long=False):
    from carr_library.audio.production import parse_script
    script = ('[output sample_rate=8000 stems=false]\n# First\n[pause 0.1]\n'
              '# Second\n[pause ' + ('3600' if long else '0.2') + ']\n')
    plan = parse_script(script, voice='unused:local')
    path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False), encoding='utf-8')
    return plan


def test_cli_explicit_ffmpeg_is_used_for_runtime_voice_discovery(tmp_path, monkeypatch):
    import carr_library.audio.engines as engines
    selected = []
    class Registry:
        def voices(self): return ()
        def capabilities(self): return ()
    def registry(**settings):
        selected.append(settings)
        return Registry()
    monkeypatch.setattr(engines, 'default_registry', registry)
    binary = tmp_path / 'explicit-local-ffmpeg'
    binary.write_bytes(b'Existing runtime selection; the registry is replaced by this test.')
    with redirect_stdout(io.StringIO()):
        assert main(['--audio-voices', '--ffmpeg', str(binary)]) == 0
    assert selected[0]['ffmpeg'] == binary


def test_actual_cli_plan_render_resume_preserves_source_and_exact_duration(tmp_path):
    from carr_library.audio.ffmpeg import wav_info
    plan_file, output = tmp_path / 'План.json', tmp_path / 'Output folder'
    _pause_plan(plan_file)
    original = plan_file.read_bytes()
    command = [sys.executable, '-m', 'carr_library', '--audio-plan', str(plan_file),
               '--audio-output', str(output), '--ffmpeg', _ffmpeg()]
    completed = subprocess.run(command, env=_environment(), capture_output=True,
                               text=True, encoding='utf-8', timeout=30)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result['status'] == 'complete' and result['frames'] == 2400
    assert wav_info(result['output']).frames == 2400
    before = Path(result['output']).read_bytes()
    repeated = subprocess.run(command, env=_environment(), capture_output=True,
                              text=True, encoding='utf-8', timeout=30)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)['output'] == result['output']
    assert 'verified its output checksums' in repeated.stderr
    assert Path(result['output']).read_bytes() == before and plan_file.read_bytes() == original
