"""Real local research to readable documents, with historical source authority."""
from html.parser import HTMLParser
from io import BytesIO

from docx import Document
import pytest

from carr_library.application import LibraryApp


class TextReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, text):
        self.parts.append(text)


def document_text(payload, format):
    if format == 'docx':
        assert isinstance(payload, bytes)
        document = Document(BytesIO(payload))
        return '\n'.join(paragraph.text for paragraph in document.paragraphs)
    assert isinstance(payload, str)
    reader = TextReader()
    reader.feed(payload)
    return '\n'.join(reader.parts)


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Local scholarship')
    topic = app.create_topic(workspace, 'History & society <primary sources>')
    imported = app.add_text(workspace, 'Джерело <synthetic>',
                            'A genuine synthetic passage about archives and memory.',
                            authors=('Synthetic Author',))
    hit, = app.search(workspace, 'archives and memory')
    app.save_hit(workspace, topic, hit.ref, hit.revision,
                 'My separate note <script>never execute</script>.')
    foreign = app.create_workspace('Unrelated library')
    foreign_topic = app.create_topic(foreign, 'History & society <primary sources>')
    app.add_text(foreign, 'Private foreign source',
                 'A foreign phrase about archives and memory.', authors=('Another Author',))
    other_hit, = app.search(foreign, 'archives and memory')
    app.save_hit(foreign, foreign_topic, other_hit.ref, other_hit.revision,
                 'Foreign note must stay in its own library.')
    return app, workspace, topic, foreign, imported


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_application_exports_the_selected_topic_with_readable_title(research, format):
    app, workspace, topic, foreign, imported = research
    before_database = app.database.read_bytes()
    before_source = app.assets.resolve(imported['asset']).read_bytes()

    payload = app.export_topic(workspace, topic, format=format)
    text = document_text(payload, format)

    assert 'History & society <primary sources>' in text
    assert 'archives and memory' in text
    assert 'My separate note <script>never execute</script>.' in text
    assert 'Джерело <synthetic>' in text
    assert 'Synthetic Author' in text
    assert 'Foreign note' not in text and 'Private foreign source' not in text
    assert app.database.read_bytes() == before_database
    assert app.assets.resolve(imported['asset']).read_bytes() == before_source
    if format == 'html':
        assert '<script>' not in payload


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_document_export_refuses_a_topic_from_another_workspace(research, format):
    app, workspace, topic, foreign, imported = research
    with pytest.raises(ValueError):
        app.export_topic(foreign, topic, format=format)


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_archived_topic_keeps_existing_read_only_export_semantics(research, format):
    app, workspace, topic, foreign, imported = research
    with app.topic_transaction(workspace) as tree:
        tree.archive(topic)
    before = app.database.read_bytes()
    text = document_text(app.export_topic(workspace, topic, format=format), format)
    assert 'archives and memory' in text
    assert 'My separate note' in text
    assert app.database.read_bytes() == before


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_source_drift_cannot_be_published_as_a_research_document(research, format):
    app, workspace, topic, foreign, imported = research
    original = app.assets.resolve(imported['asset'])
    original.write_bytes(b'A substituted source, not the saved historical original.')
    with pytest.raises(ValueError):
        app.export_topic(workspace, topic, format=format)


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_empty_topic_exports_a_named_document_without_fabricated_sources(research, format):
    app, workspace, topic, foreign, imported = research
    empty = app.create_topic(workspace, 'New research without evidence')
    text = document_text(app.export_topic(workspace, empty, format=format), format)
    assert 'New research without evidence' in text
    assert 'Synthetic Author' not in text and 'archives and memory' not in text
