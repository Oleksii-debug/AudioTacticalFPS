"""Source-bound live-reading chunk positions; never saved word alignment.

Only exact source locations are persisted. Edited reading copies use session-only
playback positions. Recovery requires an explicit action and fresh authorization.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile

from ..ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer
from .reading_state import ReadingLocation, _file_identity, _location, _now, _text, _timestamp, _unique


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


def encode_live_state(positions):
    rows = sorted(positions, key=lambda item: (item.location.workspace, item.location.edition))
    raw = (json.dumps({"kind": "carr-live-reading-state", "version": 1,
                       "positions": [asdict(item) for item in rows]},
                      ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    validate_live_state(raw)
    return raw


def live_state_locations(raw):
    return tuple(item.location for item in validate_live_state(raw))


def _check(cancelled):
    if not callable(cancelled):
        raise TypeError("A live-reading cancellation callback is required.")
    if cancelled():
        raise InterruptedError("Live-reading position was not saved after cancellation.")


class LiveState:
    def __init__(self, root):
        self.root = _private_root(root)
        self.path = self.root / FILENAME

    def _read(self):
        _regular_if_present(self.path)
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            return (), None, None
        if not 0 < before.st_size <= MAX_BYTES:
            raise ValueError("Live-reading state exceeds its byte budget.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(self.path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or _file_identity(opened) != _file_identity(before)):
                raise ValueError("Live-reading state changed while opening.")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) != before.st_size or _file_identity(self.path.lstat()) != _file_identity(before):
            raise ValueError("Live-reading state changed while reading.")
        return validate_live_state(raw), _file_identity(before), raw

    def snapshot(self):
        """Exact validated bytes; a backup caller holds the shared writer lock."""
        return self._read()[2]

    def resume(self, workspace, edition):
        _text(workspace, "library identity")
        _text(edition, "edition identity")
        return next((item for item in self._read()[0]
                     if (item.location.workspace, item.location.edition) == (workspace, edition)), None)

    def remember(self, location, offset, end, text_length, voice_id, *, rate=1.0, volume=1.0,
                 cancelled=lambda: False):
        _check(cancelled)
        item = LivePosition(location, offset, end, text_length, voice_id, rate, volume, _now())
        with exclusive_writer(self.root):
            rows, previous, _raw = self._read()
            records = {(row.location.workspace, row.location.edition): row for row in rows}
            records[location.workspace, location.edition] = item
            raw = encode_live_state(records.values())
            temporary = None
            try:
                with NamedTemporaryFile(mode="wb", prefix=".live-reading-state-", dir=self.root,
                                        delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                _regular_if_present(self.path)
                try:
                    current = _file_identity(self.path.lstat())
                except FileNotFoundError:
                    current = None
                if current != previous:
                    raise ValueError("Live-reading state changed before saving.")
                _check(cancelled)
                os.replace(temporary, self.path)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return item
