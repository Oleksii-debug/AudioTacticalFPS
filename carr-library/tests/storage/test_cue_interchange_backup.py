"""Actual common4 capture/restore of the new inert CUE producer metadata.

The small timings are authored declaration fixtures, not a render qualification.
The existing Studio production suite separately executes the real render path.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_producers
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.speech import Voice
from carr_library.studio.interchange import chapter_cues
from carr_library.studio.render import ProductionService


class InventoryOnly:
    def voices(self):
        return (Voice('fixture:cue', 'Enumeration only', 'fixture', 'en-US'),)


@pytest.fixture
def authored(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('CUE backup')
    app.add_text(workspace, 'Original', 'Exact retained source for passive recovery.')
    output = tmp_path / 'external-output'
    output.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=InventoryOnly())
    script = '[Scene: Архів]\nNarrator: First exact line.\n[Scene: Žilina]\nNarrator: Second exact line.\n'
    project = service.create(script, title='Measured declaration fixture', voice_ids=('fixture:cue',))
    job = service.submit(project, output)
    chapters, lines = [], []
    for index, scene in enumerate(project.scenes):
        start, end = index * .25, (index + 1) * .25
        chapters.append(dict(id=scene.id, title=scene.title, start=start, end=end))
        line, = scene.lines
        lines.append(dict(scene=scene.id, line=line.id, role=line.role, text=line.text,
                          source_ref=line.source_ref, start=start, end=end, timing='authored fixture'))
    timeline = dict(schema=1, project=project.id, revision=project.revision,
        source_sha256=sha256(project.original_script.encode()).hexdigest(),
        chapters=chapters, lines=lines, duration=.5, sample_rate=project.sample_rate,
        channels=2, mode=project.mode, local_only=True, loudness={},
        music_kind='authored fixture', timing_notice='Authored times, not actual native measurements.')
    cue_files, timeline['editor_interchange'] = chapter_cues(project, timeline)
    job_root = service.jobs_root / job
    atomic_json(job_root / 'project.json', project.to_dict())
    atomic_json(job_root / 'timeline.json', timeline)
    # A pending declaration can retain a prior measured result, but this fixture
    # makes no completed-render claim and no engine is started.
    state = service.get(job)
    state['result'] = deepcopy(timeline)
    atomic_json(job_root / 'job.json', state, replace=True)
    for name, raw in cue_files:
        (job_root / name).write_bytes(raw)
        (output / name).write_bytes(raw)
    yield app, workspace, service, project, job_root, timeline, cue_files, output
    assert service.close()
    assert app.close_search()


def resign(backup, manifest):
    raw = encode(manifest)
    (backup / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def rewrite(backup, manifest, name, value):
    raw = encode(value)
    (backup / name).write_bytes(raw)
    entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize('include_generated', [False, True])
def test_common4_preserves_exact_declaration_excludes_only_private_rebuildable_cues(
        authored, tmp_path, monkeypatch, include_generated):
    app, workspace, service, project, job_root, timeline, cue_files, output = authored
    originals = {name: (job_root / name).read_bytes() for name, _ in cue_files}
    external = {name: (output / name).read_bytes() for name, _ in cue_files}
    def forbidden(*args, **kwargs):
        pytest.fail('Passive CUE backup/restore must not execute a renderer or process.')
    monkeypatch.setattr(ProductionService, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup, include_generated=include_generated)
    assert verify_backup(backup, checksum)
    manifest = read_json(backup / 'manifest.json')
    prefix = job_root.relative_to(app.root).as_posix() + '/'
    names = {row['filename'] for row in manifest['sidecars']}
    assert prefix + 'timeline.json' in names and prefix + 'project.json' in names
    assert not any(name.endswith('.cue') for name in names)
    assert read_json(backup / prefix / 'timeline.json') == timeline
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    assert read_json(destination / prefix / 'timeline.json') == timeline
    job = read_json(destination / prefix / 'job.json')
    assert job['status'] == 'waiting-input' and job['destination'] == ''
    assert job['recovery']['requires_output_directory'] is True and 'result' not in job
    assert not any((destination / prefix / name).exists() for name, _ in cue_files)
    restored_project = read_json(destination / prefix / 'project.json')
    from carr_library.studio.project import Project
    again, report = chapter_cues(Project.from_dict(restored_project), timeline)
    assert again == cue_files and report == timeline['editor_interchange']
    assert {name: (job_root / name).read_bytes() for name in originals} == originals
    assert {name: (output / name).read_bytes() for name in external} == external
    # A fresh second archive proves the retained declaration remains valid after
    # passive output-authority withdrawal, without reconstructing any CUE file.
    restored = LibraryApp(destination)
    try:
        second = tmp_path / 'second-backup'
        digest = create_backup(restored, second)
        assert verify_backup(second, digest)
    finally:
        assert restored.close_search()


@pytest.mark.parametrize('target', ['timeline', 'job'])
@pytest.mark.parametrize('damage', ['foreign-scene', 'shifted-index', 'title-provenance', 'qualification'])
def test_resealed_cue_metadata_never_grants_false_source_or_editor_authority(
        authored, tmp_path, target, damage):
    app, _, _, _, job_root, _, _, _ = authored
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    name = job_root.relative_to(app.root).as_posix() + '/' + target + '.json'
    record = read_json(backup / name)
    selected = record if target == 'timeline' else record['result']
    report = selected['editor_interchange']
    if damage == 'foreign-scene':
        report['files'][0]['tracks'][0]['scene'] = 'foreign-scene'
    elif damage == 'shifted-index':
        report['files'][0]['tracks'][0]['index_75hz'] += 1
    elif damage == 'title-provenance':
        report['project_title_preserved'] = not report['project_title_preserved']
    else:
        report['editor_import'] = 'PASS'
    rewrite(backup, manifest, name, record)
    target_root = tmp_path / 'refused'
    with pytest.raises(ValueError, match='[Ii]nterchange'):
        restore_backup(backup, target_root, resign(backup, manifest))
    assert not target_root.exists()


def test_new_timeline_metadata_requires_its_exact_project_checkpoint(authored, tmp_path):
    app, _, _, _, job_root, _, _, _ = authored
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    name = job_root.relative_to(app.root).as_posix() + '/project.json'
    (backup / name).unlink()
    manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != name]
    # Remove the corresponding original-project record in the inert retained
    # input receipt too, so the actual interchange graph is the remaining refusal.
    receipt_name = 'state/studio/backup-inputs.json'
    receipt = read_json(backup / receipt_name)
    receipt['projects'] = [row for row in receipt['projects'] if row['filename'] != name.removeprefix('state/')]
    rewrite(backup, manifest, receipt_name, receipt)
    target = tmp_path / 'refused'
    with pytest.raises(ValueError, match='exact retained project checkpoint'):
        restore_backup(backup, target, resign(backup, manifest))
    assert not target.exists()


@pytest.mark.parametrize('name', ['chapters-0000.cue', 'chapters-0103.cue',
    'chapters-1.cue', 'CHAPTERS-0001.cue', 'notes.cue'])
def test_unknown_private_cue_files_remain_visible_backup_refusals(authored, tmp_path, name):
    app, _, _, _, job_root, _, _, _ = authored
    (job_root / name).write_bytes(b'FILE "foreign.wav" WAVE\r\n')
    with pytest.raises(ValueError, match='validated backup adapter'):
        create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def test_generated_cue_alias_is_refused_before_capture_exclusion(authored, tmp_path):
    app, _, _, _, job_root, _, cue_files, _ = authored
    selected = job_root / cue_files[0][0]
    alias = tmp_path / 'outside-hardlink'
    os.link(selected, alias)
    try:
        with pytest.raises(ValueError):
            create_backup(app, tmp_path / 'refused')
        assert not (tmp_path / 'refused').exists()
    finally:
        alias.unlink()


def test_a_cue_named_directory_is_not_an_exclusion_rule(authored, tmp_path):
    app, _, _, _, job_root, _, cue_files, _ = authored
    selected = job_root / cue_files[0][0]
    selected.unlink()
    selected.mkdir()
    (selected / 'unregistered.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='validated backup adapter'):
        create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def test_legacy_timeline_bytes_and_optional_field_contract_stay_exact(authored, tmp_path):
    app, _, service, _, job_root, timeline, _, _ = authored
    legacy = deepcopy(timeline)
    legacy.pop('editor_interchange')
    atomic_json(job_root / 'timeline.json', legacy, replace=True)
    state = service.get(job_root.name)
    state['result'] = deepcopy(legacy)
    atomic_json(job_root / 'job.json', state, replace=True)
    backup = tmp_path / 'legacy'
    checksum = create_backup(app, backup)
    target = restore_backup(backup, tmp_path / 'legacy-restored', checksum)
    name = job_root.relative_to(app.root) / 'timeline.json'
    assert (target / name).read_bytes() == (job_root / 'timeline.json').read_bytes()


def test_actual_interchange_iteration_cancellation_leaves_no_completed_backup(authored, tmp_path, monkeypatch):
    app, _, _, _, _, _, _, _ = authored
    from carr_library.studio import interchange
    original = interchange.chapter_cues
    reached = False
    def cancel_after_real_validation(*args, **kwargs):
        nonlocal reached
        result = original(*args, **kwargs)
        reached = True
        return result
    monkeypatch.setattr(interchange, 'chapter_cues', cancel_after_real_validation)
    with pytest.raises(InterruptedError):
        create_backup(app, tmp_path / 'cancelled', cancelled=lambda: reached)
    assert reached and not (tmp_path / 'cancelled').exists()


@pytest.mark.parametrize('damage', ['schema-bool', 'timebase-bool', 'extra-field', 'bad-sheet-name'])
def test_exact_interchange_shape_rejects_disguised_or_extra_values(authored, tmp_path, damage):
    app, _, service, _, job_root, _, _, _ = authored
    state = service.get(job_root.name)
    report = state['result']['editor_interchange']
    if damage == 'schema-bool':
        report['schema'] = True
    elif damage == 'timebase-bool':
        report['timebase'] = True
    elif damage == 'extra-field':
        report['execute'] = 'never'
    else:
        report['files'][0]['file'] = '../foreign.cue'
    atomic_json(job_root / 'job.json', state, replace=True)
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()
