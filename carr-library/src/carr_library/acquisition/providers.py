"""Bounded DOI parsers and explicit clients with an injected transport.

No function in this module opens a socket, reads a file or changes a catalog.
Schemas are based on the providers' primary documentation:
https://www.crossref.org/documentation/retrieve-metadata/rest-api/
https://github.com/CrossRef/rest-api-doc/blob/master/api_format.md
https://unpaywall.org/products/api and https://unpaywall.org/data-format
https://europepmc.org/docs/EBI_Europe_PMC_Web_Service_Reference.pdf

A metadata URL, full-text link or reported open-access flag does not establish
download/reuse permission. Crossref version-specific licenses are not assigned
to unrelated links, and embargoed Unpaywall locations are not active sources.
"""
from hashlib import sha256
import json
import re
from urllib.parse import quote, urlencode

from ..research.academic_library import normalize_isbn
from .models import (AccessLocation, MetadataPreview, ProviderConfig,
    ProviderRequest, MAX_RESPONSE_BYTES, MAX_TEXT, MAX_ABSTRACT, MAX_AUTHORS,
    MAX_LOCATIONS, normalize_doi, safe_source_url, text_value)


EUROPE_PMC_PAGE_SIZE = 25


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Acquisition cancelled.')


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f'The provider returned an invalid {name} object.')
    return value


def _optional_object(value, name):
    return {} if value is None else _object(value, name)


def _list(value, name, maximum=100):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'The provider returned invalid or excessive {name}; limit {maximum}.')
    return value


def _text(value, name, maximum=MAX_TEXT, *, multiline=False):
    if value is None:
        return ''
    text_value(value, name, maximum, multiline=multiline)
    return value.strip()


def _json(raw, cancelled):
    _cancel(cancelled)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise ValueError('The provider response is empty or exceeds the 2 MiB response limit.')

    def pairs(values):
        result = {}
        for name, value in values:
            if name in result:
                raise ValueError('The provider response contains duplicate JSON fields.')
            result[name] = value
        return result

    def constant(_value):
        raise ValueError('The provider returned a nonstandard JSON number.')

    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('The provider returned invalid UTF-8 JSON.') from None
    pending, count = [(result, 0)], 0
    while pending:
        _cancel(cancelled)
        value, depth = pending.pop()
        count += 1
        if depth > 32 or count > 100_000:
            raise ValueError('The provider response exceeds the JSON structure limit.')
        if isinstance(value, dict):
            pending.extend((part, depth + 1) for part in value.values())
        elif isinstance(value, list):
            pending.extend((part, depth + 1) for part in value)
    return _object(result, 'response')


def _exact_doi(value, expected):
    try:
        actual = normalize_doi(value)
    except (ValueError, TypeError):
        raise ValueError('The provider did not return a valid DOI.') from None
    if actual != expected:
        raise ValueError('The provider DOI does not exactly match the requested DOI.')


def _first_text(value, name, warnings):
    values = [_text(item, name) for item in _list(value, name)]
    if len(values) > 1:
        warnings.append(f'The provider returned multiple {name}; the preview uses the first value. All values remain in the raw response.')
    return values[0] if values else ''


def _year(value):
    if value in (None, ''):
        return ''
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,4}', value)
            or not 1 <= int(value) <= 9999):
        raise ValueError('The provider returned an invalid publication year.')
    return value


def _crossref_year(record):
    # Prefer Crossref's earliest publication date. Some valid works supply only
    # a specific publication date; registration/deposit dates are never used.
    for field in ('issued', 'published', 'published-print', 'published-online'):
        published = _optional_object(record.get(field), 'publication date')
        dates = _list(published.get('date-parts'), 'publication date parts', 2)
        if dates:
            first = _list(dates[0], 'publication date components', 3)
            if first:
                return _year(first[0])
    return ''


def _item_type(value):
    value = _text(value, 'Document type').lower()
    return {'journal-article': 'article', 'article': 'article',
            'journal article': 'article', 'book': 'book', 'monograph': 'book',
            'edited-book': 'book', 'reference-book': 'book',
            'dissertation': 'thesis', 'thesis': 'thesis', 'report': 'report',
            'webpage': 'webpage'}.get(value, 'other')


def _format(value):
    value = _text(value, 'Document format').lower().split(';', 1)[0].strip()
    return {'pdf': 'pdf', 'application/pdf': 'pdf', 'xml': 'xml',
            'text/xml': 'xml', 'application/xml': 'xml',
            'html': 'html', 'text/html': 'html'}.get(value, 'unknown')


def _url(value, warnings, label='source URL'):
    if value in (None, ''):
        return ''
    try:
        return safe_source_url(value)
    except ValueError:
        # Never echo credentials or tokens from an untrusted URL into the UI.
        warnings.append(f'An unsafe or unsupported {label} was omitted; the original value remains in the raw response.')
        return ''


def _unique_locations(locations):
    result, seen = [], set()
    for location in locations:
        if location.id not in seen:
            result.append(location)
            seen.add(location.id)
    if len(result) > MAX_LOCATIONS:
        raise ValueError('The provider returned too many distinct source locations.')
    return tuple(result)


def _preview(provider, doi, raw, *, warnings, locations, **fields):
    return MetadataPreview(provider=provider, doi=doi, raw_json=raw,
        raw_sha256=sha256(raw).hexdigest(), warnings=tuple(dict.fromkeys(warnings)),
        locations=_unique_locations(locations), **fields)


def parse_crossref(raw, doi, *, cancelled=lambda: False):
    """Parse one /works/{DOI} response, retaining deposited metadata verbatim."""
    doi = normalize_doi(doi)
    envelope = _json(raw, cancelled)
    if envelope.get('status') != 'ok' or envelope.get('message-type') != 'work':
        raise ValueError('Crossref did not return a successful single-work response.')
    record = _object(envelope.get('message'), 'Crossref work')
    _exact_doi(record.get('DOI'), doi)
    warnings, authors, locations, isbns = [], [], [], []
    for author in _list(record.get('author'), 'authors', MAX_AUTHORS):
        _cancel(cancelled)
        author = _object(author, 'author')
        literal = _text(author.get('name'), 'Author name')
        given, family = _text(author.get('given'), 'Given name'), _text(author.get('family'), 'Family name')
        name = literal or ' '.join(part for part in (given, family) if part)
        if name:
            text_value(name, 'Author name')
            authors.append(name)
        else:
            warnings.append('An author with no deposited name was omitted from the preview.')
    for isbn in _list(record.get('ISBN'), 'ISBNs'):
        _cancel(cancelled)
        isbn = _text(isbn, 'ISBN', 100)
        try:
            normalized = normalize_isbn(isbn)
        except ValueError:
            warnings.append('An ISBN failed checksum validation and remains only in the raw response.')
            continue
        if normalized and normalized not in isbns:
            isbns.append(normalized)
    if record.get('license'):
        _list(record['license'], 'licenses')
        warnings.append('Crossref licenses can be version-specific or take effect later; no license was assigned automatically to a full-text link.')
    for link in _list(record.get('link'), 'full-text links', MAX_LOCATIONS):
        _cancel(cancelled)
        link = _object(link, 'full-text link')
        url = _url(link.get('URL'), warnings)
        if url:
            locations.append(AccessLocation(provider='crossref', url=url,
                format=_format(link.get('content-type')),
                version=_text(link.get('content-version'), 'Version')))
    if locations:
        warnings.append('Crossref full-text links do not establish open access, license permission or an exact downloaded version.')
    title = _first_text(record.get('title'), 'titles', warnings)
    container = _first_text(record.get('container-title'), 'container titles', warnings)
    _cancel(cancelled)
    return _preview('crossref', doi, raw, warnings=warnings, locations=locations,
        title=title, authors=tuple(authors), year=_crossref_year(record),
        item_type=_item_type(record.get('type')), container_title=container,
        publisher=_text(record.get('publisher'), 'Publisher'),
        volume=_text(record.get('volume'), 'Volume'), issue=_text(record.get('issue'), 'Issue'),
        pages=_text(record.get('page'), 'Publication pages'), isbn=tuple(isbns),
        abstract=_text(record.get('abstract'), 'Abstract', MAX_ABSTRACT, multiline=True),
        language=_text(record.get('language'), 'Language'),
        source_url='https://api.crossref.org/works/' + quote(doi, safe=''))


def parse_unpaywall(raw, doi, *, cancelled=lambda: False):
    """Parse active Unpaywall locations; embargoed locations are never offered."""
    doi = normalize_doi(doi)
    record = _json(raw, cancelled)
    _exact_doi(record.get('doi'), doi)
    is_oa = record.get('is_oa')
    if type(is_oa) is not bool:
        raise ValueError('Unpaywall did not return a valid open-access statement.')
    warnings, locations = [], []
    active = _list(record.get('oa_locations'), 'open-access locations', MAX_LOCATIONS)
    best = record.get('best_oa_location')
    if best is not None:
        active = [_object(best, 'best open-access location'), *active]
    if active and not is_oa:
        raise ValueError('Unpaywall returned contradictory active open-access locations.')
    embargoed = _list(record.get('oa_locations_embargoed'), 'embargoed locations', MAX_LOCATIONS)
    if embargoed:
        warnings.append(f'{len(embargoed)} embargoed location(s) were excluded from active sources.')
    if record.get('z_authors'):
        warnings.append('Unpaywall z_authors is deprecated and may be stale; author names were not applied from this field.')
    for location in active:
        _cancel(cancelled)
        location = _object(location, 'open-access location')
        pdf = _url(location.get('url_for_pdf'), warnings, 'PDF URL')
        landing = _url(location.get('url_for_landing_page'), warnings, 'landing URL')
        fallback = _url(location.get('url'), warnings)
        url = pdf or landing or fallback
        if not url:
            warnings.append('A provider location without a safe HTTPS URL was omitted.')
            continue
        license_value = _text(location.get('license'), 'License')
        if license_value in ('', 'implied-oa'):
            warnings.append('At least one open-access location has no explicit reuse license; review permission before downloading.')
        locations.append(AccessLocation(provider='unpaywall', url=url, landing_url=landing,
            format='pdf' if pdf else ('html' if landing else 'unknown'),
            version=_text(location.get('version'), 'Version'), license=license_value,
            reported_open_access=True, host_type=_text(location.get('host_type'), 'Host type')))
    _cancel(cancelled)
    return _preview('unpaywall', doi, raw, warnings=warnings, locations=locations,
        title=_text(record.get('title'), 'Title'), year=_year(record.get('year')),
        item_type=_item_type(record.get('genre')),
        container_title=_text(record.get('journal_name'), 'Journal'),
        publisher=_text(record.get('publisher'), 'Publisher'),
        source_url='https://api.unpaywall.org/v2/' + quote(doi, safe=''))


def _epmc_query(doi):
    return 'DOI:"' + doi.replace('\\', '\\\\').replace('"', '\\"') + '"'


def parse_europe_pmc(raw, doi, *, cancelled=lambda: False):
    """Select an exact DOI from a complete, bounded Europe PMC core result."""
    doi = normalize_doi(doi)
    envelope = _json(raw, cancelled)
    result_list = _optional_object(envelope.get('resultList'), 'Europe PMC result list')
    rows = _list(result_list.get('result'), 'Europe PMC results', EUROPE_PMC_PAGE_SIZE)
    count = envelope.get('hitCount')
    if type(count) is not int or count < 0 or count != len(rows):
        raise ValueError('Europe PMC returned an incomplete or inconsistent DOI result set; no truncated preview was accepted.')
    matches = []
    for row in rows:
        _cancel(cancelled)
        row = _object(row, 'Europe PMC result')
        if row.get('doi'):
            try:
                actual = normalize_doi(row['doi'])
            except (ValueError, TypeError):
                continue
            if actual == doi:
                matches.append(row)
    if not matches:
        raise ValueError('Europe PMC returned no exact match for the requested DOI.')
    if len(matches) != 1:
        raise ValueError('Europe PMC returned multiple records for this DOI; their versions must be reviewed separately.')
    record = matches[0]
    warnings, authors, locations = [], [], []
    author_list = _optional_object(record.get('authorList'), 'author list')
    for author in _list(author_list.get('author'), 'authors', MAX_AUTHORS):
        _cancel(cancelled)
        author = _object(author, 'author')
        name = (_text(author.get('collectiveName'), 'Collective author')
                or _text(author.get('fullName'), 'Author name'))
        if not name:
            name = ' '.join(part for part in (_text(author.get('firstName'), 'Given name'),
                _text(author.get('lastName'), 'Family name')) if part)
        if name:
            text_value(name, 'Author name')
            authors.append(name)
        else:
            warnings.append('An author with no provider name was omitted from the preview.')
    if not authors and record.get('authorString'):
        warnings.append('Only an unstructured author string was supplied; it was not split into invented author identities.')
    journal_info = _optional_object(record.get('journalInfo'), 'journal information')
    journal = _optional_object(journal_info.get('journal'), 'journal')
    book = _optional_object(record.get('bookOrReportDetails'), 'book or report details')
    pub_types = _optional_object(record.get('pubTypeList'), 'publication types')
    mapped = [_item_type(item) for item in _list(pub_types.get('pubType'), 'publication types')]
    item_type = next((item for item in mapped if item != 'other'), 'other')
    links = _optional_object(record.get('fullTextUrlList'), 'full-text URL list')
    if record.get('license'):
        _text(record['license'], 'Record license')
        warnings.append('Europe PMC record-level licensing was not assigned to arbitrary third-party full-text locations.')
    for link in _list(links.get('fullTextUrl'), 'full-text URLs', MAX_LOCATIONS):
        _cancel(cancelled)
        link = _object(link, 'full-text URL')
        url = _url(link.get('url'), warnings)
        if not url:
            continue
        code = _text(link.get('availabilityCode'), 'Availability code')
        # F is free-to-read; it is not a statement of a reuse license. S is
        # subscription-required. Only the provider's OA code claims open access.
        reported_oa = True if code == 'OA' else (False if code == 'S' else None)
        if code == 'F':
            warnings.append('A Europe PMC location is free to read; this alone does not establish open-access reuse permission.')
        locations.append(AccessLocation(provider='europe_pmc', url=url,
            format=_format(link.get('documentStyle')), reported_open_access=reported_oa,
            host_type=_text(link.get('site'), 'Site')))
    if locations:
        warnings.append('Europe PMC location availability does not verify the PDF version or confer reuse rights; review the source license.')
    _cancel(cancelled)
    return _preview('europe_pmc', doi, raw, warnings=warnings, locations=locations,
        title=_text(record.get('title'), 'Title'), authors=tuple(authors),
        year=_year(record.get('pubYear')), item_type=item_type,
        container_title=_text(journal.get('title') or book.get('comprisingTitle'), 'Container title'),
        publisher=_text(book.get('publisher'), 'Publisher'),
        volume=_text(journal_info.get('volume'), 'Volume'), issue=_text(journal_info.get('issue'), 'Issue'),
        pages=_text(record.get('pageInfo'), 'Publication pages'),
        abstract=_text(record.get('abstractText'), 'Abstract', MAX_ABSTRACT, multiline=True),
        language=_text(record.get('language'), 'Language'),
        source_url='https://www.ebi.ac.uk/europepmc/webservices/rest/search?' +
            urlencode({'query': _epmc_query(doi), 'format': 'json', 'resultType': 'core',
                       'pageSize': str(EUROPE_PMC_PAGE_SIZE)}))


def provider_request(provider, doi, *, config=None):
    """Build one fixed-provider request without performing any network work."""
    doi = normalize_doi(doi)
    config = ProviderConfig() if config is None else config
    if not isinstance(config, ProviderConfig):
        raise ValueError('A provider configuration is required.')
    if provider == 'crossref':
        return ProviderRequest(provider, 'https://api.crossref.org/works/' + quote(doi, safe=''),
            (('mailto', config.crossref_mailto),) if config.crossref_mailto else ())
    if provider == 'unpaywall':
        if not config.unpaywall_email:
            raise ValueError('Unpaywall requires your configured contact email; no default account is used.')
        return ProviderRequest(provider, 'https://api.unpaywall.org/v2/' + quote(doi, safe=''),
            (('email', config.unpaywall_email),))
    if provider == 'europe_pmc':
        return ProviderRequest(provider, 'https://www.ebi.ac.uk/europepmc/webservices/rest/search',
            (('query', _epmc_query(doi)), ('format', 'json'), ('resultType', 'core'),
             ('pageSize', str(EUROPE_PMC_PAGE_SIZE))))
    raise ValueError('Unsupported metadata provider.')


def lookup_doi(provider, doi, *, transport, consent=False, config=None,
               cancelled=lambda: False):
    """Make one explicit lookup through a caller-supplied, scoped transport.

    There is no automatic fallback, background query, upload or download. A
    truthy value such as 1 is insufficient: the caller must pass consent=True.
    """
    if consent is not True:
        raise PermissionError('Online DOI lookup requires explicit consent for this request.')
    _cancel(cancelled)
    doi = normalize_doi(doi)
    request = provider_request(provider, doi, config=config)
    _cancel(cancelled)
    raw = transport(request, cancelled=cancelled)
    _cancel(cancelled)
    parser = {'crossref': parse_crossref, 'unpaywall': parse_unpaywall,
              'europe_pmc': parse_europe_pmc}[provider]
    return parser(raw, doi, cancelled=cancelled)
