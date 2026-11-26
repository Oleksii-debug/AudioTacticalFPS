"""State/path/native-ABI regressions. A fake MCI device is not hardware evidence."""
import ctypes
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from carr_library.speech import player


def make_wav(path, frames=22050):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(22050)
        stream.writeframes(b"\x01\x00" * frames)


class FakeDevice:
    def __init__(self):
        self.devices = {}
        self.next_id = 1
        self.calls = []
        self.bad_length = False
        self.fail_close = None

    def open(self, path):
        device = self.next_id
        self.next_id += 1
        self.devices[device] = {"length": 3000 if self.bad_length else 1000, "position": 0, "mode": 525}
        self.calls.append(("open", path))
        return device

    def query(self, device, item):
        mapping = {player._STATUS_LENGTH: "length", player._STATUS_POSITION: "position", player._STATUS_MODE: "mode"}
        return self.devices[device][mapping[item]]

    def play(self, device):
        self.calls.append(("play", device))
        self.devices[device]["mode"] = 526

    def pause(self, device):
        self.calls.append(("pause", device))
        self.devices[device]["mode"] = 529

    def resume(self, device):
        self.calls.append(("resume", device))
        self.devices[device]["mode"] = 526

    def stop(self, device):
        self.calls.append(("stop", device))
        self.devices[device]["mode"] = 525

    def seek(self, device, milliseconds):
        self.calls.append(("seek", device, milliseconds))
        self.devices[device]["position"] = milliseconds
        self.devices[device]["mode"] = 525

    def close(self, device):
        if device == self.fail_close:
            raise player.PlayerError("fake device failure")
        self.calls.append(("close", device))
        del self.devices[device]


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio = self.root / "Čítanie українською.wav"
        make_wav(self.audio)
        self.backend = FakeDevice()
        self.factory = patch.object(player, "_new_backend", return_value=self.backend)
        self.factory.start()
        self.instance = player.LocalPlayer()

    def tearDown(self):
        self.backend.fail_close = None
        self.instance.close()
        self.factory.stop()
        self.tmp.cleanup()

    def test_open_does_not_play_or_modify_audio(self):
        before = sha256(self.audio.read_bytes()).hexdigest()
        result = self.instance.open(self.audio)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["duration_seconds"], 1)
        self.assertEqual(result["path"], str(self.audio))
        self.assertEqual([call[0] for call in self.backend.calls], ["open"])
        self.assertEqual(sha256(self.audio.read_bytes()).hexdigest(), before)

    def test_pause_resume_preserve_position_and_stop_rewinds(self):
        self.instance.open(self.audio)
        self.assertEqual(self.instance.play()["state"], "playing")
        self.backend.devices[1]["position"] = 340
        paused = self.instance.pause()
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["position_seconds"], .34)
        self.assertEqual(self.instance.resume()["state"], "playing")
        self.assertIn(("resume", 1), self.backend.calls)
        stopped = self.instance.stop()
        self.assertEqual((stopped["state"], stopped["position_seconds"]), ("stopped", 0.0))

    def test_seek_while_playing_resumes_and_paused_seek_is_silent(self):
        self.instance.open(self.audio)
        self.instance.play()
        result = self.instance.seek(.5)
        self.assertEqual((result["state"], result["position_seconds"]), ("playing", .5))
        self.instance.pause()
        self.backend.calls.clear()
        result = self.instance.seek(.75)
        self.assertEqual((result["state"], result["position_seconds"]), ("paused", .75))
        self.assertEqual(self.backend.calls, [("seek", 1, 750)])
        self.instance.resume()
        self.assertEqual(self.backend.calls[-1], ("play", 1))

    def test_end_is_reported_and_play_restarts(self):
        self.instance.open(self.audio)
        self.backend.devices[1].update(position=1000, mode=525)
        self.assertEqual(self.instance.status()["state"], "ended")
        self.instance.play()
        self.assertIn(("seek", 1, 0), self.backend.calls)
        self.assertEqual(self.instance.status()["state"], "playing")

    def test_invalid_seek_never_reaches_device(self):
        self.instance.open(self.audio)
        self.backend.calls.clear()
        for value in (-1, 1.1, True, float("nan"), float("inf"), "0.2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.instance.seek(value)
        self.assertEqual(self.backend.calls, [])

    def test_close_is_idempotent_and_closes_only_owned_device(self):
        self.instance.open(self.audio)
        self.assertEqual(self.instance.close()["state"], "closed")
        self.instance.close()
        self.assertEqual([call for call in self.backend.calls if call[0] == "close"], [("close", 1)])

    def test_failed_new_open_preserves_old_file_and_releases_candidate(self):
        self.instance.open(self.audio)
        second = self.root / "second.wav"
        make_wav(second)
        self.backend.bad_length = True
        with self.assertRaises(player.PlayerError):
            self.instance.open(second)
        self.assertEqual(self.instance.status()["path"], str(self.audio))
        self.assertEqual(set(self.backend.devices), {1})

    def test_failed_old_close_releases_new_candidate_and_retains_retry_handle(self):
        self.instance.open(self.audio)
        self.backend.fail_close = 1
        with self.assertRaises(player.PlayerError):
            self.instance.open(self.audio)
        self.assertEqual(set(self.backend.devices), {1})
        self.assertEqual(self.instance.status()["path"], str(self.audio))

    def test_unavailable_backend_is_explicit(self):
        with patch.object(player, "_new_backend", side_effect=player.PlayerUnavailable("no device")):
            instance = player.LocalPlayer()
            self.assertFalse(player.available())
            self.assertFalse(instance.available())
            self.assertEqual(instance.status()["state"], "unavailable")
            with self.assertRaises(player.PlayerUnavailable):
                instance.open(self.audio)
            instance.close()

    def test_playback_speed_not_conflated_with_synthesis(self):
        self.assertNotIn("rate", self.instance.status()["capabilities"])
        with self.assertRaises(player.PlayerUnavailable):
            self.instance.set_rate(1.5)
        self.assertEqual(self.instance.set_rate(1)["rate"], 1)

    def test_closed_commands_fail_without_native_actions(self):
        for action in (self.instance.play, self.instance.pause, self.instance.stop, lambda: self.instance.seek(.5)):
            with self.assertRaises(player.PlayerError):
                action()
        self.assertEqual(self.backend.calls, [])

    def test_remote_nonpcm_truncated_and_symlink_inputs_fail(self):
        for path in ("https://example.test/file.wav", "//server/share/audio.wav", r"\\server\share\audio.wav",
                     self.root / "file.mp3"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                player._input_file(path)
        self.audio.write_bytes(self.audio.read_bytes()[:-2])
        with self.assertRaises(ValueError):
            player._input_file(self.audio)
        if os.name == "posix":
            target = self.root / "link.wav"
            target.symlink_to(self.audio)
            with self.assertRaises(ValueError):
                player._input_file(target)

    def test_windows_api_layout_uses_packed_fixed_width_fields(self):
        # SDK mmsystem.h includes mciapi.h under pshpack1.h. DWORD stays 32-bit
        # even when ctypes.c_ulong is 64-bit on the Linux test host.
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(player._Open.device_type.offset, pointer + 4)
        self.assertEqual(ctypes.sizeof(player._Open), 4 * pointer + 8)
        self.assertEqual(ctypes.sizeof(player._Seek), pointer + 4)
        self.assertEqual(ctypes.sizeof(player._Status), 2 * pointer + 8)


if __name__ == "__main__":
    unittest.main()
