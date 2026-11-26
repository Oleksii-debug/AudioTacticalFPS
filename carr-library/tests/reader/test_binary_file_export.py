"""Document bytes use the same atomic destination boundary as text exports."""
from io import BytesIO
from threading import Event
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


@pytest.fixture(params=['text', 'binary'])
def cancellation_writer(request, payload):
    if request.param == 'text':
        return write_export, 'Точна цитата\r\nExact source\ttext.'
    return write_binary_export, payload


def test_pre_cancelled_export_does_not_inspect_destination(
        tmp_path, cancellation_writer, monkeypatch):
    writer, content = cancellation_writer
    destination = tmp_path / 'existing-output'
    destination.write_bytes(b'Previous output.')

    def forbidden(*args, **kwargs):
        pytest.fail('A cancelled export accessed the filesystem.')

    monkeypatch.setattr(export_file, '_destination', forbidden)
    monkeypatch.setattr(export_file, 'NamedTemporaryFile', forbidden)
    with pytest.raises(TimeoutError, match='Export cancelled; the destination was not changed.'):
        writer(destination, content, private_root=tmp_path / 'private', cancelled=lambda: True)
    assert destination.read_bytes() == b'Previous output.'
    assert not tuple(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('cancelled', [None, False, object()])
def test_noncallable_cancellation_fails_before_destination_access(
        tmp_path, cancellation_writer, monkeypatch, cancelled):
    writer, content = cancellation_writer

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid cancellation callback reached the filesystem.')

    monkeypatch.setattr(export_file, '_destination', forbidden)
    monkeypatch.setattr(export_file, 'NamedTemporaryFile', forbidden)
    with pytest.raises(ValueError, match='An export cancellation callback is required.'):
        writer(tmp_path / 'output', content, private_root=tmp_path / 'private', cancelled=cancelled)
    assert not (tmp_path / 'output').exists()


def test_entry_callback_exception_propagates_before_filesystem_access(
        tmp_path, cancellation_writer, monkeypatch):
    writer, content = cancellation_writer
    destination = tmp_path / 'existing-output'
    destination.write_bytes(b'Previous output.')

    def callback():
        raise LookupError('Synthetic callback failure')

    def forbidden(*args, **kwargs):
        pytest.fail('A failed callback reached the filesystem.')

    monkeypatch.setattr(export_file, '_destination', forbidden)
    monkeypatch.setattr(export_file, 'NamedTemporaryFile', forbidden)
    with pytest.raises(LookupError, match='Synthetic callback failure'):
        writer(destination, content, private_root=tmp_path / 'private', cancelled=callback)
    assert destination.read_bytes() == b'Previous output.'
    assert not tuple(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('stage', ['initial_validation', 'fsync', 'final_validation'])
@pytest.mark.parametrize('failure', ['cancel', 'raise'])
def test_late_cancellation_or_callback_failure_preserves_old_file_and_cleans_temp(
        tmp_path, cancellation_writer, monkeypatch, stage, failure):
    writer, content = cancellation_writer
    destination = tmp_path / 'existing-output'
    destination.write_bytes(b'Previous output.')
    stopped = Event()
    actual_destination = export_file._destination
    actual_fsync = export_file.os.fsync
    validations = 0

    def validate(*args):
        nonlocal validations
        result = actual_destination(*args)
        validations += 1
        if (stage == 'initial_validation' and validations == 1 or
                stage == 'final_validation' and validations == 2):
            stopped.set()
        return result

    def sync(fd):
        actual_fsync(fd)
        if stage == 'fsync':
            stopped.set()

    def callback():
        if stopped.is_set() and failure == 'raise':
            raise LookupError('Synthetic callback failure')
        return stopped.is_set()

    monkeypatch.setattr(export_file, '_destination', validate)
    monkeypatch.setattr(export_file.os, 'fsync', sync)
    if stage == 'initial_validation':
        def forbidden(*args, **kwargs):
            pytest.fail('A cancelled export created a temporary file.')
        monkeypatch.setattr(export_file, 'NamedTemporaryFile', forbidden)
    error = LookupError if failure == 'raise' else TimeoutError
    message = ('Synthetic callback failure' if failure == 'raise' else
               'Export cancelled; the destination was not changed.')
    with pytest.raises(error, match=message):
        writer(destination, content, private_root=tmp_path / 'private', cancelled=callback)
    assert destination.read_bytes() == b'Previous output.'
    assert not tuple(tmp_path.glob('.research-export-*'))


def test_explicit_false_callback_preserves_normal_text_and_binary_output(
        tmp_path, cancellation_writer):
    writer, content = cancellation_writer
    destination = tmp_path / 'existing-output'
    destination.write_bytes(b'Previous output.')
    writer(destination, content, private_root=tmp_path / 'private', cancelled=lambda: False)
    assert destination.read_bytes() == (content.encode('utf-8') if isinstance(content, str) else content)
    assert not tuple(tmp_path.glob('.research-export-*'))


def test_completed_replacement_is_not_reported_as_cancelled(
        tmp_path, cancellation_writer, monkeypatch):
    writer, content = cancellation_writer
    destination = tmp_path / 'existing-output'
    destination.write_bytes(b'Previous output.')
    stopped = Event()
    actual_replace = export_file.os.replace

    def replace(source, target):
        actual_replace(source, target)
        stopped.set()

    monkeypatch.setattr(export_file.os, 'replace', replace)
    writer(destination, content, private_root=tmp_path / 'private', cancelled=stopped.is_set)
    assert stopped.is_set()
    assert destination.read_bytes() == (content.encode('utf-8') if isinstance(content, str) else content)
    assert not tuple(tmp_path.glob('.research-export-*'))
