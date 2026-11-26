"""Provider schema and consent checks using synthetic, offline JSON responses.

These tests exercise real parsers and immutable DTOs. The injected boundary
never opens a socket; passing them is not a live-provider availability claim.
"""
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from urllib.parse import unquote, urlsplit

import pytest

from carr_library.acquisition.models import (
    AccessLocation, MetadataPreview, ProviderConfig, ProviderRequest,
    MAX_RESPONSE_BYTES, MAX_TEXT, MAX_AUTHORS, MAX_LOCATIONS,
    normalize_doi, safe_source_url,
)
from carr_library.acquisition.providers import (
    lookup_doi, provider_request, parse_crossref, parse_unpaywall, parse_europe_pmc,
)


DOI = '10.1234/test'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def crossref(**changes):
    work = dict(DOI=DOI, title=['Точне джерело'], type='journal-article',
        author=[{'given': 'Олексій', 'family': 'Тест'}, {'name': '東京研究会'}],
        issued={'date-parts': [[2024, 5, 9]]},
        created={'date-parts': [[2026, 1, 1]]},
        publisher='Synthetic press', volume='7', issue='2', page='e12–e19',
        language='uk', abstract='<jats:p>Metadata abstract only.</jats:p>',
        **{'container-title': ['Test journal']})
    work.update(changes)
    return {'status': 'ok', 'message-type': 'work', 'message': work}


def unpaywall(**changes):
    record = dict(doi=DOI, title='Test article', year=2024, genre='journal-article',
        journal_name='Synthetic journal', publisher='Synthetic press', is_oa=True,
        oa_locations=[dict(url='https://repository.example/document.pdf',
            url_for_pdf='https://repository.example/document.pdf',
            url_for_landing_page='https://repository.example/record',
            host_type='repository', license='cc-by', version='acceptedVersion')])
    record.update(changes)
    return record


def epmc_record(**changes):
    record = dict(doi=DOI, title='Exact biomedical record', pubYear='2023',
        id='123456', source='MED', isOpenAccess='Y',
        authorList={'author': [{'fullName': 'Petrenko O'}, {'collectiveName': 'Research Group'}]},
        journalInfo={'journal': {'title': 'Journal'}, 'volume': '9', 'issue': '1'},
        pubTypeList={'pubType': ['Journal Article']}, pageInfo='17–23',
        abstractText='Abstract\ntext', language='eng',
        fullTextUrlList={'fullTextUrl': [dict(url='https://europepmc.org/articles/PMC123?pdf=render',
            availability='Open access', availabilityCode='OA', documentStyle='pdf', site='Europe_PMC')]})
    record.update(changes)
    return record


def epmc(*records, count=None):
    return {'hitCount': len(records) if count is None else count,
            'resultList': {'result': list(records)}}


def test_crossref_order_unicode_publication_date_and_exact_raw_provenance():
    raw = b'\n ' + encoded(crossref(ISBN=['9780306406157'])) + b' \n'
    result = parse_crossref(raw, 'https://doi.org/10.1234/TEST')
    assert result.doi == DOI
    assert result.title == 'Точне джерело'
    assert result.authors == ('Олексій Тест', '東京研究会')
    assert result.year == '2024'  # issued, never registration/deposit date
    assert result.item_type == 'article' and result.container_title == 'Test journal'
    assert (result.volume, result.issue, result.pages, result.language) == ('7', '2', 'e12–e19', 'uk')
    assert result.isbn == ('9780306406157',)
    assert result.abstract == '<jats:p>Metadata abstract only.</jats:p>'
    assert result.raw_json == raw and result.raw_sha256 == sha256(raw).hexdigest()
    assert 'raw_json=' not in repr(result)


def test_crossref_does_not_infer_rights_or_copy_version_specific_license():
    record = crossref(link=[{'URL': 'https://publisher.example/article.pdf',
        'content-type': 'application/pdf', 'content-version': 'vor'}],
        license=[{'URL': 'https://creativecommons.org/licenses/by/4.0/',
            'content-version': 'am', 'start': {'date-parts': [[2099, 1, 1]]}}])
    result = parse_crossref(encoded(record), DOI)
    location, = result.locations
    assert location.format == 'pdf' and location.version == 'vor'
    assert location.license == '' and location.reported_open_access is None
    assert any('version-specific' in warning for warning in result.warnings)


def test_crossref_multiple_titles_and_invalid_isbn_are_explicit_not_silent():
    result = parse_crossref(encoded(crossref(title=['First', 'Second'],
        ISBN=['9780306406157', 'invalid'])), DOI)
    assert result.title == 'First' and result.isbn == ('9780306406157',)
    assert any('multiple titles' in warning for warning in result.warnings)
    assert any('checksum' in warning for warning in result.warnings)
    assert b'Second' in result.raw_json and b'invalid' in result.raw_json


def test_missing_fields_are_empty_without_fabricating_authors_year_or_title():
    record = {'status': 'ok', 'message-type': 'work', 'message': {'DOI': DOI}}
    result = parse_crossref(encoded(record), DOI)
    assert (result.title, result.authors, result.year, result.language) == ('', (), '', '')
    assert result.item_type == 'other' and result.locations == ()


@pytest.mark.parametrize('field', ('published', 'published-print', 'published-online'))
def test_publication_year_falls_back_only_to_an_actual_publication_date(field):
    record = crossref(issued=None, **{field: {'date-parts': [[2019, 2]]}})
    assert parse_crossref(encoded(record), DOI).year == '2019'
    record['message']['issued'] = {'date-parts': [[2018]]}
    assert parse_crossref(encoded(record), DOI).year == '2018'


def test_registration_date_cannot_stand_in_for_missing_publication_year():
    assert parse_crossref(encoded(crossref(issued=None)), DOI).year == ''


@pytest.mark.parametrize('value,expected', (
    (' DOI:10.1234/ABC ', '10.1234/abc'),
    ('https://doi.org/10.1234%2FABC%25', '10.1234/abc%'),
    ('10.1234/ÄA', '10.1234/Äa'),
    ('10.1234/äA', '10.1234/äa'),
    ('10.1234/ABC%25', '10.1234/abc%25'),
))
def test_doi_uses_catalog_normalization_without_unicode_case_equivalence(value, expected):
    assert normalize_doi(value) == expected


@pytest.mark.parametrize('value', ('', None, 'not-a-doi', 'https://doi.org/10.1/x?token=secret',
    'https://user:secret@doi.org/10.1/x', 'https://doi.org/10.1/x#fragment'))
def test_invalid_doi_is_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        normalize_doi(value)


@pytest.mark.parametrize('parser,record', (
    (parse_crossref, crossref(DOI='10.1234/wrong')),
    (parse_unpaywall, unpaywall(doi='10.1234/wrong')),
    (parse_europe_pmc, epmc(epmc_record(doi='10.1234/wrong'))),
))
def test_exact_doi_mismatch_never_becomes_a_preview(parser, record):
    with pytest.raises(ValueError, match='DOI|exact match'):
        parser(encoded(record), DOI)


def test_non_ascii_doi_case_is_not_collapsed_to_another_identifier():
    with pytest.raises(ValueError, match='exactly match'):
        parse_crossref(encoded(crossref(DOI='10.1234/ä')), '10.1234/Ä')


def test_unpaywall_reports_active_location_dedupes_best_and_excludes_embargoes():
    record = unpaywall()
    record['best_oa_location'] = dict(record['oa_locations'][0])
    record['oa_locations_embargoed'] = [{'url_for_pdf': 'https://repository.example/future.pdf'}]
    record['z_authors'] = [{'family': 'Stale'}]
    result = parse_unpaywall(encoded(record), DOI)
    location, = result.locations
    assert location.url.endswith('document.pdf') and location.format == 'pdf'
    assert location.license == 'cc-by' and location.version == 'acceptedVersion'
    assert location.reported_open_access is True and location.host_type == 'repository'
    assert result.authors == ()
    assert any('embargoed' in warning for warning in result.warnings)
    assert any('deprecated' in warning for warning in result.warnings)
    assert all('future.pdf' not in item.url for item in result.locations)


@pytest.mark.parametrize('license_value', (None, '', 'implied-oa'))
def test_open_access_does_not_invent_a_license(license_value):
    record = unpaywall()
    record['oa_locations'][0]['license'] = license_value
    result = parse_unpaywall(encoded(record), DOI)
    assert result.locations[0].license == (license_value or '')
    assert any('no explicit reuse license' in warning for warning in result.warnings)


@pytest.mark.parametrize('oa', ('false', 0, None, False))
def test_unpaywall_rejects_invalid_or_contradictory_open_access_flags(oa):
    with pytest.raises(ValueError):
        parse_unpaywall(encoded(unpaywall(is_oa=oa)), DOI)


def test_unpaywall_closed_record_is_valid_metadata_without_sources():
    result = parse_unpaywall(encoded(unpaywall(is_oa=False, oa_locations=[])), DOI)
    assert result.title == 'Test article' and result.locations == ()


def test_europe_pmc_selects_exact_second_record_not_first_search_hit():
    result = parse_europe_pmc(encoded(epmc(epmc_record(doi='10.1234/unrelated'), epmc_record())), DOI)
    assert result.title == 'Exact biomedical record'
    assert result.authors == ('Petrenko O', 'Research Group')
    assert result.year == '2023' and result.item_type == 'article'
    assert (result.volume, result.issue, result.pages) == ('9', '1', '17–23')
    assert result.abstract == 'Abstract\ntext'
    assert result.locations[0].reported_open_access is True
    assert result.locations[0].url == 'https://europepmc.org/articles/PMC123?pdf=render'


def test_europe_pmc_distinguishes_free_subscription_and_unknown_from_oa():
    links = [dict(url=f'https://repository.example/{code or "unknown"}.pdf',
                  documentStyle='pdf', availabilityCode=code)
             for code in ('OA', 'F', 'S', '')]
    result = parse_europe_pmc(encoded(epmc(epmc_record(license='CC BY',
        fullTextUrlList={'fullTextUrl': links}))), DOI)
    assert tuple(item.reported_open_access for item in result.locations) == (True, None, False, None)
    assert all(item.license == '' for item in result.locations)
    assert any('free to read' in warning for warning in result.warnings)
    assert any('record-level' in warning for warning in result.warnings)


def test_europe_pmc_does_not_invent_pdf_links_from_pmcid_or_has_pdf():
    record = epmc_record(pmcid='PMC123', hasPDF='Y', fullTextUrlList=None,
                         authorList=None, authorString='Unstructured A, Names B')
    result = parse_europe_pmc(encoded(epmc(record)), DOI)
    assert result.locations == () and result.authors == ()
    assert any('unstructured author' in warning for warning in result.warnings)


@pytest.mark.parametrize('record', (
    epmc(epmc_record(), count=26),
    epmc(epmc_record(), count=0),
    epmc(epmc_record(), epmc_record()),
    epmc(),
))
def test_europe_pmc_refuses_truncation_no_match_and_ambiguous_versions(record):
    with pytest.raises(ValueError):
        parse_europe_pmc(encoded(record), DOI)


def test_request_endpoints_escape_doi_and_keep_contact_address_out_of_repr():
    doi = '10.1234/a?x=1&b=#c;d'
    config = ProviderConfig(unpaywall_email='reader@example.org', crossref_mailto='reader@example.org')
    for provider in ('crossref', 'unpaywall'):
        request = provider_request(provider, doi, config=config)
        parts = urlsplit(request.endpoint)
        assert not parts.query and not parts.fragment
        assert unquote(parts.path.split('/', 2)[2]) == doi
        assert 'reader@example.org' not in repr(request)
    assert 'reader@example.org' not in repr(config)


def test_europe_pmc_query_escapes_lucene_quotes_and_limits_full_core_results():
    request = provider_request('europe_pmc', '10.1234/a"OR*')
    query = dict(request.query)
    assert query == {'query': 'DOI:"10.1234/a\\"or*"', 'format': 'json',
                     'resultType': 'core', 'pageSize': '25'}
    assert request.endpoint == 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
    result = parse_europe_pmc(encoded(epmc(epmc_record(doi='10.1234/a"OR*'))), '10.1234/a"OR*')
    assert result.doi == '10.1234/a"or*'


@pytest.mark.parametrize('consent', (False, 0, 1, 'yes', 'true', None, object()))
def test_no_lookup_without_literal_per_call_consent(consent):
    called = []
    with pytest.raises(PermissionError):
        lookup_doi('crossref', DOI, consent=consent,
            transport=lambda *args, **kwargs: called.append(args))
    assert called == []


def test_lookup_calls_only_selected_provider_once_and_passes_cancellation():
    calls = []
    cancelled = lambda: False
    def transport(request, *, cancelled):
        calls.append((request, cancelled))
        return encoded(crossref())
    result = lookup_doi('crossref', DOI, transport=transport, consent=True, cancelled=cancelled)
    assert result.provider == 'crossref' and len(calls) == 1
    assert calls[0][0].provider == 'crossref' and calls[0][1] is cancelled


def test_unpaywall_requires_user_contact_before_transport():
    calls = []
    with pytest.raises(ValueError, match='configured contact email'):
        lookup_doi('unpaywall', DOI, consent=True,
            transport=lambda *args, **kwargs: calls.append(args))
    assert calls == []


@pytest.mark.parametrize('email', ('bad', 'user@example.org\n', 'user name@example.org', 1, None))
def test_provider_contact_rejects_invalid_input_without_echoing_it(email):
    with pytest.raises(ValueError) as caught:
        ProviderConfig(unpaywall_email=email)
    if isinstance(email, str):
        assert email not in str(caught.value)


def test_cancellation_before_or_after_transport_never_returns_a_preview():
    calls = []
    with pytest.raises(InterruptedError):
        lookup_doi('crossref', DOI, consent=True, cancelled=lambda: True,
            transport=lambda *args, **kwargs: calls.append(args))
    assert calls == []
    state = [False]
    def transport(request, *, cancelled):
        state[0] = True
        return encoded(crossref())
    with pytest.raises(InterruptedError):
        lookup_doi('crossref', DOI, consent=True, transport=transport, cancelled=lambda: state[0])


@pytest.mark.parametrize('url', (
    'http://repository.example/paper.pdf', 'file:///etc/passwd',
    'https://user:secret@repository.example/paper.pdf',
    'https://127.0.0.1/paper.pdf', 'https://[::1]/paper.pdf',
    'https://localhost/paper.pdf', 'https://storage.internal/paper.pdf',
    'https://repository.example:8443/paper.pdf',
    'https://repository.example/paper.pdf?access_token=secret',
    'https://repository.example/paper.pdf?X-Amz-Signature=secret',
    'https://repository.example/paper.pdf?api_key=secret',
    'https://repository.example/paper.pdf#private',
    'https://repository.example/paper%0aInjected.pdf',
    'https://repository.example/paper.pdf\n',
))
def test_unsafe_source_links_are_rejected_and_never_echoed(url):
    with pytest.raises(ValueError) as caught:
        safe_source_url(url)
    assert url not in str(caught.value) and 'secret' not in str(caught.value)
    result = parse_crossref(encoded(crossref(link=[{'URL': url, 'content-type': 'application/pdf'}])), DOI)
    assert result.locations == ()
    assert any('omitted' in warning for warning in result.warnings)
    assert all(url not in warning and 'secret' not in warning for warning in result.warnings)


@pytest.mark.parametrize('raw', (
    b'', b'\xff', b'[]', b'{"a":1,"a":2}', b'{"a":NaN}',
    b'{"a":Infinity}', b'{not JSON}', b'[' * 40 + b'0' + b']' * 40,
))
def test_malformed_or_ambiguous_json_is_not_accepted(raw):
    with pytest.raises(ValueError):
        parse_crossref(raw, DOI)


def test_exact_response_limit_is_checked_before_json_decoding():
    base = encoded(crossref())
    raw = base + b' ' * (MAX_RESPONSE_BYTES - len(base))
    assert parse_crossref(raw, DOI).raw_json == raw
    with pytest.raises(ValueError, match='2 MiB'):
        parse_crossref(raw + b' ', DOI)


@pytest.mark.parametrize('changes', (
    {'title': ['x' * (MAX_TEXT + 1)]},
    {'author': [{'name': 'Author'}] * (MAX_AUTHORS + 1)},
    {'link': [{'URL': 'https://repository.example/a'}] * (MAX_LOCATIONS + 1)},
    {'title': 'Not an array'}, {'author': 'Not a list'},
    {'volume': {'not': 'text'}}, {'issued': {'date-parts': [[True]]}},
))
def test_excess_or_wrong_type_metadata_is_rejected_instead_of_truncated(changes):
    with pytest.raises(ValueError):
        parse_crossref(encoded(crossref(**changes)), DOI)


def test_dataclasses_are_deeply_immutable_and_digest_binds_exact_bytes():
    raw = encoded(crossref())
    preview = parse_crossref(raw, DOI)
    with pytest.raises(FrozenInstanceError):
        preview.title = 'Changed'
    with pytest.raises(ValueError):
        replace(preview, authors=['Changed'])
    with pytest.raises(ValueError):
        replace(preview, raw_json=b'{}')
    with pytest.raises(ValueError):
        MetadataPreview('crossref', DOI, raw_json=b'{}', raw_sha256='0' * 64)
    assert MetadataPreview('crossref', DOI, raw_json=b'{}').raw_sha256 == sha256(b'{}').hexdigest()


def test_location_identity_binds_all_permission_and_version_claims():
    original = AccessLocation('unpaywall', 'https://repository.example/paper.pdf',
        format='pdf', license='cc-by', version='acceptedVersion', reported_open_access=True)
    assert original.id == replace(original).id and original.host == 'repository.example'
    changed = (replace(original, license='cc-by-nc'), replace(original, version='publishedVersion'),
        replace(original, reported_open_access=None), replace(original, provider='crossref'),
        replace(original, landing_url='https://repository.example/other'),
        replace(original, host_type='publisher'), replace(original, format='html'))
    assert all(item.id != original.id for item in changed)
    with pytest.raises(ValueError):
        replace(original, reported_open_access=1)


def test_provider_request_does_not_allow_unrelated_host_or_arbitrary_queries():
    with pytest.raises(ValueError):
        ProviderRequest('crossref', 'https://attacker.example/works/10.1234%2Ftest')
    with pytest.raises(ValueError):
        ProviderRequest('crossref', 'https://api.crossref.org/members')
    with pytest.raises(ValueError):
        ProviderRequest('crossref', 'https://api.crossref.org/works/10.1234%2Ftest',
                        (('private_document', 'secret'),))


@pytest.mark.parametrize('control', ('\u0080', '\u202e', '\u2066', '\u2028', '\ud800'))
def test_metadata_controls_are_rejected_before_preview_or_catalog_apply(control):
    raw = json.dumps(crossref(title=['Visible' + control + 'name'])).encode('utf-8')
    with pytest.raises(ValueError, match='control'):
        parse_crossref(raw, DOI)


def test_normal_rtl_unicode_names_remain_unchanged():
    name = 'أحمد محمود'
    result = parse_crossref(encoded(crossref(author=[{'name': name}])), DOI)
    assert result.authors == (name,)
