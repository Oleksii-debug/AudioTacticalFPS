"""Notebook acceptance through the installed application's real public services.

Synthetic UTF-8 originals, actual SQLite, retained files and fresh backup
restores are used throughout. No source-tree imports, native GUI doubles,
model downloads or implicit approval of user PDF pages are involved.
"""
from hashlib import sha256
from types import SimpleNamespace

import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library import application as application_module
from carr_library.application import LibraryApp
from carr_library.citations.anchors import make_anchor
from carr_library.library_backup import restore_backup
from carr_library.research import notebook_search as notebook_module


def identities(result):
    return {(hit.evidence.topic_id, hit.evidence.evidence_id) for hit in result.hits}


@pytest.fixture
def notebook(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Transport research')
    other = app.create_workspace('Separate private research')
    parent = app.create_topic(workspace, 'Transport')
    child = app.create_topic(workspace, 'Архів України', parent=parent)
    foreign_topic = app.create_topic(other, 'Transport')
    english = 'Railway archives record industrial change.\r\nSource-exact English line.'
    ukrainian = 'Залізничні архіви зберігають історію міст.\r\nНезмінний український оригінал.'
    originals = {}
    for name, raw in [('english.txt', english), ('український.txt', ukrainian)]:
        path = tmp_path / name
        path.write_bytes(raw.encode('utf-8'))
        originals[name] = path
    imported_en = app.import_file(workspace, originals['english.txt'],
        title='Historical railway records', authors=('Original English Author',),
        language='en', edition='First edition', volume='I')
    imported_uk = app.import_file(workspace, originals['український.txt'],
        title='Історичний український архів', authors=('Український автор',),
        language='uk', edition='Перше видання')
    imported_foreign = app.import_file(other, originals['english.txt'],
        title='Private foreign records', authors=('Foreign Author',), language='en')
    assert all(item['state'] == 'complete'
               for item in (imported_en, imported_uk, imported_foreign))
    assert app.assets.resolve(imported_en['asset']).read_bytes() == originals['english.txt'].read_bytes()
    assert app.assets.resolve(imported_uk['asset']).read_bytes() == originals['український.txt'].read_bytes()
    en, = app.search(workspace, 'Railway archives')
    uk, = app.search(workspace, 'Залізничні архіви')
    foreign, = app.search(other, 'Railway archives')
    en_id = app.save_hit(workspace, parent, en.ref, en.revision,
        'Railway archives support this note. Shared review marker.')
    uk_id = app.save_hit(workspace, child, uk.ref, uk.revision,
        'Перевірити опис фонду. Shared review marker.')
    foreign_id = app.save_hit(other, foreign_topic, foreign.ref, foreign.revision,
        'Private foreign note. Shared review marker.')
    app.set_evidence_tags(workspace, parent, en_id, ('Shared', 'rail', 'Україна'))
    app.set_evidence_tags(workspace, child, uk_id, ('Shared', 'railway'))
    app.set_evidence_tags(other, foreign_topic, foreign_id, ('Shared', 'Private'))
    return SimpleNamespace(app=app, workspace=workspace, other=other, parent=parent,
        child=child, foreign_topic=foreign_topic, en=en, uk=uk, en_id=en_id,
        uk_id=uk_id, foreign_id=foreign_id, imported_en=imported_en,
        imported_uk=imported_uk, originals=originals,
        expected={(parent, en_id), (child, uk_id)})


def test_notebook_search_fresh_navigation_and_backup_restore(notebook, tmp_path):
    """Stable required CI case: real search, fresh navigation, reopen and restore."""
    n = notebook
    before = n.app.search_notebook(n.workspace, 'Shared review marker', field='note')
    assert identities(before) == n.expected
    assert (before.total_matches, before.evidence_scanned, before.topics_scanned) == (2, 2, 2)
    assert before.truncated is False
    assert all(hit.matched_fields == ('note',) and not hit.archived for hit in before.hits)
    expected_quotes = {n.en_id: 'Railway archives', n.uk_id: 'Залізничні архіви'}
    expected_paths = {n.parent: ('Transport',), n.child: ('Transport', 'Архів України')}
    for hit in before.hits:
        row = hit.evidence
        assert row.anchor.quote == expected_quotes[row.evidence_id]
        assert hit.topic_path == expected_paths[row.topic_id]
        assert row.anchor.locator.kind == 'logical_segment'
        assert row.anchor.locator.printed is None and not row.anchor.locator.verified
        fresh = n.app.get_notebook_hit(n.workspace, row.topic_id, row.evidence_id)
        assert fresh.evidence == row and fresh.matched_fields == ()
        assert fresh.tags == n.app.evidence_tags(n.workspace, row.topic_id, row.evidence_id)
    capped = n.app.search_notebook(n.workspace, 'Shared review marker', field='note', limit=1)
    assert len(capped.hits) == 1 and capped.total_matches == 2 and capped.truncated
    assert capped.evidence_scanned == 2
    reopened = LibraryApp(n.app.root)
    assert reopened.search_notebook(n.workspace, 'Shared review marker', field='note') == before
    originals = {p.name: p.read_bytes() for p in n.app.assets_dir.iterdir() if p.is_file()}
    exports = {topic: n.app.export_topic(n.workspace, topic, format='text')
               for topic in (n.parent, n.child)}
    folder = tmp_path / 'backup'
    checksum = reopened.backup(folder)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', checksum))
    assert restored.search_notebook(n.workspace, 'Shared review marker', field='note') == before
    for old in before.hits:
        row = old.evidence
        current = restored.get_notebook_hit(n.workspace, row.topic_id, row.evidence_id)
        assert current.evidence == row
        assert current.topic_path == old.topic_path and current.tags == old.tags
        assert current.evidence.anchor.metadata == row.anchor.metadata
        assert restored.export_topic(n.workspace, row.topic_id, format='text') == exports[row.topic_id]
    assert {p.name: p.read_bytes() for p in restored.assets_dir.iterdir() if p.is_file()} == originals
    assert identities(restored.search_notebook(n.other, 'Shared review marker', field='note')) == {
        (n.foreign_topic, n.foreign_id)}


def test_phrase_fields_and_exact_tag_only_search_stay_in_one_library(notebook):
    n = notebook
    both = n.app.search_notebook(n.workspace, 'Railway archives')
    assert identities(both) == {(n.parent, n.en_id)}
    assert both.hits[0].matched_fields == ('quote', 'note')
    assert n.app.search_notebook(n.workspace, 'Railway archives', field='quote').hits[0].matched_fields == ('quote',)
    assert n.app.search_notebook(n.workspace, 'Railway archives', field='note').hits[0].matched_fields == ('note',)
    uk = n.app.search_notebook(n.workspace, 'Залізничні архіви', field='quote')
    assert identities(uk) == {(n.child, n.uk_id)} and uk.hits[0].evidence.anchor.quote == n.uk.quote
    assert not n.app.search_notebook(n.workspace, 'Private foreign note').hits
    assert identities(n.app.search_notebook(n.workspace, '', tag='Shared')) == n.expected
    assert identities(n.app.search_notebook(n.workspace, '', tag='rail')) == {(n.parent, n.en_id)}
    assert not n.app.search_notebook(n.workspace, '', tag='Private').hits
    with pytest.raises(ValueError):
        n.app.search_notebook(n.workspace, '')


def test_equal_evidence_ids_in_different_topics_remain_distinct(notebook):
    n = notebook
    en, = n.app.evidence(n.workspace, n.parent)
    uk, = n.app.evidence(n.workspace, n.child)
    identity = 'same-local-evidence-id'
    with n.app.evidence_transaction(n.workspace) as store:
        store.save(n.parent, identity, en.anchor, 'Collision marker in English topic.')
        store.save(n.child, identity, uk.anchor, 'Collision marker in Ukrainian topic.')
    for topic in (n.parent, n.child):
        n.app.set_evidence_tags(n.workspace, topic, identity, ('Collision',))
    result = n.app.search_notebook(n.workspace, 'Collision marker', field='note', tag='Collision')
    assert identities(result) == {(n.parent, identity), (n.child, identity)}
    assert result.total_matches == 2 and result.evidence_scanned == 2
    first = n.app.get_notebook_hit(n.workspace, n.parent, identity)
    second = n.app.get_notebook_hit(n.workspace, n.child, identity)
    assert first.evidence.anchor == en.anchor and second.evidence.anchor == uk.anchor
    assert first.evidence.note != second.evidence.note
    with pytest.raises(ValueError):
        n.app.get_notebook_hit(n.other, n.parent, identity)


def test_archived_parent_hides_descendants_until_explicitly_included(notebook):
    n = notebook
    active_topic = n.app.create_topic(n.workspace, 'Current project')
    active_id = n.app.save_hit(n.workspace, active_topic, n.en.ref, n.en.revision,
                              'Shared review marker.')
    with n.app.topic_transaction(n.workspace) as tree:
        tree.archive(n.parent)
    visible = n.app.search_notebook(n.workspace, 'Shared review marker', field='note')
    assert identities(visible) == {(active_topic, active_id)}
    historical = n.app.search_notebook(n.workspace, 'Shared review marker', field='note',
                                      include_archived=True)
    assert identities(historical) == n.expected | {(active_topic, active_id)}
    assert {h.evidence.topic_id for h in historical.hits if h.archived} == {n.parent, n.child}
    with pytest.raises(ValueError):
        n.app.get_notebook_hit(n.workspace, n.child, n.uk_id)
    old = n.app.get_notebook_hit(n.workspace, n.child, n.uk_id, include_archived=True)
    assert old.archived and old.topic_path == ('Transport', 'Архів України')


def test_fresh_navigation_reloads_note_tags_path_and_archive_authorization(notebook):
    n = notebook
    old, = n.app.search_notebook(n.workspace, 'Залізничні архіви', field='quote').hits
    with n.app.topic_transaction(n.workspace) as tree:
        tree.rename(n.child, 'Перейменований архів')
    with n.app.evidence_transaction(n.workspace) as store:
        store.edit_note(n.child, n.uk_id, 'Updated researcher commentary.')
    n.app.set_evidence_tags(n.workspace, n.child, n.uk_id, ('Updated',))
    fresh = n.app.get_notebook_hit(n.workspace, n.child, n.uk_id)
    assert fresh.topic_path == ('Transport', 'Перейменований архів')
    assert fresh.tags == ('Updated',) and fresh.evidence.note == 'Updated researcher commentary.'
    assert fresh.evidence.anchor == old.evidence.anchor
    assert fresh.evidence.checksum == old.evidence.checksum
    assert old.evidence.note != fresh.evidence.note and old.topic_path != fresh.topic_path
    with pytest.raises(ValueError):
        n.app.get_notebook_hit(n.workspace, n.parent, n.uk_id)
    with n.app.topic_transaction(n.workspace) as tree:
        tree.archive(n.child)
    with pytest.raises(ValueError):
        n.app.get_notebook_hit(n.workspace, n.child, n.uk_id)


def test_current_metadata_edits_preserve_historical_notebook_citations(notebook):
    n = notebook
    old, = n.app.search_notebook(n.workspace, 'Railway archives', field='quote').hits
    before = n.app.export_topic(n.workspace, n.parent, format='text')
    revision = n.app.update_metadata(n.workspace, n.imported_en['edition'],
        title='Corrected modern catalog title', authors=('Corrected Author',), language='en',
        edition='Corrected edition', volume='II')
    current, = n.app.search(n.workspace, 'Railway archives')
    assert current.revision == revision and current.title == 'Corrected modern catalog title'
    fresh = n.app.get_notebook_hit(n.workspace, n.parent, n.en_id)
    assert fresh.evidence == old.evidence
    assert fresh.evidence.anchor.metadata.title == 'Historical railway records'
    assert fresh.evidence.anchor.metadata.author == 'Original English Author'
    assert n.app.search_notebook(n.workspace, 'Railway archives', field='quote').hits == (old,)
    assert n.app.export_topic(n.workspace, n.parent, format='text') == before


def test_cancellation_and_finite_deadlines_apply_to_search_and_fresh_navigation(notebook, monkeypatch):
    n = notebook
    actions = (
        lambda **options: n.app.search_notebook(n.workspace, 'Shared review marker', **options),
        lambda **options: n.app.get_notebook_hit(n.workspace, n.parent, n.en_id, **options),
    )
    before = n.app.evidence(n.workspace, n.parent)
    original_loaders = n.app._loaders
    for action in actions:
        with pytest.raises(TimeoutError):
            action(cancelled=lambda: True)
        # A positive interval need not elapse between two coarse Windows clock
        # ticks. Advance the shared clock only after a real source is loaded,
        # proving that an in-progress operation observes its elapsed deadline.
        clock, loaded = [100.0], []
        def timed_loaders(*args, **kwargs):
            load, metadata, attest = original_loaders(*args, **kwargs)
            def charged_load(*source_args):
                value = load(*source_args)
                loaded.append(source_args)
                clock[0] += 2.0
                return value
            return charged_load, metadata, attest
        with monkeypatch.context() as timed:
            timed.setattr(application_module, 'monotonic', lambda: clock[0])
            timed.setattr(notebook_module, 'monotonic', lambda: clock[0])
            timed.setattr(n.app, '_loaders', timed_loaders)
            with pytest.raises(TimeoutError):
                action(timeout=1.0)
        assert len(loaded) == 1
        for timeout in (float('nan'), float('inf')):
            with pytest.raises(ValueError):
                action(timeout=timeout)
    assert n.app.evidence(n.workspace, n.parent) == before
    assert identities(n.app.search_notebook(n.workspace, '', tag='Shared')) == n.expected


def test_source_corruption_fails_instead_of_returning_a_partial_notebook(notebook):
    n = notebook
    before = n.app.search_notebook(n.workspace, 'Shared review marker', field='note')
    retained = n.app.assets.resolve(n.imported_uk['asset'])
    original = retained.read_bytes()
    retained.write_bytes(original + b'\nchanged retained original')
    try:
        with pytest.raises(ValueError):
            n.app.search_notebook(n.workspace, 'Shared review marker', field='note', limit=1)
        with pytest.raises(ValueError):
            n.app.get_notebook_hit(n.workspace, n.child, n.uk_id)
        # An explicit exact tag limits the authorized verification scope too.
        scoped = n.app.search_notebook(n.workspace, '', tag='rail')
        assert identities(scoped) == {(n.parent, n.en_id)}
    finally:
        retained.write_bytes(original)
    assert n.app.search_notebook(n.workspace, 'Shared review marker', field='note') == before


def reviewed_pdf(app, workspace, tmp_path, lines):
    path = tmp_path / 'synthetic-notebook-pages.pdf'
    canvas = Canvas(str(path), invariant=1)
    for line in lines:
        canvas.drawString(72, 700, line)
        canvas.showPage()
    canvas.save()
    imported = app.import_file(workspace, path, title='Synthetic reviewed notebook PDF',
                               authors=('Synthetic PDF Author',))
    assert imported['state'] == 'complete'
    hits = []
    for line in lines:
        candidate, = app.search(workspace, line)
        # Explicit approval of this known drawing fixture is not approval of
        # arbitrary documents, OCR output or a user's original material.
        app.review_pdf_page(workspace, candidate.ref, candidate.revision,
                            'Synthetic fixture reviewer', confirmed=True)
        fresh, = app.search(workspace, line)
        assert fresh.verified
        hits.append(fresh)
    return imported, tuple(hits)


def test_revoked_pdf_review_blocks_full_and_fresh_notebook_results(notebook, tmp_path):
    n = notebook
    imported, (hit,) = reviewed_pdf(n.app, n.workspace, tmp_path,
                                    ('Notebook PDF railway evidence',))
    topic = n.app.create_topic(n.workspace, 'Reviewed PDF sources')
    evidence = n.app.save_hit(n.workspace, topic, hit.ref, hit.revision,
                              'Shared review marker.')
    before = n.app.search_notebook(n.workspace, 'Shared review marker', field='note')
    assert before.total_matches == 3
    original = n.app.assets.resolve(imported['asset']).read_bytes()
    n.app.revoke_pdf_review(n.workspace, imported['edition'], hit.revision, 1)
    with pytest.raises(ValueError):
        n.app.search_notebook(n.workspace, 'Shared review marker', field='note', limit=1)
    with pytest.raises(ValueError):
        n.app.get_notebook_hit(n.workspace, topic, evidence)
    assert identities(n.app.search_notebook(n.workspace, '', tag='Shared')) == n.expected
    assert sha256(n.app.assets.resolve(imported['asset']).read_bytes()).digest() == sha256(original).digest()


def test_saved_span_never_creates_a_phrase_across_source_pages(notebook, tmp_path):
    n = notebook
    _, hits = reviewed_pdf(n.app, n.workspace, tmp_path,
                           ('First source ending', 'next source opening'))
    anchors = []
    for hit in hits:
        page = n.app.context(n.workspace, hit.ref, hit.revision).pages[0]
        a = page.anchor
        anchors.append(make_anchor(a.identity, a.locator, page.raw_text,
            0, len(page.raw_text), a.metadata, text_verified=True))
    with n.app.evidence_transaction(n.workspace) as store:
        store.save(n.parent, 'two-page-span', tuple(anchors), 'Separate span note.')
    n.app.set_evidence_tags(n.workspace, n.parent, 'two-page-span', ('Span',))
    assert not n.app.search_notebook(n.workspace, 'ending next', field='quote', tag='Span').hits
    result = n.app.search_notebook(n.workspace, 'next source', field='quote', tag='Span')
    assert identities(result) == {(n.parent, 'two-page-span')} and result.total_matches == 1
    assert result.hits[0].evidence.anchors == tuple(anchors)
    assert result.hits[0].evidence.anchors[0].quote.endswith('ending\n')


def test_archived_library_is_not_enabled_by_the_archived_topic_option(notebook):
    n = notebook
    n.app.archive_workspace(n.workspace)
    with pytest.raises(ValueError):
        n.app.search_notebook(n.workspace, 'Shared review marker', include_archived=True)
    with pytest.raises(ValueError):
        n.app.get_notebook_hit(n.workspace, n.parent, n.en_id, include_archived=True)
    assert identities(n.app.search_notebook(n.other, '', tag='Shared')) == {
        (n.foreign_topic, n.foreign_id)}
    n.app.restore_workspace(n.workspace)
    assert identities(n.app.search_notebook(n.workspace, '', tag='Shared')) == n.expected
