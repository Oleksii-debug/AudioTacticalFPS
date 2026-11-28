"""Exact PCM/source pacing, not speech-quality or linguistic-alignment claims."""
from hashlib import sha256
from pathlib import Path
import wave

import pytest

from carr_library.speech import SpeechSettings, Voice, VoiceRegistry, split_utterances
from carr_library.speech import runtime
from carr_library.speech.focus import FocusPlan, ReadingAudio
from carr_library.speech.reading import ReadingDocument


class PcmVoice:
    name = 'Purpose-written integer PCM pacing fixture'

    def __init__(self, *, width=2, channels=1, rate=8000, frames=80):
        self.width, self.channels, self.rate, self.frames = width, channels, rate, frames
        self.voice = Voice('fixture:pcm-pacing', self.name, 'Synthetic PCM', 'und',
                           ('rate', 'volume', 'pitch', 'punctuation'))
        self.calls = []
        sample = b'\xc0' if width == 1 else (4096).to_bytes(width, 'little', signed=True)
        self.data = sample * channels * frames

    def voices(self):
        return (self.voice,)

    def render(self, text, voice, output, settings, cancelled):
        runtime.check_cancelled(cancelled)
        self.calls.append((text, settings))
        with wave.open(str(output), 'wb') as stream:
            stream.setnchannels(self.channels)
            stream.setsampwidth(self.width)
            stream.setframerate(self.rate)
            stream.writeframes(self.data)


def read_pcm(path):
    with wave.open(str(path), 'rb') as stream:
        return (stream.getframerate(), stream.getnchannels(), stream.getsampwidth(),
                stream.getnframes(), stream.readframes(stream.getnframes()))


@pytest.mark.parametrize('width', [1, 2, 3, 4])
@pytest.mark.parametrize('channels', [1, 2])
def test_extra_pauses_have_exact_integer_pcm_and_unchanged_source(tmp_path, width, channels):
    provider = PcmVoice(width=width, channels=channels)
    registry = VoiceRegistry([provider])
    text = 'One. Two.\n\nThree.\fFour.'
    original = text.encode('utf-8')
    settings = SpeechSettings(rate=2, volume=37, pitch=23, punctuation=True,
                              sentence_pause_ms=125, paragraph_pause_ms=250, section_pause_ms=375)
    try:
        result = registry.synthesize(text, provider.voice.id, tmp_path / 'paced.wav', settings=settings)
    finally:
        registry.close()
    pauses = (1000, 2000, 3000)
    silence = b'\x80' if width == 1 else b'\0'
    expected = b''.join(provider.data + silence * (frames * channels * width) for frames in pauses)
    expected += provider.data
    assert read_pcm(result.path) == (8000, channels, width, 320 + sum(pauses), expected)
    assert result.frames == 320 + sum(pauses)
    assert result.duration_seconds == result.frames / 8000
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    assert result.text_sha256 == sha256(original).hexdigest() and text.encode('utf-8') == original
    assert ''.join(row[0] for row in provider.calls) == text and result.chunks == 4
    assert [pause.kind for pause in result.pauses] == ['sentence', 'paragraph', 'section']
    assert [pause.text_offset for pause in result.pauses] == [text.index(word) for word in ('Two', 'Three', 'Four')]
    assert [(pause.start_frame, pause.end_frame) for pause in result.pauses] == [
        (80, 1080), (1160, 3160), (3240, 6240)]
    assert all(row[1] == SpeechSettings(rate=2, volume=37, pitch=23, punctuation=True)
               for row in provider.calls)


def test_zero_pauses_preserve_original_chunks_options_and_pcm_bytes(tmp_path):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    text = ('Sentence. Next.\n\nParagraph.\fSection. ' * 100) + 'End.'
    original = SpeechSettings()
    explicit_zero = SpeechSettings(sentence_pause_ms=0, paragraph_pause_ms=0, section_pause_ms=0)
    try:
        first = registry.synthesize(text, provider.voice.id, tmp_path / 'original.wav', settings=original)
        first_calls = tuple(provider.calls)
        provider.calls.clear()
        second = registry.synthesize(text, provider.voice.id, tmp_path / 'zero.wav', settings=explicit_zero)
    finally:
        registry.close()
    expected = [chunk for chunk in split_utterances(text) if chunk.strip()]
    assert [row[0] for row in first_calls] == [row[0] for row in provider.calls] == expected
    assert all(row[1] is original for row in first_calls)
    assert all(row[1] is explicit_zero for row in provider.calls)
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == second.sha256 and first.pauses == second.pauses == ()
    assert first.chunks == second.chunks == len(expected)


@pytest.mark.parametrize('text,offsets,kinds', [
    ('\fOne.\n\nTwo.\f', (7,), ('paragraph',)),
    ('Heading\nText\n\nNext\fLast', (8, 14, 19), ('sentence', 'paragraph', 'section')),
    ('One.\n\n\fTwo.', (7,), ('section',)),
    ('One\u2029Two', (4,), ('paragraph',)),
])
def test_explicit_boundaries_choose_one_pause_without_leading_or_trailing_silence(
        tmp_path, text, offsets, kinds):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    try:
        result = registry.synthesize(text, provider.voice.id, tmp_path / 'boundaries.wav',
            settings=SpeechSettings(sentence_pause_ms=1, paragraph_pause_ms=2, section_pause_ms=3))
    finally:
        registry.close()
    assert tuple(p.text_offset for p in result.pauses) == offsets
    assert tuple(p.kind for p in result.pauses) == kinds
    assert result.frames == provider.frames * result.chunks + sum(p.end_frame - p.start_frame for p in result.pauses)
    assert all(0 < p.text_offset < len(text) for p in result.pauses)
    assert read_pcm(result.path)[4].startswith(provider.data)
    assert read_pcm(result.path)[4].endswith(provider.data)
    assert result.text_sha256 == sha256(text.encode('utf-8')).hexdigest()


def test_zero_specific_pause_does_not_fall_back_to_a_different_boundary(tmp_path):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    text = 'First.\n\nSecond.\fThird. Fourth.'
    try:
        result = registry.synthesize(text, provider.voice.id, tmp_path / 'specific.wav',
            settings=SpeechSettings(sentence_pause_ms=25))
    finally:
        registry.close()
    assert [(p.text_offset, p.kind) for p in result.pauses] == [(text.index('Fourth'), 'sentence')]
    assert [row[0] for row in provider.calls] == ['First.\n\nSecond.\fThird. ', 'Fourth.']
    assert result.frames == 160 + 200


def test_long_blank_run_preserves_one_pause_after_last_nonempty_utterance(tmp_path):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    text = 'First.' + ' \n' * 5000 + 'Last.'
    try:
        result = registry.synthesize(text, provider.voice.id, tmp_path / 'blank-run.wav',
            settings=SpeechSettings(paragraph_pause_ms=250))
    finally:
        registry.close()
    assert len(provider.calls) == result.chunks == 2
    assert all(len(row[0]) <= 2400 for row in provider.calls)
    assert len(result.pauses) == 1 and result.pauses[0].text_offset == text.index('Last')
    assert result.pauses[0].kind == 'paragraph'
    assert result.frames == 160 + 2000


@pytest.mark.parametrize('settings', [
    {'sentence_pause_ms': True}, {'sentence_pause_ms': -1}, {'sentence_pause_ms': 30001},
    {'paragraph_pause_ms': 1.5}, {'paragraph_pause_ms': float('nan')}, {'paragraph_pause_ms': '10'},
    {'section_pause_ms': None}, {'section_pause_ms': False}, {'section_pause_ms': 30001},
])
def test_pause_settings_reject_untyped_or_unbounded_values(settings):
    with pytest.raises(ValueError, match='integer milliseconds'):
        SpeechSettings(**settings)


@pytest.mark.parametrize('limit', ['utterances', 'total-silence'])
def test_pacing_limit_is_checked_before_any_engine_or_output(tmp_path, limit):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    text = 'A. ' * 10001 + 'Z' if limit == 'utterances' else 'A\f' * 121 + 'Z'
    settings = (SpeechSettings(sentence_pause_ms=1) if limit == 'utterances'
                else SpeechSettings(section_pause_ms=30000))
    try:
        with pytest.raises(ValueError, match='10000 utterances'):
            registry.synthesize(text, provider.voice.id, tmp_path / 'never.wav', settings=settings)
    finally:
        registry.close()
    assert not provider.calls and not list(tmp_path.iterdir())


def test_pcm_output_budget_counts_inserted_silence_before_publication(tmp_path):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    try:
        with pytest.raises(ValueError, match='audio budget'):
            registry.synthesize('First. Last.', provider.voice.id, tmp_path / 'never.wav',
                settings=SpeechSettings(sentence_pause_ms=1000, max_output_bytes=4096))
    finally:
        registry.close()
    assert len(provider.calls) == 1 and not list(tmp_path.iterdir())


def test_cancellation_interrupts_actual_silence_blocks_and_removes_unpublished_pcm(tmp_path, monkeypatch):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    original = runtime._silence
    inside, checks = False, 0
    def silenced(*args, **kwargs):
        nonlocal inside
        inside = True
        return original(*args, **kwargs)
    def cancelled():
        nonlocal checks
        if inside:
            checks += 1
        return checks == 2
    monkeypatch.setattr(runtime, '_silence', silenced)
    try:
        with pytest.raises(InterruptedError, match='cancelled'):
            registry.synthesize('First. Last.', provider.voice.id, tmp_path / 'never.wav',
                settings=SpeechSettings(sentence_pause_ms=30000), cancelled=cancelled)
    finally:
        registry.close()
    assert inside and checks >= 2 and len(provider.calls) == 1
    assert not list(tmp_path.iterdir())


def test_cancellation_interrupts_boundary_preparation_before_engine(tmp_path):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 6
    try:
        with pytest.raises(InterruptedError, match='cancelled'):
            registry.synthesize('First.' + '\n' * 100000 + 'Last.', provider.voice.id,
                tmp_path / 'never.wav', settings=SpeechSettings(paragraph_pause_ms=20),
                cancelled=cancelled)
    finally:
        registry.close()
    assert calls == 6 and not provider.calls and not list(tmp_path.iterdir())


def test_generic_helper_keeps_exact_old_six_field_settings_payload(tmp_path):
    calls = []
    provider = object.__new__(runtime.LocalHelperProvider)
    provider._tokens = {'fixture:helper': 'exact-token'}
    provider._request = lambda data, **kwargs: (calls.append((data, kwargs)) or {'status': 'rendered'})
    options = SpeechSettings(rate=3, volume=41, pitch=23, punctuation=True,
                             timeout_seconds=17, max_output_bytes=8192, sentence_pause_ms=125)
    provider.render('Exact text.', Voice('fixture:helper', 'Fixture', 'Fixture', 'und'),
                    tmp_path / 'not-generated.wav', options, lambda: False)
    assert len(calls) == 1
    assert calls[0][0]['settings'] == {
        'rate': 3, 'volume': 41, 'pitch': 23, 'punctuation': True,
        'timeout_seconds': 17, 'max_output_bytes': 8192}
    assert tuple(calls[0][0]['settings']) == (
        'rate', 'volume', 'pitch', 'punctuation', 'timeout_seconds', 'max_output_bytes')
    assert calls[0][0]['text'] == 'Exact text.' and calls[0][1]['budget'] == 8192
    assert not (tmp_path / 'not-generated.wav').exists()


def test_measured_pause_map_holds_previous_word_and_exact_rendered_selection(tmp_path):
    provider = PcmVoice()
    registry = VoiceRegistry([provider])
    doc = ReadingDocument('Prefix. First. Second.\n\nThird.\fFourth. Suffix.')
    first, last = doc.text.index('First'), doc.text.index(' Suffix')
    text, signature = doc.text[first:last], doc._signature()
    try:
        result = registry.synthesize(text, provider.voice.id, tmp_path / 'mapped.wav',
            settings=SpeechSettings(sentence_pause_ms=1000, paragraph_pause_ms=2000, section_pause_ms=3000))
    finally:
        registry.close()
    binding = ReadingAudio.from_render(doc, signature, first, last, text, result)
    plan = FocusPlan(doc)
    assert binding.pauses == result.pauses and len(binding.pauses) == 3
    for pause, word in zip(result.pauses, ('First', 'Second', 'Third')):
        for frame in (pause.start_frame, (pause.start_frame + pause.end_frame) / 2, pause.end_frame - .5):
            _, span = binding.span_at(plan, result.path, frame / result.sample_rate)
            assert doc.text[slice(*span)] == word
            assert first <= span[0] < span[1] <= last
    _, final = binding.span_at(plan, result.path, result.duration_seconds)
    assert doc.text[slice(*final)] == 'Fourth'
    assert doc.source_text == doc.text and doc._signature() == signature
    doc.replace(first, first + 5, 'Other')
    with pytest.raises(ValueError, match='changed'):
        binding.span_at(plan, result.path, .1)


@pytest.mark.parametrize('rate,milliseconds,extra_frames', [(8000, 1, 8), (11025, 20, 221), (44100, 5, 221)])
def test_millisecond_to_pcm_rounding_is_half_up_at_actual_sample_rate(tmp_path, rate, milliseconds, extra_frames):
    provider = PcmVoice(rate=rate)
    registry = VoiceRegistry([provider])
    try:
        result = registry.synthesize('First. Last.', provider.voice.id, tmp_path / 'rounded.wav',
            settings=SpeechSettings(sentence_pause_ms=milliseconds))
    finally:
        registry.close()
    assert result.sample_rate == rate and result.frames == 160 + extra_frames
    assert len(result.pauses) == 1
    assert result.pauses[0].end_frame - result.pauses[0].start_frame == extra_frames
    assert read_pcm(result.path)[4] == provider.data + b'\0' * (extra_frames * 2) + provider.data
