"""Resource budgets and provenance-bearing document derivatives."""
from dataclasses import dataclass
import math
from pathlib import Path


class ConversionError(ValueError):
    pass


class ConversionUnavailable(ConversionError):
    pass


@dataclass(frozen=True)
class ConversionBudget:
    """Parser/byte/time quotas; memory_bytes is an OS limit on POSIX only.

    Windows retains process isolation, byte/structure quotas and parent-enforced
    cancellation/time limits. It does not claim a Windows address-space cap.
    """
    max_source_bytes: int = 256 * 1024 * 1024
    max_text_bytes: int = 32 * 1024 * 1024
    max_archive_bytes: int = 128 * 1024 * 1024
    max_member_bytes: int = 32 * 1024 * 1024
    max_members: int = 10000
    max_xml_nodes: int = 500000
    max_sections: int = 100000
    max_pages: int = 1000
    max_image_pixels: int = 40000000
    max_total_image_pixels: int = 200000000
    memory_bytes: int = 768 * 1024 * 1024
    timeout: float = 120

    def __post_init__(self):
        for name, value in vars(self).items():
            if name == 'timeout':
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
                    raise ValueError('Invalid conversion timeout.')
            elif type(value) is not int or value < 1:
                raise ValueError('Invalid conversion resource budget.')
        if self.max_text_bytes > self.memory_bytes // 2 or self.max_member_bytes > self.memory_bytes // 2:
            raise ValueError('Conversion memory budget is too small for the selected text/member budgets.')


@dataclass(frozen=True)
class ConversionResult:
    path: Path
    report: dict
    report_path: Path
    warnings: tuple[str, ...] = ()
