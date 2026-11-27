"""Explicit bounded large-original profiles; ordinary intake stays unchanged.

The profile admits an amount of work, not a throughput or OCR quality claim.
The same journal, retained originals, isolated owner and publisher are used.
"""
from dataclasses import dataclass
import json

MIB = 1024**2
GIB = 1024**3
MAX_ORIGINAL_BYTES = 4 * GIB
MAX_TEXT_FRAGMENTS = 65536
MAX_PDF_PAGES = 1000
MAX_PDF_CHECKPOINT_BYTES = 256 * MIB
MAX_LARGE_TIMEOUT = 7200
SCHEMA = 'carr-large-source-profile/1'
TEXT_ADAPTER = 'strict-utf8-stream-v2'
PDF_ADAPTER = 'page_ocr-file-v1'


@dataclass(frozen=True)
class LargeTextProfile:
    maximum_bytes: int = MAX_ORIGINAL_BYTES

    def __post_init__(self):
        _maximum(self.maximum_bytes)

    mime = 'text/plain'
    suffix = '.txt'
    maximum_fragments = MAX_TEXT_FRAGMENTS
    maximum_records = MAX_TEXT_FRAGMENTS
    maximum_timeout = MAX_LARGE_TIMEOUT

    def to_dict(self):
        return dict(schema=SCHEMA, format='txt', maximum_bytes=self.maximum_bytes)

    def configuration(self):
        return dict(adapter=TEXT_ADAPTER, kind='logical_segment', fragment_chars=64 * 1024,
                    maximum_fragments=MAX_TEXT_FRAGMENTS, maximum_bytes=self.maximum_bytes)


@dataclass(frozen=True)
class PdfProfile:
    maximum_bytes: int = MAX_ORIGINAL_BYTES

    def __post_init__(self):
        _maximum(self.maximum_bytes)

    mime = 'application/pdf'
    suffix = '.pdf'
    maximum_records = MAX_PDF_PAGES
    maximum_timeout = MAX_LARGE_TIMEOUT
    maximum_checkpoint_bytes = MAX_PDF_CHECKPOINT_BYTES

    def to_dict(self):
        return dict(schema=SCHEMA, format='pdf', maximum_bytes=self.maximum_bytes)

    def configuration(self, config, runtime_identity, *, pypdf, pdfium):
        from dataclasses import asdict
        return dict(adapter=PDF_ADAPTER, kind='physical_page', source_profile=self.to_dict(),
                    config=asdict(config), runtime_identity=runtime_identity,
                    pypdf=pypdf, pdfium=pdfium)


def _maximum(value):
    if type(value) is not int or not 1 <= value <= MAX_ORIGINAL_BYTES:
        raise ValueError('Choose an original byte allowance from 1 through 4 GiB.')


def is_large(value):
    return isinstance(value, (LargeTextProfile, PdfProfile))


def profile_from_value(value):
    if isinstance(value, (LargeTextProfile, PdfProfile)):
        return value
    if type(value) is dict and value.get('schema') == SCHEMA:
        if set(value) != {'schema', 'format', 'maximum_bytes'} or value['format'] not in ('txt', 'pdf'):
            raise ValueError('Invalid large-source profile.')
        return (LargeTextProfile if value['format'] == 'txt' else PdfProfile)(value['maximum_bytes'])
    from .streamed_text import profile_from_value as text_profile
    return text_profile(value)


def profile_from_config(value):
    if isinstance(value, str):
        value = json.loads(value)
    if type(value) is not dict:
        return None
    if value.get('adapter') == TEXT_ADAPTER:
        profile = LargeTextProfile(value.get('maximum_bytes'))
        if value != profile.configuration():
            raise ValueError('Invalid large streamed-text extraction identity.')
        return profile
    if value.get('adapter') == PDF_ADAPTER:
        required = {'adapter', 'kind', 'source_profile', 'config',
                    'runtime_identity', 'pypdf', 'pdfium'}
        if (set(value) != required or value['kind'] != 'physical_page'
                or type(value['config']) is not dict or type(value['runtime_identity']) is not dict
                or any(type(value[key]) is not str or not 0 < len(value[key]) <= 128
                       for key in ('pypdf', 'pdfium'))):
            raise ValueError('Invalid file-backed PDF extraction identity.')
        profile = profile_from_value(value['source_profile'])
        if not isinstance(profile, PdfProfile):
            raise ValueError('A PDF extraction needs its exact PDF source profile.')
        from .publication_identity import validate_configuration
        validate_configuration(value['config'])
        return profile
    from .streamed_text import profile_from_config as text_profile
    return text_profile(value)


def catalog_profile(db, asset, mime, size):
    """Only exact sealed extraction identities admit larger catalog originals."""
    from ..catalog.intake import MAX_BYTES
    if type(size) is not int or size < 1:
        raise ValueError('Invalid original source size.')
    if size <= MAX_BYTES:
        return None
    if mime not in ('text/plain', 'application/pdf') or size > MAX_ORIGINAL_BYTES:
        raise ValueError('The original exceeds its supported format-specific byte limit.')
    selected = None
    for row in db.execute('SELECT DISTINCT r.config FROM revision r JOIN sealed s '
                          'ON s.revision=r.id WHERE r.asset=?', (asset,)):
        profile = profile_from_config(row[0])
        if profile is not None and profile.mime == mime and size <= profile.maximum_bytes:
            if selected is None or profile.maximum_bytes > selected.maximum_bytes:
                selected = profile
    if selected is None:
        raise ValueError('A large original has no exact sealed source profile.')
    return selected


def inspect_source(path, profile, **options):
    profile = profile_from_value(profile)
    if isinstance(profile, PdfProfile):
        from .file_pdf import inspect_pdf
        return inspect_pdf(path, profile, **options)
    from .streamed_text import inspect_text
    return inspect_text(path, profile, **options)
