"""Canonical project recovery uses the current typed Studio and managed assets."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import legacy_project as bridge
from carr_library.audio.backup import _studio
from carr_library.audio.managed import ManagedProjects
from carr_library.audio.models import Line, Pause
from carr_library.audio.production import parse_script
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.narration_text import NarrationDraft
from carr_library.ui.audio_studio import StudioController


def original():
    return {
        'id': 'original-studio-id', 'title': 'Original production',
        'roles': [{'id': 'narrator-id', 'name': 'Narrator', 'voice_id': 'sapi5:default',
                   'language': 'uk', 'pan': -.25, 'gain_db': -3, 'rate': 2, 'volume': 75}],
        'scenes': [{'id': 'chapter-id', 'title': 'Exact scene',
                    'lines': [{'id': 'original-line-id', 'role': 'narrator-id',
                               'text': '[music off] remains spoken text.',
                               'pause_after': .3, 'source_ref': 'inert original source label'}],
                    'music': {'mood': 'calm', 'tempo': 90, 'gain_db': -24,
                              'engine': 'procedural', 'seed': 7},
                    'cues': [], 'minimum_duration': 2}],
        'original_script': 'Exact source\r\n[This is not an imported instruction.]\r\n',
        'mode': 'faithful', 'sample_rate': 24000, 'revision': 3, 'schema': 1,
    }


def selected(tmp_path, document=None):
    path = tmp_path / 'canonical.json'
    raw = (json.dumps(original() if document is None else document, ensure_ascii=False, indent=3)
           + '\r\n').encode('utf-8')
    path.write_bytes(raw)
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Recovery library')
    return app, path, raw


def review(app, path, **kwargs):
    return bridge.review_project(path, private_root=app.root,
        default_voice='sapi5:default', default_language='uk', **kwargs)


def pcm(path):
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\x01\x00' * 2400)


def with_cue(path):
    value = original()
    value['scenes'][0]['cues'] = [{
        'id': 'original-cue', 'path': str(path), 'start': .5,
        'gain_db': -4, 'pan': .75, 'role': 'ambience'}]
    return value


def test_review_captures_controls_without_creating_a_project_or_running_an_engine(tmp_path):
    app, path, raw = selected(tmp_path)
    proposal = review(app, path)
    assert not (app.root / 'audio-projects').exists()
    assert path.read_bytes() == raw
    plan = parse_script(proposal.script, title='Original production',
                        voice='sapi5:default', language='uk')
    role, = plan.roles
    assert (role.name, role.voice, role.language, role.pan, role.rate, role.volume) == (
        'Narrator', 'sapi5:default', 'uk', -.25, 2, .75)
    assert role.effects[0].parameters == (('gain_db', -3.0),)
    scene, = plan.scenes
    assert scene.title == 'Exact scene' and scene.minimum_duration == 2
    assert isinstance(scene.events[0], Line) and scene.events[0].text == original()['scenes'][0]['lines'][0]['text']
    assert isinstance(scene.events[1], Pause) and scene.events[1].seconds == .3
    assert scene.music.gain == 10 ** (-24 / 20)
    assert plan.sample_rate == 24000 and plan.export_stems


def test_import_creates_an_independent_editable_project_with_exact_original_bytes(tmp_path):
    app, path, raw = selected(tmp_path)
    proposal = review(app, path)
    document = bridge.import_reviewed(proposal)
    controller = StudioController(private_root=app.root).open(document)
    assert document == ManagedProjects(app.root).paths(controller.project_id)[1]
    assert controller.input_kind == 'script' and controller.script == proposal.script
    assert controller.draft.source_text == original()['original_script']
    assert controller.source_reference['document_utf8'].encode('utf-8') == raw
    assert controller.source_reference['bytes_sha256'] == sha256(raw).hexdigest()
    assert controller.draft.source_id == 'canonical-studio:' + sha256(raw).hexdigest()
    bridge.validate_reference(controller.source_reference, controller.draft)
    controller.edit_script(controller.script + '\nNarrator: Explicit new edit.\n')
    controller.save()
    reopened = StudioController(private_root=app.root).open(document)
    assert reopened.draft.source_text == original()['original_script']
    assert reopened.source_reference['document_utf8'].encode('utf-8') == raw
    assert path.read_bytes() == raw
    second = bridge.import_reviewed(proposal)
    assert second != document and document.read_bytes() != second.read_bytes()
    assert len(ManagedProjects(app.root).projects()) == 2


def test_cue_requires_selected_scope_and_is_copied_with_all_controls(tmp_path):
    input_root = tmp_path / 'approved'
    input_root.mkdir()
    sound = input_root / 'chosen.wav'
    pcm(sound)
    before = sound.read_bytes()
    app, path, raw = selected(tmp_path, with_cue(sound))
    with pytest.raises(ValueError, match='input folder'):
        review(app, path)
    proposal = review(app, path, input_roots=(input_root,))
    document = bridge.import_reviewed(proposal)
    controller = StudioController(private_root=app.root).open(document)
    clip, = controller.plan().scenes[0].clips
    asset = controller.output_dir / 'assets' / clip.asset
    assert (clip.start, clip.gain_db, clip.pan, clip.track) == (.5, -4, .75, 'ambience')
    assert clip.title == 'original-cue'
    assert clip.asset == sha256(before).hexdigest() + '.wav'
    assert asset.read_bytes() == before and asset.stat().st_nlink == 1
    assert (asset.stat().st_dev, asset.stat().st_ino) != (sound.stat().st_dev, sound.stat().st_ino)
    assert sound.read_bytes() == before and path.read_bytes() == raw


def test_foreign_saved_cue_path_needs_explicit_current_file_selection(tmp_path):
    chosen = tmp_path / 'chosen.wav'
    pcm(chosen)
    value = with_cue(Path('missing'))
    value['scenes'][0]['cues'][0]['path'] = '/old-disconnected-location/original.wav'
    app, path, raw = selected(tmp_path, value)
    proposal = review(app, path, input_roots=(tmp_path,),
                      asset_sources={'chapter-id:original-cue': chosen})
    document = bridge.import_reviewed(proposal)
    controller = StudioController(private_root=app.root).open(document)
    assert controller.plan().scenes[0].clips[0].asset == sha256(chosen.read_bytes()).hexdigest() + '.wav'
    assert controller.source_reference['document_utf8'].encode('utf-8') == raw


@pytest.mark.parametrize('change', [
    'unknown-project-field', 'schema-bool', 'unknown-role-field', 'unknown-line-role',
    'trailing-spoken-space', 'multiline-spoken-text', 'tiny-pause',
    'scene-title-whitespace', 'unrepresentable-role-name', 'neural-engine',
    'unsupported-mood', 'unsupported-tempo', 'excessive-music-gain',
    'duplicate-role-name',
])
def test_conversion_refuses_unknown_or_lossy_features_before_publication(tmp_path, change):
    value = original()
    if change == 'unknown-project-field':
        value['command'] = 'never run'
    elif change == 'schema-bool':
        value['schema'] = True
    elif change == 'unknown-role-field':
        value['roles'][0]['executable'] = 'never run'
    elif change == 'unknown-line-role':
        value['scenes'][0]['lines'][0]['role'] = 'missing'
    elif change == 'trailing-spoken-space':
        value['scenes'][0]['lines'][0]['text'] += ' '
    elif change == 'multiline-spoken-text':
        value['scenes'][0]['lines'][0]['text'] += '\n[output format=mp3]'
    elif change == 'tiny-pause':
        value['scenes'][0]['lines'][0]['pause_after'] = .0001
    elif change == 'scene-title-whitespace':
        value['scenes'][0]['title'] += ' '
    elif change == 'unrepresentable-role-name':
        value['roles'][0]['name'] = '# Heading'
    elif change == 'neural-engine':
        value['scenes'][0]['music']['engine'] = 'musicgen'
    elif change == 'unsupported-mood':
        value['scenes'][0]['music']['mood'] = 'hopeful'
    elif change == 'unsupported-tempo':
        value['scenes'][0]['music']['tempo'] = 250
    elif change == 'excessive-music-gain':
        value['scenes'][0]['music']['gain_db'] = 0
    else:
        value['roles'].append({**value['roles'][0], 'id': 'another-role'})
    app, path, raw = selected(tmp_path, value)
    with pytest.raises(ValueError):
        review(app, path)
    assert path.read_bytes() == raw
    assert not (app.root / 'audio-projects').exists()


@pytest.mark.parametrize('change', ['source', 'asset', 'library', 'input-root'])
def test_reviewed_source_and_authorities_must_remain_current(tmp_path, change):
    inputs = tmp_path / 'approved'
    inputs.mkdir()
    sound = inputs / 'selected.wav'
    pcm(sound)
    app, path, raw = selected(tmp_path, with_cue(sound))
    proposal = review(app, path, input_roots=(inputs,))
    if change == 'source':
        path.write_bytes(raw + b' ')
    elif change == 'asset':
        with sound.open('r+b') as stream:
            stream.seek(-2, os.SEEK_END)
            stream.write(b'\x02\x00')
    elif change == 'library':
        saved = app.root.with_name('old-library')
        app.root.rename(saved)
        app.root.mkdir()
    else:
        inputs.rename(inputs.with_name('old-inputs'))
        inputs.mkdir()
        pcm(inputs / sound.name)
    with pytest.raises(ValueError, match='changed'):
        bridge.import_reviewed(proposal)
    assert not (app.root / 'audio-projects').exists()


@pytest.mark.parametrize('failure', ['cancel-after-copy', 'publication', 'after-publication'])
def test_failed_import_removes_only_its_own_new_project(tmp_path, monkeypatch, failure):
    from carr_library.audio import clips
    sound = tmp_path / 'selected.wav'
    pcm(sound)
    app, path, raw = selected(tmp_path, with_cue(sound))
    proposal = review(app, path, input_roots=(tmp_path,))
    cancelled = [False]
    copy = clips.retain_pcm_asset
    def retain(*args, **kwargs):
        result = copy(*args, **kwargs)
        if failure == 'cancel-after-copy':
            cancelled[0] = True
        return result
    monkeypatch.setattr(clips, 'retain_pcm_asset', retain)
    if failure in ('publication', 'after-publication'):
        actual_publish = bridge.publish_noreplace
        def fail(*args, **kwargs):
            if failure == 'after-publication':
                actual_publish(*args, **kwargs)
            raise OSError('injected managed directory publication failure')
        monkeypatch.setattr(bridge, 'publish_noreplace', fail)
    with pytest.raises((InterruptedError, OSError)):
        bridge.import_reviewed(proposal, cancelled=lambda: cancelled[0])
    assert tuple((app.root / 'audio-projects').iterdir()) == ()
    assert path.read_bytes() == raw and sound.exists()


def test_uuid_collision_keeps_existing_managed_project_untouched(tmp_path, monkeypatch):
    app, path, _ = selected(tmp_path)
    proposal = review(app, path)
    first = bridge.import_reviewed(proposal)
    before = {p.relative_to(first.parents[1]).as_posix(): p.read_bytes()
              for p in first.parents[1].rglob('*') if p.is_file()}
    identity = first.parents[1].name
    monkeypatch.setattr(bridge, 'uuid4', lambda: SimpleNamespace(hex=identity))
    with pytest.raises(FileExistsError):
        bridge.import_reviewed(proposal)
    assert {p.relative_to(first.parents[1]).as_posix(): p.read_bytes()
            for p in first.parents[1].rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('damage', ['bytes', 'digest', 'source-id', 'source-text', 'extra-field'])
def test_backup_cannot_accept_forged_canonical_source_provenance(tmp_path, damage):
    app, path, _ = selected(tmp_path)
    document = bridge.import_reviewed(review(app, path))
    payload = json.loads(document.read_bytes())
    if damage == 'bytes':
        payload['source_reference']['document_utf8'] += ' '
    elif damage == 'digest':
        payload['source_reference']['bytes_sha256'] = '0' * 64
    elif damage == 'extra-field':
        payload['source_reference']['executable'] = '/must/not/run'
    else:
        source = payload['draft']['source_text']
        replacement = NarrationDraft(source + (' changed' if damage == 'source-text' else ''),
            source_id='different' if damage == 'source-id' else payload['draft']['source_id'])
        payload['draft'] = replacement.to_dict()
    with pytest.raises(ValueError):
        _studio(payload)


def test_complete_backup_preserves_imported_project_inputs_and_exact_canonical_document(tmp_path):
    sound = tmp_path / 'selected.wav'
    pcm(sound)
    app, path, raw = selected(tmp_path, with_cue(sound))
    document = bridge.import_reviewed(review(app, path, input_roots=(tmp_path,)))
    old = StudioController(private_root=app.root).open(document)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored_root = restore_backup(backup, tmp_path / 'restored', digest)
    restored_path = ManagedProjects(restored_root).paths(old.project_id)[1]
    restored = StudioController(private_root=restored_root).open(restored_path)
    assert restored.output_dir == restored_path.parents[1] / 'audio'
    assert restored.script == old.script and restored.draft.source_text == old.draft.source_text
    assert restored.source_reference['document_utf8'].encode('utf-8') == raw
    clip, = restored.plan().scenes[0].clips
    assert (restored.output_dir / 'assets' / clip.asset).read_bytes() == sound.read_bytes()
    assert path.read_bytes() == raw


def test_empty_canonical_scene_keeps_the_old_one_second_minimum(tmp_path):
    value = original()
    value['scenes'][0].update(lines=[], minimum_duration=0)
    app, path, _ = selected(tmp_path, value)
    proposal = review(app, path)
    scene, = parse_script(proposal.script).scenes
    assert scene.minimum_duration == 1 and scene.events == () and scene.music is not None


def test_schema_two_clip_selection_microphone_fades_and_title_survive(tmp_path):
    sound = tmp_path / 'selected.wav'
    pcm(sound)
    value = with_cue(sound)
    value['schema'] = 2
    value['scenes'][0]['cues'][0].update(role='microphone', trim_start=.02,
        duration=.05, fade_in=.01, fade_out=.02, title='Selected microphone phrase')
    app, path, raw = selected(tmp_path, value)
    proposal = review(app, path, input_roots=(tmp_path,))
    controller = StudioController(private_root=app.root).open(bridge.import_reviewed(proposal))
    plan = controller.plan()
    clip, = plan.scenes[0].clips
    assert plan.schema_version == 2
    assert (clip.track, clip.trim_start, clip.duration, clip.fade_in, clip.fade_out, clip.title) == (
        'microphone', .02, .05, .01, .02, 'Selected microphone phrase')
    assert controller.source_reference['document_utf8'].encode('utf-8') == raw


@pytest.mark.parametrize('damage', ['overlap', 'past-source', 'unknown-field'])
def test_schema_two_unrepresentable_or_unavailable_clip_window_is_rejected(tmp_path, damage):
    sound = tmp_path / 'selected.wav'
    pcm(sound)
    value = with_cue(sound)
    value['schema'] = 2
    cue = value['scenes'][0]['cues'][0]
    cue.update(trim_start=.02, duration=.05, fade_in=.01, fade_out=.02, title='')
    if damage == 'overlap':
        cue.update(fade_in=.04, fade_out=.04)
    elif damage == 'past-source':
        cue['trim_start'] = 3
    else:
        cue['executable'] = 'must remain impossible'
    app, path, raw = selected(tmp_path, value)
    with pytest.raises(ValueError):
        review(app, path, input_roots=(tmp_path,))
    assert path.read_bytes() == raw
    assert not (app.root / 'audio-projects').exists()


def test_current_studio_reopen_checks_provenance_and_save_as_protects_original(tmp_path):
    app, path, raw = selected(tmp_path)
    document = bridge.import_reviewed(review(app, path))
    controller = StudioController(private_root=app.root).open(document)
    with pytest.raises(ValueError, match='immutable'):
        controller.save(path, overwrite=True)
    assert path.read_bytes() == raw and controller.project_path == document
    payload = json.loads(document.read_bytes())
    payload['source_reference']['document_utf8'] += ' '
    document.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='provenance'):
        StudioController(private_root=app.root).open(document)


