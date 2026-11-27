"""Real draft completion, failure and re-entry contracts for Save All."""
import pytest

from carr_library.ui.save_all import SaveCoordinator, SaveResult


def test_coordinator_follows_native_parent_links_and_rejects_a_cycle():
    from types import SimpleNamespace
    from carr_library.ui.save_all import coordinator_for
    coordinator = SaveCoordinator()
    root = SimpleNamespace(_save_coordinator=coordinator)
    child = SimpleNamespace(GetParent=lambda: root)
    assert coordinator_for(child) is coordinator
    root = SimpleNamespace(GetParent=lambda: child)
    assert coordinator_for(child) is None


def test_many_clean_documents_do_not_exhaust_the_ui_stack():
    coordinator = SaveCoordinator()
    for number in range(2500):
        coordinator.register(str(number), f'Document {number}', dirty=lambda: False,
                             save=lambda completed: None)
    summaries = []
    assert coordinator.save_all(summaries.append)
    assert len(summaries) == 1
    assert summaries[0].complete
    assert not coordinator.busy


def test_many_synchronous_durable_saves_finish_once_without_recursion():
    coordinator = SaveCoordinator()
    dirty = set(range(2500))
    for number in range(2500):
        def save(completed, number=number):
            dirty.remove(number)
            completed(SaveResult(True))
        coordinator.register(str(number), f'Document {number}',
                             dirty=lambda number=number: number in dirty, save=save)
    summaries = []
    assert coordinator.save_all(summaries.append)
    assert len(summaries) == 1
    assert summaries[0].complete
    assert len(summaries[0].saved) == 2500
    assert not dirty


def test_cancel_check_failure_releases_busy_and_retains_drafts():
    coordinator = SaveCoordinator()
    coordinator.register('a', 'Research note', dirty=lambda: True,
                         save=lambda completed: (_ for _ in ()).throw(AssertionError('must not save')))
    summaries = []
    def broken_cancel():
        raise OSError('Cancellation state unavailable')
    assert coordinator.save_all(summaries.append, cancelled=broken_cancel)
    assert not coordinator.busy
    assert len(summaries) == 1
    assert not summaries[0].complete
    assert summaries[0].cancelled
    assert any(label == 'Research note' for label, _ in summaries[0].remaining)


def test_async_completion_waits_for_durable_result_and_preserves_newer_edit():
    coordinator = SaveCoordinator()
    callbacks, summaries = [], []
    dirty = {'note': True}
    coordinator.register('note', 'Note', dirty=lambda: dirty['note'], save=callbacks.append)
    assert coordinator.save_all(summaries.append)
    assert coordinator.busy and summaries == []
    assert not coordinator.save_all(summaries.append)
    # A durable older snapshot was written, but the user typed a newer draft.
    callbacks[0](SaveResult(True))
    callbacks[0](SaveResult(True))
    assert len(summaries) == 1
    assert summaries[0].saved == ('Note',)
    assert summaries[0].remaining == (('Note', 'newer or unsaved edits are retained'),)
    assert not summaries[0].complete


def test_failure_does_not_skip_other_drafts_or_claim_all_saved():
    coordinator = SaveCoordinator()
    focused, summaries = [], []
    dirty = {'good': True}
    coordinator.register('bad', 'Audio project', dirty=lambda: True,
        save=lambda completed: completed(SaveResult(False, 'Destination is read only')),
        focus=lambda: focused.append('bad'))
    def save_good(completed):
        dirty['good'] = False
        completed(SaveResult(True))
    coordinator.register('good', 'Reference note', dirty=lambda: dirty['good'], save=save_good)
    coordinator.save_all(summaries.append)
    assert summaries[0].saved == ('Reference note',)
    assert summaries[0].remaining == (('Audio project', 'Destination is read only'),)
    assert 'All ' not in summaries[0].message()
    assert focused == ['bad']


def test_newly_opened_dirty_document_prevents_false_complete():
    coordinator = SaveCoordinator()
    callbacks, summaries = [], []
    dirty = {'old': True}
    coordinator.register('old', 'Old', dirty=lambda: dirty['old'], save=callbacks.append)
    coordinator.save_all(summaries.append)
    coordinator.register('new', 'New', dirty=lambda: True, save=callbacks.append)
    dirty['old'] = False
    callbacks[0](SaveResult(True))
    assert not summaries[0].complete
    assert ('New', 'newer or unsaved edits are retained') in summaries[0].remaining


def test_cancellation_after_one_completed_draft_reports_remaining():
    coordinator = SaveCoordinator()
    cancelled, dirty, summaries = [False], {'a': True, 'b': True}, []
    def save_a(completed):
        dirty['a'] = False
        cancelled[0] = True
        completed(SaveResult(True))
    coordinator.register('a', 'A', dirty=lambda: dirty['a'], save=save_a)
    coordinator.register('b', 'B', dirty=lambda: dirty['b'], save=lambda done: None)
    coordinator.save_all(summaries.append, cancelled=lambda: cancelled[0])
    assert summaries[0].saved == ('A',)
    assert summaries[0].cancelled and not summaries[0].complete
    assert summaries[0].remaining[0][0] == 'B'


def test_unregister_is_lease_scoped_and_replacement_is_not_lost():
    coordinator = SaveCoordinator()
    old_unregister = coordinator.register('a', 'Old', dirty=lambda: True, save=lambda done: None)
    coordinator.register('a', 'New', dirty=lambda: True, save=lambda done: done(SaveResult(False, 'New draft')))
    old_unregister()
    summaries = []
    coordinator.save_all(summaries.append)
    assert summaries[0].remaining == (('New', 'New draft'),)


def test_truthy_return_is_not_a_completed_save():
    coordinator = SaveCoordinator()
    coordinator.register('a', 'A', dirty=lambda: True, save=lambda done: True)
    summaries = []
    coordinator.save_all(summaries.append)
    assert coordinator.busy
    assert summaries == []


def _retained_reader_participant(values):
    """Use the actual reader's canonical save callbacks without a native modal."""
    import ast
    from pathlib import Path
    from carr_library.ui import speech_dialog

    module = ast.parse(Path(speech_dialog.__file__).read_text(encoding='utf-8'))
    host = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                and node.name == 'show_speech_reader')
    names = {'unsaved', 'save_registered', 'focus_registered'}
    definitions = [node for node in host.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    namespace = {'SaveResult': SaveResult, **values}
    exec(compile(ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[])),
                 speech_dialog.__file__, 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('blocked', ['none', 'source-worker', 'live-reader', 'closed'])
def test_retained_reading_copy_save_all_waits_for_the_same_owner_and_durable_save(tmp_path, blocked):
    from types import SimpleNamespace
    from carr_library.speech.reading import ReadingDocument
    from carr_library.speech.session import ReaderDraftStore, ReaderResume

    document = ReadingDocument('Exact source text')
    document.set_text('Edited reading copy')
    store = ReaderDraftStore(tmp_path)
    saves, focus, summaries = [], [], []
    def save_project():
        store.save(ReaderResume(document))
        document._saved_signature = document._signature()
        saves.append(True)
        return True
    namespace = _retained_reader_participant(dict(
        alive=blocked != 'closed', source_loaded=True, document=document,
        busy=blocked == 'source-worker', live=SimpleNamespace(core=SimpleNamespace(busy=blocked == 'live-reader')),
        sync_document=lambda: None, save_project=save_project, dialog=object(),
        editor=SimpleNamespace(SetFocus=lambda: focus.append(True))))
    coordinator = SaveCoordinator()
    unregister = coordinator.register('reading.exact', 'Voice reading copy',
        dirty=namespace['unsaved'], save=namespace['save_registered'], focus=namespace['focus_registered'])
    assert coordinator.save_all(summaries.append)
    if blocked == 'none':
        recovered = store.load()
        assert recovered.document.text == 'Edited reading copy' and recovered.document.undo()
        assert recovered.document.text == 'Exact source text'
        assert summaries[0].complete and saves == [True] and not document.dirty
    else:
        assert not store.path.exists() and not saves and document.dirty
        assert summaries[0].complete is (blocked == 'closed')
        if blocked != 'closed':
            assert 'Stop the current reading operation' in summaries[0].message()
    unregister()
    namespace['alive'] = False
    namespace['sync_document'] = lambda: pytest.fail('A detached reader touched its editor.')
    namespace['focus_registered']()
    assert not namespace['unsaved']() and coordinator.participants == {}
