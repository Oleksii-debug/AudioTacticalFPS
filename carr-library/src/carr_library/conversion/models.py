"""Budgets for the existing isolated readers and their reading derivatives."""
from dataclasses import dataclass
import math
from pathlib import Path

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024


class ConversionError(ValueError):
    pass


class ConversionUnavailable(ConversionError):
    pass


@dataclass(frozen=True)
class ConversionBudget:
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_text_bytes: int = MAX_TEXT_BYTES
    max_records: int = 10_000
    max_pages: int = 1000
    max_files: int = 1000
    timeout: float = 120.0

    def __post_init__(self):
        for name, maximum in (('max_source_bytes', MAX_SOURCE_BYTES),
                              ('max_text_bytes', MAX_TEXT_BYTES),
                              ('max_records', 10_000), ('max_pages', 1000), ('max_files', 1000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Conversion resource limits must fit the existing reader budgets.')
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0.1 <= self.timeout <= 600):
            raise ValueError('Choose a conversion time limit between 0.1 and 600 seconds.')


@dataclass(frozen=True)
class ConversionResult:
    path: Path
    report: dict
    report_path: Path
