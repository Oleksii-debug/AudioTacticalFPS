"""Real source, permission and publication boundaries of the local assistant."""
from hashlib import sha256
import json
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.models import canonical
from carr_library.assistant.service import _hash_file
from carr_library.assistant.state import atomic_json, read_json


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected research')
    imported = app.add_text(workspace, 'Exact source', 'Залізниця connects these towns.', authors=('Writer',))
    other = app.create_workspace('Different research')
    app.add_text(other, 'Exact source', 'Залізниця must stay in the other library.')
    output = tmp_path / 'output'
    output.mkdir()
    return app, workspace, imported, output


def searched(source):
    app, workspace, imported, output = source
    service = AssistantService(app)
    plan = service.preview('Search "Залізниця"', Scope(workspace, output_root=output))
    identity = service.submit(plan)
    report = service.run(identity)
    assert report.status == 'complete', report
    return service, plan, identity, report


def test_search_owns_private_selection_cache_and_exact_source_receipt(source):
    app, workspace, imported, _ = source
    held, = app.search(workspace, 'towns')
    selections = dict(app._selections)
    service, _, _, report = searched(source)
    assert app._selections == selections
    hit, = report.results[0]['results']
    assert 'ref' not in hit
    assert hit['workspace'] == workspace and hit['verified'] is True
    anchor = hit['source_citation']
    assert anchor['identity']['asset_sha256'] == imported['asset']
    assert anchor['identity']['edition_id'] == imported['edition']
    assert anchor['identity']['workspace'] == workspace
    assert anchor['quote'] == hit['quote'] == 'Залізниця'
    assert app.copy(workspace, held.ref, held.revision, 'quote') == 'towns'
    assert service._search.current is None


def test_saved_search_is_historical_and_resume_resolves_actual_source(source):
    app, _, imported, _ = source
    service, _, identity, report = searched(source)
    saved = service.get(identity).results[0]
    assert saved['source_status'] == 'historical_unchecked'
    assert saved['results'][0]['verified_at_capture'] is True
    assert saved['results'][0]['verified'] is False
    path = app.assets.resolve(imported['asset'])
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    resumed = service.resume(identity)
    assert resumed.status == 'failed' and not resumed.results
    assert report.results[0]['results'][0]['verified'] is True  # The prior report was not mutated.


def test_resume_refreshes_metadata_and_never_relabels_old_receipts(source):
    app, workspace, imported, _ = source
    service, _, identity, first = searched(source)
    app.update_metadata(workspace, imported['edition'], title='Explicit new title', authors=('Different writer',))
    refreshed = service.resume(identity)
    assert refreshed.status == 'complete', refreshed
    assert refreshed.results[0]['results'][0]['title'] == 'Explicit new title'
    assert first.results[0]['results'][0]['title'] == 'Exact source'
    assert refreshed.results[0]['results'][0]['source_citation']['metadata']['title'] == 'Explicit new title'


def test_reopened_executor_requires_reviewed_fingerprint(source):
    app, workspace, _, output = source
    original = AssistantService(app)
    plan = original.preview('List books', Scope(workspace, output_root=output))
    identity = original.submit(plan)
    reopened = AssistantService(LibraryApp(app.root))
    with pytest.raises(PermissionError, match='fingerprint'):
        reopened.resume(identity)
    reviewed = reopened.review(identity)
    assert reviewed.digest == plan.digest
    assert reopened.resume(identity, approved_plan_sha256=reviewed.digest).status == 'complete'


@pytest.mark.parametrize('new_executor', [False, True])
def test_recomputed_checkpoint_digest_cannot_replace_reviewed_approval(source, new_executor):
    app, workspace, _, output = source
    service = AssistantService(app)
    plan = service.preview('List books', Scope(workspace, output_root=output))
    identity = service.submit(plan)
    record = service.store.load(identity)
    record['plan']['steps'][0] = {'tool': 'library.search', 'arguments': {'query': 'unreviewed'}}
    record['plan_sha256'] = sha256(canonical(record['plan']).encode()).hexdigest()
    service.store.save(record)
    executor = AssistantService(app) if new_executor else service
    with pytest.raises(PermissionError, match='fingerprint'):
        executor.run(identity, approved_plan_sha256=plan.digest if new_executor else None)
    assert service.store.load(identity)['status'] == 'queued'


def test_mutable_preview_arguments_do_not_override_captured_digest(source):
    app, workspace, _, output = source
    service = AssistantService(app)
    plan = service.preview('Search "towns"', Scope(workspace, output_root=output))
    reviewed = plan.digest
    plan.steps[0].arguments['query'] = 'not reviewed'
    with pytest.raises(PermissionError, match='previewed'):
        service.submit(plan, approved_plan_sha256=reviewed)
    assert not service.jobs()


def test_reentrant_data_root_change_cancels_before_creating_library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    other = LibraryApp(tmp_path / 'other')
    service = AssistantService(app)
    identity = service.submit(service.preview('Create library "Must not exist"', Scope(allow_create=True)))
    original = app.root
    def changed():
        app.root = other.root
        return False
    try:
        with pytest.raises(PermissionError, match='directory changed'):
            service.run(identity, cancelled=changed)
    finally:
        app.root = original
    assert not app.workspaces() and not other.workspaces()


def test_cancel_inside_creation_rolls_back_the_actual_transaction(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('Create library "Must not exist"', Scope(allow_create=True)))
    calls = []
    def stopped():
        calls.append(True)
        return len(calls) == 2
    result = service.run(identity, cancelled=stopped)
    assert result.status == 'paused' and result.completed == 0
    assert len(calls) == 2 and not app.workspaces()


def test_sticky_cancellation_does_not_resume_when_callback_returns_false_again(source):
    app, workspace, _, output = source
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace, output_root=output)))
    answers = iter([True, False, False])
    report = service.run(identity, cancelled=lambda: next(answers))
    assert report.status == 'paused' and report.completed == 0


def test_snapshot_reader_rejects_ambiguous_duplicate_control_fields(tmp_path):
    path = tmp_path / 'control.json'
    path.write_bytes(b'{"command":"run","command":"cancel"}')
    with pytest.raises(ValueError, match='Duplicate'):
        read_json(path)


def test_artifact_modification_during_hashing_is_rejected(tmp_path):
    path = tmp_path / 'artifact.bin'
    path.write_bytes(b'a' * 8192)
    changed = False
    def mutate():
        nonlocal changed
        if not changed:
            path.write_bytes(b'b' * 8192)
            changed = True
        return False
    with pytest.raises(ValueError, match='changed'):
        _hash_file(path, mutate)


def test_json_plan_rejects_duplicate_tool_fields():
    with pytest.raises(ValueError, match='Duplicate'):
        Plan.from_json('{"steps":[{"tool":"library.list","tool":"library.create","arguments":{"name":"x"}}]}',
                       scope=Scope(allow_create=True))


@pytest.mark.parametrize('tool', ['library.extract', 'library.backup'])
def test_parent_output_scope_cannot_write_back_into_private_library(source, tool):
    app, workspace, imported, _ = source
    service = AssistantService(app)
    scope = Scope(workspace, output_root=app.root.parent, allow_full_backup=True)
    args = {'name': app.root.name + '/new-output'}
    if tool == 'library.extract':
        args['edition'] = imported['edition']
    with pytest.raises(PermissionError, match='outside the private'):
        service.submit(Plan(scope, (Step(tool, args),)))
    assert not (app.root / 'new-output').exists() and not service.jobs()
