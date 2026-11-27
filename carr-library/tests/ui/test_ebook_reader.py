"""EPUB reader authorization and asynchronous selection regressions.

These headless event tests do not claim native Windows or NVDA acceptance.
"""
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.citations.anchors import Identity
from carr_library.reader.ebook import EbookBook, EbookChapter, EbookSection, EbookPage
from carr_library.ui import ebook_reader


def book():
    return EbookBook('workspace', 'edition', 'revision', 'Synthetic book', 'Author', 'First', 2)


def section(position=1):
    chapters = (EbookChapter(1, 'First', 'OPS/first.xhtml', True),
                EbookChapter(2, 'Appendix', 'OPS/appendix.xhtml', False))
    chapter = chapters[position - 1]
    return EbookSection(Identity('workspace', 'edition', 'a' * 64, 'revision'),
        'Synthetic book', chapter.title, chapter.resource, position, 2,
        chapter.linear, 'Original extracted text.', chapters, ('layout_not_verified',), ())


def test_session_retains_exact_edition_and_section_list():
    session = ebook_reader.EbookSession(book(), lambda: True)
    assert session.accept(section(), 1).title == 'First'
    assert session.accept(section(2), 2).linear is False
    assert session.position == 2


@pytest.mark.parametrize('changed', ['workspace', 'edition_id', 'revision', 'position', 'chapters', 'scope'])
def test_session_rejects_stale_or_retargeted_section(changed):
    current = [True]
    session = ebook_reader.EbookSession(book(), lambda: current[0])
    value = section()
    session.accept(value, 1)
    if changed in ('workspace', 'edition_id', 'revision'):
        value = replace(value, identity=replace(value.identity, **{changed: 'different'}))
    elif changed == 'position':
        value = replace(value, position=2)
    elif changed == 'chapters':
        value = replace(value, chapters=(replace(value.chapters[0], resource='other.xhtml'), value.chapters[1]))
    else:
        current[0] = False
    with pytest.raises(ValueError, match='changed'):
        session.accept(value, 1)
    assert session.position == 1


@pytest.fixture
def actions(monkeypatch):
    # These selection/session tests use synthetic source callbacks. Real
    # post-worker archive authorization is covered by the EPUB E2E suite.
    monkeypatch.setattr(ebook_reader, '_authorized_scope', lambda *_: True)
    frame = SimpleNamespace(working=False, _closing=False, active=('workspace', 'topic'),
        library=object(), report=Mock(), run_job=Mock(), choice=Mock(),
        cancel_event=Event(), _workspace=lambda: 'workspace')
    dialogs = SimpleNamespace(choose=Mock(return_value=0))
    show = Mock()
    listing, reading = Mock(return_value=EbookPage((book(),), None, 1)), Mock(return_value=section())
    monkeypatch.setattr(ebook_reader, 'list_ebooks_page', listing)
    monkeypatch.setattr(ebook_reader, 'read_ebook_section', reading)
    handler = ebook_reader.EbookReaderActions(frame, dialogs=dialogs, show=show)
    return handler, frame, listing, reading, dialogs, show


def listed(actions):
    handler, frame, *_ = actions
    handler.read()
    _, operation, done = frame.run_job.call_args.args
    result = operation()
    done(result)


def test_reader_loads_in_jobs_and_preserves_scope_and_focus(actions):
    handler, frame, listing, reading, dialogs, show = actions
    handler.read()
    listing.assert_not_called()
    _, operation, done = frame.run_job.call_args.args
    done(operation())
    listing.assert_called_once_with(frame.library, 'workspace', authorized_topic='topic',
                                    cursor=None, cancelled=frame.cancel_event.is_set)
    reading.assert_not_called()
    _, operation, done = frame.run_job.call_args.args
    done(operation())
    reading.assert_called_once_with(frame.library, 'workspace', 'edition', 'revision', 1,
                                    authorized_topic='topic', cancelled=frame.cancel_event.is_set)
    show.assert_called_once()
    frame.choice.SetFocus.assert_called_once()


@pytest.mark.parametrize('change', ['library', 'scope', 'cancel', 'close'])
def test_stale_completed_listing_cannot_offer_books_from_another_context(actions, change):
    handler, frame, listing, reading, dialogs, show = actions
    handler.read()
    _, operation, done = frame.run_job.call_args.args
    result = operation()
    if change == 'library':
        frame.library = object()
    elif change == 'scope':
        frame.active = ('different', None)
    elif change == 'cancel':
        frame.cancel_event.set()
    else:
        frame._closing = True
    done(result)
    dialogs.choose.assert_not_called()
    reading.assert_not_called()
    show.assert_not_called()


def test_changed_library_before_section_read_rejects_without_touching_source(actions):
    _, frame, _, reading, _, show = actions
    listed(actions)
    _, operation, done = frame.run_job.call_args.args
    frame.library = object()
    with pytest.raises(ValueError, match='changed'):
        operation()
    done(section())
    reading.assert_not_called()
    show.assert_not_called()


@pytest.mark.parametrize('choice', [-1, True, 2])
def test_invalid_native_chooser_index_cannot_pick_an_unintended_book(actions, choice):
    handler, frame, _, reading, dialogs, show = actions
    dialogs.choose.return_value = choice
    handler.read()
    _, operation, done = frame.run_job.call_args.args
    with pytest.raises(ValueError, match='Choose'):
        done(operation())
    reading.assert_not_called()
    show.assert_not_called()


def test_cancelled_chooser_preserves_the_current_library(actions):
    handler, frame, _, reading, dialogs, show = actions
    dialogs.choose.return_value = None
    listed(actions)
    assert frame.run_job.call_count == 1
    assert frame.active == ('workspace', 'topic')
    reading.assert_not_called()
    show.assert_not_called()


def test_busy_frame_does_not_start_reader_jobs(actions):
    handler, frame, _, _, dialogs, show = actions
    frame.working = True
    handler.read()
    frame.run_job.assert_not_called()
    dialogs.choose.assert_not_called()
    show.assert_not_called()


def test_dismissal_during_background_read_does_not_focus_disabled_control(actions):
    _, frame, _, _, _, show = actions
    listed(actions)
    _, operation, done = frame.run_job.call_args.args
    show.side_effect = lambda *args: setattr(frame, 'working', True)
    done(operation())
    frame.choice.SetFocus.assert_not_called()


def test_epub_chooser_uses_bounded_next_and_previous_without_caching_source_text(actions):
    handler, frame, listing, reading, dialogs, show = actions
    following = replace(book(), edition_id='second-edition')
    listing.side_effect = [EbookPage((book(),), 'next-cursor', 1),
                           EbookPage((following,), None, 2),
                           EbookPage((book(),), 'next-cursor', 1)]
    dialogs.choose.side_effect = [1, 1, 0]
    handler.read()
    for _ in range(3):
        _, operation, done = frame.run_job.call_args.args
        done(operation())
    assert [call.kwargs['cursor'] for call in listing.call_args_list] == [None, 'next-cursor', None]
    assert dialogs.choose.call_args_list[0].args[3][-1] == 'Next page of EPUB books'
    assert dialogs.choose.call_args_list[1].args[3][-1] == 'Previous page of EPUB books'
    reading.assert_not_called()
    _, operation, done = frame.run_job.call_args.args
    done(operation())
    reading.assert_called_once()
    assert reading.call_args.args[2:4] == ('edition', 'revision')
    show.assert_called_once()


def test_stale_second_epub_page_cannot_replace_the_active_scope(actions):
    handler, frame, listing, reading, dialogs, show = actions
    listing.return_value = EbookPage((book(),), 'next-cursor', 1)
    dialogs.choose.return_value = 1
    handler.read()
    _, operation, done = frame.run_job.call_args.args
    done(operation())
    _, operation, done = frame.run_job.call_args.args
    frame.active = ('another-library', None)
    done(EbookPage((book(),), None, 2))
    assert dialogs.choose.call_count == 1
    reading.assert_not_called()
    show.assert_not_called()


def test_cancelling_after_epub_page_navigation_opens_no_section(actions):
    handler, frame, listing, reading, dialogs, show = actions
    listing.side_effect = [EbookPage((book(),), 'next-cursor', 1),
                           EbookPage((book(),), None, 2)]
    dialogs.choose.side_effect = [1, None]
    handler.read()
    for _ in range(2):
        _, operation, done = frame.run_job.call_args.args
        done(operation())
    assert frame.active == ('workspace', 'topic')
    assert frame.run_job.call_count == 2
    reading.assert_not_called()
    show.assert_not_called()
