"""Typed backups of real prepared Book items, without starting a speech engine."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library import backup_producers
from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, encode, read_json, state_lock
from carr_library.library_backup import restore_backup
from carr_library.keybindings import Command, FILENAME as KEYBINDING_FILE, KeybindingStore
from carr_library.studio.book_audio import BookAudioService, SplitOptions, capture_plan
from carr_library.studio.project import Project, Role
from carr_library.studio.render import ProductionService, _operation_sha256


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
    original_operation = value['operation_sha256']
    assert original_operation == _operation_sha256(value)
    plan_path = book.service.plans / (book.plan.id + '.json')
    original_plan = plan_path.read_bytes()
    if changed == 'project':
        project = replace(Project.from_dict(value['project']), title='Another valid project title')
        value.update(project=project.to_dict(), fingerprint=project.fingerprint)
    elif changed == 'format':
        value['format'] = 'flac'
    else:
        value['stems'] = True
    # Re-sign the deliberately different, independently valid operation.
    # The immutable Book relation must still refuse this journal even when
    # the operation's own project/options checksum is self-consistent.
    value['operation_sha256'] = _operation_sha256(value)
    assert value['operation_sha256'] != original_operation
    backup_producers._studio_job(value, identity)
    atomic_json(path, value, replace=True)
    changed_journal = path.read_bytes()
    with pytest.raises(ValueError, match='immutable reviewed production plan'):
        book.service.review(book.plan, book.output)
    with pytest.raises(ValueError, match='Book job.*immutable reviewed plan'):
        book.app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert not list(book.output.iterdir())
    assert path.read_bytes() == changed_journal
    assert plan_path.read_bytes() == original_plan


def test_actual_preparation_locks_are_not_restored_and_new_output_requires_review(book, tmp_path):
    _prepare(book)
    # This case intentionally qualifies the composite format-5 envelope. A
    # library with only state and pending records correctly emits format 4.
    bindings = {'voice_reader.prepare': 'Ctrl+Alt+Shift+P'}
    commands = (Command('voice_reader.prepare', 'Prepare reading copy', context='voice_reader'),)
    preferences = KeybindingStore(book.app.root).save(commands, bindings)
    lock = book.service.plans / ('.book-' + book.plan.id + '.lock')
    plan_path = book.service.plans / (book.plan.id + '.json')
    original_plan = plan_path.read_bytes()
    with state_lock(book.service.plans, 'book-' + book.plan.id):
        assert lock.is_file()
        checksum = book.app.backup(tmp_path / 'backup')
    manifest = read_json(tmp_path / 'backup' / 'manifest.json')
    assert manifest['version'] == 5
    assert not any(row['filename'].endswith('.lock') for row in manifest['state'])
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored',
                              expected_manifest_sha256=checksum)
    assert (restored / KEYBINDING_FILE).read_bytes() == preferences
    assert KeybindingStore(restored).load() == bindings
    recovered_app = LibraryApp(restored)
    production = ProductionService(restored / 'state' / 'studio', voices=book.voices)
    service = BookAudioService(recovered_app, book.workspace, production=production)
    try:
        retained = service.load(book.plan.id)
        assert retained.fingerprint == book.plan.fingerprint
        assert (service.plans / plan_path.name).read_bytes() == original_plan
        assert not (service.plans / lock.name).exists()
        historical = {path: path.read_bytes() for path in production.jobs_root.glob('*/job.json')}
        for index in (1, 2):
            job = production.get(retained.job_id(index))
            original = book.production.get(retained.job_id(index))
            assert job['fingerprint'] == retained.project(index).fingerprint
            assert job['status'] == original['status'] and job['destination'] == original['destination']
            assert job['restore_requires_new_output'] is True
        output = tmp_path / 'explicit-new-output'
        output.mkdir()
        review = service.review(retained, output)
        assert not review.prepared
        with pytest.raises(ValueError, match='changed'):
            service.prepare(retained, output, approval='unreviewed')
        with pytest.raises(ValueError, match='new output review'):
            service.prepare(retained, output, approval=review.fingerprint)
        fresh = service.fresh(retained)
        preview = service.review(fresh, output)
        ready = service.prepare(fresh, output, approval=preview.fingerprint)
        assert ready.prepared and not list(output.iterdir())
        assert all(Path(production.get(fresh.job_id(index))['destination']).parent == output
                   for index in (1, 2))
        assert historical == {path: path.read_bytes() for path in historical}
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

def test_no_book_paths_never_opens_a_database(tmp_path, monkeypatch):
    from carr_library.studio import book_backup
    def forbidden(*args, **kwargs):
        raise AssertionError('Unrelated backup must not open an optional Book database.')
    monkeypatch.setattr(book_backup.sqlite3, 'connect', forbidden)
    book_backup.validate_database_snapshot(tmp_path / 'absent.db', tmp_path / 'state',
        [{'filename': 'speech/other.json', 'size_bytes': 20, 'sha256': '0' * 64}])


def test_rehashed_forged_book_source_is_refused_against_actual_database(book, tmp_path):
    from hashlib import sha256
    source = book.plan.source
    source['text'] = source['text'].replace('First', 'Other', 1)
    for row in source['source_anchor']['positions']:
        row['text_sha256'] = sha256(source['text'][row['start']:row['end']].encode()).hexdigest()
    forged = replace(book.plan, source_json=encode(source).decode('utf-8'))
    path = book.service.plans / (forged.id + '.json')
    atomic_json(path, {'plan': forged.to_dict(), 'fingerprint': forged.fingerprint})
    # Structurally valid, self-consistently rehashed data still cannot claim
    # another exact sealed page. No source or voice runtime is consulted.
    assert not book.production.jobs()
    with pytest.raises(ValueError, match='exact sealed revision'):
        book.app.backup(tmp_path / 'forged-backup')
    assert not (tmp_path / 'forged-backup').exists()


def test_unbound_book_job_without_saved_plan_cannot_be_backed_up(book, tmp_path):
    _prepare(book)
    (book.service.plans / (book.plan.id + '.json')).unlink()
    with pytest.raises(ValueError, match='no matching retained reviewed plan'):
        book.app.backup(tmp_path / 'orphan-backup')
    assert not (tmp_path / 'orphan-backup').exists()


def test_plan_namespace_case_alias_keeps_exact_bytes_and_binding(book, tmp_path):
    _prepare(book)
    path = book.service.plans / (book.plan.id + '.json')
    before = path.read_bytes()
    alias = path.with_name(book.plan.id.upper() + '.JSON')
    path.rename(alias)
    checksum = book.app.backup(tmp_path / 'case-backup')
    restored = restore_backup(tmp_path / 'case-backup', tmp_path / 'case-restored',
                              expected_manifest_sha256=checksum)
    assert (restored / alias.relative_to(book.app.root)).read_bytes() == before
