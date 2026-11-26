"""Cached source selections cannot outlive their bound visible research topic."""
import pytest

from carr_library.application import LibraryApp


def test_archived_bound_topic_blocks_cached_preview_save_and_review(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    bound = app.create_topic(workspace, 'Bound topic')
    destination = app.create_topic(workspace, 'Separate destination')
    app.add_text(workspace, 'Source', 'An exact historical quotation.')
    hit, = app.search(workspace, 'historical quotation', authorized_topic=bound)
    assert 'historical quotation' in app.preview(workspace, hit.ref, hit.revision,
                                                 authorized_topic=bound)
    with app.topic_transaction(workspace) as tree:
        tree.archive(bound)
    actions = [
        lambda: app.search(workspace, 'historical quotation', authorized_topic=bound),
        lambda: app.preview(workspace, hit.ref, hit.revision, authorized_topic=bound),
        lambda: app.save_hit(workspace, destination, hit.ref, hit.revision, authorized_topic=bound),
        lambda: app.review_pdf_page(workspace, hit.ref, hit.revision, 'Reviewer',
                                    confirmed=True, authorized_topic=bound),
    ]
    for action in actions:
        with pytest.raises(ValueError, match='archived'):
            action()
    assert app.evidence(workspace, destination) == ()
    assert app.search(workspace, 'historical quotation')[0].verified
