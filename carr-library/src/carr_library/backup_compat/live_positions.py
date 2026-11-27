"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from dataclasses import asdict, dataclass
import json
import math
from ..reader.reading_state import ReadingLocation, _location, _timestamp, _unique, _text


FILENAME = "live-reading-state.json"


MAX_BYTES = 1024 * 1024


MAX_POSITIONS = 1000


MAX_TEXT_CHARACTERS = 2_000_000


@dataclass(frozen=True)
class LivePosition:
    location: ReadingLocation
    offset: int
    end: int
    text_length: int
    voice_id: str
    rate: float = 1.0
    volume: float = 1.0
    updated_at: str = ""

    def __post_init__(self):
        if type(self.location) is not ReadingLocation:
            raise ValueError("A verified source location is required for live-reading recovery.")
        if (any(type(value) is not int for value in (self.offset, self.end, self.text_length))
                or not 0 <= self.offset <= self.end <= self.text_length <= MAX_TEXT_CHARACTERS):
            raise ValueError("Invalid live-reading source offsets.")
        _text(self.voice_id, "local voice identity", limit=512)
        if (type(self.rate) not in (int, float) or not math.isfinite(self.rate)
                or not 0.5 <= self.rate <= 2):
            raise ValueError("Live-reading speech rate must be between 0.5 and 2.")
        if (type(self.volume) not in (int, float) or not math.isfinite(self.volume)
                or not 0 <= self.volume <= 1):
            raise ValueError("Live-reading volume must be between 0 and 1.")
        _timestamp(self.updated_at)


_POSITION_KEYS = {"location", "offset", "end", "text_length", "voice_id", "rate", "volume", "updated_at"}


def validate_live_state(raw):
    """Validate bounded UTF-8 bytes; callers also authenticate source bindings."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Live-reading state is empty or exceeds its byte budget.")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number.")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Live-reading state is not valid UTF-8 JSON.") from exc
    if (type(value) is not dict or set(value) != {"kind", "version", "positions"}
            or value["kind"] != "carr-live-reading-state"
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["positions"]) is not list or len(value["positions"]) > MAX_POSITIONS):
        raise ValueError("Unsupported or oversized live-reading state.")
    positions, seen = [], set()
    for row in value["positions"]:
        if type(row) is not dict or set(row) != _POSITION_KEYS:
            raise ValueError("Invalid live-reading position fields.")
        item = LivePosition(**{**row, "location": _location(row["location"])})
        key = item.location.workspace, item.location.edition
        if key in seen:
            raise ValueError("Duplicate live-reading position for one source.")
        positions.append(item)
        seen.add(key)
    return tuple(positions)
