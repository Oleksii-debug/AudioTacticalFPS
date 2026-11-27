"""Source-bound narration with real SQLite/FFmpeg; the speech fixture is a tone."""
from dataclasses import replace
from pathlib import Path
import math
import struct
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.engines import Voice, VoiceRegistry
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.runtime import available_tool
from carr_library.reader.live_reader import ReaderNarrator, select_source
from carr_library.reader.reading_desk import ReadingDesk


@pytest.fixture
def page(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact source')
    raw = 'First 😀\r\n[pause 2] is literal.\r\nLast words.'
    book = app.add_text(workspace, 'Synthetic source', raw)
    desk = ReadingDesk(app, workspace, book['edition'])
    return app, workspace, desk, desk.page(1), raw


def test_native_prefix_text_maps_emoji_and_crlf_without_offset_guessing(page):
    _, _, _, source, raw = page
    shown = raw.replace('\r\n', '\n')
    selected = select_source(source, mode='selection', prefix='First 😀\n',
                             through_selection='First 😀\n[pause 2]', displayed=shown)
    assert selected.text == '[pause 2]'
    assert raw[selected.owner.start:selected.owner.end] == selected.text
    cursor = select_source(source, mode='cursor', prefix='First 😀\n', displayed=shown)
    assert cursor.text == '[pause 2] is literal.\r\nLast words.'
    assert select_source(source).text == raw


@pytest.mark.parametrize('values', [dict(mode='selection'), dict(mode='cursor', prefix='wrong'),
                                    dict(displayed='edited'), dict(mode='not-a-mode')])
def test_empty_or_mismatched_native_selection_is_refused(page, values):
    with pytest.raises(ValueError):
        select_source(page[3], **values)


class ToneEngine:
    """Synthetic PCM checks routing/integrity, never speech intelligibility."""
    id = 'test-tone'
    def __init__(self):
        self.spoken = []
    def voices(self):
        return (Voice('test-tone:one', 'Synthetic tone fixture', ('en',), self.id),)
    def cache_identity(self, voice):
        return {'fixture': 'reader-tone-1', 'voice': voice}
    def synthesize(self, text, output, **kwargs):
        self.spoken.append(text)
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
            stream.writeframes(b''.join(struct.pack('<h', int(2000 * math.sin(i * math.pi / 10))) for i in range(800)))
        return output


def test_actual_renderer_reads_literal_source_and_reuses_verified_recording(page):
    app, _, desk, source, raw = page
    binary = available_tool('ffmpeg')
    assert binary is not None, 'The reviewed local FFmpeg runtime is required.'
    engine = ToneEngine()
    narrator = ReaderNarrator(select_source(source), desk.validate,
                              registry=VoiceRegistry((engine,)), ffmpeg=binary)
    before = app.assets.resolve(source.location.asset_sha256).read_bytes()
    try:
        result = narrator.render(voice='test-tone:one')
        assert result['status'] == 'complete' and wav_info(result['output']).frames > 0
        assert ''.join(engine.spoken) == raw
        calls = len(engine.spoken)
        again = narrator.render(voice='test-tone:one')
        assert again['files'] == result['files'] and len(engine.spoken) == calls
        assert app.assets.resolve(source.location.asset_sha256).read_bytes() == before
        output = Path(result['output'])
    finally:
        narrator.close()
    assert not output.exists()


def test_unknown_or_unsupported_voice_never_starts_a_render(page):
    _, _, desk, source, _ = page
    narrator = ReaderNarrator(select_source(source), desk.validate, registry=VoiceRegistry((ToneEngine(),)))
    with pytest.raises(ValueError, match='currently installed'):
        narrator.render(voice='missing:voice')
    with pytest.raises(ValueError, match='does not support'):
        narrator.render(voice='test-tone:one', pitch=2)
    assert narrator.directory is None


def test_render_rechecks_voice_availability_after_an_earlier_inventory(page):
    _, _, desk, source, _ = page
    engine = ToneEngine()
    narrator = ReaderNarrator(select_source(source), desk.validate, registry=VoiceRegistry((engine,)))
    assert narrator.voices()
    engine.voices = lambda: ()
    with pytest.raises(ValueError, match='currently installed'):
        narrator.render(voice='test-tone:one')
    assert narrator.directory is None and not engine.spoken


def test_cancel_and_revoked_source_precede_render_or_cache_creation(page):
    app, workspace, desk, source, _ = page
    narrator = ReaderNarrator(select_source(source), desk.validate, registry=VoiceRegistry((ToneEngine(),)))
    with pytest.raises(InterruptedError):
        narrator.render(voice='test-tone:one', cancelled=lambda: True)
    assert narrator.directory is None
    app.archive_workspace(workspace)
    with pytest.raises(ValueError):
        narrator.render(voice='test-tone:one')
    assert narrator.directory is None


def test_saved_selection_revalidates_exact_original_characters(page):
    _, _, _, source, _ = page
    selected = select_source(source)
    with pytest.raises(ValueError, match='changed'):
        selected.validate(replace(source, raw_text=source.raw_text.upper()))
