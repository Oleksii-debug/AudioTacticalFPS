"""Duration controls retain the existing queue-owned worker and explicit review."""
from types import SimpleNamespace

import pytest

from tests.ui.test_audio_batches import view


def prepared(view):
    item = SimpleNamespace(id='b' * 32, kind='book', state='complete')
    view.batch = SimpleNamespace(items=(item,))
    view.items = SimpleNamespace(GetSelection=lambda: 0)
    view.duration_seconds = SimpleNamespace(GetValue=lambda: '0.137')
    view.duration_format = SimpleNamespace(GetStringSelection=lambda: 'wav')
    view.duration_reviewed = None
    return item


def test_review_is_owned_background_work_and_cannot_implicitly_publish(view):
    item = prepared(view)
    calls = []
    review = {'batch_id': view.batch_id, 'item_id': item.id, 'id': 'c' * 32, 'sha256': 'd' * 64}
    view.service.review_duration = lambda *a, **k: calls.append((a, k)) or review
    view.service.publish_duration = lambda *a, **k: pytest.fail('Review implicitly published parts.')
    view.review_duration()
    assert view.busy and calls == [] and view.duration_reviewed is None
    view.host.finish()
    assert calls[0][0] == (view.batch_id, item.id, '0.137')
    assert calls[0][1]['output_format'] == 'wav'
    assert view.duration_reviewed == review and not view.busy


def test_changing_duration_or_encoder_revokes_the_prior_publication_review(view):
    prepared(view)
    view.duration_reviewed = {'id': 'c' * 32}
    view.duration_changed()
    assert view.duration_reviewed is None and view.refreshes == [True]


def test_publish_passes_only_the_exact_current_review_and_existing_cancellation(view):
    item = prepared(view)
    view.duration_reviewed = {'batch_id': view.batch_id, 'item_id': item.id,
                              'id': 'c' * 32, 'sha256': 'd' * 64}
    calls = []
    view.service.publish_duration = lambda *a, **k: calls.append((a, k)) or {'status': 'complete'}
    view.publish_duration()
    assert view.busy and calls == []
    view.host.finish()
    assert calls[0][0] == (view.batch_id, item.id, 'c' * 32)
    assert calls[0][1]['approved_sha256'] == 'd' * 64
    assert callable(calls[0][1]['cancelled']) and not view.busy


@pytest.mark.parametrize('change', ['wrong-item', 'wrong-batch', 'missing', 'not-complete', 'not-book'])
def test_stale_or_inapplicable_duration_review_never_starts_a_worker(view, change):
    item = prepared(view)
    view.duration_reviewed = {'batch_id': view.batch_id, 'item_id': item.id,
                              'id': 'c' * 32, 'sha256': 'd' * 64}
    if change == 'wrong-item':
        view.duration_reviewed['item_id'] = 'e' * 32
    elif change == 'wrong-batch':
        view.duration_reviewed['batch_id'] = 'f' * 32
    elif change == 'missing':
        view.duration_reviewed = None
    elif change == 'not-complete':
        item.state = 'pending'
    else:
        item.kind = 'audio'
    view.publish_duration()
    assert view.host.work is None and not view.busy and view.messages
