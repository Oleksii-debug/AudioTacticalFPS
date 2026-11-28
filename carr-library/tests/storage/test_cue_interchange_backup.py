"""Actual native and legacy flat backups retain passive measured CUE metadata.

The declared timings are synthetic fixtures. Actual PCM publication/replay is
covered separately by the Studio interchange tests.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.studio.interchange import chapter_cues
from carr_library.studio.project import Project
from carr_library.studio.render import ProductionService

FLAT = runpy.run_path(str(Path(__file__).with_name('test_canonical_backup_compat.py')))


class InventoryOnly:
    def voices(self):
        return (SimpleNamespace(id='fixture:cue'),)


@pytest.fixture
def authored(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('CUE backup')
    app.add_text(workspace, 'Original', 'Exact retained source for passive recovery.')
    KeybindingStore(app.root).save(
        [Command('fixture.cue', 'Synthetic CUE recovery command')],
        {'fixture.cue': 'Ctrl+F9'})
    output = tmp_path / 'external-output'
    output.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=InventoryOnly())
    script = '[Scene: Архів]\nNarrator: First exact line.\n[Scene: Žilina]\nNarrator: Second exact line.\n'
    project = service.create(script, title='Measured declaration fixture', voice_ids=('fixture:cue',))
    identity = service.submit(project, output)
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
    job_root = service.jobs_root / identity
    atomic_json(job_root / 'project.json', project.to_dict())
    atomic_json(job_root / 'timeline.json', timeline)
    saved = service.get(identity)
    saved['result'] = deepcopy(timeline)
    atomic_json(job_root / 'job.json', saved, replace=True)
    # CUE payloads are public artifacts. Private generation uses the existing
    # excluded pending namespace; this is an inert abandoned staging fixture.
    for name, raw in cue_files:
        (job_root / ('.pending-' + name)).write_bytes(raw)
        (output / name).write_bytes(raw)
    yield app, workspace, service, project, job_root, timeline, cue_files, output
    assert service.close() is None
    assert service._closed and service._voices is None


def snapshot(app, directory, flat, generated=False):
    if flat:
        return FLAT['_flat_backup'](app, directory, generated=generated)
    checksum = create_backup(app, directory, include_generated=generated)
    return read_json(directory / 'manifest.json'), checksum


def rewrite(directory, manifest, relative, value, flat):
    raw = encode(value)
    (directory / relative).write_bytes(raw)
    rows = manifest['sidecars'] if flat else manifest['state']
    name = relative.as_posix() if flat else relative.relative_to('state').as_posix()
    entry, = [row for row in rows if row['filename'] == name]
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


def resign(directory, manifest):
    raw = encode(manifest)
    (directory / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('generated', [False, True])
def test_actual_backup_retains_exact_cue_declaration_and_revokes_output_authority(
        authored, tmp_path, monkeypatch, flat, generated):
    app, workspace, service, project, job_root, timeline, cue_files, output = authored
    source_bytes = {path: path.read_bytes() for path in job_root.iterdir() if path.is_file()}
    external = {name: (output / name).read_bytes() for name, _ in cue_files}
    def forbidden(*args, **kwargs):
        pytest.fail('Passive interchange backup or restore started an audio runtime.')
    monkeypatch.setattr(ProductionService, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    directory = tmp_path / 'backup'
    manifest, checksum = snapshot(app, directory, flat, generated)
    assert manifest['version'] == (4 if flat else 5)
    assert verify_backup(directory, checksum)
    relative = job_root.relative_to(app.root)
    assert read_json(directory / relative / 'timeline.json') == timeline
    rows = manifest['sidecars'] if flat else manifest['state']
    assert not any(row['filename'].endswith('.cue') for row in rows)
    target = restore_backup(directory, tmp_path / 'restored', checksum)
    assert read_json(target / relative / 'timeline.json') == timeline
    original_job = read_json(job_root / 'job.json')
    restored_job = read_json(target / relative / 'job.json')
    assert restored_job == {**original_job, 'restore_requires_new_output': True}
    restored_project = Project.from_dict(read_json(target / relative / 'project.json'))
    again, report = chapter_cues(restored_project, timeline)
    assert again == cue_files and report == timeline['editor_interchange']
    assert not any((target / relative / name).exists() for name, _ in cue_files)
    restored = LibraryApp(target)
    assert restored.search(workspace, 'retained')
    second = tmp_path / 'second-backup'
    next_checksum = create_backup(restored, second, include_generated=generated)
    assert verify_backup(second, next_checksum)
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes
    assert {name: (output / name).read_bytes() for name in external} == external


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('damage', ['false-index', 'false-source', 'false-project',
                                   'false-qualification', 'false-schema'])
def test_rehashed_cue_metadata_cannot_replace_its_project_or_measured_timeline(
        authored, tmp_path, flat, damage):
    app, _, _, _, job_root, timeline, _, _ = authored
    directory = tmp_path / 'backup'
    manifest, _ = snapshot(app, directory, flat)
    relative = job_root.relative_to(app.root) / 'timeline.json'
    damaged = deepcopy(timeline)
    if damage == 'false-index':
        damaged['editor_interchange']['files'][0]['tracks'][0]['index_75hz'] += 1
    elif damage == 'false-source':
        damaged['source_sha256'] = '0' * 64
    elif damage == 'false-project':
        damaged['project'] = 'foreign-project'
    elif damage == 'false-schema':
        damaged['editor_interchange']['schema'] = True
    else:
        damaged['editor_interchange']['editor_import'] = 'PASS'
    rewrite(directory, manifest, relative, damaged, flat)
    checksum = resign(directory, manifest)
    raw = (directory / relative).read_bytes()
    target = tmp_path / 'must-not-publish'
    with pytest.raises((ValueError, RestoreError), match='interchange|timeline|chapter|project|schema'):
        restore_backup(directory, target, checksum)
    assert not target.exists()
    assert (directory / relative).read_bytes() == raw
    assert read_json(job_root / 'timeline.json') == timeline


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
def test_cue_timeline_requires_its_retained_project_checkpoint(authored, tmp_path, flat):
    app, _, _, _, job_root, _, _, _ = authored
    directory = tmp_path / 'backup'
    manifest, _ = snapshot(app, directory, flat)
    relative = job_root.relative_to(app.root) / 'project.json'
    (directory / relative).unlink()
    field = 'sidecars' if flat else 'state'
    name = relative.as_posix() if flat else relative.relative_to('state').as_posix()
    manifest[field] = [row for row in manifest[field] if row['filename'] != name]
    checksum = resign(directory, manifest)
    target = tmp_path / 'must-not-publish'
    with pytest.raises((ValueError, RestoreError), match='retained project checkpoint'):
        restore_backup(directory, target, checksum)
    assert not target.exists()
    assert (job_root / 'project.json').is_file()


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
def test_legacy_timeline_without_interchange_keeps_its_original_bytes(authored, tmp_path, flat):
    app, _, service, _, job_root, timeline, _, _ = authored
    legacy = deepcopy(timeline)
    legacy.pop('editor_interchange')
    atomic_json(job_root / 'timeline.json', legacy, replace=True)
    state = service.get(job_root.name)
    state['result'] = deepcopy(legacy)
    atomic_json(job_root / 'job.json', state, replace=True)
    raw = (job_root / 'timeline.json').read_bytes()
    directory = tmp_path / 'backup'
    _, checksum = snapshot(app, directory, flat)
    target = restore_backup(directory, tmp_path / 'restored', checksum)
    relative = job_root.relative_to(app.root) / 'timeline.json'
    assert (target / relative).read_bytes() == raw
    assert (job_root / 'timeline.json').read_bytes() == raw


def test_cancellation_during_real_cue_metadata_validation_publishes_no_backup(
        authored, tmp_path, monkeypatch):
    app, _, _, _, job_root, timeline, _, _ = authored
    from carr_library.studio import interchange
    original = interchange.validated_cues
    reached = False
    def observe(*args, **kwargs):
        nonlocal reached
        result = original(*args, **kwargs)
        reached = True
        return result
    monkeypatch.setattr(interchange, 'validated_cues', observe)
    directory = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError):
        create_backup(app, directory, cancelled=lambda: reached)
    assert reached and not directory.exists()
    assert read_json(job_root / 'timeline.json') == timeline
