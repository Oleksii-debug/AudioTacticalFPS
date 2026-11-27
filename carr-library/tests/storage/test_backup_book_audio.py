"""Actual source/Studio producers survive backup with inert exact book linkage.

Generated tones exercise the audio pipeline only. No voice quality, microphone,
private engine, listening or screen-reader qualification is claimed here.
"""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
from types import SimpleNamespace
import wave

import pytest

from carr_library import backup_book_audio, state_backup
from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.durable_state import atomic_json, encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.studio.book_audio import BookAudioService, BookPlan, SplitOptions, capture_plan
from carr_library.studio.project import Project, Role
from carr_library.studio.render import ProductionService


class ToneVoices:
    def __init__(self):
        self.received = []

    def voices(self):
        return (SimpleNamespace(id='backup-tone', name='Synthetic backup tone', language='en'),)

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        assert voice_id == 'backup-tone' and not cancelled()
        self.received.append(text)
        with wave.open(str(destination), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(2000 * math.sin(
                2 * math.pi * 440 * frame / 24000))) for frame in range(4800)))


@pytest.fixture
def book(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Book backup source')
    topic = app.create_topic(workspace, 'Source topic')
    text = 'First literal source paragraph.\n\nНезмінне друге речення.'
    imported = app.add_text(workspace, 'Original metadata', text)
    voices = ToneVoices()
    production = ProductionService(app.root / 'state' / 'studio', voices=voices,
        broker=ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1)),
        runtime_factory=lambda root, **options: AudioRuntime(root,
            budget=AudioBudget(threads=1), **options))
    service = BookAudioService(app, workspace, production=production)
    output = tmp_path / 'old-output'
    output.mkdir()
    plan = capture_plan(app, workspace, imported['edition'], topic=topic,
        options=SplitOptions('blank-lines'),
        role=Role('narrator', 'Narrator', 'backup-tone', 'en'))
    saved = service.plans / (plan.id + '.json')
    yield SimpleNamespace(app=app, workspace=workspace, topic=topic, imported=imported,
        text=text, voices=voices, production=production, service=service,
        output=output, plan=plan, saved=saved)
    service.close()
    production.close()


def prepare(e):
    review = e.service.review(e.plan, e.output)
    return e.service.prepare(e.plan, e.output, approval=review.fingerprint)


def save_zero(e):
    atomic_json(e.saved, {'plan': e.plan.to_dict(), 'fingerprint': e.plan.fingerprint})


def resign(folder, manifest):
    raw = encode(manifest)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def update_manifest(folder, manifest, relative):
    path = folder / relative
    rows = [row for row in manifest['sidecars'] if row['filename'] != relative]
    if path.exists():
        raw = path.read_bytes()
        rows.append({'filename': relative, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    manifest['sidecars'] = rows


def state_inventory(root):
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_file():
            raw = path.read_bytes()
            result.append({'filename': path.relative_to(root).as_posix(),
                           'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    return result


def no_runtime(*args, **kwargs):
    pytest.fail('Passive backup opened a runtime, voice or old output.')


@pytest.mark.parametrize('prepared_count', [0, 1, 2])
def test_real_partial_prepare_roundtrips_without_source_or_output_authority(
        book, tmp_path, monkeypatch, prepared_count):
    e = book
    original_submit = e.production.submit
    submitted = []
    def interrupted(*args, **kwargs):
        if len(submitted) == prepared_count:
            raise InterruptedError('Synthetic interruption at the real submission boundary.')
        result = original_submit(*args, **kwargs)
        submitted.append(result)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(e.production, 'submit', interrupted)
        if prepared_count < 2:
            with pytest.raises(InterruptedError):
                prepare(e)
        else:
            assert prepare(e).prepared
    assert len(e.production.jobs()) == prepared_count and e.saved.exists()
    if prepared_count == 1:
        e.app.archive_workspace(e.workspace)  # Historical sources remain backup data.
    retained = {path.relative_to(e.app.root): path.read_bytes()
                for path in (e.app.root / 'state' / 'studio').rglob('*.json')
                if path.name != '.local-origin.json'}
    folder, target = tmp_path / 'backup', tmp_path / 'restored'
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'Popen', no_runtime)
        patch.setattr(ProductionService, '_runtime', no_runtime)
        patch.setattr(ToneVoices, 'synthesize', no_runtime)
        digest = create_backup(e.app, folder)
        restore_backup(folder, target, digest)
    assert {name: (target / name).read_bytes() for name in retained} == retained
    assert not (target / 'state/studio/.local-origin.json').exists()
    assert not tuple(e.output.iterdir()) and e.voices.received == []
    restored = LibraryApp(target)
    assert restored.assets.resolve(e.imported['asset']).read_bytes() == e.text.encode('utf-8')
    assert restored._selections == {}
    other = ProductionService(target / 'state/studio', voices=e.voices)
    try:
        for identity in submitted:
            assert other.get(identity)['recovery']['requires_output_directory'] is True
            with monkeypatch.context() as patch:
                patch.setattr(other, '_runtime', no_runtime)
                with pytest.raises(ValueError, match='newly selected output'):
                    other.run(identity, source_guard=no_runtime)
    finally:
        other.close()


@pytest.mark.parametrize('operation', ['snapshot', 'restore'])
@pytest.mark.parametrize('damage', ['missing-plan', 'job-project', 'format', 'stems',
                                    'receipt', 'orphan-receipt'])
def test_book_link_corruption_fails_even_with_a_resigned_manifest(
        book, tmp_path, operation, damage):
    e = book
    prepare(e)
    folder, destination = tmp_path / 'backup', tmp_path / 'rejected'
    if operation == 'restore':
        create_backup(e.app, folder)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        root = folder
    else:
        root = e.app.root
    job_id = e.plan.job_id(1)
    relative = 'state/studio/jobs/' + job_id + '/job.json'
    path = root / relative
    if damage == 'missing-plan':
        relative = e.saved.relative_to(e.app.root).as_posix()
        (root / relative).unlink()
    elif damage in ('receipt', 'orphan-receipt'):
        relative = 'state/studio/jobs/' + job_id + '/book-source.json'
        value = e.plan.provenance(2 if damage == 'receipt' else 1)
        (root / relative).write_bytes(encode(value))
        if damage == 'orphan-receipt':
            job_relative = 'state/studio/jobs/' + job_id + '/job.json'
            (root / job_relative).unlink()
            if operation == 'restore':
                update_manifest(root, manifest, job_relative)
    else:
        value = json.loads(path.read_bytes())
        if damage == 'job-project':
            project = replace(Project.from_dict(value['project']), title='A different valid project')
            value.update(project=project.to_dict(), fingerprint=project.fingerprint)
        elif damage == 'format':
            value['format'] = 'flac'
        else:
            value['stems'] = True
        path.write_bytes(encode(value))
    if operation == 'restore':
        update_manifest(root, manifest, relative)
        digest = resign(folder, manifest)
        with pytest.raises(ValueError):
            restore_backup(folder, destination, digest)
        assert not destination.exists()
    else:
        with pytest.raises(ValueError):
            create_backup(e.app, folder)
        assert not folder.exists()


@pytest.mark.parametrize('operation', ['snapshot', 'restore'])
@pytest.mark.parametrize('damage', ['missing-plan', 'project', 'receipt'])
def test_standalone_state_keeps_the_same_exact_book_file_linkage(book, tmp_path, operation, damage):
    e = book
    prepare(e)
    source, output = tmp_path / 'state-copy', tmp_path / 'state-restored'
    if operation == 'restore':
        state_backup.snapshot_state(e.app.root / 'state', source)
        root = source
    else:
        root = e.app.root / 'state'
    if damage == 'missing-plan':
        (root / e.saved.relative_to(e.app.root / 'state')).unlink()
    elif damage == 'project':
        path = root / 'studio/projects' / e.plan.job_id(1) / 'project.json'
        project = Project.from_dict(json.loads(path.read_bytes()))
        path.write_bytes(encode(replace(project, title='Changed valid project').to_dict()))
    else:
        path = root / 'studio/jobs' / e.plan.job_id(1) / 'book-source.json'
        path.write_bytes(encode(e.plan.provenance(2)))
    if operation == 'restore':
        # Recompute the outer inventory: inner semantic linkage must still fail.
        entries = state_inventory(source)
        with pytest.raises(ValueError):
            state_backup.restore_state(source, output, entries)
    else:
        with pytest.raises(ValueError):
            state_backup.snapshot_state(root, source)


@pytest.mark.parametrize('damage', ['source-text', 'metadata', 'topic'])
def test_valid_resigned_zero_job_plan_must_match_the_copied_database(book, tmp_path, damage):
    e = book
    value = e.plan.to_dict()
    if damage == 'source-text':
        source = value['source']
        source['text'] = source['text'].replace('literal', 'changed')
        for position in source['source_anchor']['positions']:
            position['text_sha256'] = sha256(
                source['text'][position['start']:position['end']].encode('utf-8')).hexdigest()
    elif damage == 'metadata':
        value['source']['source_metadata']['title'] = value['source']['title'] = 'Different valid title'
    else:
        value['source']['source_topic'] = 'missing_topic'
    changed = BookPlan.from_dict(value)
    atomic_json(e.saved, {'plan': changed.to_dict(), 'fingerprint': changed.fingerprint})
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(e.app, destination)
    assert not destination.exists() and not e.production.jobs()


@pytest.mark.parametrize('damage', ['receipt', 'timeline', 'result', 'missing-receipt'])
def test_actual_completed_book_receipts_cannot_be_rebound(book, tmp_path, damage):
    e = book
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the reviewed FFmpeg test runtime.'
    reviewed = prepare(e)
    rows = e.service.run(e.plan.id, e.output, approval=reviewed.fingerprint)
    assert all(row['status'] == 'complete' for row in rows) and len(e.voices.received) == 2
    folder = tmp_path / 'completed-backup'
    digest = create_backup(e.app, folder)
    clean = tmp_path / 'clean-restored'
    restore_backup(folder, clean, digest)
    assert not (clean / 'state/studio/.local-origin.json').exists()
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    leaf = {'receipt': 'book-source.json', 'missing-receipt': 'book-source.json',
            'timeline': 'timeline.json', 'result': 'job.json'}[damage]
    relative = 'state/studio/jobs/' + e.plan.job_id(1) + '/' + leaf
    path = folder / relative
    if damage == 'missing-receipt':
        path.unlink()
    else:
        value = json.loads(path.read_bytes())
        if damage == 'receipt':
            value['source_metadata']['title'] = 'A forged source label'
        elif damage == 'timeline':
            value['book_source_sha256'] = '0' * 64
        else:
            value['result']['book_source_sha256'] = '0' * 64
        path.write_bytes(encode(value))
    update_manifest(folder, manifest, relative)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, target, resign(folder, manifest))
    assert not target.exists()


def test_interrupted_submit_may_retain_only_its_exact_project(book, tmp_path):
    e = book
    save_zero(e)
    e.production.projects.save(e.plan.project(1))
    assert e.production.jobs() == ()
    folder = tmp_path / 'project-only'
    digest = create_backup(e.app, folder)
    restored = tmp_path / 'restored-project-only'
    restore_backup(folder, restored, digest)
    name = 'state/studio/projects/' + e.plan.job_id(1) + '/project.json'
    assert (restored / name).read_bytes() == (e.app.root / name).read_bytes()
    assert not (restored / 'state/studio/jobs' / e.plan.job_id(1) / 'job.json').exists()


def test_copied_state_hash_failure_is_not_treated_as_opaque_history(book, tmp_path):
    e = book
    prepare(e)
    source = tmp_path / 'state-copy'
    entries = state_backup.snapshot_state(e.app.root / 'state', source)
    # Remove the plan and its inventory to reach the unbound marker scan.
    saved = source / e.saved.relative_to(e.app.root / 'state')
    saved.unlink()
    entries = [row for row in entries if row['filename'] != saved.relative_to(source).as_posix()]
    name = next(row['filename'] for row in entries
                if row['filename'].startswith('studio/jobs/') and row['filename'].endswith('/job.json'))
    path = source / name
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='manifest'):
        backup_book_audio.validate_collection(None, source, entries)


def test_cancelled_book_closure_keeps_prior_backup_and_publishes_nothing(book, tmp_path, monkeypatch):
    e = book
    prepare(e)
    previous = tmp_path / 'previous'
    checksum = create_backup(e.app, previous)
    retained = (previous / 'manifest.json').read_bytes()
    original = backup_book_audio.validate_collection
    def stopped(db, root, entries, *, cancelled):
        return original(db, root, entries, cancelled=lambda: True)
    monkeypatch.setattr(backup_book_audio, 'validate_collection', stopped)
    destination = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError):
        create_backup(e.app, destination)
    assert not destination.exists()
    assert (previous / 'manifest.json').read_bytes() == retained
    assert sha256(retained).hexdigest() == checksum
