"""Explicit actual-engine acceptance. Run directly; missing engines are a failure.

No recording is a speech-quality/listening assertion. These checks establish
real synthesis, duration/rate/volume, Unicode paths, no-clobber and checksums.
"""
from array import array
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import wave

from carr_library.speech import SpeechRequest, discover_voices, engine_fingerprint, render_speech


def main():
    voices = discover_voices()
    if not voices:
        raise RuntimeError("No installed local speech engine: actual speech acceptance NOT_RUN.")
    preferred = next((v for v in voices if v.id == "espeak:en-us"), None)
    preferred = preferred or next((v for v in voices if (v.language or "").startswith("en")), voices[0])
    text = "The local library preserves the original source. This is actual speech synthesis."
    reports = []
    with tempfile.TemporaryDirectory(prefix="speech-acceptance-") as directory:
        root = Path(directory)
        fingerprint = engine_fingerprint(preferred.id)
        slow = render_speech(SpeechRequest(text, preferred.id, rate=.7), root / "slow.wav")
        fast = render_speech(SpeechRequest(text, preferred.id, rate=1.4), root / "fast.wav")
        assert slow.frames > fast.frames, (slow.frames, fast.frames)
        assert fingerprint == slow.engine_fingerprint == fast.engine_fingerprint
        silent = render_speech(SpeechRequest(text, preferred.id, volume=0), root / "silent.wav")
        with wave.open(str(silent.path), "rb") as audio:
            assert not any(audio.readframes(audio.getnframes())), "Volume zero must produce silence."
        for lang, sentence in (("uk", "Це місцева перевірка голосу українською мовою."),
                               ("sk", "Toto je miestna skúška slovenského hlasu."),
                               (preferred.language, text)):
            selected = next((v for v in voices if v.language == lang), None)
            if selected is None:
                continue  # The report states what is actually installed and exercised.
            output = root / f"Čítanie українською {lang}.wav"
            rendered = render_speech(SpeechRequest(sentence, selected.id, language=lang), output)
            digest = sha256(output.read_bytes()).hexdigest()
            assert digest == rendered.sha256 and rendered.frames > 0
            with wave.open(str(output), "rb") as audio:
                samples = array("h", audio.readframes(audio.getnframes()))
                assert any(samples), "Actual nonzero speech PCM is required."
                peak = max(abs(sample) for sample in samples)
            try:
                render_speech(SpeechRequest("replacement", selected.id), output)
            except FileExistsError:
                pass
            else:
                raise AssertionError("Existing speech output was overwritten.")
            assert sha256(output.read_bytes()).hexdigest() == digest
            reports.append({"voice": selected.id, "language": lang, "frames": rendered.frames,
                            "sample_rate": rendered.sample_rate, "duration_seconds": rendered.duration_seconds,
                            "sha256": rendered.sha256, "peak_pcm16": peak})
    print(json.dumps({"actual_local_speech": "PASS", "installed_voice_entries": len(voices),
                      "rate_duration_order": "PASS", "zero_volume": "PASS", "no_overwrite": "PASS",
                      "source_text_unchanged": text == "The local library preserves the original source. This is actual speech synthesis.",
                      "renders": reports, "listening_quality": "NOT_RUN"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
