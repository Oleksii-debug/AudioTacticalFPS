"""Run against the installed package on Windows; no-device is explicit."""
import json

from carr_library.audio.playback import native_playback_probe


if __name__ == "__main__":
    result = native_playback_probe()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 77 if result["status"] == "UNAVAILABLE" else 1)
