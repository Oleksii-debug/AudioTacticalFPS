"""Actual passive parsing, filesystem, CAS and scoped-review boundaries."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from carr_library.durable_state import encode
from carr_library.language_qualification import build_capability_matrix, load_catalog
from carr_library.language_qualification import evidence as module
from carr_library.language_qualification.evidence import (
    CatalogError, EvidenceSnapshot, EvidenceStore, PersonProvenance,
    QualificationObservation, RecordedReview, import_observation,
)

_spec = importlib.util.spec_from_file_location('_qualification_fixture', Path(__file__).with_name('evidence_fixture.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def matrix(snapshot, context=None):
    return build_capability_matrix(evidence=snapshot, recorded_context=context)


def status(snapshot, value):
    return matrix(snapshot, value.context.fingerprint).search('Англійська')[0].capability(value.capability).status


@pytest.mark.parametrize('outcome', ['pass', 'not_run'])
def test_passive_import_and_zero_default_never_execute_or_accept_reviews(tmp_path, monkeypatch, outcome):
    from carr_library import runtime_settings
    forbidden = lambda *a, **k: pytest.fail('Passive evidence attempted execution or discovery.')
    # Preserve the class used by Windows asyncio while forbidding process creation.
    monkeypatch.setattr(subprocess.Popen, '__init__', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    value = fixture.observation(outcome=outcome)
    path = tmp_path / 'selected.json'
    raw = encode(value.to_dict())
    path.write_bytes(raw)
    root = tmp_path / 'library'
    root.mkdir()
    store = EvidenceStore(root)
    assert not (root / 'state').exists()
    assert store.load() == (EvidenceSnapshot(), None)
    restored, file_digest = import_observation(path)
    assert restored == value and file_digest == sha256(raw).hexdigest()
    snapshot, token = store.add(restored, expected_token=None)
    assert snapshot.reviews == () and status(snapshot, value) == 'candidate'
    assert matrix(snapshot).summary()['qualified_entries'] == 0
    assert not matrix(snapshot).search(status='verified')
    assert path.read_bytes() == raw and store.load() == (snapshot, token)


@pytest.mark.parametrize('change', [
    'schema', 'approval', 'catalog', 'language', 'capability', 'tag', 'scope', 'coverage',
    'app-version', 'app-hash', 'app-na', 'engine-mixed', 'pack-hash', 'input-hash',
    'outcome', 'declaration-pass', 'time', 'executor', 'oversize', 'nested-review',
])
def test_import_refuses_forged_or_ambiguous_observation_shapes(tmp_path, change):
    value = fixture.observation().to_dict()
    if change == 'schema':
        value['schema'] = 'carr-language-evidence-report/1'
    elif change == 'approval':
        value['reviews'] = [{'decision': 'accept'}]
    elif change == 'catalog':
        value['catalog_sha256'] = 'd' * 64
    elif change == 'language':
        value['language_id'] = 'selected-not-present'
    elif change == 'capability':
        value['capability'] = 'arbitrary-code'
    elif change == 'tag':
        value['language_tag'] = 'EN-gb'
    elif change == 'scope':
        value['language_scope'] = ''
    elif change == 'coverage':
        value['coverage'] = 'verified'
    elif change == 'app-version':
        value['context']['application']['version'] = ''
    elif change == 'app-hash':
        value['context']['application']['sha256'] = 'not-a-hash'
    elif change == 'app-na':
        value['context']['application'] = dict(name=None, version=None, sha256=None,
                                               not_applicable_reason='Missing application')
    elif change == 'engine-mixed':
        value['engine']['not_applicable_reason'] = 'Contradictory identity'
    elif change == 'pack-hash':
        value['pack']['sha256'] = True
    elif change == 'input-hash':
        value['test']['input_text'] += 'Changed retained input'
    elif change == 'outcome':
        value['test']['outcome'] = 'verified'
    elif change == 'declaration-pass':
        value['test']['kind'] = 'declaration'
    elif change == 'time':
        value['test']['performed_utc'] = '2026-02-30T00:00:00Z'
    elif change == 'executor':
        value['test']['executor']['source'] = ''
    elif change == 'oversize':
        value['test']['result'] = 'x' * 8193
    elif change == 'nested-review':
        value['test']['approved'] = True
    path = tmp_path / 'untrusted.json'
    raw = encode(value)
    path.write_bytes(raw)
    with pytest.raises((ValueError, TypeError)):
        import_observation(path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('raw', [
    b'{"schema":"one","schema":"two"}',
    b'{"schema":"carr-language-observation/1","number":NaN}',
    b'{"schema":"carr-language-observation/1","input":"\xff"}',
])
def test_import_uses_strict_real_json_snapshot_decoder(tmp_path, raw):
    path = tmp_path / 'untrusted.json'
    path.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        import_observation(path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('change', ['self', 'unknown', 'confirmation', 'independence'])
def test_review_binds_complete_observation_and_explicit_distinct_provenance(tmp_path, change):
    store, value, before, token = fixture.saved(tmp_path)
    raw = store.path.read_bytes()
    identity = value.fingerprint if change != 'unknown' else 'f' * 64
    reviewer = (replace(fixture.reviewer(), identity='ＦＩＸＴＵＲＥ-ＥＸＥＣＵＴＯＲ')
                if change == 'self' else fixture.reviewer())
    confirmation = 'ACCEPT ' + identity if change != 'confirmation' else 'ACCEPT another observation'
    with pytest.raises(ValueError):
        store.review(identity, reviewer, decision='accept', independent_assessment=change != 'independence',
            statement='Synthetic review only', confirmation=confirmation, expected_token=token)
    assert store.load() == (before, token) and store.path.read_bytes() == raw


@pytest.mark.parametrize('outcome,coverage,expected', [
    ('pass', 'full-selected-entry', 'verified'),
    ('pass', 'partial-variety', 'partial'),
    ('partial', 'full-selected-entry', 'partial'),
    ('fail', 'full-selected-entry', 'failed'),
    ('not_run', 'full-selected-entry', 'untested'),
])
def test_scoped_assessment_never_upgrades_partial_failed_or_unexecuted_tests(tmp_path, outcome, coverage, expected):
    value = fixture.observation(outcome=outcome, coverage=coverage)
    store, _, _, token = fixture.saved(tmp_path, value)
    snapshot, token = fixture.assess(store, value, token)
    assert status(snapshot, value) == expected
    assert matrix(snapshot).summary()['qualified_entries'] == 0
    assert matrix(snapshot, value.context.fingerprint).summary()['qualified_entries'] == 0
    detail = matrix(snapshot, value.context.fingerprint).search('Англійська')[0].capability('tts').detail
    assert value.context.fingerprint in detail and value.engine.sha256 in detail and value.pack.sha256 in detail
    assert 'not authenticated identities' in detail and 'no automatic execution' in detail
    revoked, token = fixture.assess(store, value, token, 'revoke')
    assert status(revoked, value) == 'revoked'
    assert store.add(value, expected_token=token) == (revoked, token)


@pytest.mark.parametrize('variant,qualified', [
    ('complete', 1), ('missing', 0), ('other-context', 0), ('other-language-scope', 0),
])
def test_all_twenty_two_capabilities_require_one_exact_selected_context_and_scope(variant, qualified):
    values = [fixture.observation(name) for name in module._CAPABILITIES]
    chosen = values[0].context.fingerprint
    if variant == 'missing':
        values.pop()
    elif variant == 'other-context':
        values[-1] = replace(values[-1], context=replace(values[-1].context,
                                                       environment_sha256='f' * 64))
    elif variant == 'other-language-scope':
        values[-1] = replace(values[-1], language_scope='A different exact variety')
    snapshot = EvidenceSnapshot(1, tuple(values), tuple(fixture.review(row) for row in values))
    assert matrix(snapshot).summary()['qualified_entries'] == 0
    selected = matrix(snapshot, chosen)
    assert selected.summary()['qualified_entries'] == qualified
    assert len(selected.entries) == 135
    assert all(len(row.capabilities) == 22 for row in selected.entries)
    with pytest.raises(ValueError):
        matrix(snapshot, 'e' * 64)


def test_real_token_cas_and_noop_preserve_files_and_detect_same_revision_changes(tmp_path):
    store, value, snapshot, token = fixture.saved(tmp_path)
    before = store.path.stat()
    assert store.add(value, expected_token=token) == (snapshot, token)
    after = store.path.stat()
    assert (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) == (
        after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
    changed = EvidenceSnapshot(snapshot.revision)
    raw = encode(changed.to_dict())
    store.path.write_bytes(raw)
    with pytest.raises(ValueError, match='changed'):
        store.add(fixture.observation('unicode_text'), expected_token=token)
    assert store.path.read_bytes() == raw
    assert store.load()[0] == changed


@pytest.mark.parametrize('part', ['library', 'state', 'namespace'])
def test_real_directory_substitution_is_not_silently_adopted(tmp_path, part):
    root = tmp_path / 'library'
    root.mkdir()
    store, value, snapshot, token = fixture.saved(root)
    before = store.path.read_bytes()
    selected = root if part == 'library' else root / 'state' if part == 'state' else store.path.parent
    held = selected.with_name(selected.name + '-original')
    selected.rename(held)
    shutil.copytree(held, selected)
    with pytest.raises(ValueError, match='changed'):
        fixture.assess(store, value, token)
    assert store.path.read_bytes() == before
    original = held / store.path.relative_to(selected)
    assert original.read_bytes() == before


@pytest.mark.parametrize('replacement', ['hardlink', 'directory'])
def test_linked_or_nonregular_evidence_is_refused_without_modification(tmp_path, replacement):
    store, value, _, token = fixture.saved(tmp_path)
    raw = store.path.read_bytes()
    if replacement == 'hardlink':
        other = tmp_path / 'other.json'
        os.link(store.path, other)
    else:
        store.path.unlink()
        store.path.mkdir()
    with pytest.raises((ValueError, OSError)):
        fixture.assess(store, value, token)
    if replacement == 'hardlink':
        assert store.path.read_bytes() == other.read_bytes() == raw
    else:
        assert store.path.is_dir() and not list(store.path.iterdir())


@pytest.mark.parametrize('kind', ['true', 'interrupt', 'timeout', 'keyboard'])
def test_original_cancellation_objects_leave_real_state_and_cleanup_unchanged(tmp_path, kind):
    store, _, snapshot, token = fixture.saved(tmp_path)
    before = store.path.read_bytes()
    stop = {'interrupt': InterruptedError('original stop'),
            'timeout': TimeoutError('original deadline'),
            'keyboard': KeyboardInterrupt('original interruption')}.get(kind)

    def cancelled():
        if stop is not None:
            raise stop
        return True

    with pytest.raises(type(stop) if stop else InterruptedError) as caught:
        store.add(fixture.observation('unicode_text'), expected_token=token, cancelled=cancelled)
    if stop is not None:
        assert caught.value is stop
    assert store.path.read_bytes() == before and store.load() == (snapshot, token)
    assert not list(store.path.parent.glob('.pending-*'))


@pytest.mark.parametrize('mutation', ['document', 'root'])
def test_final_callback_cannot_replace_state_or_scope_before_actual_publication(tmp_path, mutation):
    root = tmp_path / 'library'
    root.mkdir()
    store, value, snapshot, token = fixture.saved(root)
    snapshot, token = fixture.assess(store, value, token)
    assert status(snapshot, value) == 'verified' and len(snapshot.reviews) == 1
    before = store.path.read_bytes()
    root_info, state_info = root.stat(), store.path.stat()
    held_root = tmp_path / 'held-root'
    armed = False
    changed = False
    rename_denied = propagated_denial = None
    replacement = encode(EvidenceSnapshot(snapshot.revision + 7).to_dict())

    def operation(current):
        nonlocal armed
        armed = True
        return replace(current, revision=current.revision + 1,
                       observations=current.observations + (fixture.observation('unicode_text'),))

    def cancelled():
        nonlocal changed, rename_denied
        if armed and not changed:
            changed = True
            if mutation == 'document':
                store.path.write_bytes(replacement)
            else:
                try:
                    root.rename(held_root)
                except PermissionError as error:
                    # Windows may refuse the actual ancestor rename while the
                    # real state-lock handle is open. Retain only that exact denial.
                    if os.name != 'nt' or getattr(error, 'winerror', None) != 5:
                        raise
                    rename_denied = error
                    raise
                shutil.copytree(held_root, root)
        return False

    try:
        with pytest.raises(ValueError, match='changed'):
            store._change(operation, token, cancelled)
    except PermissionError as error:
        assert mutation == 'root' and error is rename_denied
        propagated_denial = error
    assert changed and propagated_denial is rename_denied
    assert store.path.read_bytes() == (replacement if mutation == 'document' else before)
    assert not list(store.path.parent.glob('.pending-*'))
    if rename_denied is not None:
        assert os.name == 'nt' and rename_denied.winerror == 5
        assert not held_root.exists()
        after_root, after_state = root.stat(), store.path.stat()
        assert (after_root.st_dev, after_root.st_ino) == (root_info.st_dev, root_info.st_ino)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
        assert tuple(getattr(after_state, name) for name in fields) == tuple(
            getattr(state_info, name) for name in fields)
        # The failed operation released its genuine lock and committed no candidate.
        with module.state_lock(store.path.parent, 'language-evidence'):
            restored, restored_token = store.load()
        assert (restored, restored_token) == (snapshot, token)
        assert restored.reviews == snapshot.reviews and status(restored, value) == 'verified'
        assert tuple(row.capability for row in restored.observations) == ('tts',)
    elif mutation == 'root':
        after_root = root.stat()
        assert (after_root.st_dev, after_root.st_ino) != (root_info.st_dev, root_info.st_ino)
        assert (held_root / store.path.relative_to(root)).read_bytes() == before
        with pytest.raises(ValueError, match='changed'):
            store.load()
        restored, restored_token = EvidenceStore(root).load()
        assert (restored, restored_token) == (snapshot, token)
        assert restored.reviews == snapshot.reviews and status(restored, value) == 'verified'


def test_visible_late_commit_is_reported_without_repeating_or_erasing_it(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path)
    value = fixture.observation()
    actual = module.atomic_bytes

    def late_failure(*args, **kwargs):
        actual(*args, **kwargs)
        raise OSError('Synthetic post-publication fsync failure')

    monkeypatch.setattr(module, 'atomic_bytes', late_failure)
    with pytest.raises(module.EvidenceSaveCommittedError) as caught:
        store.add(value, expected_token=None)
    snapshot, token = store.load()
    assert (snapshot, token) == (caught.value.snapshot, caught.value.token)
    assert not caught.value.durability_confirmed and snapshot.observations == (value,)
    assert store.add(value, expected_token=token) == (snapshot, token)
    assert not list(store.path.parent.glob('.pending-*'))


def test_review_history_preserves_all_prior_rejections_and_revocations(tmp_path):
    store, value, _, token = fixture.saved(tmp_path)
    decisions = ('accept', 'reject', 'revoke', 'accept')
    for decision in decisions:
        snapshot, token = fixture.assess(store, value, token, decision)
    assert tuple(row.decision for row in snapshot.reviews) == decisions
    assert status(snapshot, value) == 'verified'
    assert store.load() == (snapshot, token)
    forged = snapshot.to_dict()
    forged['reviews'][0]['observation_sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='missing'):
        EvidenceSnapshot.from_dict(forged)


@pytest.mark.parametrize('collection', ['observations', 'reviews'])
def test_collection_bounds_refuse_before_unbounded_history_processing(collection):
    value = fixture.observation()
    with pytest.raises(ValueError, match='allowance'):
        if collection == 'observations':
            EvidenceSnapshot(1, (value,) * (module.MAX_OBSERVATIONS + 1))
        else:
            EvidenceSnapshot(1, (value,), (fixture.review(value),) * (module.MAX_REVIEWS + 1))
