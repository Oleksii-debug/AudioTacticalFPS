"""Narration reads through real library authorization and immutable originals."""
from hashlib import sha256

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech import ReadingDocument
from carr_library.speech.source import read_source_range


def test_reading_copy_keeps_library_original_and_saved_evidence(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Narration source')
    topic = app.create_topic(workspace, 'Evidence')
    text = 'Exact railway evidence.\r\nOriginal second line.'
    imported = app.add_text(workspace, 'A real synthetic source', text)
    hit, = app.search(workspace, 'railway evidence')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Keep this note.')
    original_evidence = app.evidence(workspace, topic)
    desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    source = read_source_range(desk, 1, 1)
    assert source.text == text
    assert source.workspace == workspace and source.edition == imported['edition']
    assert source.revision == desk.revision
    assert source.positions[0]['text_sha256'] == sha256(text.encode('utf-8')).hexdigest()
    copy = ReadingDocument(source.text, title=source.title)
    copy.set_text('An independently authored reading copy.')
    assert app.assets.resolve(imported['asset']).read_bytes() == text.encode('utf-8')
    assert app.evidence(workspace, topic) == original_evidence
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway evidence'


def test_new_active_revision_rejects_stale_narration_source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source')
    imported = app.add_text(workspace, 'Source', 'A source for narration.')
    desk = ReadingDesk(app, workspace, imported['edition'])
    app.update_metadata(workspace, imported['edition'], title='New source metadata')
    with pytest.raises(ValueError):
        read_source_range(desk, 1, 1)


def test_source_range_guard_cancel_and_character_budget(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source')
    imported = app.add_text(workspace, 'Source', 'A source for narration.')
    desk = ReadingDesk(app, workspace, imported['edition'])
    with pytest.raises(InterruptedError):
        read_source_range(desk, 1, 1, cancelled=lambda: True)
    with pytest.raises(ValueError, match='budget'):
        read_source_range(desk, 1, 1, max_characters=4)
    def denied():
        raise ValueError('Source authorization revoked.')
    with pytest.raises(ValueError, match='revoked'):
        read_source_range(desk, 1, 1, guard=denied)
