"""Adapter safety/atomicity tests. Synthetic WAVs here are not speech-quality evidence."""
import base64
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from carr_library.speech import local
from carr_library.speech import SpeechRequest, SpeechUnavailable, SpeechError, RenderCancelled


def pcm(path, frames=1000):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"\0\x10" * frames)


class AdapterSafetyTests(unittest.TestCase):
    def test_invalid_request_never_starts_process(self):
        request = SpeechRequest("ordinary text", "espeak:en-us")
        changes = [{"text": ""}, {"text": "x" * (local.MAX_TEXT_CHARACTERS + 1)},
                   {"text": "secret\x00hidden"}, {"text": "\ud800"}, {"rate": True},
                   {"rate": float("nan")}, {"pitch": float("inf")}, {"volume": -0.1},
                   {"language": "en; evil"}]
        with patch.object(local.subprocess, "Popen") as process:
            for change in changes:
                with self.subTest(change=list(change)), self.assertRaises(ValueError):
                    local.render_speech(replace(request, **change), Path("new.wav"))
            process.assert_not_called()

    def test_missing_engine_does_not_substitute_or_invent_voice(self):
        with patch.object(local, "_entries", return_value=()):
            self.assertEqual(local.discover_voices(), ())
            with self.assertRaises(SpeechUnavailable):
                local.engine_fingerprint("espeak:en-us")

    def test_unsupported_named_engine_is_explicit(self):
        for voice in ("dordiai-voices:voice1", "sapi4:legacy"):
            with self.subTest(voice=voice), self.assertRaises(SpeechUnavailable):
                local.engine_fingerprint(voice)

    def test_powershell_code_is_static_and_plain_text_mode_is_forced(self):
        args = local._powershell_args(Path("powershell.exe"))
        code = base64.b64decode(args[-1]).decode("utf-16le")
        self.assertIn("[Console]::In.ReadToEnd() | ConvertFrom-Json", code)
        self.assertIn("Speak([string]$request.text, 16)", code)
        self.assertNotIn("Invoke-Expression", code)
        self.assertIn("-NoProfile", args)
        self.assertNotIn("-ExecutionPolicy", args)

    def test_actual_process_stdin_is_not_shell_or_command_input(self):
        payload = b"$(touch evil); `echo nope`; <speak>ordinary</speak>\n"
        code = "import sys,hashlib; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"
        result = local._run([sys.executable, "-c", code], payload)
        self.assertEqual(result.decode().strip(), sha256(payload).hexdigest())

    def test_actual_process_cancellation_is_prompt(self):
        start = time.monotonic()
        with self.assertRaises(RenderCancelled):
            local._run([sys.executable, "-c", "import time; time.sleep(20)"], b"",
                       cancelled=lambda: time.monotonic() - start > .2)
        self.assertLess(time.monotonic() - start, 3)

    def test_cancel_before_start_does_not_launch(self):
        with patch.object(local.subprocess, "Popen") as process:
            with self.assertRaises(RenderCancelled):
                local._run(["not-called"], b"", cancelled=lambda: True)
            process.assert_not_called()

    def test_actual_process_timeout_and_bounded_diagnostics(self):
        with self.assertRaises(SpeechError):
            local._run([sys.executable, "-c", "import time; time.sleep(20)"], b"", timeout=.15)
        for channel in ("stdout", "stderr"):
            with self.subTest(channel=channel), self.assertRaises(SpeechError):
                local._run([sys.executable, "-c", f"import sys; sys.{channel}.write('x'*3000000)"], b"")

    def test_failure_does_not_leak_private_input_or_stderr(self):
        secret = "private sentence should not be in diagnostic"
        with self.assertRaises(SpeechError) as raised:
            local._run([sys.executable, "-c", "import sys; sys.stderr.write(sys.stdin.read()); sys.exit(2)"],
                       secret.encode())
        self.assertNotIn(secret, str(raised.exception))

    def test_live_output_budget_checks_fast_exit_and_stops_a_running_process(self):
        for delay in (0, 20):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'stage.wav'
                started = time.monotonic()
                code = 'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_bytes(b"x"*10000); time.sleep(float(sys.argv[2]))'
                with self.assertRaises(local.SpeechBudgetExceeded):
                    local._run([sys.executable, '-c', code, str(target), str(delay)], b'',
                               output_path=target, max_output_bytes=1000)
                self.assertLess(time.monotonic() - started, 3)

    def test_existing_output_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.wav"
            target.write_bytes(b"original")
            with patch.object(local, "_entry") as registry:
                with self.assertRaises(FileExistsError):
                    local.render_speech(SpeechRequest("text", "voice"), target)
                registry.assert_not_called()
            self.assertEqual(target.read_bytes(), b"original")

    def test_remote_nonwav_and_symlink_outputs_are_rejected(self):
        for path in ("//server/share/output.wav", r"\\server\share\output.wav", "output.mp3"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                local._output_target(path)
        if os.name == "posix":
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / "real").mkdir()
                (base / "link").symlink_to(base / "real", target_is_directory=True)
                with self.assertRaises(ValueError):
                    local._output_target(base / "link" / "speech.wav")

    def test_explicit_remote_runtime_and_data_rejected_before_io(self):
        with patch.object(Path, "is_file", side_effect=AssertionError("remote path lookup")):
            for path in (r"\\server\share\espeak.exe", "//server/share/espeak.exe",
                         r"\\?\UNC\server\share\espeak.exe", "https://example.invalid/espeak.exe"):
                with self.subTest(path=path), patch.dict(os.environ, {"CARR_ESPEAK_EXECUTABLE": path}), \
                        self.assertRaises(SpeechUnavailable):
                    local._espeak_runtime()
        with patch.dict(os.environ, {"CARR_ESPEAK_EXECUTABLE": sys.executable,
                                    "CARR_ESPEAK_DATA_PATH": r"\\server\share\voices"}):
            with self.assertRaises(SpeechUnavailable):
                local._espeak_runtime()

    def test_default_discovery_does_not_select_current_directory_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / ("espeak.exe" if os.name == "nt" else "espeak")
            executable.write_bytes(b"not an installed speech engine")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"PATH": directory, "CARR_ESPEAK_EXECUTABLE": "",
                                        "CARR_ESPEAK_DATA_PATH": ""}), \
                    patch.object(Path, "cwd", return_value=Path(directory)), \
                    patch("carr_library.audio.ffmpeg.os.path.abspath",
                          side_effect=lambda value: directory if value == os.curdir else os.fspath(value)):
                self.assertIsNone(local._espeak_runtime())

    def test_wav_validator_rejects_empty_truncated_and_wrong_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            pcm(path)
            self.assertEqual(local._inspect_wav(path), (22050, 1, 1000))
            path.write_bytes(path.read_bytes()[:-2])
            with self.assertRaises(SpeechError):
                local._inspect_wav(path)
            pcm(path, 0)
            with self.assertRaises(SpeechError):
                local._inspect_wav(path)

    def test_synthetic_backend_cancellation_keeps_target_absent_and_removes_stage(self):
        """Test publication policy only, with an explicitly synthetic backend fixture."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "читання ž.wav"
            voice = local.Voice("sapi5:64:fixture", "sapi5", "Fixture", "en", ("local", "wav"))
            entry = local._Entry(voice, Path(sys.executable), "fixture", {})
            def cancelled_render(args, payload, **options):
                pcm(Path(json.loads(payload)["output"]))
                raise RenderCancelled("cancelled")
            with patch.object(local, "_entry", return_value=entry), \
                    patch.object(local, "_fingerprint", return_value="a" * 64), \
                    patch.object(local, "_run", side_effect=cancelled_render):
                with self.assertRaises(RenderCancelled):
                    local.render_speech(SpeechRequest("text", voice.id), target)
            self.assertFalse(target.exists())
            self.assertEqual(list(base.iterdir()), [])

    def test_synthetic_publication_is_no_clobber_even_after_target_race(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target, identity = local._output_target(base / "audio.wav")
            with tempfile.TemporaryDirectory(dir=base) as stage_dir:
                stage = Path(stage_dir) / "render.wav"
                pcm(stage)
                target.write_bytes(b"concurrent existing output")
                with self.assertRaises(FileExistsError):
                    local._publish(stage, target, identity)
            self.assertEqual(target.read_bytes(), b"concurrent existing output")

    def test_language_and_unsupported_pitch_fail_before_engine_render(self):
        voice = local.Voice("sapi5:64:fixture", "sapi5", "Fixture", "en-US", ("local", "wav", "rate"))
        entry = local._Entry(voice, Path(sys.executable), "fixture", {})
        with tempfile.TemporaryDirectory() as directory, patch.object(local, "_entry", return_value=entry):
            with patch.object(local, "_run") as run:
                for request in (SpeechRequest("text", voice.id, language="uk"),
                                SpeechRequest("text", voice.id, pitch=.2)):
                    with self.subTest(request=request), self.assertRaises(SpeechUnavailable):
                        local.render_speech(request, Path(directory) / "audio.wav")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
