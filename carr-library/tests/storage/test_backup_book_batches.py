"""Manifest-verified BookBatch references are inert across both backup routes."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from carr_library import backup_book_batches, state_backup
from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.book_audio import SplitOptions, capture_plan
from carr_library.studio.book_batch import BookBatchService, select_books
from carr_library.studio.render import ProductionService

_spec = importlib.util.spec_from_file_location('batch_backup_book_fixture',
    Path(__file__).with_name('test_backup_book_audio.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
book = _fixture.book


@pytest.fixture
def selected(book):
    e = book
    other = e.app.add_text(e.workspace, 'Second source', 'Other literal source.')
    plan = capture_plan(e.app, e.workspace, other['edition'], topic=e.topic,
        options=SplitOptions('document'), role=e.plan.role)
    output = e.output.with_name('other-output')
    output.mkdir()
    plans = (e.plan, plan)
    groups = BookBatchService(e.service)
    return SimpleNamespace(**vars(e), groups=groups, plans=plans,
        batch=select_books(plans, title='Exact ordered sources'), outputs=(e.output, output),
        imported_second=other)


def prepare(e):
    review = e.groups.review(e.batch, e.outputs, plans=e.plans)
    return e.groups.prepare(e.batch, e.outputs, plans=e.plans, approval=review.fingerprint)


def no_runtime(*args, **kwargs):
    pytest.fail('Passive batch backup opened a voice, runtime, source output or queue.')


@pytest.mark.parametrize('route', ['full', 'state'])
@pytest.mark.parametrize('prepared_count', [0, 1, 3])
def test_partial_preparation_roundtrips_order_and_plans_without_execution_authority(
        selected, tmp_path, monkeypatch, route, prepared_count):
    e = selected
    actual, submitted = e.production.submit, []
    def interrupt(*args, **kwargs):
        if len(submitted) == prepared_count:
            raise InterruptedError('Stopped at an actual ordinary Studio submission boundary.')
        identity = actual(*args, **kwargs)
        submitted.append(identity)
        return identity
    with monkeypatch.context() as patch:
        patch.setattr(e.production, 'submit', interrupt)
        if prepared_count < 3:
            with pytest.raises(InterruptedError):
                prepare(e)
        else:
            assert prepare(e).prepared
    assert len(e.production.jobs()) == prepared_count
    saved = e.groups.root / (e.batch.id + '.json')
    original = saved.read_bytes()
    plan_bytes = {(e.service.plans / (plan.id + '.json')).relative_to(e.app.root / 'state'):
                  (e.service.plans / (plan.id + '.json')).read_bytes() for plan in e.plans}
    folder, target = tmp_path / 'backup', tmp_path / 'restored'
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'Popen', no_runtime)
        patch.setattr(ProductionService, '_runtime', no_runtime)
        patch.setattr(_fixture.ToneVoices, 'synthesize', no_runtime)
        if route == 'full':
            digest = create_backup(e.app, folder)
            restore_backup(folder, target, digest)
            copied = target / 'state'
        else:
            entries = state_backup.snapshot_state(e.app.root / 'state', folder)
            state_backup.restore_state(folder, target, entries)
            copied = target
    assert (copied / saved.relative_to(e.app.root / 'state')).read_bytes() == original
    assert {path: (copied / path).read_bytes() for path in plan_bytes} == plan_bytes
    assert not (copied / 'studio/.local-origin.json').exists()
    assert not (target / 'runtime/book-batches').exists()
    assert all(not tuple(path.iterdir()) for path in e.outputs)
    if route == 'full':
        restored = LibraryApp(target)
        borrowed = ProductionService(target / 'state/studio', voices=e.voices)
        service = type(e.service)(restored, e.workspace, production=borrowed)
        try:
            status = BookBatchService(service).status(e.batch.id)
            assert [row['plan'] for row in status['books']] == [plan.id for plan in e.plans]
            assert all(row['output_parent'] is None for row in status['books'])
            assert restored.assets.resolve(e.imported['asset']).read_bytes() == e.text.encode('utf-8')
            assert not restored._selections
        finally:
            service.close()
            borrowed.close()


@pytest.mark.parametrize('route', ['full', 'state'])
@pytest.mark.parametrize('damage', ['missing-plan', 'fingerprint', 'topic', 'namespace', 'unknown-field'])
def test_resigned_copied_batch_or_plan_mismatch_refuses_before_target_creation(
        selected, tmp_path, route, damage):
    e = selected
    prepare(e)
    folder, target = tmp_path / 'backup', tmp_path / 'rejected'
    if route == 'full':
        create_backup(e.app, folder)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        copied = folder / 'state'
    else:
        state_backup.snapshot_state(e.app.root / 'state', folder)
        copied = folder
    relative = (e.groups.root / (e.batch.id + '.json')).relative_to(e.app.root / 'state')
    path = copied / relative
    touched = [relative]
    if damage == 'missing-plan':
        relative = (e.service.plans / (e.plans[1].id + '.json')).relative_to(e.app.root / 'state')
        (copied / relative).unlink()
        touched = [relative]
    elif damage == 'namespace':
        moved = Path('studio/book-batches') / ('f' * 64) / path.name
        (copied / moved).parent.mkdir(parents=True)
        path.rename(copied / moved)
        touched.append(moved)
    else:
        if damage == 'fingerprint':
            changed = replace(e.batch, items=(replace(e.batch.items[0], plan_sha256='0' * 64), e.batch.items[1]))
        elif damage == 'topic':
            changed = replace(e.batch, topic='different-but-well-formed-topic')
        else:
            changed = e.batch
        value = {'batch': changed.to_dict(), 'fingerprint': changed.fingerprint}
        if damage == 'unknown-field':
            value['run_automatically'] = True
        path.write_bytes(encode(value))
    if route == 'full':
        for name in touched:
            _fixture.update_manifest(folder, manifest, 'state/' + name.as_posix())
        digest = _fixture.resign(folder, manifest)
        with pytest.raises(ValueError):
            restore_backup(folder, target, digest)
    else:
        entries = _fixture.state_inventory(copied)
        with pytest.raises(ValueError):
            state_backup.restore_state(copied, target, entries)
    assert not target.exists() and not e.voices.received


@pytest.mark.parametrize('name', [
    'studio/Book-Batches',
    'studio/book-batches/UNKNOWN',
    'studio/book-batches/' + 'a' * 64 + '/nested',
])
@pytest.mark.parametrize('route', ['full', 'state'])
def test_reserved_directory_alias_and_nested_namespace_fail_closed(selected, tmp_path, name, route):
    e = selected
    path = e.app.root / 'state' / name
    if name == 'studio/Book-Batches':
        # Change the real stored spelling even on a case-insensitive filesystem.
        source = e.app.root / 'state/studio/book-batches'
        temporary = source.with_name('batch-namespace-move')
        source.rename(temporary)
        temporary.rename(path)
    else:
        path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        if route == 'full':
            create_backup(e.app, target)
        else:
            state_backup.snapshot_state(e.app.root / 'state', target)


def test_full_backup_checks_resealed_plan_source_against_copied_database(selected, tmp_path):
    e = selected
    for plan in e.plans:
        e.service.save(plan)
    value = e.plans[0].to_dict()
    value['source']['source_metadata']['title'] = value['source']['title'] = 'Forged valid source label'
    changed = type(e.plans[0]).from_dict(value)
    atomic_json(e.service.plans / (changed.id + '.json'),
                {'plan': changed.to_dict(), 'fingerprint': changed.fingerprint}, replace=True)
    selection = select_books((changed, e.plans[1]), title=e.batch.title)
    # All JSON signatures are internally consistent; only the copied catalog
    # source facts can reject this forgery.
    atomic_json(e.groups.root / (selection.id + '.json'),
                {'batch': selection.to_dict(), 'fingerprint': selection.fingerprint})
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(e.app, target)
    assert not target.exists() and not e.production.jobs()


@pytest.mark.parametrize('damage', ['raw-bytes', 'oversize', 'collection-count', 'total-bytes', 'cancelled'])
def test_bounded_passive_reader_refuses_untrusted_inventory_before_runtime(
        selected, tmp_path, monkeypatch, damage):
    from carr_library.studio import book_batch
    e = selected
    prepare(e)
    copied = tmp_path / 'copied-state'
    entries = state_backup.snapshot_state(e.app.root / 'state', copied)
    batch_name = (e.groups.root / (e.batch.id + '.json')).relative_to(e.app.root / 'state').as_posix()
    row = next(row for row in entries if row['filename'] == batch_name)
    if damage == 'raw-bytes':
        (copied / batch_name).write_bytes((copied / batch_name).read_bytes() + b' ')
    elif damage == 'oversize':
        row['size_bytes'] = book_batch.MAX_BATCH_BYTES + 1
    elif damage == 'collection-count':
        monkeypatch.setattr(book_batch, 'MAX_SAVED_BATCHES', 1)
        another = select_books(e.plans, title='Another legitimate selection')
        path = copied / Path(batch_name).with_name(another.id + '.json')
        raw = encode({'batch': another.to_dict(), 'fingerprint': another.fingerprint})
        path.write_bytes(raw)
        entries.append({'filename': path.relative_to(copied).as_posix(),
                        'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    elif damage == 'total-bytes':
        monkeypatch.setattr(book_batch, 'MAX_COLLECTION_BYTES', row['size_bytes'] - 1)
    monkeypatch.setattr(subprocess, 'Popen', no_runtime)
    with pytest.raises(InterruptedError if damage == 'cancelled' else ValueError):
        backup_book_batches.validate_collection(copied, entries, cancelled=lambda: damage == 'cancelled')


@pytest.mark.parametrize('incoming', [False, True])
def test_exact_local_batch_lock_is_not_portable_authority(selected, tmp_path, incoming):
    e = selected
    prepare(e)
    folder = tmp_path / 'copy'
    entries = state_backup.snapshot_state(e.app.root / 'state', folder)
    name = (e.groups.root / ('.batch-' + e.batch.id + '.lock')).relative_to(e.app.root / 'state').as_posix()
    assert all(row['filename'] != name for row in entries)
    if incoming:
        (folder / name).write_bytes(b'')
        entries = _fixture.state_inventory(folder)
        with pytest.raises(ValueError):
            state_backup.restore_state(folder, tmp_path / 'rejected', entries)
        assert not (tmp_path / 'rejected').exists()
    else:
        state_backup.restore_state(folder, tmp_path / 'restored', entries)
        assert not (tmp_path / 'restored' / name).exists()


@pytest.mark.parametrize('route', ['full', 'state'])
def test_batch_record_hardlink_is_not_a_private_regular_backup_input(selected, tmp_path, route):
    import os
    e = selected
    prepare(e)
    record = e.groups.root / (e.batch.id + '.json')
    outside = tmp_path / 'linked-selection'
    os.link(record, outside)
    original = record.read_bytes()
    try:
        with pytest.raises(ValueError):
            if route == 'full':
                create_backup(e.app, tmp_path / 'rejected')
            else:
                state_backup.snapshot_state(e.app.root / 'state', tmp_path / 'rejected')
        assert record.read_bytes() == outside.read_bytes() == original
    finally:
        outside.unlink()


def test_standalone_restore_never_overwrites_existing_batch_bytes(selected, tmp_path):
    e = selected
    prepare(e)
    folder, target = tmp_path / 'copy', tmp_path / 'existing'
    entries = state_backup.snapshot_state(e.app.root / 'state', folder)
    relative = (e.groups.root / (e.batch.id + '.json')).relative_to(e.app.root / 'state')
    existing = target / relative
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b'other owner selection')
    with pytest.raises(FileExistsError):
        state_backup.restore_state(folder, target, entries)
    assert existing.read_bytes() == b'other owner selection'
