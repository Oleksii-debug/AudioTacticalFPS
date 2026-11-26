"""Document bytes use the same atomic destination boundary as text exports."""
from io import BytesIO
from zipfile import ZipFile

import pytest

from carr_library.research import export_file
from carr_library.research.export_file import write_binary_export, write_export


@pytest.fixture
def payload():
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('word/document.xml', '<document>Синтетична цитата</document>')
    return output.getvalue()


def test_binary_document_survives_atomic_replacement(tmp_path, payload):
    private = tmp_path / 'private'
    private.mkdir()
    destination = tmp_path / 'Дослідження.docx'
    destination.write_bytes(b'previous document')
    write_binary_export(destination, payload, private_root=private)
    assert destination.read_bytes() == payload
    with ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert 'Синтетична цитата' in archive.read('word/document.xml').decode('utf-8')
    assert not tuple(tmp_path.glob('.research-export-*'))


def test_binary_document_cannot_replace_private_source(tmp_path, payload):
    private = tmp_path / 'private'
    private.mkdir()
    original = private / 'source.docx'
    original.write_bytes(b'private source')
    with pytest.raises(ValueError, match='outside the private library'):
        write_binary_export(original, payload, private_root=private)
    assert original.read_bytes() == b'private source'
    assert not tuple(private.glob('.research-export-*'))


@pytest.mark.parametrize('operation', ['fsync', 'replace'])
def test_binary_failed_publication_preserves_previous_document(
        tmp_path, payload, monkeypatch, operation):
    destination = tmp_path / 'research.docx'
    destination.write_bytes(b'previous document')

    def fail(*args):
        raise OSError('Synthetic document write failure')

    monkeypatch.setattr(export_file.os, operation, fail)
    with pytest.raises(OSError, match='Synthetic document write failure'):
        write_binary_export(destination, payload, private_root=tmp_path / 'private')
    assert destination.read_bytes() == b'previous document'
    assert not tuple(tmp_path.glob('.research-export-*'))


def test_binary_publication_rejects_a_changed_destination(tmp_path, payload, monkeypatch):
    destination = tmp_path / 'research.docx'
    destination.write_bytes(b'previous document')

    def another_writer(_fd):
        destination.write_bytes(b'newer independently saved document')

    monkeypatch.setattr(export_file.os, 'fsync', another_writer)
    with pytest.raises(ValueError, match='destination changed'):
        write_binary_export(destination, payload, private_root=tmp_path / 'private')
    assert destination.read_bytes() == b'newer independently saved document'
    assert not tuple(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('content', ['not document bytes', bytearray(b'mutable'), None])
def test_binary_export_rejects_nonimmutable_payload_before_writing(tmp_path, content):
    destination = tmp_path / 'research.docx'
    with pytest.raises(ValueError, match='Binary document export required'):
        write_binary_export(destination, content, private_root=tmp_path / 'private')
    assert not destination.exists()
    assert not tuple(tmp_path.glob('.research-export-*'))


def test_text_writer_still_rejects_binary_documents(tmp_path, payload):
    with pytest.raises(ValueError, match='Text export required'):
        write_export(tmp_path / 'research.txt', payload, private_root=tmp_path / 'private')
