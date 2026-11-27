"""Actual retained headings/bookmarks remain immutable BookPlan source inputs."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import importlib.util
from pathlib import Path
import runpy
import shutil
import threading
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.durable_state import encode
from carr_library.library_backup import restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.studio.book_audio import BookAudioService, BookPlan, SplitOptions, capture_plan
from carr_library.studio.book_landmarks import available_bookmarks, entries
from carr_library.studio.project import Role
from carr_library.studio.render import ProductionService

_reader = runpy.run_path(str(Path(__file__).parents[1] / 'reader' / 'test_ebook_contents.py'))
_spec = importlib.util.spec_from_file_location('landmark_book_fixtures', Path(__file__).with_name('test_book_audio.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
ROLE = Role('narrator', 'Narrator', 'synthetic-tone', 'en')


def source_bytes(tmp_path):
    source = tmp_path / 'retained.epub'
    _reader['write_book'](source)
    out = BytesIO()
    with ZipFile(source) as before, ZipFile(out, 'w') as after:
        for item in before.infolist():
            data = before.read(item.filename)
            if item.filename == 'OPS/chapters/z.xhtml':
                data = data.replace(b'</body>',
                    '<h2>Внутрішня межа</h2><p>Literal second section without invented timing.</p></body>'.encode())
            after.writestr(item, data)
    return out.getvalue()


@pytest.fixture
def edition(tmp_path):
    app, workspace, revision, original, extracted, config = _reader['_published'](
        tmp_path, enhanced=True, source_bytes=source_bytes(tmp_path))
    voices = _fixtures.ToneVoices()
    production = ProductionService(app.root / 'state' / 'studio', voices=voices,
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options))
    books = BookAudioService(app, workspace, production=production)
    output = tmp_path / 'reviewed-output'
    output.mkdir()
    desk = ReadingDesk(app, workspace, 'edition')
    state = ReadingState(app.root)
    bookmarks = (state.add_bookmark(desk.page(1).location, 'First retained position'),
                 state.add_bookmark(desk.page(2).location, 'Second retained position'))
    yield SimpleNamespace(app=app, workspace=workspace, revision=revision, original=original,
        extracted=extracted, config=config, voices=voices, production=production, books=books,
        output=output, desk=desk, state=state, bookmarks=bookmarks)
    books.close()
    production.close()
    assert voices.closed == 0


def capture(e, mode, **kwargs):
    return capture_plan(e.app, e.workspace, 'edition', options=SplitOptions(mode),
                        role=ROLE, **kwargs)


def test_legacy_plan_bytes_fingerprint_and_job_id_do_not_gain_empty_fields(edition):
    e = edition
    plan = capture(e, 'document')
    expected = {'schema': 1, 'id': plan.id, 'source': plan.source,
        'options': asdict(plan.options), 'role': ROLE.to_dict(),
        'parts': [asdict(part) for part in plan.parts], 'format': 'wav', 'sample_rate': 24000}
    assert plan.to_dict() == expected and 'landmarks' not in expected
    assert plan.fingerprint == sha256(encode(expected)).hexdigest()
    restored = BookPlan.from_dict(expected)
    assert restored == plan and restored.job_id(1) == plan.job_id(1)
    assert 'landmarks' not in restored.provenance(1)


def test_retained_epub_heading_offsets_split_inside_chapter_and_preserve_every_character(edition):
    e = edition
    before = e.original.read_bytes()
    plan = capture(e, 'headings')
    rows = entries(plan.source, plan.landmarks)
    assert [row['label'] for row in rows] == ['Перший розділ', 'Внутрішня межа', 'Другий розділ']
    assert [row['origin']['ordinal'] for row in rows] == [1, 1, 2]
    assert rows[0]['start'] == 0 < rows[1]['start'] < rows[2]['start']
    assert len(plan.parts) == 3
    assert [part.start for part in plan.parts] == [row['start'] for row in rows]
    assert ''.join(plan.project(i).original_script for i in range(1, 4)) == plan.source['text']
    assert plan.source['text'] == '\n\n'.join(item['raw_text'] for item in e.extracted['records'])
    assert all(row['origin']['text_sha256'] == e.extracted['records'][row['origin']['ordinal'] - 1]['text_sha256']
               for row in rows)
    assert len({row['id'] for row in rows}) == 3
    assert all(not position['text_verified_at_capture'] for position in plan.source['source_anchor']['positions'])
    assert plan.source['source_anchor']['quotation_authority'] is False
    assert BookPlan.from_dict(plan.to_dict()) == plan
    assert e.original.read_bytes() == before and not e.production.jobs() and not e.voices.received


def test_old_epub_without_retained_headings_never_reparses_original(tmp_path, monkeypatch):
    app, workspace, _, _, _, _ = _reader['_published'](tmp_path, enhanced=False)
    def forbidden(*args, **kwargs):
        pytest.fail('A missing retained heading inventory must not be invented by parsing the original again.')
    monkeypatch.setattr('carr_library.extract.epub_ordered.extract_epub', forbidden)
    with pytest.raises(ValueError, match='No retained EPUB heading'):
        capture_plan(app, workspace, 'edition', options=SplitOptions('headings'), role=ROLE)


def test_explicit_bookmarks_keep_exact_ids_and_page_start_scope(edition):
    e = edition
    selected = (e.bookmarks[1].id, e.bookmarks[0].id)
    plan = capture(e, 'bookmarks', bookmark_ids=selected)
    rows = entries(plan.source, plan.landmarks)
    assert [row['id'] for row in rows] == [bookmark.id for bookmark in e.bookmarks]
    assert [part.start for part in plan.parts] == [position['start'] for position in plan.source['source_anchor']['positions']]
    assert [row['origin'] for row in rows] == list(map(asdict, e.bookmarks))
    assert plan.provenance(2)['landmarks']['boundaries'] == [rows[1]]
    choices = available_bookmarks(e.app, e.workspace, 'edition', revision=e.revision)
    assert [row['bookmark'] for row in choices] == list(e.bookmarks)
    assert all(row['current_revision'] for row in choices)
    assert BookPlan.from_dict(plan.to_dict()) == plan
    assert not e.production.jobs() and not e.voices.received


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'outside-range', 'wrong-mode', 'stale-revision', 'foreign-source'])
def test_bookmark_selection_refuses_guessed_or_stale_boundaries(edition, case):
    e = edition
    kwargs = {'bookmark_ids': [e.bookmarks[1].id]}
    mode = 'bookmarks'
    if case == 'missing':
        kwargs['bookmark_ids'] = ['f' * 32]
    elif case == 'duplicate':
        kwargs['bookmark_ids'] *= 2
    elif case == 'outside-range':
        kwargs.update(first=1, last=1)
    elif case == 'wrong-mode':
        mode = 'document'
    elif case == 'stale-revision':
        e.app.update_metadata(e.workspace, 'edition', title='A changed exact source revision')
    else:
        other = e.app.add_text(e.workspace, 'Another edition', 'Separate original.')
        foreign = e.state.add_bookmark(ReadingDesk(e.app, e.workspace, other['edition']).page(1).location, 'Other book')
        kwargs['bookmark_ids'] = [foreign.id]
    with pytest.raises(ValueError):
        capture(e, mode, **kwargs)
    assert not e.production.jobs() and not e.voices.received


@pytest.mark.parametrize('change', ['rename', 'delete'])
def test_changed_selected_bookmark_refuses_preparation_and_resume(edition, change):
    e = edition
    plan = capture(e, 'bookmarks', bookmark_ids=[e.bookmarks[1].id])
    review = e.books.review(plan, e.output)
    if change == 'rename':
        e.state.rename_bookmark(e.workspace, 'edition', e.bookmarks[1].id, 'Changed after explicit review')
    else:
        e.state.delete_bookmark(e.workspace, 'edition', e.bookmarks[1].id)
    with pytest.raises(ValueError, match='changed|unavailable'):
        e.books.prepare(plan, e.output, approval=review.fingerprint)
    assert not e.production.jobs() and not e.voices.received and not list(e.output.iterdir())


def test_added_unselected_bookmark_does_not_retarget_frozen_selected_id(edition):
    e = edition
    plan = capture(e, 'bookmarks', bookmark_ids=[e.bookmarks[1].id])
    review = e.books.review(plan, e.output)
    e.state.add_bookmark(e.desk.page(1).location, 'Unselected new bookmark')
    ready = e.books.prepare(plan, e.output, approval=review.fingerprint)
    assert ready.prepared and e.books.load(plan.id) == plan
    assert entries(plan.source, plan.landmarks)[0]['id'] == e.bookmarks[1].id
    assert not e.voices.received


def test_changed_retained_heading_inventory_refuses_jobs_even_with_unchanged_text(edition, monkeypatch):
    import carr_library.studio.book_landmarks as landmarks
    e = edition
    plan = capture(e, 'headings')
    review = e.books.review(plan, e.output)
    actual = landmarks.open_ebook
    def changed(*args, **kwargs):
        value = deepcopy(actual(*args, **kwargs))
        value['chapters'][0]['headings'][1]['title'] = 'Changed retained navigation label'
        return value
    monkeypatch.setattr(landmarks, 'open_ebook', changed)
    with pytest.raises(ValueError, match='inventory changed'):
        e.books.prepare(plan, e.output, approval=review.fingerprint)
    assert not e.production.jobs() and not e.voices.received


@pytest.mark.parametrize('change', ['offset', 'source-hash', 'mode', 'part', 'metadata-kind'])
def test_passive_plan_validation_rejects_relabelled_boundaries(edition, change):
    plan = capture(edition, 'headings')
    value = deepcopy(plan.to_dict())
    if change == 'offset':
        value['landmarks']['records'][0]['headings'][1]['offset'] += 1
    elif change == 'source-hash':
        value['landmarks']['records'][0]['text_sha256'] = '0' * 64
    elif change == 'mode':
        value['options']['mode'] = 'document'
    elif change == 'part':
        value['parts'][0]['label'] = 'A different label'
    else:
        value['landmarks']['kind'] = 'bookmarks'
    with pytest.raises(ValueError):
        BookPlan.from_dict(value)


def test_bookmark_change_after_actual_speech_refuses_publication_and_no_automatic_retry(edition):
    e = edition
    plan = capture(e, 'bookmarks', bookmark_ids=[e.bookmarks[1].id])
    preview = e.books.review(plan, e.output)
    ready = e.books.prepare(plan, e.output, approval=preview.fingerprint)
    def change():
        e.voices.after_write = None
        e.state.rename_bookmark(e.workspace, 'edition', e.bookmarks[1].id, 'Changed while speaking')
    e.voices.after_write = change
    with pytest.raises(ValueError, match='inventory changed'):
        e.books.run(plan.id, e.output, approval=ready.fingerprint)
    assert len(e.voices.received) == 1
    assert not list(e.output.iterdir())
    assert e.production.get(plan.job_id(1))['status'] == 'failed'
    with pytest.raises(ValueError, match='inventory changed'):
        e.books.run(plan.id, e.output, approval=ready.fingerprint)
    assert len(e.voices.received) == 1


@pytest.mark.parametrize('mode', ['headings', 'bookmarks'])
def test_actual_render_backup_delete_and_restore_preserves_landmarks_and_requires_new_output(edition, tmp_path, mode):
    e = edition
    kwargs = {'bookmark_ids': [bookmark.id for bookmark in e.bookmarks]} if mode == 'bookmarks' else {}
    plan = capture(e, mode, **kwargs)
    preview = e.books.review(plan, e.output)
    ready = e.books.prepare(plan, e.output, approval=preview.fingerprint)
    result = e.books.run(plan.id, e.output, approval=ready.fingerprint)
    assert len(result) == len(plan.parts) and all(row['status'] == 'complete' for row in result)
    backup = tmp_path / 'typed-backup'
    receipt = e.app.backup(backup)
    e.books.close()
    e.production.close()
    shutil.rmtree(e.app.root)
    e.original.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', receipt))
    production = ProductionService(restored.root / 'state' / 'studio', voices=e.voices,
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options))
    books = BookAudioService(restored, e.workspace, production=production)
    destination = tmp_path / 'fresh-review'
    destination.mkdir()
    try:
        reopened = books.load(plan.id)
        assert reopened == plan
        assert ReadingState(restored.root).bookmarks(e.workspace, 'edition') == e.bookmarks
        assert all(production.get(plan.job_id(index))['recovery']['requires_output_directory']
                   for index in range(1, len(plan.parts) + 1))
        preview = books.review(reopened, destination)
        with pytest.raises(ValueError, match='Prepare'):
            books.run(plan.id, destination, approval=preview.fingerprint)
        ready = books.prepare(reopened, destination, approval=preview.fingerprint)
        rerendered = books.run(plan.id, destination, approval=ready.fingerprint)
        assert all(row['status'] == 'complete' for row in rerendered)
        for index in range(1, len(plan.parts) + 1):
            assert production.get(plan.job_id(index))['result']['book_source_sha256'] == sha256(encode(plan.provenance(index))).hexdigest()
        assert reopened.landmarks['kind'] == mode
        if mode == 'bookmarks':
            assert all(row['location']['revision'] == e.revision for row in reopened.landmarks['records'])
    finally:
        books.close()
        production.close()


def test_cancellation_during_actual_bookmark_read_is_stopped_without_jobs(edition, monkeypatch):
    e = edition
    plan = capture(e, 'bookmarks', bookmark_ids=[e.bookmarks[1].id])
    preview = e.books.review(plan, e.output)
    cancelled = threading.Event()
    actual = ReadingState.bookmarks
    def interrupted(self, *args):
        value = actual(self, *args)
        cancelled.set()
        raise ValueError('A retained read noticed stop before returning its current metadata.')
    monkeypatch.setattr(ReadingState, 'bookmarks', interrupted)
    with pytest.raises(InterruptedError):
        e.books.prepare(plan, e.output, approval=preview.fingerprint, cancelled=cancelled.is_set)
    assert not e.production.jobs() and not e.voices.received

