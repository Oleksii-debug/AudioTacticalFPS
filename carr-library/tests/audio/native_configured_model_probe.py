"""Optional real installed-model inference, never satisfied by protocol fixtures.

python -I -B -X utf8 tests/audio/native_configured_model_probe.py \
    --config C:/ReviewedVoices/voice-settings.json --voice <enumerated-id> --language uk
No model/runtime is downloaded and no generated audio is retained or published.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--voice')
    parser.add_argument('--language', choices=('uk', 'sk', 'en'), default='en')
    args = parser.parse_args()
    if not args.config or not args.voice:
        print(json.dumps({'status': 'NOT_RUN',
            'reason': 'An explicitly configured real local model and enumerated voice are required.',
            'manual_nvda': 'NOT_RUN', 'native_language_listening': 'NOT_RUN'}))
        return 77
    from carr_library.audio.voices import VoiceRegistry
    registry = VoiceRegistry.from_config_file(Path(args.config).absolute())
    voice = registry.choose_voice(args.language, args.voice)
    if voice.engine not in ('piper', 'dordiai-voices'):
        raise ValueError('This optional-model probe requires Piper or dordiai-voices, not a system-voice substitute.')
    text = {'uk': 'Це перевірка встановленого локального голосу українською мовою.',
            'sk': 'Toto je skúška nainštalovaného miestneho hlasu v slovenčine.',
            'en': 'This is a test of the installed local voice model.'}[args.language]
    fingerprint = registry.fingerprint()
    with TemporaryDirectory(prefix='carr-model-acceptance-') as temporary:
        result = registry.synthesize(text, Path(temporary) / 'speech.wav',
            voice_id=voice.id, language=args.language, timeout_seconds=600)
        assert result.frames > 0 and result.duration_seconds > 0
        report = {'status': 'PASS', 'engine': voice.engine, 'language': args.language,
            'voice_id_sha256': sha256(voice.id.encode()).hexdigest(), 'runtime_sha256': fingerprint,
            'audio_sha256': result.sha256, 'text_sha256': result.text_sha256,
            'sample_rate': result.sample_rate, 'channels': result.channels,
            'frames': result.frames, 'duration_seconds': result.duration_seconds,
            'real_configured_model_invoked': True, 'manual_nvda': 'NOT_RUN',
            'native_language_listening': 'NOT_RUN'}
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
