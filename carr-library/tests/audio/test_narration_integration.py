"""Reading derivatives are actually synthesized, retaining original mappings."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from carr_library.audio.director import ProductionDirector
from carr_library.audio.narration import (cue_derivative, retained_narration, set_cue_derivative,
                                           spoken_cue, validate_narration)
from carr_library.audio.pronunciation import NarrationDerivative, PronunciationRule
from carr_library.audio.projects import Project, ProjectStore, Settings, parse_script
from carr_library.audio.voices import VoiceRegistry
from carr_library.ui.narration_editor import NarrationEditorModel


def project():
    return parse_script('# Chapter\r\nNarrator: Alice meets Alice.\r\nGuest: Alice replies.\r\n',
        ('flite:slt', 'flite:rms'), title='Immutable pronunciation source',
        settings=Settings(sample_rate=8000, channels=1, stems=False))


def test_editor_preview_accept_undo_export_preserves_original_and_global_mapping():
    original = project()
    model = NarrationEditorModel(original)
    cue = model.cues[0]
    before = original.to_dict()
    rules = (PronunciationRule('Alice', 'Al-iss', project_id=original.id, language='en'),)
    proposals = model.preview(rules, all_cues=True)
    assert len(proposals) == 3 and model.result() is None
    assert original.to_dict() == before and original.narration == {}
    model.accept()
    revised = model.result()
    assert revised.revision == original.revision + 1
    assert revised.script == original.script and revised.scenes == original.scenes
    spoken = spoken_cue(revised, cue)
    assert spoken['text'] == 'Al-iss meets Al-iss.'
    first = spoken['mapping'][0]
    assert first['source_start'] == cue.source_start
    assert first['source_end'] == cue.source_start + len('Alice')
    assert original.script[first['source_start']:first['source_end']] == 'Alice'
    assert spoken['derivative_sha256'] != spoken_cue(original, cue)['derivative_sha256']
    restored = Project.from_dict(json.loads(json.dumps(revised.to_dict())))
    assert spoken_cue(restored, cue) == spoken
    model.undo()
    assert model.derivative().text == cue.text
    model.redo()
    assert model.derivative().text == 'Al-iss meets Al-iss.'
    model.reject(model.derivative().edits[0].id)
    assert model.derivative().text == 'Alice meets Al-iss.'
    assert model.result().revision == original.revision + 1
    assert original.to_dict() == before


def test_dictionary_scope_and_proposal_invalidation():
    original = project()
    model = NarrationEditorModel(original)
    first_voice = original.roles[0].voice_id
    rules = (PronunciationRule('Alice', 'A-lice', voice_id=first_voice),)
    assert len(model.preview(rules, all_cues=True)) == 2
    model.accept()
    model.preview((PronunciationRule('meets', 'greets'),))
    model.undo()
    with pytest.raises(ValueError, match='Preview'):
        model.accept()
    assert model.derivative().text == model.cues[0].text


def test_narration_cannot_attach_to_other_source_or_hidden_non_speech_cue():
    original = project()
    cue = original.scenes[0].cues[0]
    altered = NarrationDerivative.create('Different text', f'{original.id}:{cue.id}')
    with pytest.raises(ValueError, match='exact original cue'):
        set_cue_derivative(original, cue.id, altered)
    derivative = cue_derivative(original, cue.id).apply(0, 5, 'Al-iss')
    wrong_identity = replace(derivative, source_id='other-project:other-cue')
    with pytest.raises(ValueError, match='exact original cue'):
        set_cue_derivative(original, cue.id, wrong_identity)
    with pytest.raises(ValueError, match='unknown speech cue'):
        replace(original, narration={'unknown': derivative.to_dict()})


def test_original_script_edits_keep_only_matching_derivatives():
    original = project()
    cue = original.scenes[0].cues[0]
    derivative = cue_derivative(original, cue.id).apply(0, 5, 'Al-iss')
    revised = set_cue_derivative(original, cue.id, derivative)
    unchanged = parse_script(revised.script, ('flite:slt', 'flite:rms'), previous=revised)
    assert unchanged.narration == revised.narration
    changed = parse_script(revised.script.replace('Alice meets', 'Olena meets'),
                           ('flite:slt', 'flite:rms'), previous=revised)
    assert cue.id not in changed.narration
    assert retained_narration(revised, (cue,), 'new-project-identity') == {}


def test_project_export_import_rebinds_reading_copies_without_changing_original_or_history(tmp_path):
    store = ProjectStore(tmp_path / 'library')
    original = project()
    store.create(original)
    cue = original.scenes[0].cues[0]
    derivative = cue_derivative(original, cue.id).apply(0, 5, 'Al-iss')
    derivative = derivative.apply(12, 17, 'Al-iss').undo()
    edited = set_cue_derivative(original, cue.id, derivative)
    store.save(edited)
    exported = tmp_path / 'project.json'
    store.export(edited.id, exported)
    exported_bytes = exported.read_bytes()
    imported = store.import_project(exported)
    assert imported.id != edited.id and imported.revision == 1
    assert imported.script == edited.script and imported.scenes == edited.scenes
    assert imported.source == edited.source
    rebound = cue_derivative(imported, cue.id)
    assert rebound.source_id == f'{imported.id}:{cue.id}'
    assert replace(rebound, source_id=derivative.source_id) == derivative
    assert rebound.redo().text == derivative.redo().text == 'Al-iss meets Al-iss.'
    assert spoken_cue(imported, cue)['mapping'] == spoken_cue(edited, cue)['mapping']
    assert store.load(edited.id) == edited
    assert store.load(imported.id) == imported
    assert exported.read_bytes() == exported_bytes


def test_cancelled_literal_preview_does_not_change_source_or_draft():
    original = project()
    model = NarrationEditorModel(original)
    with pytest.raises(InterruptedError):
        model.preview((PronunciationRule('Alice', 'Al-iss'),), all_cues=True, cancelled=lambda: True)
    assert model.result() is None and original.narration == {}


def test_real_production_speaks_reading_copy_and_publishes_original_mapping(tmp_path, monkeypatch):
    registry = VoiceRegistry()
    assert 'flite:slt' in {voice.id for voice in registry.list_voices()}
    original = parse_script('Narrator: Alice speaks.\r\n', ('flite:slt',),
        settings=Settings(sample_rate=8000, channels=1, stems=False))
    store = ProjectStore(tmp_path / 'library')
    store.create(original)
    cue = original.scenes[0].cues[0]
    revised = set_cue_derivative(original, cue.id, cue_derivative(original, cue.id).apply(0, 5, 'Al-iss'))
    store.save(revised)
    utterances = []
    real_synthesize = registry.synthesize
    def actual(text, *args, **kwargs):
        utterances.append(text)
        return real_synthesize(text, *args, **kwargs)
    monkeypatch.setattr(registry, 'synthesize', actual)
    result = ProductionDirector(store.root, registry=registry).render(revised.id, tmp_path / 'output')
    assert result['state'] == 'complete' and utterances == ['Al-iss speaks.']
    report = json.loads((Path(result['output_dir']) / 'production.json').read_text())
    timing, = report['timings']
    assert timing['text'] == 'Al-iss speaks.' and timing['original_text'] == 'Alice speaks.'
    assert timing['source_mapping'][0]['source_start'] == cue.source_start
    assert timing['source_mapping'][0]['source_end'] == cue.source_start + 5
    assert store.load(revised.id).script == original.script
    assert original.to_dict()['narration'] == {}
