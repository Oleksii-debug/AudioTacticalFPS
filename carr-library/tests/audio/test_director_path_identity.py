"""Regression for the actual Windows native Studio output-path failure."""
from pathlib import Path
import ctypes
import json
import os
import stat

import pytest

from carr_library.audio.director import ProductionDirector
from carr_library.audio.projects import ProjectStore, Settings, parse_script
from carr_library.audio.voices import VoiceRegistry


def test_actual_render_and_resume_use_the_same_canonical_output_parent(tmp_path, monkeypatch):
    registry = VoiceRegistry()
    assert 'flite:kal' in {voice.id for voice in registry.list_voices()}
    project = parse_script('Narrator: The chosen destination is preserved.', ['flite:kal'],
        settings=Settings(sample_rate=8000, channels=2, stems=False))
    store = ProjectStore(tmp_path / 'library')
    store.create(project)
    parent = tmp_path / 'existing parent with spaces'
    parent.mkdir()
    if os.name == 'nt':
        # Use the actual filesystem's short spelling when it provides one,
        # exactly as Windows temporary directories do in the native CI job.
        short_path = ctypes.windll.kernel32.GetShortPathNameW
        short_path.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
        short_path.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = short_path(str(parent), buffer, len(buffer))
        assert 0 < length < len(buffer)
        requested = Path(buffer.value) / 'finished audio'
    else:
        requested = parent / 'finished audio'
    real_fsync = os.fsync
    def writable_flush(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.write(descriptor, b'')  # Requires a writable handle, changes no bytes.
        return real_fsync(descriptor)
    monkeypatch.setattr(os, 'fsync', writable_flush)
    director = ProductionDirector(store.root, registry=registry)
    result = director.render(project.id, requested)
    assert result['state'] == 'complete'
    assert Path(result['output_dir']) == parent.resolve() / 'finished audio'
    monkeypatch.setattr(registry, 'synthesize',
        lambda *args, **kwargs: pytest.fail('A verified completed job must not synthesize again.'))
    assert director.render(project.id, requested, job_id=result['job_id']) == result
    with pytest.raises(ValueError, match='keeps its original output folder'):
        director.render(project.id, tmp_path / 'different output', job_id=result['job_id'])


@pytest.mark.parametrize('invalid', [[], None, 1, {'key': 'incomplete'}])
def test_invalid_checkpoint_document_is_quarantined_and_regenerated(tmp_path, invalid):
    registry = VoiceRegistry()
    assert 'flite:kal' in {voice.id for voice in registry.list_voices()}
    project = parse_script('Narrator: This unchanged line remains recoverable.', ['flite:kal'],
        settings=Settings(sample_rate=8000, channels=2, stems=False))
    store = ProjectStore(tmp_path / 'library')
    store.create(project)
    director = ProductionDirector(store.root, registry=registry)
    original = director.render(project.id, tmp_path / 'first')
    master_bytes = Path(original['master']).read_bytes()
    metadata = next(path for path in (store.path(project.id) / 'cache').glob('*.json')
                    if json.loads(path.read_text())['kind'] == 'speech')
    metadata.write_text(json.dumps(invalid))
    recovered = director.render(project.id, tmp_path / 'second')
    assert recovered['state'] == 'complete'
    assert Path(original['master']).read_bytes() == master_bytes
    assert json.loads(metadata.read_text())['kind'] == 'speech'
    assert list(metadata.parent.glob(metadata.name + '.invalid-*'))
