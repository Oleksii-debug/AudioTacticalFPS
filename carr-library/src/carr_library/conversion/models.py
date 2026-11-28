"""Budgets for the existing isolated readers and their reading derivatives."""
from dataclasses import dataclass
import math
from pathlib import Path
import re

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024


class ConversionError(ValueError):
    pass


class ConversionUnavailable(ConversionError):
    pass


_TEXT_ENCODINGS = {
    'utf-8': 'utf-8', 'utf-8-sig': 'utf-8-sig',
    'utf-16': 'utf-16', 'utf-16-le': 'utf-16-le', 'utf-16-be': 'utf-16-be',
    'windows-1251': 'cp1251', 'cp1251': 'cp1251',
    'windows-1252': 'cp1252', 'cp1252': 'cp1252',
    'iso-8859-1': 'iso-8859-1', 'ascii': 'ascii',
}


def selected_text_encoding(value):
    """Validate an explicit TXT codec; absence retains strict UTF-8 intake."""
    if value is None:
        return None
    if type(value) is not str or len(value) > 32:
        raise ConversionError('Choose an explicitly supported plain-text encoding.')
    selected = _TEXT_ENCODINGS.get(value.lower().replace('_', '-'))
    if selected is None:
        raise ConversionError('Choose an explicitly supported plain-text encoding.')
    return selected


def email_subject_metadata(value):
    """One passive, bounded email field; never a filename or catalog authority."""
    if type(value) is not dict or set(value) - {'subject'}:
        raise ConversionError('The conversion worker returned invalid email metadata.')
    if 'subject' in value:
        subject = value['subject']
        if (type(subject) is not str or len(subject) > 512
                or re.search(r'[\x00-\x08\x0b\x0e-\x1f\x7f]', subject)):
            raise ConversionError('The conversion worker returned invalid email metadata.')
        try:
            subject.encode('utf-8', errors='strict')
        except UnicodeError as exc:
            raise ConversionError('The conversion worker returned invalid email metadata.') from exc
    return dict(value)


@dataclass(frozen=True)
class ConversionBudget:
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_text_bytes: int = MAX_TEXT_BYTES
    max_records: int = 10_000
    max_pages: int = 1000
    max_files: int = 1000
    max_image_pixels: int = 20_000_000
    max_total_image_pixels: int = 80_000_000
    pandoc_memory_bytes: int = 512 * 1024 * 1024
    timeout: float = 120.0

    def __post_init__(self):
        for name, maximum in (('max_source_bytes', MAX_SOURCE_BYTES),
                              ('max_text_bytes', MAX_TEXT_BYTES),
                              ('max_records', 10_000), ('max_pages', 1000), ('max_files', 1000),
                              ('max_image_pixels', 20_000_000), ('max_total_image_pixels', 200_000_000),
                              ('pandoc_memory_bytes', 1024 * 1024 * 1024)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Conversion resource limits must fit the existing reader budgets.')
        if self.pandoc_memory_bytes < 64 * 1024 * 1024:
            raise ValueError('Pandoc memory allowance must be at least 64 MiB.')
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0.1 <= self.timeout <= 600):
            raise ValueError('Choose a conversion time limit between 0.1 and 600 seconds.')


@dataclass(frozen=True)
class ConversionResult:
    path: Path
    report: dict
    report_path: Path
