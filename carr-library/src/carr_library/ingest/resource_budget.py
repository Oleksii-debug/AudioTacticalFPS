"""Reviewed resource allowances for the existing isolated import pipeline.

These are operation budgets, not measured machine capacities or source authority.
An omitted profile keeps the historical request bytes and admission behavior.
"""
from dataclasses import asdict, dataclass
import math

SCHEMA = 'carr-import-budget/1'
MIB = 1024 ** 2
MAX_COUNT = 2 ** 63 - 1
MAX_SEGMENT_BYTES = 64 * MIB
MAX_CHECKPOINT_BYTES = 2 * MIB


def _positive(value, name, maximum=MAX_COUNT):
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f'{name} must be a positive bounded integer.')
    return value


@dataclass(frozen=True)
class ImportBudget:
    """Explicit source, work-slice and reservation policy, frozen before launch.

    Source and total segment allowances can exceed the legacy limits. Individual
    text records, JSON checkpoints and process messages retain their existing
    bounded formats. Longer jobs continue from the same durable journal.
    """
    source_bytes: int = 64 * MIB
    segment_bytes: int = 64 * MIB
    segments: int = 1000
    steps: int = 1001
    seconds: float = 120.0
    memory_bytes: int = 3 * 1024 ** 3
    library_bytes: int = 256 * MIB
    temporary_bytes: int = 256 * MIB

    def __post_init__(self):
        for name in ('source_bytes', 'segment_bytes', 'segments', 'steps',
                     'memory_bytes', 'library_bytes', 'temporary_bytes'):
            _positive(getattr(self, name), name)
        if self.segment_bytes > MAX_SEGMENT_BYTES:
            raise ValueError('One source segment must remain within the existing 64 MiB record allowance.')
        if self.segments > 2 ** 31 - 1 or self.steps > self.segments + 1:
            raise ValueError('Choose a finite segment count and at most one publication step beyond it.')
        if (type(self.seconds) not in (int, float) or not math.isfinite(self.seconds)
                or not 0.1 <= self.seconds <= 86400):
            raise ValueError('Choose a finite import work-slice allowance between 0.1 and 86400 seconds.')
        # The existing streamed adapter retains a bounded Python identity per
        # 64 Ki-character fragment, not the donor's compact two-integer index.
        # Reserve conservative identity/container overhead before allocating it.
        required_memory = 64 * MIB + 4 * self.segment_bytes + 1024 * self.segments
        if self.memory_bytes < required_memory:
            raise ValueError('The memory allowance does not cover the selected segment/index budgets.')
        if self.library_bytes < self.source_bytes or self.temporary_bytes < self.source_bytes:
            raise ValueError('Both selected disk allowances must cover one complete source copy.')
        if self.temporary_bytes < 4096:
            raise ValueError('The private native-worker protocol needs at least 4096 temporary bytes.')
        object.__setattr__(self, 'seconds', float(self.seconds))

    def to_dict(self):
        return dict(schema=SCHEMA, **asdict(self))

    @classmethod
    def from_dict(cls, value):
        fields = {'schema', 'source_bytes', 'segment_bytes', 'segments', 'steps',
                  'seconds', 'memory_bytes', 'library_bytes', 'temporary_bytes'}
        if type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA:
            raise ValueError('Unsupported import resource budget.')
        result = cls(**{key: item for key, item in value.items() if key != 'schema'})
        if result.to_dict() != value:
            raise ValueError('Noncanonical import resource budget.')
        return result


def checked_budget(value):
    """Accept only a reviewed typed object; saved JSON uses from_dict explicitly."""
    if value is not None and type(value) is not ImportBudget:
        raise TypeError('Choose an exact ImportBudget before reviewing this import.')
    return value


def effective_budget(value=None):
    return checked_budget(value) or ImportBudget()


def source_limit(mime, value=None):
    """Only the current strict TXT adapter admits larger original bytes."""
    maximum = effective_budget(value).source_bytes
    return maximum if mime == 'text/plain' else min(maximum, 64 * MIB)
