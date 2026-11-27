"""Headless public DOI/ISBN workflows use the same local scoped services."""
import json
from types import SimpleNamespace

import pytest

from carr_library.__main__ import main
from carr_library.acquisition.cli import run


ISBN_REQUEST = ['--lookup-isbn', '9780306406157', '--allow-online-isbn', '--workspace', 'one']


@pytest.mark.parametrize('lookup', (
    ['--lookup-doi', '10.5555/example', '--allow-online-doi', '--workspace', 'one'],
    ISBN_REQUEST,
))
@pytest.mark.parametrize('other', (
    ['studio', 'voices'],
    ['studio', 'render', '--script', 'synthetic.txt', '--output', 'synthetic-output'],
    ['assistant', '--instructions', 'synthetic.txt', '--run'],
    ['--document-formats'],
))
def test_canonical_entrypoint_rejects_mixed_identifier_operations_before_side_effects(
        lookup, other, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('Mixed operations must not open a library, start audio or initialize acquisition.')
    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
    monkeypatch.setattr('carr_library.studio.cli.ProductionService', forbidden)
    monkeypatch.setattr('carr_library.acquisition.service.service_for', forbidden)
    monkeypatch.setattr('carr_library.native_runtime.activate_bundled_runtime', forbidden)
    # Canonical uses studio/assistant subcommands. The standalone format flag
    # belongs to the separate producer and must not bypass lookup validation.
    with pytest.raises(SystemExit) as result:
        main([*other, *lookup])
    assert result.value.code == 2 and 'error:' in capsys.readouterr().err


def test_real_canonical_studio_jobs_cli_remains_offline_and_reopens_after_lookup_merge(
        tmp_path, monkeypatch, capsys):
    import sys
    def forbidden(*args, **kwargs):
        raise AssertionError('Listing local jobs must not initialize metadata acquisition or speech.')
    monkeypatch.setattr('carr_library.acquisition.service.service_for', forbidden)
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry', forbidden)
    monkeypatch.setitem(sys.modules, 'wx', None)
    directory = tmp_path / 'canonical-studio-library'
    arguments = ['studio', '--data-dir', str(directory), 'jobs']
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert (directory / 'library.sqlite').is_file()
    assert (directory / 'state' / 'studio' / 'jobs').is_dir()
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == []


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
    ['--lookup-doi', '10.5555/example', '--allow-online-doi', '--workspace', 'one',
     '--list-ocr-models'],
    ['--lookup-isbn', '9780306406157', '--allow-online-isbn', '--workspace', 'one',
     '--install-ocr-models', 'offline-models'],
    ['--lookup-isbn', '9780306406157', '--allow-online-isbn', '--workspace', 'one',
     '--verify-ocr-models', 'eng'],
))
def test_missing_or_ambiguous_consent_is_rejected_before_app_or_network(arguments, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid command must not open a library or start acquisition.')
    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
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


@pytest.mark.parametrize('arguments', (
    ['--lookup-isbn', '9780306406157'],
    ['--lookup-isbn', '9780306406157', '--allow-online-isbn'],
    [*ISBN_REQUEST, '--workspace', 'two'],
    ['--allow-online-isbn'],
    ['--save-isbn-reference'],
    [*ISBN_REQUEST, '--allow-online-doi'],
    [*ISBN_REQUEST, '--doi-provider', 'openalex'],
    [*ISBN_REQUEST, '--unpaywall-email-env', 'PRIVATE_CONTACT'],
    [*ISBN_REQUEST, '--unpaywall-email-env', ''],
    [*ISBN_REQUEST, '--save-doi-reference'],
    [*ISBN_REQUEST, '--download-source-url', 'https://example.org/paper.pdf'],
    [*ISBN_REQUEST, '--download-source-url', ''],
    [*ISBN_REQUEST, '--acquisition-folder', 'new-folder'],
    [*ISBN_REQUEST, '--confirm-source-rights'],
    [*ISBN_REQUEST, '--lookup-doi', '10.1234/test'],
    [*ISBN_REQUEST, '--search', 'private words'],
    [*ISBN_REQUEST, '--backup', 'backup-folder'],
    [*ISBN_REQUEST, '--self-test'],
    ['--lookup-doi', '10.1234/test', '--allow-online-doi', '--workspace', 'one', '--allow-online-isbn'],
    ['--lookup-doi', '10.1234/test', '--allow-online-doi', '--workspace', 'one', '--save-isbn-reference'],
))
def test_isbn_consent_and_cross_operation_flags_fail_before_opening_library(arguments, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid ISBN arguments must not create a library or perform network work.')
    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
    with pytest.raises(SystemExit) as result:
        main(arguments)
    assert result.value.code == 2 and 'error:' in capsys.readouterr().err


def test_isbn_headless_lookup_uses_separate_api_and_renders_typed_identity(monkeypatch, capsys):
    from dataclasses import dataclass
    from carr_library.acquisition.models import MetadataPreview
    from carr_library.acquisition.service import AcquisitionPreview
    metadata = MetadataPreview(provider='openlibrary', doi='', title='Synthetic book',
        authors=('Олена Коваль',), isbn=('9780306406157', '0306406152'),
        source_url='https://openlibrary.org/books/OL123M', raw_json=b'{"private":"raw-only"}')
    preview = AcquisitionPreview('private-token', 'selected', '', metadata, (metadata,), (), (),
        '2026-11-26T00:00:00Z', identifier_kind='isbn', identifier='9780306406157')
    @dataclass
    class Reference:
        id: str = 'saved-isbn-reference'
    calls = []
    class App:
        def lookup_isbn(self, workspace, isbn, **options):
            calls.append(('isbn', workspace, isbn, options))
            return preview
        def lookup_doi(self, *args, **kwargs):
            raise AssertionError('ISBN must not use DOI providers.')
        def save_doi_reference(self, workspace, candidate):
            assert candidate is preview
            calls.append(('save', workspace))
            return Reference()
    # Direct runner use is also defensive: DOI email options are never read.
    monkeypatch.setenv('PRIVATE_CONTACT', 'private-contact@example.org')
    args = SimpleNamespace(workspaces=['selected'], lookup_isbn='978-0-306-40615-7',
        allow_online_isbn=True, save_isbn_reference=True, unpaywall_email_env='PRIVATE_CONTACT')
    assert run(App(), args) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result['preview']['identifier_kind'] == 'isbn'
    assert result['preview']['identifier'] == '9780306406157'
    assert result['preview']['doi'] == '' and result['preview']['sources'] == []
    assert result['reference']['id'] == 'saved-isbn-reference'
    assert calls == [('isbn', 'selected', '978-0-306-40615-7', {'online_allowed': True}), ('save', 'selected')]
    for private in ('raw-only', 'raw_json', 'private-token', 'private-contact'):
        assert private not in output


def test_openalex_option_reaches_doi_api_without_loading_desktop(monkeypatch, capsys, tmp_path):
    import sys
    from carr_library.acquisition.models import MetadataPreview
    from carr_library.acquisition.service import AcquisitionPreview
    metadata = MetadataPreview(provider='openalex', doi='10.1234/test', title='Synthetic work')
    preview = AcquisitionPreview('test-token', 'selected', metadata.doi, metadata, (metadata,), (), (),
        '2026-11-26T00:00:00Z', identifier='10.1234/test')
    calls = []
    class App:
        def lookup_doi(self, workspace, doi, **options):
            calls.append((workspace, doi, options))
            return preview
    monkeypatch.setattr('carr_library.application.LibraryApp', lambda *args, **kwargs: App())
    monkeypatch.setattr('carr_library.native_runtime.activate_bundled_runtime', lambda: None)
    monkeypatch.setitem(sys.modules, 'wx', None)
    assert main(['--data-dir', str(tmp_path / 'unused'), '--workspace', 'selected',
        '--lookup-doi', '10.1234/test', '--allow-online-doi', '--doi-provider', 'openalex']) == 0
    assert calls == [('selected', '10.1234/test', dict(online_allowed=True,
        providers=('openalex',), unpaywall_email=''))]
    assert json.loads(capsys.readouterr().out)['preview']['metadata']['provider'] == 'openalex'


def test_real_isbn_cli_lookup_and_save_dispatch_without_wx(monkeypatch, capsys, tmp_path):
    """Actual argument parser, application, provider parser and SQLite; synthetic HTTP."""
    import sys
    from carr_library.application import LibraryApp
    from carr_library.acquisition.service import AcquisitionService
    app = LibraryApp(tmp_path / 'isbn-cli-library')
    workspace = app.create_workspace('ISBN command line')
    requests = []
    def transport(permission):
        assert permission.allowed is True and permission.purpose == 'metadata'
        assert permission.approved_hosts == ('openlibrary.org',)
        def request(value, *, cancelled):
            assert not cancelled()
            requests.append(value)
            return json.dumps({'ISBN:9780306406157': {
                'identifiers': {'isbn_13': ['9780306406157'], 'isbn_10': ['0306406152']},
                'title': 'Synthetic ISBN book', 'authors': [{'name': 'Олена Коваль'}],
                'publish_date': '2024', 'url': 'https://openlibrary.org/books/OL123M'}}).encode('utf-8')
        return request
    app._acquisition_service = AcquisitionService(app, transport_factory=transport)
    monkeypatch.setattr('carr_library.application.LibraryApp', lambda *args, **kwargs: app)
    monkeypatch.setattr('carr_library.native_runtime.activate_bundled_runtime', lambda: None)
    monkeypatch.setitem(sys.modules, 'wx', None)
    args = ['--data-dir', str(app.root), '--workspace', workspace,
        '--lookup-isbn', '978-0-306-40615-7', '--allow-online-isbn', '--save-isbn-reference']
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['preview']['identifier_kind'] == 'isbn' and first['preview']['doi'] == ''
    assert first['reference']['title'] == 'Synthetic ISBN book'
    assert first['reference']['isbn'] == '9780306406157'
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second['reference']['id'] == first['reference']['id']
    assert len(requests) == 1 and requests[0].provider == 'openlibrary'



def _cli_download_fixture(sources, state):
    from dataclasses import dataclass
    from pathlib import Path

    @dataclass
    class Metadata:
        provider: str = 'openalex'
        raw_json: bytes = b'{"private":"raw-only"}'

    @dataclass
    class Source:
        url: str
        reported_open_access: bool
        format: str

    @dataclass
    class Result:
        state: str
        path: Path = Path('synthetic.pdf')
        receipt_path: Path = Path('receipt.json')

    record = Metadata()
    preview = SimpleNamespace(doi='10.5555/example', workspace='one',
        fetched_utc='2026-11-27T00:00:00Z', cached=False, metadata=record,
        provider_records=(record,), sources=tuple(Source(*source) for source in sources),
        warnings=(), identifier_kind='doi', identifier='10.5555/example')
    calls = []

    class App:
        def lookup_doi(self, workspace, doi, **options):
            assert (workspace, doi) == ('one', preview.doi)
            assert options['online_allowed'] is True
            assert options['providers'] == ('openalex',)
            return preview

        def acquire_doi_source(self, workspace, candidate, index, folder, **options):
            assert candidate is preview
            calls.append((workspace, index, folder, options))
            return Result(state)

    args = SimpleNamespace(workspaces=['one'], lookup_doi=preview.doi,
        allow_online_doi=True, doi_provider=['openalex'], unpaywall_email_env=None,
        download_source_url='https://example.org/paper.pdf', acquisition_folder=Path('new'),
        confirm_source_rights=True, save_doi_reference=False)
    return App(), args, calls


@pytest.mark.parametrize('state,expected', (
    ('complete', 0), ('duplicate', 0), ('quarantined', 2), ('failed', 2),
    ('imported_reference_pending', 2),
))
def test_cli_download_status_reports_partial_results(state, expected, capsys):
    from pathlib import Path
    app, args, calls = _cli_download_fixture((('https://example.org/paper.pdf', True, 'pdf'),), state)
    assert run(app, args) == expected
    assert calls == [('one', 0, Path('new'), {'online_allowed': True, 'rights_confirmed': True})]
    output = capsys.readouterr().out
    assert json.loads(output)['acquisition']['state'] == state
    assert 'raw_json' not in output and 'raw-only' not in output


@pytest.mark.parametrize('sources', (
    (),
    (('https://example.org/paper.pdf', False, 'pdf'),),
    (('https://example.org/paper.pdf', True, 'html'),),
    (('https://example.org/other.pdf', True, 'pdf'),),
    (('https://example.org/paper.pdf', True, 'pdf'), ('https://example.org/paper.pdf', True, 'pdf')),
))
def test_cli_wrong_or_ambiguous_source_never_downloads(sources):
    app, args, calls = _cli_download_fixture(sources, 'complete')
    with pytest.raises(ValueError, match='missing or ambiguous'):
        run(app, args)
    assert not calls
