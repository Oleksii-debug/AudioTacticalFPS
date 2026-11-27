"""One format capability surface, without side effects or operation ambiguity."""
import pytest

from carr_library.__main__ import main
from carr_library.extract.document_formats import (
    EXTENDED_FORMATS, FORMAT_BY_SUFFIX, MIME_SUFFIXES, SUPPORTED_SUFFIXES,
    file_dialog_wildcard,
)


def test_all_new_format_aliases_share_intake_batch_and_retention_capabilities():
    from carr_library.ingest.batch_scan import SUPPORTED
    from carr_library.ingest.retained_assets import SUFFIXES
    assert len(EXTENDED_FORMATS) == 10
    assert len(FORMAT_BY_SUFFIX) == 13
    assert SUPPORTED == SUPPORTED_SUFFIXES
    assert SUFFIXES == MIME_SUFFIXES
    choices = file_dialog_wildcard().split('|')[1].split(';')
    assert set(choices) == {'*' + suffix for suffix in SUPPORTED_SUFFIXES}


def test_document_format_help_does_not_open_data_or_activate_native_runtime(tmp_path, monkeypatch, capsys):
    from carr_library import native_runtime
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime',
        lambda: pytest.fail('Format help activated the native runtime'))
    destination = tmp_path / 'never-created'
    assert main(['--document-formats', '--data-dir', str(destination)]) == 0
    output = capsys.readouterr().out
    assert 'unverified logical segments' in output
    assert 'RTF' in output and 'not handled' in output
    assert not destination.exists()


@pytest.mark.parametrize('operation', [
    ['--audio-voices'], ['--audio-script', 'script.txt', '--audio-preview'],
    ['--self-test'], ['--backup', 'backup'], ['--restore', 'backup'],
    ['--list-libraries'], ['--search', 'text'], ['--lookup-doi', '10.1234/example'],
    ['--list-ocr-models'], ['--verify-ocr-models', 'eng'],
    ['--install-ocr-models', 'source', '--ocr-model', 'eng'],
])
def test_document_format_help_cannot_silently_replace_another_operation(operation):
    with pytest.raises(SystemExit) as error:
        main(['--document-formats', *operation])
    assert error.value.code == 2
