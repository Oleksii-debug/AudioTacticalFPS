"""Headless entrypoint uses the same typed local production model."""
from contextlib import redirect_stdout, redirect_stderr
import argparse
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from carr_library.__main__ import main
from carr_library.audio.engines import Voice
from carr_library.audio_cli import run_audio, validate_audio_arguments


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

    def test_ocr_audio_and_doi_commands_are_exclusive_before_runtime_activation(self):
        commands = (
            ['--audio-voices', '--list-ocr-models'],
            ['--audio-voices', '--verify-ocr-models', 'eng'],
            ['--audio-voices', '--install-ocr-models', 'source', '--ocr-model', 'eng'],
            ['--lookup-doi', '10.1234/synthetic', '--workspace', 'fixture', '--list-ocr-models'],
            ['--lookup-doi', '10.1234/synthetic', '--workspace', 'fixture', '--audio-voices'],
        )
        for options in commands:
            with self.subTest(options=options), redirect_stderr(io.StringIO()), \
                    patch('carr_library.native_runtime.activate_bundled_runtime') as activate:
                with self.assertRaises(SystemExit) as error:
                    main(options)
                self.assertEqual(error.exception.code, 2)
                activate.assert_not_called()

    def test_audio_and_doi_are_distinct_operations_with_legacy_namespace_compatibility(self):
        args = SimpleNamespace(audio_voices=True, audio_script=None, audio_plan=None,
            self_test=False, backup=None, restore=None, list_libraries=False, search=None,
            audio_preview=False, audio_output=None, voice=None, ffmpeg=None,
            dordiai_manifest=None, piper_executable=None, piper_manifest=None)
        parser = argparse.ArgumentParser()
        validate_audio_arguments(args, parser)  # Older callers have no DOI option.
        args.lookup_doi = None
        validate_audio_arguments(args, parser)
        args.lookup_doi = '10.1234/synthetic'
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            validate_audio_arguments(args, parser)
        self.assertEqual(raised.exception.code, 2)

    def test_default_render_selects_first_available_voice_or_stops_without_rendering(self):
        unknown = Voice('dordiai-voices:unknown', 'Unknown profile', (), 'dordiai-voices',
                        available=False, reason='Per-profile languages are not declared.')
        usable = Voice('sapi5:synthetic', 'Synthetic installed voice', ('en',), 'sapi5')
        for rows in ((unknown, usable), (unknown,)):
            with self.subTest(usable=len(rows) == 2), tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / 'script.txt'
                source.write_text('Synthetic narration.', encoding='utf-8')
                args = SimpleNamespace(audio_voices=False, audio_script=source, audio_plan=None,
                    audio_preview=False, audio_output=Path(temp) / 'output', voice=None,
                    language='en', ffmpeg=None, dordiai_manifest=None,
                    piper_executable=None, piper_manifest=None)
                registry = SimpleNamespace(voices=lambda: rows)
                renderer = Mock(return_value={'state': 'synthetic-only'})
                with patch('carr_library.audio.engines.default_registry', return_value=registry), \
                        patch('carr_library.audio.runtime.find_ffmpeg', return_value='synthetic-ffmpeg'), \
                        patch('carr_library.audio.production.render_project', renderer), \
                        redirect_stdout(io.StringIO()):
                    if len(rows) == 2:
                        self.assertEqual(run_audio(args), 0)
                        plan = renderer.call_args.args[0]
                        self.assertEqual(plan.roles[0].voice, usable.id)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'No local speech voice is available'):
                            run_audio(args)
                        renderer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
