"""Regression for the actual Windows native Studio output-path failure."""
from pathlib import Path

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
    alias = tmp_path / 'existing parent'
    alias.mkdir()
    requested = alias / '..' / 'finished audio'
    director = ProductionDirector(store.root, registry=registry)
    result = director.render(project.id, requested)
    assert result['state'] == 'complete'
    assert Path(result['output_dir']) == tmp_path / 'finished audio'
    monkeypatch.setattr(registry, 'synthesize',
        lambda *args, **kwargs: pytest.fail('A verified completed job must not synthesize again.'))
    assert director.render(project.id, requested, job_id=result['job_id']) == result
    with pytest.raises(ValueError, match='keeps its original output folder'):
        director.render(project.id, tmp_path / 'different output', job_id=result['job_id'])
