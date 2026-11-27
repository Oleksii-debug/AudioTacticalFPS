"""Copied Studio selections stay bound to immutable revisions and managed originals."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
from pathlib import Path

import pytest

from carr_library.audio import AudioCancelled
from carr_library.durable_state import atomic_json
from carr_library.operations import ResourceBroker
from carr_library.studio.project import Scene
from carr_library.studio.project_history import RevisionRef

_spec = importlib.util.spec_from_file_location(
    '_carr_clip_copy_history_fixture', Path(__file__).with_name('test_project_history.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
editor = fixture.editor


@pytest.fixture
def selected(editor, tmp_path):
    path = fixture.tone(tmp_path / 'copied-original.wav')
    original = path.read_bytes()
    project = editor.create('Copied clip project')
    project = editor.add(project, project.scenes[0].id, path,
        title='Exact saved clip', duration=2, gain_db=0, pan=-1)
    return project, path, original


def copied(editor, project):
    scene, = project.scenes
    cue, = scene.cues
    return editor.copy(project, scene.id, cue.id)


def test_actual_copy_and_paste_preserve_pcm_original_and_saved_selection(editor, selected, tmp_path, monkeypatch):
    project, source, original = selected
    scene, = project.scenes
    cue, = scene.cues
    with monkeypatch.context() as passive:
        passive.setattr(editor.service, '_runtime',
            lambda: pytest.fail('Copy opened a decoder or a voice engine.'))
        reference = copied(editor, project)
    assert editor.service.projects.load(project.id) == project
    assert all(row.revision < project.revision for row in editor.history(project))
    outputs = tmp_path / 'copy-pcm'
    outputs.mkdir()
    def render(value):
        row = editor.service.run(editor.service.submit(value, outputs))
        assert row['status'] == 'complete'
        return fixture.pcm(Path(row['destination']) / 'master.wav')
    baseline, channels, rate = render(project)
    # Change the current selection after Copy. Paste must read the reviewed old
    # revision, not silently duplicate the newer clip settings or caller fields.
    current = editor.edit(project, scene.id, cue.id, gain_db=-12, duration=1)
    pasted = editor.paste(current, scene.id, reference, 2)
    first, second = pasted.scenes[0].cues
    assert first == current.scenes[0].cues[0]
    assert replace(second, id=cue.id, start=cue.start) == cue
    assert second.id != first.id and second.path == first.path
    assert pasted.revision == current.revision + 1
    actual, actual_channels, actual_rate = render(pasted)
    assert (actual_channels, actual_rate) == (channels, rate) == (2, 24000)
    assert len(actual) == 2 * len(baseline)
    assert max(abs(a - b) for a, b in zip(actual[len(baseline):], baseline)) <= 1
    assert source.read_bytes() == original
    assert (editor.service.root / cue.path).read_bytes() == original
    assert ResourceBroker.for_app(source.parent / 'library').snapshot()['leases'] == []


def test_cut_for_paste_retains_exact_old_revision_and_can_restore_or_reuse_it(editor, selected, monkeypatch):
    project, source, original = selected
    scene, = project.scenes
    cue, = scene.cues
    with monkeypatch.context() as passive:
        passive.setattr(editor.service, '_runtime',
            lambda: pytest.fail('Cut of a saved selection opened media.'))
        result = editor.cut_for_paste(project, scene.id, cue.id)
    current, reference = result['project'], result['reference']
    assert not current.scenes[0].cues and current.revision == project.revision + 1
    assert reference.source == RevisionRef(project.revision, project.fingerprint)
    pasted = editor.paste(current, scene.id, reference, 1)
    replacement, = pasted.scenes[0].cues
    assert replace(replacement, id=cue.id, start=cue.start) == cue
    restored = editor.restore(pasted, RevisionRef(project.revision, project.fingerprint))
    assert restored.scenes == project.scenes and source.read_bytes() == original


def test_current_revision_copy_can_be_pasted_without_an_intervening_edit(editor, selected):
    project, _, _ = selected
    reference = copied(editor, project)
    pasted = editor.paste(project, project.scenes[0].id, reference, 2)
    assert len(pasted.scenes[0].cues) == 2
    assert pasted.scenes[0].cues[1].start == 2


def test_saved_copy_can_move_between_scenes_without_changing_other_content(editor, selected):
    project, _, _ = selected
    reference = copied(editor, project)
    current = editor.service.projects.save(replace(project, revision=project.revision + 1,
        scenes=(*project.scenes, Scene('second-scene', 'Destination', (), minimum_duration=1))),
        expected_revision=project.revision, expected_fingerprint=project.fingerprint)
    pasted = editor.paste(current, 'second-scene', reference, 3)
    assert pasted.scenes[0] == current.scenes[0]
    assert len(pasted.scenes[1].cues) == 1 and pasted.scenes[1].cues[0].start == 3
    assert pasted.original_script == current.original_script


@pytest.mark.parametrize('change', ['foreign-project', 'changed-fingerprint', 'unknown-clip'])
def test_forged_clip_reference_is_refused_before_decoder_or_publication(editor, selected, monkeypatch, change):
    project, _, _ = selected
    reference = copied(editor, project)
    reference = {
        'foreign-project': replace(reference, project_id='foreign'),
        'changed-fingerprint': replace(reference, source=RevisionRef(project.revision, 'f' * 64)),
        'unknown-clip': replace(reference, clip_id='absent'),
    }[change]
    monkeypatch.setattr(editor.service, '_runtime',
        lambda: pytest.fail('Unbound copied reference opened media.'))
    with pytest.raises(ValueError):
        editor.paste(project, project.scenes[0].id, reference, 2)
    assert editor.service.projects.load(project.id) == project


def test_changed_saved_history_cannot_relabel_a_copied_selection(editor, selected, monkeypatch):
    project, _, _ = selected
    reference = copied(editor, project)
    current = editor.scene_duration(project, project.scenes[0].id, 4)
    path = fixture.history_path(editor, project)
    atomic_json(path, replace(project, title='Different stored content').to_dict(), replace=True)
    monkeypatch.setattr(editor.service, '_runtime',
        lambda: pytest.fail('Changed history opened media.'))
    with pytest.raises(ValueError, match='differs'):
        editor.paste(current, current.scenes[0].id, reference, 2)
    assert editor.service.projects.load(current.id) == current


def test_changed_retained_original_is_refused_before_paste_publication(editor, selected):
    project, _, _ = selected
    reference = copied(editor, project)
    cue, = project.scenes[0].cues
    target = editor.service.root / cue.path
    changed = bytearray(target.read_bytes())
    changed[-1] ^= 1
    target.write_bytes(changed)
    with pytest.raises(ValueError):
        editor.paste(project, project.scenes[0].id, reference, 2)
    assert editor.service.projects.load(project.id) == project
    assert target.read_bytes() == changed


def test_cancel_after_original_verification_still_prevents_paste_commit(editor, selected, monkeypatch):
    project, _, _ = selected
    reference = copied(editor, project)
    actual = editor._validate_selection
    stopping = False
    def validated(*args, **kwargs):
        nonlocal stopping
        actual(*args, **kwargs)
        stopping = True
    monkeypatch.setattr(editor, '_validate_selection', validated)
    with pytest.raises(AudioCancelled):
        editor.paste(project, project.scenes[0].id, reference, 2, cancelled=lambda: stopping)
    assert stopping and editor.service.projects.load(project.id) == project


def test_paste_compare_and_swap_keeps_a_competing_same_revision_edit(editor, selected, monkeypatch):
    project, _, _ = selected
    reference = copied(editor, project)
    actual = editor.service.projects.save
    foreign = replace(project, title='Competing current content')
    def competing(value, **kwargs):
        atomic_json(editor.service.projects.directory(project.id) / 'project.json',
                    foreign.to_dict(), replace=True)
        return actual(value, **kwargs)
    monkeypatch.setattr(editor.service.projects, 'save', competing)
    with pytest.raises(ValueError, match='content changed'):
        editor.paste(project, project.scenes[0].id, reference, 2)
    assert editor.service.projects.load(project.id) == foreign


@pytest.mark.parametrize('operation', ['copy', 'cut_for_paste', 'paste'])
def test_pre_cancelled_clipboard_operation_opens_no_saved_project(editor, selected, monkeypatch, operation):
    project, _, _ = selected
    reference = copied(editor, project)
    monkeypatch.setattr(editor.service.projects, 'load',
        lambda *args: pytest.fail('Cancelled clipboard operation read a project.'))
    with pytest.raises(AudioCancelled):
        if operation == 'paste':
            editor.paste(project, project.scenes[0].id, reference, 2, cancelled=lambda: True)
        else:
            getattr(editor, operation)(project, project.scenes[0].id,
                project.scenes[0].cues[0].id, cancelled=lambda: True)


@pytest.mark.parametrize('start', [-1, float('nan'), float('inf')])
def test_invalid_paste_position_never_opens_media_or_saves(editor, selected, monkeypatch, start):
    project, _, _ = selected
    reference = copied(editor, project)
    monkeypatch.setattr(editor.service, '_runtime',
        lambda: pytest.fail('Invalid pasted position opened media.'))
    with pytest.raises(ValueError):
        editor.paste(project, project.scenes[0].id, reference, start)
    assert editor.service.projects.load(project.id) == project
