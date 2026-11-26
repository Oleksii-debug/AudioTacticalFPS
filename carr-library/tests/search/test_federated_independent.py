"""Independent action/session acceptance over real private libraries and sources."""
from dataclasses import replace
from threading import Event, Thread

import pytest

from carr_library.application import LibraryApp
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.search.federated import FederatedSearch


@pytest.fixture
def libraries(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspaces = (app.create_workspace('First library'), app.create_workspace('Second library'))
    topics = tuple(app.create_topic(workspace, 'Evidence') for workspace in workspaces)
    for number, workspace in enumerate(workspaces, 1):
        # Identical catalog IDs and title deliberately do not identify a library.
        app.add_text(workspace, 'Shared title',
                     f'Before. Shared railway history. Original ending {number}.',
                     authors=(f'Author {number}',), work_id='work', edition_id='edition')
    return app, workspaces, topics


def test_actions_require_the_exact_session_handle(libraries):
    app, workspaces, topics = libraries
    controller = FederatedSearch(app)
    session = controller.search(workspaces, 'railway history')
    hit = session.groups[0].hits[0]
    other = FederatedSearch(app)
    other.search(workspaces, 'railway history')
    forged = (
        replace(hit),
        replace(hit, result=replace(hit.result, workspace=workspaces[1])),
        replace(hit, result=replace(hit.result, quote='A substituted quotation')),
        replace(hit, session_id=other.current.id),
        hit.result,
    )
    actions = (controller.context, controller.preview, controller.copy, controller.topics,
               lambda row: controller.save_hit(row, topics[0]))
    for candidate in forged:
        for action in actions:
            with pytest.raises(ValueError, match='latest completed search session'):
                action(candidate)
    with pytest.raises(ValueError, match='latest completed search session'):
        other.copy(hit)
    assert controller.copy(hit, 'quote') == 'railway history'
    assert all(app.evidence(workspace, topic) == ()
               for workspace, topic in zip(workspaces, topics))


def test_destination_topic_identity_is_resolved_in_the_origin_library(libraries):
    app, workspaces, topics = libraries
    for workspace in workspaces:
        with app.topic_transaction(workspace) as tree:
            tree.create('same-topic-id', 'A local topic')
    controller = FederatedSearch(app)
    hit = controller.search(workspaces, 'railway history').groups[0].hits[0]
    assert {topic.id for topic in controller.topics(hit)} == {topics[0], 'same-topic-id'}
    with pytest.raises(ValueError):
        controller.save_hit(hit, topics[1])
    evidence = controller.save_hit(hit, 'same-topic-id', 'Independent research note')
    saved, = app.evidence(workspaces[0], 'same-topic-id')
    assert saved.evidence_id == evidence
    assert saved.anchor.identity.workspace == workspaces[0]
    assert saved.anchor.metadata.author == 'Author 1'
    assert saved.anchor.quote == 'railway history'
    assert app.evidence(workspaces[1], 'same-topic-id') == ()
    assert app.evidence(workspaces[1], topics[1]) == ()


@pytest.mark.parametrize('operation', ('context', 'preview', 'copy', 'topics', 'save_hit'))
def test_reentrant_clear_invalidates_an_action_even_if_callback_returns_false(libraries, operation):
    app, workspaces, topics = libraries
    controller = FederatedSearch(app)
    hit = controller.search(workspaces, 'railway history').hits[0]

    def clear_without_reporting_cancelled():
        controller.clear()
        return False

    args = (hit, topics[0]) if operation == 'save_hit' else (hit,)
    with pytest.raises(InterruptedError):
        getattr(controller, operation)(*args, cancelled=clear_without_reporting_cancelled)
    assert controller.current is None
    assert app.evidence(workspaces[0], topics[0]) == ()


def test_clear_after_evidence_insertion_rolls_back_the_save(libraries, monkeypatch):
    app, workspaces, topics = libraries
    controller = FederatedSearch(app)
    hit = controller.search(workspaces, 'railway history').hits[0]
    inserted = False
    original = SavedEvidenceStore.save

    def observe_insert(store, *args, **kwargs):
        nonlocal inserted
        result = original(store, *args, **kwargs)
        inserted = True
        return result

    monkeypatch.setattr(SavedEvidenceStore, 'save', observe_insert)

    def clear_when_the_transaction_contains_evidence():
        if inserted:
            controller.clear()
        return False

    with pytest.raises(InterruptedError):
        controller.save_hit(hit, topics[0], cancelled=clear_when_the_transaction_contains_evidence)
    assert inserted, 'The test must reach the transactional evidence insertion.'
    assert controller.current is None
    assert app.evidence(workspaces[0], topics[0]) == ()


def test_clear_during_the_last_library_prevents_publication(libraries, monkeypatch):
    app, workspaces, _ = libraries
    controller = FederatedSearch(app)
    previous = controller.search(workspaces, 'railway history')
    entered, resume = Event(), Event()
    original = LibraryApp.search
    outcome = []

    def delayed_search(working, workspace, *args, **kwargs):
        result = original(working, workspace, *args, **kwargs)
        if workspace == workspaces[-1]:
            entered.set()
            if not resume.wait(5):
                raise RuntimeError('Independent test rendezvous timed out.')
        return result

    monkeypatch.setattr(LibraryApp, 'search', delayed_search)

    def run():
        try:
            outcome.append(controller.search(workspaces, 'railway history'))
        except Exception as exc:
            outcome.append(exc)

    worker = Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5), 'The final scope must finish before invalidation.'
        controller.clear()
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)
    assert controller.current is None
    with pytest.raises(ValueError, match='latest completed search session'):
        controller.copy(previous.hits[0])


def test_status_callback_clear_cannot_republish_the_session(libraries):
    app, workspaces, _ = libraries
    controller = FederatedSearch(app)
    controller.search(workspaces, 'railway history')
    messages = []

    def on_status(message):
        messages.append(message)
        controller.clear()

    # No configured model invokes the real host's explicit degradation status.
    with pytest.raises(TimeoutError):
        controller.search(workspaces, 'railway history', mode='semantic', on_status=on_status)
    assert messages and controller.current is None


def test_status_callback_failure_preserves_previous_and_releases_search_lock(libraries):
    app, workspaces, _ = libraries
    controller = FederatedSearch(app)
    previous = controller.search(workspaces, 'railway history')
    statuses = []

    def on_status(message):
        statuses.append(message)
        if len(statuses) == 2:
            raise RuntimeError('The status consumer failed.')

    with pytest.raises(RuntimeError, match='status consumer failed'):
        controller.search(workspaces, 'railway history', mode='semantic', on_status=on_status)
    assert len(statuses) == 2
    assert controller.current is previous
    assert controller.copy(previous.hits[0], 'quote') == 'railway history'
    replacement = controller.search(workspaces, 'railway history')
    assert controller.current is replacement and replacement.id != previous.id


@pytest.mark.parametrize('attribute', ('root', 'database', 'assets_dir', 'assets.root'))
def test_data_location_drift_rejects_actions_before_opening_another_path(
        libraries, tmp_path, monkeypatch, attribute):
    app, workspaces, topics = libraries
    controller = FederatedSearch(app)
    hit = controller.search(workspaces, 'railway history').hits[0]
    destination = tmp_path / 'must-not-be-created'
    if attribute == 'assets.root':
        monkeypatch.setattr(app.assets, 'root', destination)
    else:
        monkeypatch.setattr(app, attribute, destination)
    actions = (lambda: controller.copy(hit), lambda: controller.preview(hit),
               lambda: controller.topics(hit), lambda: controller.save_hit(hit, topics[0]),
               lambda: controller.search(workspaces, 'railway history'))
    for action in actions:
        with pytest.raises(ValueError, match='data directory changed'):
            action()
    assert not destination.exists()


@pytest.mark.parametrize('action_phase', ('before_search', 'before_publication'))
def test_search_does_not_wait_for_a_paused_source_action(
        libraries, monkeypatch, action_phase):
    app, workspaces, _ = libraries
    controller = FederatedSearch(app)
    previous = controller.search(workspaces, 'railway history')
    action_entered, release_action = Event(), Event()
    final_scope_ready, release_search, search_finished = Event(), Event(), Event()
    action_outcome, search_outcome = [], []
    original = LibraryApp.search

    def pause_after_the_final_scope(working, workspace, *args, **kwargs):
        rows = original(working, workspace, *args, **kwargs)
        if workspace == workspaces[-1]:
            final_scope_ready.set()
            if not release_search.wait(5):
                raise RuntimeError('Final search scope rendezvous timed out.')
        return rows

    def pause_action():
        action_entered.set()
        if not release_action.wait(5):
            raise RuntimeError('Source action rendezvous timed out.')
        return False

    def run_action():
        try:
            action_outcome.append(controller.copy(previous.hits[0], 'quote',
                                                  cancelled=pause_action))
        except Exception as exc:
            action_outcome.append(exc)

    def run_search():
        try:
            search_outcome.append(controller.search(workspaces, 'railway history', timeout=4.0))
        except Exception as exc:
            search_outcome.append(exc)
        finally:
            search_finished.set()

    action = Thread(target=run_action)
    search = Thread(target=run_search)
    started = []
    try:
        if action_phase == 'before_publication':
            monkeypatch.setattr(LibraryApp, 'search', pause_after_the_final_scope)
            search.start()
            started.append(search)
            assert final_scope_ready.wait(2), 'The last scope must reach its publication barrier.'
        action.start()
        started.append(action)
        assert action_entered.wait(2), 'The source action must own the completed session.'
        if action_phase == 'before_search':
            search.start()
            started.append(search)
        else:
            release_search.set()

        # Completion must happen while the action still holds session ownership.
        # This checks lock independence without asserting a scheduler-sensitive duration.
        assert search_finished.wait(2), 'A search must fail promptly instead of waiting behind an action.'
        assert not release_action.is_set()
        assert len(search_outcome) == 1
        assert isinstance(search_outcome[0], RuntimeError)
        assert 'action is running' in str(search_outcome[0])
    finally:
        release_search.set()
        release_action.set()
        for worker in started:
            worker.join(5)
    assert all(not worker.is_alive() for worker in started)
    assert action_outcome == ['railway history']
    assert controller.current is previous
    assert controller.copy(previous.hits[0], 'quote') == 'railway history'

    # A rejected start/publication must also release the outer search lock.
    replacement = controller.search(workspaces, 'railway history')
    assert replacement is controller.current and replacement.id != previous.id
