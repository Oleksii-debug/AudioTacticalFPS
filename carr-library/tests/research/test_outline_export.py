"""Real SQLite history and scoped evidence checks; no Windows/NVDA claim."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor
from carr_library.citations.historical import historical_loader
from carr_library.research import outline_export as outline
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.topic_tree import TopicTree
from carr_library.storage.revisions import Page, Storage


@pytest.fixture
def library(tmp_path):
    storage = Storage(tmp_path / 'outline.sqlite', schema_version=3)
    db = storage.db
    db.execute('BEGIN IMMEDIATE')
    trusted = {}
    for workspace in ('A', 'B'):
        db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
    for asset in ('a' * 64, 'b' * 64):
        db.execute('INSERT INTO catalog_asset VALUES(?,?,?)', (asset, 'application/pdf', 20))
    for workspace, edition, asset in [('A', 'pdf', 'a' * 64), ('A', 'text', 'b' * 64),
                                       ('B', 'pdf', 'a' * 64)]:
        db.execute('INSERT INTO catalog_work VALUES(?,?,?)', (workspace, edition, 'Catalog title'))
        db.execute('INSERT INTO catalog_edition VALUES(?,?,?,?,?,?,?)',
                   (workspace, edition, edition, asset, 'uk', 'Current', None))

    old = Identity('A', 'pdf', 'a' * 64, 'old')
    first = Locator('physical_page', 7, 'iv', True)
    second = Locator('physical_page', 8, 'candidate', False)
    raw = ('Першоджерело\n``` tricky [link](https://invalid.example).',
           'Železnica — second page.')
    metadata = Metadata('Original Author', 'Original title', 'II', 'First')

    def publish(identity, pages, meta):
        storage.stage_revision(identity.workspace, identity.edition_id, identity.revision,
                               identity.asset_sha256, 'synthetic', pages)
        db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                   (identity.workspace, identity.edition_id, identity.revision,
                    meta.author, meta.title, meta.volume, meta.edition))
        for ordinal, page in enumerate(pages, 1):
            loc = (Locator('physical_page', page.physical, page.printed, page.verified)
                   if page.physical is not None else Locator('logical_segment', ordinal))
            trusted[identity, loc] = digest(page.raw)

    publish(old, [Page(raw[0], 7, 'iv', True), Page(raw[1], 8, 'candidate')], metadata)
    publish(replace(old, revision='current'), [Page('Changed current text', 7)],
            Metadata('Current Author', 'Changed current title'))
    logical = Identity('A', 'text', 'b' * 64, 'logical')
    publish(logical, [Page('Unpaginated quotation.')], Metadata('Text Author', 'Text source'))
    foreign = Identity('B', 'pdf', 'a' * 64, 'foreign')
    publish(foreign, [Page('PRIVATE OTHER WORKSPACE', 7)], Metadata('Private', 'Private title'))

    def attest(identity, locator, checksum):
        return trusted.get((identity, locator)) == checksum

    def attest_metadata(identity, value):
        if identity.workspace != 'A':
            return False
        row = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                         'WHERE workspace=? AND edition=? AND revision=?',
                         (identity.workspace, identity.edition_id, identity.revision)).fetchone()
        return row is not None and Metadata(*row) == value

    load = historical_loader(db, 'A', attest)
    tree = TopicTree(db, 'A')
    tree.create('root', 'Дослідження')
    tree.create('z', 'Late child', parent='root', position=9)
    tree.create('b', 'Tied second', parent='root', position=1)
    tree.create('a', 'Early child', parent='root', position=1)
    tree.create('grandchild', 'Grandchild', parent='a')
    tree.create('hidden', 'Archived branch', parent='root')
    tree.create('hidden-child', 'Child under archive', parent='hidden')
    tree.create('outside', 'Outside selected subtree')
    TopicTree(db, 'B').create('root', 'PRIVATE OTHER WORKSPACE')
    anchors = tuple(make_anchor(old, loc, text, 0, len(text), metadata, text_verified=True)
                    for loc, text in zip((first, second), raw))
    evidence = SavedEvidenceStore(db, 'A', load, attest_metadata)
    evidence.save('root', 'root-evidence', anchors[0], 'A separate research note.\n# not a heading')
    evidence.save('a', 'span', anchors, 'A span note.')
    evidence.save('z', 'logical-evidence', make_anchor(
        logical, Locator('logical_segment', 1), 'Unpaginated quotation.', 0, 22,
        Metadata('Text Author', 'Text source'), text_verified=True))
    evidence.save('hidden-child', 'excluded', anchors[0], 'EXCLUDED ARCHIVED NOTE')
    tree.archive('hidden')
    for identity, name in [('tag-1', 'transport'), ('tag-2', 'Україна')]:
        db.execute('INSERT INTO research_tag VALUES(?,?,?)', ('A', identity, name))
        db.execute('INSERT INTO research_evidence_tag VALUES(?,?,?,?)',
                   ('A', 'root', 'root-evidence', identity))
    db.execute('INSERT INTO research_tag VALUES(?,?,?)', ('B', 'tag-1', 'PRIVATE TAG'))
    state = SimpleNamespace(db=db, tree=tree, load=load, metadata=attest_metadata,
                            evidence=evidence, anchors=anchors, raw=raw, old=old)
    try:
        yield state
    finally:
        if db.in_transaction:
            db.rollback()
        storage.close()


def export(library, topic='root', **options):
    return outline.export_outline(library.db, 'A', topic, load_page=library.load,
                                  attest_metadata=library.metadata, **options)


def headings(document):
    return [line for line in document.splitlines() if line.startswith('#')]


def test_real_historical_quotes_locators_notes_and_tags(library):
    changes = library.db.total_changes
    result = export(library)
    assert 'Topics: 5. Evidence records: 3.' in result
    assert 'Original Author' in result and 'Original title' in result
    assert 'Current Author' not in result and 'Changed current' not in result
    assert 'PDF page 7; printed page iv' in result
    assert 'PDF page 8; printed page unknown' in result and 'candidate' not in result
    assert 'Segment 1 (unpaginated)' in result
    assert '**Evidence 1, source slice 1 of 2**' in result
    assert '**Evidence 1, source slice 2 of 2**' in result
    for text in library.raw:
        assert json.dumps(text, ensure_ascii=False) in result
    assert '**Research note**' in result and '**Research tags**' in result
    assert json.dumps(['transport', 'Україна'], ensure_ascii=False) in result
    assert 'PRIVATE' not in result and 'EXCLUDED ARCHIVED NOTE' not in result
    assert library.db.in_transaction and library.db.total_changes == changes


def test_preorder_preserves_sibling_positions_and_id_tiebreak(library):
    result = export(library)
    assert headings(result) == [
        '# Research outline', '## 1 ` "Дослідження" `',
        '### 1.1 ` "Early child" `', '#### 1.1.1 ` "Grandchild" `',
        '### 1.2 ` "Tied second" `', '### 1.3 ` "Late child" `']
    assert 'Outside selected subtree' not in result
    assert 'Archived branch' not in result and 'Child under archive' not in result


def test_subtree_can_be_selected_without_its_ancestors_or_siblings(library):
    result = export(library, 'a')
    assert 'Topics: 2. Evidence records: 1.' in result
    assert headings(result) == ['# Research outline', '## 1 ` "Early child" `',
                                '### 1.1 ` "Grandchild" `']
    assert 'Дослідження' not in result and 'Late child' not in result


def test_empty_topic_is_a_meaningful_document(library):
    result = export(library, 'grandchild')
    assert 'Topics: 1. Evidence records: 0.' in result
    assert 'Grandchild' in result and 'No saved evidence in this topic.' in result


@pytest.mark.parametrize('topic', ['hidden', 'hidden-child', 'missing'])
def test_archived_or_unknown_selection_rejected(library, topic):
    with pytest.raises(ValueError, match='unknown or archived topic'):
        export(library, topic)


def test_deep_hierarchy_uses_numbered_paths_and_never_heading_level_seven(library):
    parent = 'grandchild'
    for number in range(8):
        identity = f'deep-{number}'
        library.tree.create(identity, f'Depth {number}', parent=parent)
        parent = identity
    result = export(library)
    assert 'Headings stop at level six' in result
    assert '###### 1.1.1.1.1.1.1.1.1.1.1 ' in result
    assert not any(line.startswith('#######') for line in headings(result))


def test_untrusted_topic_names_stay_inside_inline_code(library):
    name = 'A `name`\n# forged heading <img src="https://invalid.example">'
    library.tree.rename('a', name)
    result = export(library)
    assert len(headings(result)) == 6
    assert '### 1.1 `` ' + json.dumps(name, ensure_ascii=False) + ' ``' in result


@pytest.mark.parametrize('fault', ['payload', 'metadata', 'raw', 'cross-workspace'])
def test_corrupt_or_cross_scope_evidence_never_returns_partial_outline(library, fault):
    db = library.db
    if fault == 'raw':
        db.execute('DROP TRIGGER page_bu')
        db.execute("UPDATE page SET raw='Changed retained text' WHERE revision='old' AND physical=7")
    else:
        db.execute('DROP TRIGGER research_evidence_immutable')
        row = db.execute("SELECT payload FROM research_evidence WHERE workspace='A' AND topic='a'").fetchone()
        data = json.loads(row[0])
        if fault == 'metadata':
            data['anchors'][0]['metadata']['title'] = 'Invented metadata'
        elif fault == 'cross-workspace':
            data['anchors'][0]['identity']['workspace'] = 'B'
        payload = ('{}' if fault == 'payload' else
                   json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        db.execute("UPDATE research_evidence SET payload=?,checksum=? WHERE workspace='A' AND topic='a'",
                   (payload, digest(payload)))
    with pytest.raises(ValueError):
        export(library)
    assert db.in_transaction


def test_revoked_metadata_authority_fails(library):
    library.metadata = lambda *_: False
    with pytest.raises(ValueError, match='unattested metadata'):
        export(library)


def test_source_is_rechecked_when_the_verified_row_is_rendered(library):
    original = library.load
    reads = 0

    def load(*args):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise ValueError('Historical source authority was revoked.')
        return original(*args)

    library.load = load
    with pytest.raises(ValueError, match='revoked'):
        export(library)
    assert reads == 2


def test_corrupt_evidence_in_an_excluded_archive_is_not_exported(library):
    library.db.execute('DROP TRIGGER research_evidence_immutable')
    library.db.execute("UPDATE research_evidence SET payload='{}' WHERE topic='hidden-child'")
    assert 'EXCLUDED ARCHIVED NOTE' not in export(library)


def test_cancel_before_query_and_during_historical_read(library):
    with pytest.raises(InterruptedError, match='cancelled'):
        export(library, cancelled=lambda: True)
    original = library.load
    stopped = False

    def load(*args):
        nonlocal stopped
        result = original(*args)
        stopped = True
        return result

    library.load = load
    with pytest.raises(InterruptedError, match='cancelled'):
        export(library, cancelled=lambda: stopped)
    assert library.db.in_transaction


def test_cancel_after_formatting_still_returns_no_document(library, monkeypatch):
    original = outline.export_saved
    stopped = False

    def render(*args, **kwargs):
        nonlocal stopped
        result = original(*args, **kwargs)
        stopped = True
        return result

    monkeypatch.setattr(outline, 'export_saved', render)
    with pytest.raises(InterruptedError, match='cancelled'):
        export(library, cancelled=lambda: stopped)


@pytest.mark.parametrize(('name', 'value', 'message'), [
    ('MAX_TOPICS', 4, 'topic limit'), ('MAX_EVIDENCE', 2, 'evidence limit'),
    ('MAX_BYTES', 1, 'source snapshots')])
def test_limits_reject_before_source_reads(library, monkeypatch, name, value, message):
    monkeypatch.setattr(outline, name, value)
    library.load = lambda *_: pytest.fail('source read before bounded preflight')
    with pytest.raises(ValueError, match=message):
        export(library)


def test_exact_utf8_output_limit_is_enforced(library, monkeypatch):
    expected = export(library)
    size = len(expected.encode('utf-8'))
    monkeypatch.setattr(outline, 'MAX_BYTES', size)
    assert export(library) == expected
    monkeypatch.setattr(outline, 'MAX_BYTES', size - 1)
    with pytest.raises(ValueError, match='UTF-8 output byte limit'):
        export(library)


def test_topic_limit_applies_to_selected_subtree(library, monkeypatch):
    monkeypatch.setattr(outline, 'MAX_TOPICS', 1)
    assert 'Grandchild' in export(library, 'grandchild')


def test_excessive_tags_fail_safely(library):
    for number in range(outline.MAX_TAGS):
        identity = f'extra-{number}'
        library.db.execute('INSERT INTO research_tag VALUES(?,?,?)', ('A', identity, identity))
        library.db.execute('INSERT INTO research_evidence_tag VALUES(?,?,?,?)',
                           ('A', 'root', 'root-evidence', identity))
    with pytest.raises(ValueError, match='tag limit'):
        export(library)


def test_requires_caller_transaction_and_existing_authorized_scope(library):
    with pytest.raises(ValueError, match='unknown or archived topic'):
        outline.export_outline(library.db, 'not-authorized', 'root', load_page=library.load,
                               attest_metadata=library.metadata)
    library.db.commit()
    with pytest.raises(ValueError, match='consistent transaction'):
        export(library)
