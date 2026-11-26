"""Headless public DOI workflows use the same local scoped service."""
import json
from types import SimpleNamespace

import pytest

from carr_library.__main__ import main
from carr_library.acquisition.cli import run


@pytest.mark.parametrize('arguments', (
    ['--lookup-doi', '10.5555/example'],
    ['--lookup-doi', '10.5555/example', '--allow-online-doi'],
    ['--lookup-doi', '10.5555/example', '--allow-online-doi', '--workspace', 'one', '--workspace', 'two'],
    ['--allow-online-doi'],
    ['--save-doi-reference'],
    ['--doi-provider', 'crossref'],
    ['--lookup-doi', '10.5555/example', '--allow-online-doi', '--workspace', 'one',
     '--download-source-url', 'https://example.org/file.pdf'],
    ['--lookup-doi', '10.5555/example', '--allow-online-doi', '--workspace', 'one',
     '--unpaywall-email-env', 'INVALID=NAME'],
))
def test_missing_or_ambiguous_consent_is_rejected_before_app_or_network(arguments, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid command must not open a library or start acquisition.')
    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
    with pytest.raises(SystemExit) as result:
        main(arguments)
    assert result.value.code == 2
    assert 'error:' in capsys.readouterr().err


def test_document_formats_is_offline_and_does_not_open_a_library(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Format help must not open a library or start acquisition.')

    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
    monkeypatch.setattr('carr_library.acquisition.transport.SafeTransport', forbidden)
    data_dir = tmp_path / 'unopened-library'
    assert main(['--data-dir', str(data_dir), '--document-formats']) == 0
    text = capsys.readouterr().out
    assert 'HTML' in text and 'Markdown' in text and 'PowerPoint' in text
    assert 'unverified logical segments' in text
    assert not data_dir.exists()


@pytest.mark.parametrize('arguments', (
    ['--document-formats', '--lookup-doi', '10.5555/example'],
    ['--document-formats', '--allow-online-doi'],
    ['--document-formats', '--doi-provider', 'crossref'],
    ['--document-formats', '--save-doi-reference'],
    ['--document-formats', '--self-test'],
    ['--document-formats', '--backup', 'unused-backup'],
    ['--document-formats', '--restore', 'unused-backup'],
    ['--lookup-doi', '10.5555/example', '--allow-online-doi', '--workspace', 'one', '--self-test'],
))
def test_combined_document_and_acquisition_operations_fail_before_any_action(arguments, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Conflicting operations must fail before opening data or contacting a provider.')

    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
    monkeypatch.setattr('carr_library.acquisition.transport.SafeTransport', forbidden)
    with pytest.raises(SystemExit) as result:
        main(arguments)
    assert result.value.code == 2
    assert 'error:' in capsys.readouterr().err


def test_cli_preview_outputs_no_raw_payload_or_contact_email(monkeypatch, capsys):
    from carr_library.acquisition.models import MetadataPreview
    from carr_library.acquisition.service import AcquisitionPreview
    from hashlib import sha256
    payload = b'{"contact":"should-not-be-displayed"}'
    metadata = MetadataPreview(provider='crossref', doi='10.5555/example', title='Synthetic',
        source_url='https://doi.org/10.5555/example', raw_json=payload, raw_sha256=sha256(payload).hexdigest())
    preview = AcquisitionPreview('private-instance-token', 'selected', '10.5555/example',
        metadata, (metadata,), (), (), '2026-11-26T00:00:00Z')
    calls = []
    class App:
        def lookup_doi(self, workspace, doi, **options):
            calls.append((workspace, doi, options))
            return preview
    args = SimpleNamespace(workspaces=['selected'], lookup_doi=preview.doi,
        allow_online_doi=True, doi_provider=['crossref'], unpaywall_email_env='CARR_TEST_CONTACT',
        download_source_url=None, save_doi_reference=False)
    monkeypatch.setenv('CARR_TEST_CONTACT', 'private-contact@example.org')
    assert run(App(), args) == 0
    text = capsys.readouterr().out
    result = json.loads(text)
    assert result['preview']['metadata']['title'] == 'Synthetic'
    assert result['preview']['metadata']['raw_sha256'] == sha256(payload).hexdigest()
    for private in ('should-not-be-displayed', 'raw_json', 'private-instance-token', 'private-contact'):
        assert private not in text
    assert calls == [('selected', preview.doi, dict(online_allowed=True,
        providers=('crossref',), unpaywall_email='private-contact@example.org'))]
