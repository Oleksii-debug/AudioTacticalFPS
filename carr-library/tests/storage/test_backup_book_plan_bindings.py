"""Typed backups of real prepared Book items, without starting a speech engine."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library import backup_producers
from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup
from carr_library.studio.book_audio import BookAudioService, SplitOptions, capture_plan
from carr_library.studio.project import Project, Role
from carr_library.studio.render import ProductionService


class SelectedVoice:
    def voices(self):
        return (SimpleNamespace(id='selected-fixture-voice', name='Protocol fixture',
                                language='en', engine='test-only'),)

    def synthesize(self, *args, **kwargs):
        raise AssertionError('Preview, preparation and passive backup must not synthesize.')

    def close(self):
        raise AssertionError('The production owner borrowed this explicit registry.')


@pytest.fixture
def book(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact Book plan backup')
    imported = app.add_text(workspace, 'Retained title', 'First literal part.\n\nSecond literal part.')
    voices = SelectedVoice()
    production = ProductionService(app.root / 'state' / 'studio', voices=voices)
    service = BookAudioService(app, workspace, production=production)
    output = tmp_path / 'selected-output'
    output.mkdir()
    plan = capture_plan(app, workspace, imported['edition'],
        options=SplitOptions('blank-lines'), role=Role('narrator', 'Narrator', 'selected-fixture-voice', 'en'))
    try:
        yield SimpleNamespace(app=app, workspace=workspace, service=service,
            production=production, output=output, plan=plan, voices=voices)
    finally:
        service.close()
        production.close()


def _prepare(value):
    review = value.service.review(value.plan, value.output)
    result = value.service.prepare(value.plan, value.output, approval=review.fingerprint)
    assert result.prepared
    assert not list(value.output.iterdir())
    return result


@pytest.mark.parametrize('changed', ['project', 'format', 'stems'])
def test_individually_valid_job_cannot_break_its_saved_book_plan(book, tmp_path, changed):
    _prepare(book)
    identity = book.plan.job_id(1)
    path = book.production.jobs_root / identity / 'job.json'
    value = read_json(path)
    if changed == 'project':
        project = replace(Project.from_dict(value['project']), title='Another valid project title')
        value.update(project=project.to_dict(), fingerprint=project.fingerprint)
    elif changed == 'format':
        value['format'] = 'flac'
    else:
        value['stems'] = True
    # Each journal is valid in isolation; the retained immutable Book relation
    # is the additional fact that must refuse the inconsistent backup.
    backup_producers._studio_job(value, identity)
    atomic_json(path, value, replace=True)
    with pytest.raises(ValueError, match='immutable reviewed production plan'):
        book.service.review(book.plan, book.output)
    with pytest.raises(ValueError, match='Book item.*immutable reviewed plan'):
        book.app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert not list(book.output.iterdir())


def test_actual_preparation_locks_are_not_restored_and_new_output_requires_review(book, tmp_path):
    _prepare(book)
    lock = book.service.plans / ('.book-' + book.plan.id + '.lock')
    assert lock.is_file()  # Actual FileLock left by the existing producer.
    plan_path = book.service.plans / (book.plan.id + '.json')
    original_plan = plan_path.read_bytes()
    checksum = book.app.backup(tmp_path / 'backup')
    manifest = read_json(tmp_path / 'backup' / 'manifest.json')
    assert manifest['version'] == 4
    assert not any(row['filename'].endswith('.lock') for row in manifest['sidecars'])
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored',
                              expected_manifest_sha256=checksum)
    recovered_app = LibraryApp(restored)
    production = ProductionService(restored / 'state' / 'studio', voices=book.voices)
    service = BookAudioService(recovered_app, book.workspace, production=production)
    try:
        retained = service.load(book.plan.id)
        assert retained.fingerprint == book.plan.fingerprint
        assert (service.plans / plan_path.name).read_bytes() == original_plan
        assert not (service.plans / lock.name).exists()
        for index in (1, 2):
            job = production.get(retained.job_id(index))
            assert job['fingerprint'] == retained.project(index).fingerprint
            assert job['status'] == 'waiting-input' and job['destination'] == ''
            assert job['recovery']['requires_output_directory'] is True
        output = tmp_path / 'explicit-new-output'
        output.mkdir()
        review = service.review(retained, output)
        assert not review.prepared
        with pytest.raises(ValueError, match='changed'):
            service.prepare(retained, output, approval='unreviewed')
        ready = service.prepare(retained, output, approval=review.fingerprint)
        assert ready.prepared and not list(output.iterdir())
        assert all(Path(production.get(retained.job_id(index))['destination']).parent == output
                   for index in (1, 2))
        assert not list(book.output.iterdir())
    finally:
        service.close()
        production.close()


def test_plan_saved_before_interrupted_item_preparation_is_still_backed_up(book, tmp_path, monkeypatch):
    review = book.service.review(book.plan, book.output)
    def interrupted(*args, **kwargs):
        raise InterruptedError('Fixture interruption before first item submission.')
    monkeypatch.setattr(book.production, 'submit', interrupted)
    with pytest.raises(InterruptedError, match='before first item'):
        book.service.prepare(book.plan, book.output, approval=review.fingerprint)
    assert book.service.load(book.plan.id).fingerprint == book.plan.fingerprint
    assert book.production.jobs() == ()
    checksum = book.app.backup(tmp_path / 'prepared-plan')
    restored = restore_backup(tmp_path / 'prepared-plan', tmp_path / 'restored-plan',
                              expected_manifest_sha256=checksum)
    recovered = LibraryApp(restored)
    production = ProductionService(restored / 'state' / 'studio', voices=book.voices)
    service = BookAudioService(recovered, book.workspace, production=production)
    try:
        assert service.load(book.plan.id).fingerprint == book.plan.fingerprint
        assert production.jobs() == ()
    finally:
        service.close()
        production.close()
