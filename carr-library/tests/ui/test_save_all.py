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



def pending_save():
    from types import SimpleNamespace
    coordinator = SaveCoordinator()
    state = SimpleNamespace(dirty=True, alive=True)
    callbacks, summaries = [], []
    coordinator.register('note', 'Note', dirty=lambda: state.dirty, save=callbacks.append)
    assert coordinator.save_all(summaries.append)
    return coordinator, state, callbacks, summaries


def test_close_waits_for_successful_completion_and_rechecks_at_dispatch(tmp_path):
    from types import SimpleNamespace
    from carr_library.ui.save_all import defer_close
    coordinator, state, callbacks, summaries = pending_save()
    owner, posted, closed, messages = SimpleNamespace(), [], [], []
    event = SimpleNamespace(CanVeto=lambda: True, Veto=lambda: messages.append('veto'))
    options = dict(close=lambda: closed.append(True), report=messages.append,
                   alive=lambda: state.alive, post=posted.append, event=event)
    assert defer_close(owner, coordinator, **options)
    assert defer_close(owner, coordinator, **options)
    assert not posted and not closed and len(coordinator._completion_observers) == 1
    destination = tmp_path / 'saved.txt'
    destination.write_text('Durable draft', encoding='utf-8')
    state.dirty = False
    callbacks[0](SaveResult(True))
    callbacks[0](SaveResult(True))
    assert len(summaries) == 1 and len(posted) == 1 and not closed
    assert destination.read_text(encoding='utf-8') == 'Durable draft'
    posted.pop()()
    assert closed == [True] and not owner._save_close_pending


def test_failed_save_cancels_pending_close_and_preserves_dirty_state():
    from types import SimpleNamespace
    from carr_library.ui.save_all import defer_close
    coordinator, state, callbacks, _ = pending_save()
    owner, posted, closed, messages = SimpleNamespace(), [], [], []
    assert defer_close(owner, coordinator, close=lambda: closed.append(True),
        report=messages.append, alive=lambda: state.alive, post=posted.append)
    callbacks[0](SaveResult(False, 'Destination changed in another window'))
    assert not posted and not closed and state.dirty and not owner._save_close_pending
    assert 'Destination changed' in messages[-1] and 'remains open' in messages[-1]


def test_edit_between_save_confirmation_and_close_dispatch_keeps_window_open():
    from types import SimpleNamespace
    from carr_library.ui.save_all import defer_close
    coordinator, state, callbacks, _ = pending_save()
    posted, closed, messages = [], [], []
    defer_close(SimpleNamespace(), coordinator, close=lambda: closed.append(True),
                report=messages.append, alive=lambda: state.alive, post=posted.append)
    state.dirty = False
    callbacks[0](SaveResult(True))
    state.dirty = True
    posted.pop()()
    assert not closed
    assert 'newer unsaved' in messages[-1]


def test_late_save_completion_never_touches_a_destroyed_window():
    from types import SimpleNamespace
    from carr_library.ui.save_all import defer_close
    coordinator, state, callbacks, _ = pending_save()
    posted, closed, messages = [], [], []
    defer_close(SimpleNamespace(), coordinator, close=lambda: closed.append(True),
                report=messages.append, alive=lambda: state.alive, post=posted.append)
    messages.clear()
    state.alive, state.dirty = False, False
    callbacks[0](SaveResult(True))
    assert not posted and not closed and not messages and not coordinator.busy


def test_late_close_dispatch_never_touches_a_destroyed_window():
    from types import SimpleNamespace
    from carr_library.ui.save_all import defer_close
    coordinator, state, callbacks, _ = pending_save()
    posted, closed, messages = [], [], []
    defer_close(SimpleNamespace(), coordinator, close=lambda: closed.append(True),
                report=messages.append, alive=lambda: state.alive, post=posted.append)
    state.dirty = False
    callbacks[0](SaveResult(True))
    state.alive = False
    messages.clear()
    posted.pop()()
    assert not closed and not messages


def test_close_validation_failure_is_visible_and_cannot_destroy_the_window():
    from types import SimpleNamespace
    from carr_library.ui.save_all import defer_close
    coordinator, state, callbacks, _ = pending_save()
    posted, closed, messages = [], [], []
    defer_close(SimpleNamespace(), coordinator, close=lambda: closed.append(True),
                report=messages.append, alive=lambda: state.alive, post=posted.append)
    state.dirty = False
    callbacks[0](SaveResult(True))
    def unavailable():
        raise OSError('Saved source scope is unavailable')
    coordinator.register('scope', 'Source', dirty=unavailable, save=lambda done: None)
    posted.pop()()
    assert not closed and 'could not be verified' in messages[-1]


def test_forced_shutdown_is_not_falsely_claimed_to_be_delayed():
    from types import SimpleNamespace
    from carr_library.ui.save_all import defer_close
    coordinator, state, _, _ = pending_save()
    called = []
    event = SimpleNamespace(CanVeto=lambda: False, Veto=lambda: called.append('veto'))
    assert not defer_close(SimpleNamespace(), coordinator, close=lambda: called.append('close'),
                           report=called.append, alive=lambda: state.alive,
                           post=called.append, event=event)
    assert not called and not coordinator._completion_observers


def test_late_cancel_after_final_durable_save_is_complete():
    coordinator = SaveCoordinator()
    dirty, stop, summaries = [True], [False], []
    def save(done):
        dirty[0] = False
        stop[0] = True
        done(SaveResult(True))
    coordinator.register('one', 'Transcript', dirty=lambda: dirty[0], save=save)
    assert coordinator.save_all(summaries.append, cancelled=lambda: stop[0])
    assert summaries[0].complete and not summaries[0].cancelled
    assert summaries[0].saved == ('Transcript',)
    assert not coordinator.busy


def test_late_cancel_with_clean_tail_is_complete():
    coordinator = SaveCoordinator()
    dirty, stop, summaries = [True], [False], []
    callbacks = []
    coordinator.register('one', 'Note', dirty=lambda: dirty[0], save=callbacks.append)
    coordinator.register('two', 'Clean reader', dirty=lambda: False, save=lambda done: None)
    coordinator.save_all(summaries.append, cancelled=lambda: stop[0])
    dirty[0] = False
    stop[0] = True
    callbacks[0](SaveResult(True))
    assert summaries[0].complete and not summaries[0].cancelled


def test_cancel_before_saving_clean_drafts_does_not_invent_unsaved_changes():
    coordinator = SaveCoordinator()
    summaries = []
    coordinator.register('clean', 'Clean reader', dirty=lambda: False, save=lambda done: None)
    coordinator.save_all(summaries.append, cancelled=lambda: True)
    assert summaries[0].cancelled and not summaries[0].complete
    assert not summaries[0].remaining
    assert 'Unsaved changes remain:' not in summaries[0].message()


def test_cancel_with_newer_edits_still_reports_unsaved_changes():
    coordinator = SaveCoordinator()
    callbacks, summaries, stop = [], [], [False]
    coordinator.register('one', 'Note', dirty=lambda: True, save=callbacks.append)
    coordinator.save_all(summaries.append, cancelled=lambda: stop[0])
    stop[0] = True
    callbacks[0](SaveResult(True))
    assert summaries[0].cancelled and not summaries[0].complete
    assert summaries[0].remaining


def test_cancel_after_failed_save_not_misreported_as_complete():
    coordinator = SaveCoordinator()
    summaries, stop = [], [False]
    def save(done):
        stop[0] = True
        done(SaveResult(False, 'write failed'))
    coordinator.register('one', 'Note', dirty=lambda: True, save=save)
    coordinator.save_all(summaries.append, cancelled=lambda: stop[0])
    assert summaries[0].cancelled and not summaries[0].complete
    assert summaries[0].remaining == (('Note', 'write failed'),)


def test_failed_old_save_does_not_hide_replacement_draft():
    coordinator = SaveCoordinator()
    callbacks, summaries, focused = [], [], []
    unregister = coordinator.register('reader', 'Old reader', dirty=lambda: True,
                                      save=callbacks.append, focus=lambda: focused.append('old'))
    assert coordinator.save_all(summaries.append)
    coordinator.register('reader', 'New reader', dirty=lambda: True,
                         save=lambda done: None, focus=lambda: focused.append('new'))
    unregister()
    callbacks[0](SaveResult(False, 'old write failed'))
    assert len(summaries) == 1 and not summaries[0].complete
    assert summaries[0].remaining == (('Old reader', 'old write failed'),
                                      ('New reader', 'newer or unsaved edits are retained'))
    assert focused == ['new']
    assert not coordinator.busy


def test_failed_old_save_does_not_focus_clean_replacement():
    coordinator = SaveCoordinator()
    callbacks, summaries, focused = [], [], []
    coordinator.register('reader', 'Old reader', dirty=lambda: True, save=callbacks.append)
    assert coordinator.save_all(summaries.append)
    coordinator.register('reader', 'Clean reader', dirty=lambda: False,
                         save=lambda done: None, focus=lambda: focused.append('clean'))
    callbacks[0](SaveResult(False, 'old write failed'))
    assert summaries[0].remaining == (('Old reader', 'old write failed'),)
    assert focused == []


def test_queued_replacement_is_reported_separately_and_never_saved_under_old_registration():
    coordinator = SaveCoordinator()
    callbacks, summaries, focused, attempts = [], [], [], []
    first_dirty = [True]
    coordinator.register('first', 'First', dirty=lambda: first_dirty[0], save=callbacks.append)
    coordinator.register('reader', 'Old reader', dirty=lambda: True,
                         save=lambda done: attempts.append('old'))
    assert coordinator.save_all(summaries.append)
    coordinator.register('reader', 'New reader', dirty=lambda: True,
                         save=lambda done: attempts.append('new'), focus=lambda: focused.append('new'))
    first_dirty[0] = False
    callbacks[0](SaveResult(True))
    assert summaries[0].saved == ('First',)
    assert summaries[0].remaining == (('Old reader', 'the open document changed during saving'),
                                      ('New reader', 'newer or unsaved edits are retained'))
    assert focused == ['new'] and attempts == []


def test_focus_rechecks_registration_after_completion_notification():
    coordinator = SaveCoordinator()
    focused = []
    coordinator.register('reader', 'Old reader', dirty=lambda: True,
                         save=lambda done: done(SaveResult(False, 'failed')),
                         focus=lambda: focused.append('old'))
    def completed(summary):
        assert not summary.complete
        coordinator.register('reader', 'Clean reader', dirty=lambda: False,
                             save=lambda done: None, focus=lambda: focused.append('clean'))
    assert coordinator.save_all(completed)
    assert focused == []


def test_live_failed_registration_retains_its_own_focus():
    coordinator = SaveCoordinator()
    focused, summaries = [], []
    coordinator.register('reader', 'Reader', dirty=lambda: True,
                         save=lambda done: done(SaveResult(False, 'failed')),
                         focus=lambda: focused.append('reader'))
    assert coordinator.save_all(summaries.append)
    assert summaries[0].remaining == (('Reader', 'failed'),)
    assert focused == ['reader']



@pytest.mark.parametrize('first_mode,expected', [('none', ['second']), ('raises', ['second']), ('works', ['first'])])
def test_focus_chooses_first_usable_live_failed_draft(first_mode, expected):
    coordinator = SaveCoordinator()
    focused, summaries = [], []
    def unavailable():
        raise RuntimeError('Focus target unavailable')
    focus = {'none': None, 'raises': unavailable, 'works': lambda: focused.append('first')}[first_mode]
    coordinator.register('first', 'First note', dirty=lambda: True,
                         save=lambda done: done(SaveResult(False, 'first failed')), focus=focus)
    coordinator.register('second', 'Second note', dirty=lambda: True,
                         save=lambda done: done(SaveResult(False, 'second failed')),
                         focus=lambda: focused.append('second'))
    assert coordinator.save_all(summaries.append)
    assert len(summaries) == 1 and not coordinator.busy
    assert summaries[0].remaining == (('First note', 'first failed'), ('Second note', 'second failed'))
    assert focused == expected


def test_focus_skips_replaced_registration_and_unavailable_target():
    coordinator = SaveCoordinator()
    focused, summaries = [], []
    coordinator.register('first', 'First note', dirty=lambda: True,
                         save=lambda done: done(SaveResult(False, 'first failed')),
                         focus=lambda: focused.append('stale'))
    coordinator.register('second', 'Second note', dirty=lambda: True,
                         save=lambda done: done(SaveResult(False, 'second failed')))
    coordinator.register('third', 'Third note', dirty=lambda: True,
                         save=lambda done: done(SaveResult(False, 'third failed')),
                         focus=lambda: focused.append('third'))
    def complete(summary):
        summaries.append(summary)
        coordinator.register('first', 'Replacement', dirty=lambda: False,
                             save=lambda done: None, focus=lambda: focused.append('replacement'))
    assert coordinator.save_all(complete)
    assert len(summaries) == 1 and not coordinator.busy
    assert summaries[0].remaining == (('First note', 'first failed'), ('Second note', 'second failed'),
                                     ('Third note', 'third failed'))
    assert focused == ['third']
