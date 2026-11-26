"""Real render/checkpoint -> library backup -> independent restore -> resume."""
import json
from pathlib import Path
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.library_backup import create_backup, restore_backup


FFMPEG = resolve_ffmpeg()
pytestmark = pytest.mark.skipif(not FFMPEG, reason='A real local FFmpeg executable is required.')




@pytest.mark.parametrize('completed', [False, True])
def test_canonical_managed_studio_restores_into_new_output_root_and_resumes(tmp_path, completed):
    from hashlib import sha256
    from carr_library.audio.jobs import RenderCancelled as ProductionCancelled
    from carr_library.ui.audio_studio import StudioController
    class FixtureEngine:
        """Synthetic speech; actual FFmpeg validates production and portability."""
        def __init__(self):
            self.calls = 0
        def cache_identity(self, voice):
            return {'fixture': 'unified-backup-wave-v1', 'voice': voice}
        def synthesize(self, text, output, **kwargs):
            self.calls += 1
            with wave.open(str(output), 'wb') as audio:
                audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\0\x10' * 800)
            return output
    app = LibraryApp(tmp_path / 'library')
    engine = FixtureEngine()
    studio = StudioController(private_root=app.root, registry=engine, ffmpeg=FFMPEG)
    script = '[output sample_rate=8000]\n# One\nNarrator: First.\n# Two\nNarrator: Second.'
    studio.new(script, title='Managed production', input_kind='script')
    studio.configure(voice='fixture:local')
    source = studio.save_in_library()
    source_bytes = source.read_bytes()
    stopped = False
    def status(message):
        nonlocal stopped
        if not completed and 'saved an audio step' in message:
            stopped = True
    if completed:
        before = studio.render()
        before_digest = sha256(Path(before['output']).read_bytes()).hexdigest()
    else:
        with pytest.raises(ProductionCancelled):
            studio.render(cancelled=lambda: stopped, on_status=status)
    before_calls = engine.calls
    assert before_calls == (2 if completed else 1)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    new_source = restored / source.relative_to(app.root)
    reopened = StudioController(private_root=restored, registry=engine, ffmpeg=FFMPEG)
    reopened.open(new_source)
    assert reopened.output_dir.is_relative_to(restored)
    result = reopened.render()
    assert engine.calls == 2
    assert Path(result['output']).is_relative_to(restored)
    assert result['frames'] == 1600
    if completed:
        assert sha256(Path(result['output']).read_bytes()).hexdigest() == before_digest
    assert new_source.read_bytes() == source_bytes == source.read_bytes()
