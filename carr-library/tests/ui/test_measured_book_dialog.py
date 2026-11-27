"""Measured publication review policy; no synthetic wx device acceptance claim."""
from types import SimpleNamespace

import pytest

from carr_library.ui.book_batch_dialog import BookBatchDialog, BookWorkspace, duration_text
from carr_library.audio.book_audio import BookAudioService
from carr_library.audio.book_batch import BookBatchService
from tests.audio.test_book_plans import library, make_plan
from tests.ui.test_book_batch_dialog import shell


def view(library, tmp_path):
    result = shell()
    result.library, result.scope = library[0], (library[1], None)
    result.frame.library, result.frame.active = library[0], result.scope
    result.state = BookWorkspace(BookBatchService(BookAudioService(library[0], library[1])))
    plan = make_plan(library)
    result.state.add(plan)
    result.displayed = (plan.id,)
    result.selected = SimpleNamespace(GetSelection=lambda: 0)
    result.duration_generation, result.measured = 0, None
    result.buttons = {}
    result.duration_index = SimpleNamespace(GetValue=lambda: '1')
    result.duration_ms = SimpleNamespace(GetValue=lambda: '60000')
    result.duration_template = SimpleNamespace(GetValue=lambda: '{index:04d}')
    result.duration_output = SimpleNamespace(GetValue=lambda: str(tmp_path / 'selected.zip'))
    result.wx = SimpleNamespace(CallAfter=lambda callback, *args: callback(*args))
    return result, plan


def fake_review(snapshot):
    return {'approval_sha256': 'a' * 64, 'output': snapshot[-1],
        'content': {'pcm': {'frames': 48000, 'sample_rate': 48000}, 'notice': 'No word timing.',
            'parts': [{'index': 1, 'start_frame': 0, 'end_frame': 48000,
                       'frames': 48000, 'name': 'audio/0001.wav'}]}}


def test_preview_capture_is_retained_and_publish_is_separate(library, tmp_path):
    dialog, plan = view(library, tmp_path)
    submitted, calls = [], []
    dialog.frame.run_job = lambda label, work, done, failed: submitted.append((work, done))
    snapshot = dialog._duration_snapshot()
    reviewed = fake_review(snapshot)
    def review(*args, **kwargs):
        calls.append(('review', args, kwargs))
        return reviewed
    def publish(*args, **kwargs):
        calls.append(('publish', args, kwargs))
        return {'status': 'complete', 'id': 'c' * 32, 'output': snapshot[-1], 'notice': 'Retained PCM.'}
    dialog.books.review_duration = review
    dialog.books.publish_duration = publish
    dialog.measured_action()
    assert dialog.measured is None and dialog.busy and not calls
    work, done = submitted.pop()
    done(work())
    assert dialog.measured[2] is reviewed and calls[0][0] == 'review'
    approved_destination = dialog.measured[1]
    assert calls[0][2]['destination_approval'] is approved_destination
    dialog.measured_action(publish=True)
    work, done = submitted.pop()
    done(work())
    assert [row[0] for row in calls] == ['review', 'publish']
    assert calls[1][2]['review_sha256'] == reviewed['approval_sha256']
    assert calls[1][2]['destination_approval'] is approved_destination
    assert dialog.measured is None and 'publication complete' in dialog.messages[-1]


@pytest.mark.parametrize('change', ['controls', 'order', 'source'])
def test_changed_selection_cannot_publish_or_accept_stale_preview(library, tmp_path, change):
    dialog, plan = view(library, tmp_path)
    snapshot = dialog._duration_snapshot()
    dialog.measured = (snapshot, object(), fake_review(snapshot))
    if change == 'controls':
        dialog.duration_changed()
    elif change == 'order':
        dialog.state.invalidate(selection_changed=True)
    else:
        dialog.state.remove(plan.id)
    with pytest.raises(ValueError):
        dialog.measured_action(publish=True)
    with pytest.raises(ValueError):
        dialog._duration_guard(snapshot)


def test_preview_failure_and_late_stop_do_not_grant_output_permission(library, tmp_path):
    dialog, _ = view(library, tmp_path)
    submitted = []
    dialog.frame.run_job = lambda label, work, done, failed: submitted.append((done, failed))
    dialog.measured_action()
    _, failed = submitted.pop()
    failed(ValueError('The current source is no longer visible.'))
    assert dialog.measured is None and not dialog.busy
    dialog.measured_action()
    done, _ = submitted.pop()
    dialog.cancel.set()
    done(fake_review(dialog._duration_snapshot()))
    assert dialog.measured is None


def test_complete_publish_acknowledgement_survives_late_stop(library, tmp_path):
    dialog, _ = view(library, tmp_path)
    snapshot = dialog._duration_snapshot()
    dialog.measured = (snapshot, object(), fake_review(snapshot))
    callbacks = []
    dialog.frame.run_job = lambda label, work, done, failed: callbacks.append(done)
    dialog.measured_action(publish=True)
    dialog.cancel.set()
    callbacks.pop()({'status': 'complete', 'id': 'a' * 32,
                     'output': snapshot[-1], 'notice': 'Original source unchanged.'})
    assert dialog.measured is None and 'publication complete' in dialog.messages[-1]


def test_review_text_uses_measured_frames_and_no_invented_text_alignment():
    review = fake_review((0, 0, 'x', 'hash', 1, 60000, '{index}', '/selected.zip'))
    text = duration_text(review)
    assert '48000 exact frames' in text and '1.000000 seconds' in text
    assert 'no output has been published' in text and 'No word timing.' in text
