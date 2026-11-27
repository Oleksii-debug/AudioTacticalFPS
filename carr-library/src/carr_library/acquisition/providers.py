"""Bounded identifier parsers and explicit clients with an injected transport.

No function in this module opens a socket, reads a file or changes a catalog.
Schemas are based on the providers' primary documentation:
https://www.crossref.org/documentation/retrieve-metadata/rest-api/
https://github.com/CrossRef/rest-api-doc/blob/master/api_format.md
https://unpaywall.org/products/api and https://unpaywall.org/data-format
https://europepmc.org/docs/EBI_Europe_PMC_Web_Service_Reference.pdf
https://help.openalex.org/api/get-single-entities/
https://help.openalex.org/data/works/attributes/
https://help.openalex.org/data/locations/
https://openlibrary.org/dev/docs/api/books

A metadata URL, full-text link or reported open-access flag does not establish
download/reuse permission. Crossref version-specific licenses are not assigned
to unrelated links, and embargoed Unpaywall locations are not active sources.
"""
from datetime import date
from hashlib import sha256
import json
import re
from urllib.parse import quote, urlencode

from ..research.academic_library import normalize_isbn, _isbn_key
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


def _openalex_abstract(value, cancelled):
    if value is None:
        return ''
    index = _object(value, 'abstract index')
    words, characters = {}, 0
    for word, positions in index.items():
        _cancel(cancelled)
        word = _text(word, 'Abstract word')
        if not word:
            raise ValueError('The provider returned an empty abstract word.')
        for position in _list(positions, 'abstract positions', MAX_ABSTRACT):
            _cancel(cancelled)
            if (type(position) is not int or not 0 <= position < MAX_ABSTRACT
                    or position in words):
                raise ValueError('The provider returned an invalid or ambiguous abstract index.')
            characters += len(word) + (1 if words else 0)
            if characters > MAX_ABSTRACT:
                raise ValueError('The provider abstract exceeds the preview limit.')
            words[position] = word
    if words and (min(words) != 0 or max(words) != len(words) - 1):
        raise ValueError('The provider returned an incomplete abstract index.')
    return ' '.join(words[position] for position in range(len(words)))


def parse_openalex(raw, doi, *, cancelled=lambda: False):
    """Accept the exact canonical DOI, never a different merged work/version."""
    doi = normalize_doi(doi)
    record = _json(raw, cancelled)
    _exact_doi(record.get('doi'), doi)
    identifiers = _optional_object(record.get('ids'), 'work identifiers')
    if identifiers.get('doi') is not None:
        _exact_doi(identifiers['doi'], doi)
    authorships = _list(record.get('authorships'), 'authors', MAX_AUTHORS)
    truncated = record.get('is_authors_truncated', False)
    count = record.get('authors_count')
    if (type(truncated) is not bool or truncated
            or count is not None and (type(count) is not int or count != len(authorships))):
        raise ValueError('OpenAlex returned an incomplete or inconsistent author list.')
    warnings, authors, locations = [], [], []
    for authorship in authorships:
        _cancel(cancelled)
        authorship = _object(authorship, 'authorship')
        author = _optional_object(authorship.get('author'), 'author')
        name = _text(authorship.get('raw_author_name'), 'Byline author name')
        if not name:
            name = _text(author.get('display_name'), 'Author name')
            if name:
                warnings.append('An OpenAlex author profile name was used where the original byline was unavailable.')
        if name:
            authors.append(name)
        else:
            warnings.append('An author with no provider name was omitted from the preview.')
    primary = _optional_object(record.get('primary_location'), 'primary location')
    best = _optional_object(record.get('best_oa_location'), 'best open-access location')
    copies = _list(record.get('locations'), 'locations', MAX_LOCATIONS)
    count = record.get('locations_count')
    if count is not None and (type(count) is not int or count != len(copies)):
        raise ValueError('OpenAlex returned an incomplete or inconsistent location list.')
    global_access = _optional_object(record.get('open_access'), 'open-access summary').get('is_oa')
    if global_access is not None and type(global_access) is not bool:
        raise ValueError('OpenAlex returned an invalid open-access summary.')
    for location in ([best] if best else []) + ([primary] if primary else []) + copies:
        _cancel(cancelled)
        location = _object(location, 'location')
        reported_oa = location.get('is_oa')
        if reported_oa is not None and type(reported_oa) is not bool:
            raise ValueError('OpenAlex returned an invalid location access statement.')
        if global_access is False and reported_oa is True:
            raise ValueError('OpenAlex returned contradictory open-access statements.')
        pdf = _url(location.get('pdf_url'), warnings, 'PDF URL')
        landing = _url(location.get('landing_page_url'), warnings, 'landing URL')
        if not (pdf or landing):
            continue
        source = _optional_object(location.get('source'), 'location source')
        locations.append(AccessLocation(provider='openalex', url=pdf or landing,
            landing_url=landing, format='pdf' if pdf else 'html',
            version=_text(location.get('version'), 'Version'),
            license=_text(location.get('license'), 'License'),
            reported_open_access=reported_oa, host_type=_text(source.get('type'), 'Source type')))
    if locations:
        warnings.append('OpenAlex locations describe different copies or versions; each location has its own access and license claim, not blanket download or reuse permission.')
    source = _optional_object(primary.get('source'), 'primary source')
    publisher = ''
    organization = _text(source.get('host_organization'), 'Host organization')
    if re.fullmatch(r'https://openalex\.org/P[0-9]+', organization):
        publisher = _text(source.get('host_organization_name'), 'Publisher')
    biblio = _optional_object(record.get('biblio'), 'bibliographic details')
    first = _text(biblio.get('first_page'), 'First publication page')
    last = _text(biblio.get('last_page'), 'Last publication page')
    pages = first + ('–' + last if last and last != first else '') if first else ''
    if last and not first:
        warnings.append('The final publication page was supplied without a first page; no range was invented.')
    language = _text(record.get('language'), 'Metadata language', 32)
    if language:
        warnings.append(f'OpenAlex reports metadata language {language}; this does not establish full-text language and is not applied to the book.')
    _cancel(cancelled)
    return _preview('openalex', doi, raw, warnings=warnings, locations=locations,
        title=_text(record.get('title') or record.get('display_name'), 'Title'),
        authors=tuple(authors), year=_year(record.get('publication_year')),
        item_type=_item_type(record.get('type')), container_title=_text(source.get('display_name'), 'Container title'),
        publisher=publisher, volume=_text(biblio.get('volume'), 'Volume'),
        issue=_text(biblio.get('issue'), 'Issue'), pages=pages, language='',
        abstract=_openalex_abstract(record.get('abstract_inverted_index'), cancelled),
        source_url='https://api.openalex.org/works/doi:' + quote(doi, safe=''))


def _required_isbn(value):
    isbn = normalize_isbn(value)
    if not isbn:
        raise ValueError('Enter an ISBN to look up.')
    return isbn


def _openlibrary_year(value, warnings):
    value = _text(value, 'Publication date')
    if not value:
        return ''
    # Free-form catalogue dates can be approximate, ranges or copyright dates.
    # Only complete recognized calendar forms yield an exact publication year.
    numeric = re.fullmatch(r'([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?', value)
    named = re.fullmatch(r'([A-Za-z]+)\.?\s+(?:([0-9]{1,2}),?\s+)?([0-9]{4})', value)
    months = ('january', 'february', 'march', 'april', 'may', 'june', 'july',
              'august', 'september', 'october', 'november', 'december')
    try:
        if numeric:
            year, month, day = numeric.groups()
            date(int(year), int(month or 1), int(day or 1))
            return year
        if named:
            month, day, year = named.groups()
            matches = [index + 1 for index, name in enumerate(months)
                       if month.lower() in (name, name[:3])]
            if len(matches) == 1:
                date(int(year), matches[0], int(day or 1))
                return year
    except ValueError:
        pass
    warnings.append('The publication date is ambiguous or not a valid supported calendar date; no year was inferred. The original date remains in the raw response.')
    return ''


def parse_openlibrary(raw, isbn, *, cancelled=lambda: False):
    """Bind one Books API data record to its queried, checksum-valid ISBN."""
    isbn = _required_isbn(isbn)
    envelope = _json(raw, cancelled)
    key = 'ISBN:' + isbn
    if set(envelope) != {key}:
        raise ValueError('Open Library did not return one exact result for the requested ISBN.')
    record = _object(envelope[key], 'Open Library book')
    identifiers = _object(record.get('identifiers'), 'book identifiers')
    returned = []
    for field, length in (('isbn_10', 10), ('isbn_13', 13)):
        for value in _list(identifiers.get(field), 'ISBNs'):
            _cancel(cancelled)
            actual = _required_isbn(value)
            if len(actual) != length or _isbn_key(actual) != _isbn_key(isbn):
                raise ValueError('Open Library returned ambiguous or mismatched ISBN identifiers.')
            if actual not in returned:
                returned.append(actual)
    if isbn not in returned:
        raise ValueError('The Open Library record does not explicitly identify the requested ISBN.')
    warnings, authors, locations = [], [], []
    for author in _list(record.get('authors'), 'authors', MAX_AUTHORS):
        _cancel(cancelled)
        name = _text(_object(author, 'author').get('name'), 'Author name')
        if name:
            authors.append(name)
        else:
            warnings.append('An author with no provider name was omitted from the preview.')
    publishers = [_text(_object(value, 'publisher').get('name'), 'Publisher')
                  for value in _list(record.get('publishers'), 'publishers')]
    publisher = _first_text(publishers, 'publishers', warnings)
    book_url = _url(record.get('url'), warnings, 'book URL')
    if book_url:
        locations.append(AccessLocation('openlibrary', book_url, format='html'))
    for ebook in _list(record.get('ebooks'), 'ebook links', MAX_LOCATIONS):
        _cancel(cancelled)
        url = _url(_object(ebook, 'ebook link').get('preview_url'), warnings, 'preview URL')
        if url:
            locations.append(AccessLocation('openlibrary', url, format='html'))
    for link in _list(record.get('links'), 'book links', MAX_LOCATIONS):
        _cancel(cancelled)
        url = _url(_object(link, 'book link').get('url'), warnings)
        if url:
            locations.append(AccessLocation('openlibrary', url))
    if locations or record.get('preview') is not None:
        warnings.append('Open Library catalogue, preview and borrowing links are metadata only and do not grant open-access download or reuse permission.')
    if record.get('number_of_pages') is not None:
        warnings.append('The page count was not applied as a publication range or quotation pinpoint.')
    if record.get('subtitle'):
        _text(record['subtitle'], 'Subtitle')
        warnings.append('A separate subtitle remains in the original provider response; the deposited title was not rewritten.')
    _cancel(cancelled)
    return _preview('openlibrary', '', raw, warnings=warnings, locations=locations,
        title=_text(record.get('title'), 'Title'), authors=tuple(authors), item_type='book',
        year=_openlibrary_year(record.get('publish_date'), warnings), publisher=publisher,
        isbn=(isbn, *(value for value in returned if value != isbn)),
        source_url='https://openlibrary.org/api/books?' + urlencode(dict(isbn_request(isbn).query)))


def isbn_request(isbn):
    """Build exactly one JSON/data ISBN query; never a preview/download query."""
    isbn = _required_isbn(isbn)
    return ProviderRequest('openlibrary', 'https://openlibrary.org/api/books',
        (('bibkeys', 'ISBN:' + isbn), ('format', 'json'), ('jscmd', 'data')))


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
    if provider == 'openalex':
        return ProviderRequest(provider, 'https://api.openalex.org/works/doi:' + quote(doi, safe=''))
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
              'europe_pmc': parse_europe_pmc, 'openalex': parse_openalex}[provider]
    return parser(raw, doi, cancelled=cancelled)


def lookup_isbn(isbn, *, transport, consent=False, cancelled=lambda: False):
    """Explicit metadata-only ISBN lookup; no fallback or document request."""
    if consent is not True:
        raise PermissionError('Online ISBN lookup requires explicit consent for this request.')
    _cancel(cancelled)
    request = isbn_request(isbn)
    isbn = dict(request.query)['bibkeys'][5:]
    raw = transport(request, cancelled=cancelled)
    _cancel(cancelled)
    return parse_openlibrary(raw, isbn, cancelled=cancelled)

