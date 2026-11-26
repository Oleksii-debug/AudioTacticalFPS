"""Explicit installed-Windows playback probe; never a listener/NVDA quality claim.

Default mode verifies native open/status/seek/close without playing any sound.
--play additionally exercises play/pause/resume/stop on a quiet synthetic tone.
Missing backend/device is an explicit failure, not a skipped passing assertion.
"""
from array import array
import argparse
import json
import math
from pathlib import Path
import tempfile
import time
import wave

from carr_library.speech.player import LocalPlayer, PlayerUnavailable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--play", action="store_true", help="Actually play a quiet synthetic tone on the default audio device.")
    options = parser.parse_args()
    with LocalPlayer() as player:
        if not player.available():
            raise PlayerUnavailable(player.status()["reason"])
        with tempfile.TemporaryDirectory(prefix="player-probe-") as directory:
            path = Path(directory) / "Čítanie українською.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(22050)
                samples = array("h", (int(900 * math.sin(2 * math.pi * 220 * i / 22050)) for i in range(22050 * 2)))
                audio.writeframes(samples.tobytes())
            opened = player.open(path)
            assert abs(opened["duration_seconds"] - 2.0) < .01
            assert opened["state"] in ("stopped", "open")
            sought = player.seek(.5)
            assert abs(sought["position_seconds"] - .5) < .01
            if options.play:
                assert player.play()["state"] == "playing"
                time.sleep(.1)
                assert player.pause()["state"] == "paused"
                assert player.resume()["state"] == "playing"
                stopped = player.stop()
                assert stopped["state"] == "stopped" and stopped["position_seconds"] < .01
            assert player.close()["state"] == "closed"
    print(json.dumps({"native_player_metadata": "PASS", "actual_playback_commands": "PASS" if options.play else "NOT_RUN",
                      "listener_quality": "NOT_RUN", "nvda": "NOT_RUN"}))


if __name__ == "__main__":
    main()
