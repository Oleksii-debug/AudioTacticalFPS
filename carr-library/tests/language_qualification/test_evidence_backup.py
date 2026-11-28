"""Real typed library/state backup closure for passive language assessments."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode
from carr_library.language_qualification import build_capability_matrix
from carr_library.language_qualification import backup as module
from carr_library.language_qualification.evidence import EvidenceStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_backup import snapshot_state, restore_state

_spec = importlib.util.spec_from_file_location('_qualification_fixture', Path(__file__).with_name('evidence_fixture.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
ROUTES = ('library', 'state')


def snapshot(app, path, route):
    if route == 'library':
        create_backup(app, path)
        return path, json.loads((path / 'manifest.json').read_bytes()), route
    path.mkdir()
    return path, snapshot_state(app.root / 'state', path), route


def entries(saved):
    return saved[1]['sidecars'] if saved[2] == 'library' else saved[1]


def filename(saved):
    return module.FILENAME if saved[2] == 'library' else module.RELATIVE_PATH


def write_snapshot(saved, raw):
    path = saved[0] / filename(saved)
    path.write_bytes(raw)
    row = next(row for row in entries(saved) if row['filename'] == filename(saved))
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


def restore(saved, target):
    path, manifest, route = saved
    if route == 'library':
        raw = encode(manifest)
        (path / 'manifest.json').write_bytes(raw)
        return restore_backup(path, target, sha256(raw).hexdigest())
    restore_state(path, target / 'state', manifest)
    return target


def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Passive language evidence')
    app.add_text(workspace, 'Retained original', 'Незмінний збережений оригінал.', edition_id='original')
    store, first, before, token = fixture.saved(app.root)
    before, token = fixture.assess(store, first, token)
    revoked = fixture.observation('unicode_text')
    before, token = store.add(revoked, expected_token=token)
    before, token = fixture.assess(store, revoked, token, 'revoke')
    return app, store, first, revoked, before, token, workspace


@pytest.mark.parametrize('route', ROUTES)
def test_real_backups_preserve_history_but_restored_positive_reviews_are_dormant(tmp_path, monkeypatch, route):
    from carr_library import runtime_settings
    app, store, first, revoked, before, token, workspace = prepared(tmp_path)
    raw = store.path.read_bytes()
    forbidden = lambda *a, **k: pytest.fail('Passive evidence backup activated a runtime or external input.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    saved = snapshot(app, tmp_path / 'backup', route)
    assert (saved[0] / filename(saved)).read_bytes() == raw
    assert all(not row['filename'].endswith('.lock') for row in entries(saved))
    shutil.rmtree(app.root)
    target = restore(saved, tmp_path / 'restored')
    current = EvidenceStore(target)
    result, new_token = current.load()
    assert result.observations == before.observations and result.reviews == before.reviews
    assert result.dormant_through == len(result.reviews) and result.revision == before.revision + 1
    matrix = build_capability_matrix(evidence=result, recorded_context=first.context.fingerprint)
    row = next(row for row in matrix.entries if row.language.id == first.language_id)
    assert row.capability('tts').status == 'candidate'
    assert row.capability('unicode_text').status == 'revoked'
    assert matrix.summary()['qualified_entries'] == 0
    assert (saved[0] / filename(saved)).read_bytes() == raw
    if route == 'library':
        assert LibraryApp(target).edition_metadata(workspace, 'original').title == 'Retained original'
    reviewed, new_token = fixture.assess(current, first, new_token)
    matrix = build_capability_matrix(evidence=reviewed, recorded_context=first.context.fingerprint)
    row = next(row for row in matrix.entries if row.language.id == first.language_id)
    assert row.capability('tts').status == 'verified'
    assert row.capability('unicode_text').status == 'revoked'
    assert reviewed.reviews[:len(before.reviews)] == before.reviews
    assert not list(target.rglob('*.wav'))


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('damage', [
    'unknown', 'review-target', 'self-review', 'context', 'catalog', 'dormant',
    'revision', 'approval-field',
])
def test_resigned_invalid_typed_evidence_cannot_be_restored(tmp_path, route, damage):
    app, store, first, revoked, before, token, workspace = prepared(tmp_path)
    saved = snapshot(app, tmp_path / 'backup', route)
    value = before.to_dict()
    if damage == 'unknown':
        value['execute'] = 'never'
    elif damage == 'review-target':
        value['reviews'][0]['observation_sha256'] = 'f' * 64
    elif damage == 'self-review':
        value['reviews'][0]['reviewer']['identity'] = first.test.executor.identity
    elif damage == 'context':
        value['observations'][0]['context']['environment_sha256'] = 'not-a-digest'
    elif damage == 'catalog':
        value['observations'][0]['catalog_sha256'] = 'f' * 64
    elif damage == 'dormant':
        value['dormant_through'] = len(value['reviews']) + 1
    elif damage == 'revision':
        value['revision'] = True
    elif damage == 'approval-field':
        value['observations'][0]['status'] = 'verified'
    raw = encode(value)
    write_snapshot(saved, raw)
    target = tmp_path / 'refused'
    with pytest.raises((ValueError, TypeError)):
        restore(saved, target)
    assert not target.exists()
    assert (saved[0] / filename(saved)).read_bytes() == raw
    assert store.load() == (before, token)


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('unexpected', ['file', 'directory'])
def test_unknown_reserved_namespace_cannot_be_omitted_or_copied_opaquely(tmp_path, route, unexpected):
    app, store, *_ = prepared(tmp_path)
    if unexpected == 'file':
        extra = store.path.parent / 'undeclared.json'
        extra.write_bytes(b'{}')
    else:
        extra = store.path.parent / 'nested'
        extra.mkdir()
    with pytest.raises((ValueError, TypeError)):
        snapshot(app, tmp_path / 'must-refuse', route)
    assert extra.exists()


@pytest.mark.parametrize('route', ROUTES)
def test_portable_case_alias_is_rejected_before_looking_for_copied_state(tmp_path, route):
    app, store, *_ = prepared(tmp_path)
    saved = snapshot(app, tmp_path / 'backup', route)
    row = next(row for row in entries(saved) if row['filename'] == filename(saved))
    row['filename'] = row['filename'].replace('language-qualification', 'Language-Qualification')
    with pytest.raises(ValueError, match='language evidence|backup adapter|reserved'):
        restore(saved, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def test_cancelled_dormancy_preparation_preserves_exact_copied_bytes_and_exception(tmp_path):
    app, store, *_ = prepared(tmp_path)
    raw = store.path.read_bytes()
    stop = InterruptedError('Original restore cancellation')

    def cancelled():
        raise stop

    with pytest.raises(InterruptedError) as caught:
        module.prepare_restored_state(app.root / 'state', cancelled=cancelled)
    assert caught.value is stop and store.path.read_bytes() == raw
    assert not list(store.path.parent.glob('.pending-*'))


@pytest.mark.parametrize('route', ROUTES)
def test_only_exact_writer_temporaries_are_excluded_from_passive_backup(tmp_path, route):
    app, store, *_ = prepared(tmp_path)
    (store.path.parent / '.pending-interrupted').write_bytes(b'Not committed JSON')
    saved = snapshot(app, tmp_path / 'backup', route)
    names = [row['filename'] for row in entries(saved)]
    assert filename(saved) in names
    assert not any('.pending-' in name or name.endswith('.lock') for name in names)
    target = restore(saved, tmp_path / 'restored')
    assert not list((target / 'state' / 'language-qualification').glob('.pending-*'))
