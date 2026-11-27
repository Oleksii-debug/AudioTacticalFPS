"""Actual Studio M4B, measured chapters and existing durable completion reuse."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os
import shutil

import pytest

from carr_library.audio import AudioRuntime
from carr_library.audio.audiobook import inspect_chapters, line_lrc
from carr_library.speech import FliteProvider, NativeSapiProvider, VoiceRegistry
from carr_library.studio.project import parse_script
from carr_library.studio.render import ProductionService


def test_actual_studio_m4b_embeds_measured_chapters_and_lrc(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg is required.'
    # Exercise a real installed speech adapter on both platforms. The pinned
    # Windows FFmpeg build does not provide Flite; its native COM adapter does.
    voices = VoiceRegistry([NativeSapiProvider()] if os.name == 'nt'
                           else [FliteProvider(shutil.which('ffmpeg'))])
    available = voices.voices()
    assert available, 'An actual installed SAPI/Flite voice is required.'
    voice_id = available[0].id if os.name == 'nt' else 'flite:awb'
    assert voice_id in {voice.id for voice in available}
    if os.name == 'nt':
        assert all(voice.engine.startswith('SAPI 5') for voice in available)
    output = tmp_path / 'output'; output.mkdir()
    service = ProductionService(tmp_path / 'state', voices=voices)
    original = '[Scene: First]\nReader: Preserve this first line.\n[Scene: Second]\nReader: Preserve this final line.'
    project = parse_script(original, title='Measured chapters', voice_ids=(voice_id,), mode='authored')
    try:
        job = service.submit(project, output, format='m4b', stems=False)
        result = service.run(job)
        target = Path(result['destination'])
        runtime = AudioRuntime(target, input_roots=(target,))
        actual = inspect_chapters(runtime, target / 'audiobook.m4b')
        chapters = result['result']['chapters']
        assert result['status'] == 'complete' and len(chapters) == 2
        assert [item['tags']['title'] for item in actual['chapters']] == ['First', 'Second']
        for embedded, measured in zip(actual['chapters'], chapters):
            assert abs(float(embedded['start_time']) - measured['start']) <= .001
            assert abs(float(embedded['end_time']) - measured['end']) <= .001
        assert (target / 'script.lrc').read_text() == line_lrc(result['result']['lines'])
        assert project.original_script == original
        receipts = {item['path']: item['sha256'] for item in result['artifacts']}
        assert str(target / 'script.lrc') in receipts
        assert all(sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in receipts.items())
        service.runtime_factory = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Completed job relaunched'))
        assert service.run(job)['artifacts'] == result['artifacts']
        assert result['result']['lrc_sha256'] == sha256((target / 'script.lrc').read_bytes()).hexdigest()
        missing = deepcopy(result)
        missing['artifacts'] = [item for item in missing['artifacts']
                                if item['path'] != str(target / 'script.lrc')]
        with pytest.raises(ValueError, match='missing required artifacts'):
            service._verify_artifacts(missing)
        changed = deepcopy(result)
        changed['result']['lrc_sha256'] = '0' * 64
        with pytest.raises(ValueError, match='LRC receipt changed'):
            service._verify_artifacts(changed)
        assert (target / 'script.lrc').read_text() == line_lrc(result['result']['lines'])
    finally:
        service.close(); voices.close()
