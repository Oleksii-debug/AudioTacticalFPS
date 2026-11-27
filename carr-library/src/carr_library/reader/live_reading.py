"""Bounded live source reading over the shared speech renderer and PCM player.

One worker owns synthesis, playback and temporary files. The UI polls immutable
state; this module never calls a UI callback. Positions identify exact synthesized
text chunks, not guessed words or a screen-reader cursor.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
import shutil
import tempfile
import threading
import time

from ..speech.live_adapter import SpeechRequest, discover_voices, render_speech
from ..speech.runtime import split_utterances
from .live_player import CanonicalPlayer as LocalPlayer
from .live_state import LiveState


def _normalized(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def native_text_range(raw, displayed, prefix_start, prefix_end):
    """Map wx.GetRange prefixes to original offsets, including UTF-16/CRLF UI.

    Native insertion positions are never used as Python string indexes. wx does
    the native indexing when obtaining each prefix; only those Unicode strings
    are matched against the exact original text.
    """
    values = (raw, displayed, prefix_start, prefix_end)
    if any(type(value) is not str or "\x00" in value for value in values):
        raise ValueError("Reading selection must contain valid source text.")
    try:
        for value in values:
            value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Place the reading cursor between complete Unicode characters.") from exc
    if _normalized(raw) != _normalized(displayed):
        raise ValueError("The displayed reading text changed. Open the source location again.")

    def offset(prefix):
        index = 0
        for character in _normalized(prefix):
            if index >= len(raw):
                raise ValueError("The native selection no longer matches the source text.")
            actual = "\n" if raw[index] == "\r" else raw[index]
            if character != actual:
                raise ValueError("The native selection no longer matches the source text.")
            index += 2 if raw[index:index + 2] == "\r\n" else 1
        return index

    start, end = offset(prefix_start), offset(prefix_end)
    if end < start:
        raise ValueError("The native reading selection has an invalid order.")
    return start, end


@dataclass(frozen=True)
class LiveSection:
    text: str
    title: str
    ordinal: int
    total: int
    page: object = None
    derivative: bool = False

    @property
    def location(self):
        return self.page.location if self.page is not None else None


class SourceSequence:
    """Use the existing ReadingDesk, including its exact active revision checks."""
    def __init__(self, desk, guard=lambda: None):
        self.desk, self.guard = desk, guard
        self.root = desk.library.root
        self.supports_resume = True

    def _desk(self, cancelled):
        desk = copy.copy(self.desk)
        desk.cancelled = cancelled
        desk.timeout = min(30.0, desk.timeout)
        return desk

    def load(self, ordinal, cancelled):
        self.guard()
        page = self._desk(cancelled).page(ordinal)
        self.guard()
        return LiveSection(page.raw_text, page.description, page.ordinal, page.total, page)

    def load_location(self, location, cancelled):
        self.guard()
        page = self._desk(cancelled).validate(location)
        self.guard()
        return LiveSection(page.raw_text, page.description, page.ordinal, page.total, page)

    def check(self, section, cancelled, *, full=False):
        self.guard()
        if cancelled():
            raise InterruptedError("Live reading stopped.")
        if full:
            current = self.load_location(section.location, cancelled)
            if current.text != section.text:
                raise ValueError("The source text changed before playback.")
        else:
            # Cheap, fresh authorization while audio plays/pauses. Full retained
            # source and text attestation is repeated before each new chunk.
            with self.desk.library.read(self.desk.workspace) as db:
                identity, metadata = self.desk._header(db)
                row = db.execute("SELECT digest FROM page WHERE revision=? AND ordinal=?",
                                 (identity.revision, section.ordinal)).fetchone()
                if (identity != self.desk.identity or metadata != self.desk.metadata
                        or row is None or row[0] != section.location.text_sha256):
                    raise ValueError("The source revision changed. Reopen the reader.")


class DocumentSequence:
    """Frozen canonical ReadingDocument snapshots, with optional source bindings."""
    supports_resume = False

    def __init__(self, root, document, *, guard=lambda: None, source_locations=()):
        self.root, self.guard = Path(root), guard
        self.prepare(document, source_locations=source_locations)

    def prepare(self, document, *, source_locations=()):
        from ..speech.reading import ReadingDocument
        from ..speech.session import ReaderResume
        # A private canonical clone preserves the exact edit map and cannot be
        # changed by the editable native control while a worker is using it.
        cloned = ReadingDocument.from_dict(document.to_dict())
        validated = ReaderResume(cloned, source_locations=tuple(source_locations))
        self.document = validated.document
        self.source_locations = validated.source_locations
        self._desk = None
        self._pages = ()

    def load(self, ordinal, cancelled):
        if ordinal != 1:
            raise ValueError("A reading copy has one selected text section.")
        section = LiveSection(self.document.text,
            self.document.title + "; edited reading copy, not quotation evidence",
            1, 1, derivative=True)
        self.check(section, cancelled, full=True)
        return section

    def check(self, section, cancelled, *, full=False):
        self.guard()
        if cancelled():
            raise InterruptedError("Live reading stopped.")
        if section.text != self.document.text:
            raise ValueError("The reading-copy revision changed.")
        if not self.source_locations:
            return
        from ..application import LibraryApp
        from .reading_desk import ReadingDesk
        first = self.source_locations[0]
        if full or self._desk is None:
            desk = ReadingDesk(LibraryApp(self.root), first.workspace, first.edition,
                               revision=first.revision, cancelled=cancelled, timeout=30)
            pages = []
            for location in self.source_locations:
                if cancelled():
                    raise InterruptedError("Live reading stopped.")
                self.guard()
                pages.append(desk.validate(location))
            if "\n\n".join(page.raw_text for page in pages) != self.document.source_text:
                raise ValueError("The reading copy no longer matches its authenticated source positions.")
            self._desk, self._pages = desk, tuple(pages)
        else:
            source = SourceSequence(self._desk, self.guard)
            for page in self._pages:
                source.check(LiveSection(page.raw_text, page.description, page.ordinal, page.total, page), cancelled)
        self.guard()


def _chunks(text, maximum):
    """Attach exact offsets to the canonical chunker, without another splitter."""
    if not text.strip():
        if text:
            yield 0, len(text), text
        return
    position = 0
    for chunk in split_utterances(text, maximum=maximum):
        end = position + len(chunk)
        yield position, end, chunk
        position = end


@dataclass(frozen=True)
class LiveBudget:
    chunk_characters: int = 600
    chunk_timeout_seconds: float = 60.0
    chunk_audio_seconds: float = 180.0
    chunk_bytes: int = 32 * 1024 * 1024
    session_characters: int = 500_000
    session_audio_seconds: float = 4 * 3600.0
    session_wall_seconds: float = 6 * 3600.0
    minimum_free_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for name in ("chunk_characters", "chunk_bytes", "session_characters", "minimum_free_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Live-reading resource budgets must be positive integers.")
        if not 64 <= self.chunk_characters <= 16_000 or self.chunk_bytes > 64 * 1024 * 1024:
            raise ValueError("Live-reading chunks exceed the supported resource budget.")
        for name in ("chunk_timeout_seconds", "chunk_audio_seconds", "session_audio_seconds", "session_wall_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Live-reading time budgets must be positive and finite.")


@dataclass(frozen=True)
class LiveStatus:
    sequence: int = 0
    state: str = "idle"
    message: str = "Choose an installed voice explicitly before reading aloud."
    voices: tuple = ()
    section: LiveSection | None = None
    chunk_start: int = 0
    chunk_end: int = 0
    range_end: int = 0
    audio_seconds: float = 0.0
    duration_seconds: float = 0.0
    warning: str = ""


class LiveReader:
    def __init__(self, source, *, state=None, discover=discover_voices, render=render_speech,
                 player_factory=LocalPlayer, budget=None, clock=time.monotonic,
                 require_display_ack=False):
        self.source, self.root = source, Path(source.root)
        self.state = state if state is not None else (LiveState(self.root) if source.supports_resume else None)
        self.discover, self.render, self.player_factory = discover, render, player_factory
        self.budget, self.clock = budget or LiveBudget(), clock
        self.require_display_ack = require_display_ack
        self._acknowledged = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-live-reading")
        self._future = None
        self._closed = self._pause = False
        self._pending_seek = None
        self._stop_message = "Live reading stopped. Resume restarts the current verified chunk."
        self._status = LiveStatus()

    @property
    def busy(self):
        with self._lock:
            return self._future is not None and not self._future.done()

    def status(self):
        with self._lock:
            return self._status

    def _emit(self, **changes):
        with self._lock:
            if self._closed:
                return
            if self._cancel.is_set() and changes.get("state") not in (None, "stopping", "stopped"):
                return
            self._status = replace(self._status, sequence=self._status.sequence + 1, **changes)

    def _stopped(self):
        return self._closed or self._cancel.is_set()

    def _check(self):
        if self._stopped():
            raise InterruptedError("Live reading stopped. No later chunk will be played.")

    def acknowledge(self, section):
        """The UI confirms it displayed this exact immutable source section."""
        with self._lock:
            if not self._closed and self._status.section is section:
                self._acknowledged = section
        self._wake.set()

    def _await_display(self, section):
        if not self.require_display_ack:
            return
        deadline = self.clock() + 10.0
        while self._acknowledged is not section:
            self._check()
            if self.clock() >= deadline:
                raise ValueError("The reading window did not accept this source location; playback was not started.")
            self._wake.wait(.05)
            self._wake.clear()

    def _submit(self, operation, *, initial=None):
        with self._lock:
            if self._closed:
                raise ValueError("This live reader is closed.")
            if self.busy:
                raise ValueError("Stop the current reading operation and wait for it to finish.")
            self._cancel = threading.Event()
            self._pause = False
            self._pending_seek = None
            self._stop_message = "Live reading stopped. Resume restarts the current verified chunk."
            if initial is not None:
                # A queued discovery is already discovery. Source acceptance
                # must not mistake the pre-worker interval for active speech.
                self._emit(**initial)
            try:
                self._future = self._executor.submit(self._operation, operation)
            except Exception as exc:
                self._emit(state="error", message=str(exc))
                raise
        return self._future

    def _operation(self, operation):
        try:
            operation()
        except Exception as exc:
            if self._stopped():
                self._emit(state="stopped", message=self._stop_message)
            else:
                self._emit(state="error", message=str(exc))

    def discover_voices(self):
        def work():
            self.source.guard()
            rows = tuple(self.discover(cancelled=self._stopped, configuration_root=self.root))
            self._check()
            self.source.guard()
            if len(rows) > 2000 or len({voice.id for voice in rows}) != len(rows):
                raise ValueError("The local voice inventory is oversized or ambiguous.")
            self._emit(state="idle", voices=rows,
                       message=(f"{len(rows)} installed voices. Choose one explicitly."
                                if rows else "No supported local voice is installed. No replacement voice was selected."))
        return self._submit(work, initial=dict(state="discovering",
                            message="Discovering installed local voices…", warning=""))

    def start(self, ordinal, voice_id, *, start=0, end=None, rate=1.0, volume=1.0,
              continuous=False, resume=False):
        voices = self.status().voices
        if not any(voice.id == voice_id for voice in voices):
            raise ValueError("Choose one of the discovered local voices explicitly before reading.")
        if (type(rate) not in (int, float) or not math.isfinite(rate) or not .5 <= rate <= 2
                or type(volume) not in (int, float) or not math.isfinite(volume) or not 0 <= volume <= 1
                or type(continuous) is not bool or type(resume) is not bool):
            raise ValueError("Invalid live-reading voice settings.")
        voice = next(voice for voice in voices if voice.id == voice_id)
        return self._submit(lambda: self._read(ordinal, start, end, voice, rate, volume, continuous, resume))

    def toggle_pause(self):
        if not self.busy or self.status().state in ("discovering", "stopping"):
            raise ValueError("Start source reading before pausing or resuming it.")
        with self._lock:
            self._pause = not self._pause
        self._wake.set()

    def seek(self, seconds, *, expected=None):
        """Seek only the still-active chunk; source offsets remain chunk-based."""
        with self._lock:
            view = self._status
            if (not self.busy or view.state not in ('playing', 'paused')
                    or type(seconds) not in (int, float) or not math.isfinite(seconds)
                    or not 0 <= seconds <= view.duration_seconds):
                raise ValueError('Choose a measured position within the active speech chunk.')
            if expected is not None and (view.section is not expected.section
                    or (view.chunk_start, view.chunk_end) != (expected.chunk_start, expected.chunk_end)):
                raise ValueError('The speech chunk changed while choosing a position. Try again.')
            self._pending_seek = (view.section, view.chunk_start, view.chunk_end, float(seconds))
        self._wake.set()

    def stop(self, reason=None):
        if reason:
            self._stop_message = str(reason)
        self._cancel.set()
        self._wake.set()
        if self.busy:
            self._emit(state="stopping", section=None, message="Stopping local synthesis or playback…")

    def close(self):
        """Cancel without blocking the UI; return the cleanup completion future."""
        with self._lock:
            if self._closed:
                return self._future
            self._closed = True
            self._cancel.set()
            self._wake.set()
            self._status = replace(self._status, state="closed", sequence=self._status.sequence + 1)
        self._executor.shutdown(wait=False, cancel_futures=True)
        return self._future

    def _remember(self, section, offset, end, voice, rate, volume):
        if self.state is None or section.location is None:
            return
        self.source.check(section, self._stopped)
        try:
            self.state.remember(section.location, offset, end, len(section.text), voice.id,
                                rate=rate, volume=volume, cancelled=self._stopped)
        except InterruptedError:
            raise
        except Exception as exc:
            # A full disk must not silently erase an earlier valid resume point.
            self._emit(warning="The current chunk position could not be saved: " + str(exc))

    def _read(self, ordinal, start, end, voice, rate, volume, continuous, resume):
        began = self.clock()
        self._emit(state="loading", message="Checking the exact reading source…", warning="",
                   audio_seconds=0.0, duration_seconds=0.0)
        if resume:
            if self.state is None or not self.source.supports_resume:
                raise ValueError("Edited reading copies have session-only voice positions.")
            desk = self.source.desk
            saved = self.state.resume(desk.workspace, desk.edition)
            if saved is None:
                raise ValueError("This source has no saved live-reading position.")
            section = self.source.load_location(saved.location, self._stopped)
            if len(section.text) != saved.text_length:
                raise ValueError("The saved reading offsets no longer match the source text.")
            start, end = saved.offset, saved.end
        else:
            section = self.source.load(ordinal, self._stopped)
        end = len(section.text) if end is None else end
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(section.text)):
            raise ValueError("Choose a reading range within the exact source text.")
        if start == end:
            self._emit(state="complete", section=section, chunk_start=end, chunk_end=end,
                       range_end=end, message="The saved or selected reading range is complete.")
            return
        player = self.player_factory()
        directory = None
        total_characters, total_seconds = 0, 0.0
        try:
            if not player.available():
                reason = player.status().get("reason") or "No native audio output device is available."
                raise ValueError(reason)
            directory = tempfile.TemporaryDirectory(prefix=".live-reading-", dir=self.root)
            while True:
                self._check()
                self.source.check(section, self._stopped, full=True)
                self._emit(state="loading", section=section, chunk_start=start, chunk_end=start,
                           range_end=end, audio_seconds=0.0, duration_seconds=0.0,
                           message="Checking the displayed source location before speech…")
                self._await_display(section)
                for begin, finish, text in _chunks(section.text[start:end], self.budget.chunk_characters):
                    chunk_start, chunk_end = start + begin, start + finish
                    self._check()
                    if (total_characters + len(text) > self.budget.session_characters
                            or self.clock() - began > self.budget.session_wall_seconds):
                        raise ValueError("The live-reading session reached its resource budget. Resume explicitly to continue.")
                    if not text.strip():
                        self._remember(section, chunk_end, end, voice, rate, volume)
                        total_characters += len(text)
                        continue
                    if shutil.disk_usage(directory.name).free < self.budget.minimum_free_bytes + self.budget.chunk_bytes:
                        raise ValueError("Not enough free local disk for the next speech chunk.")
                    self._emit(state="rendering", section=section, chunk_start=chunk_start,
                               chunk_end=chunk_end, range_end=end, audio_seconds=0.0,
                               duration_seconds=0.0, message="Rendering the next exact text chunk locally…")
                    last_authorized = [0.0]

                    def cancelled():
                        if self._stopped():
                            return True
                        now = self.clock()
                        if now - last_authorized[0] >= .25:
                            self.source.check(section, self._stopped)
                            last_authorized[0] = now
                        return False

                    target = Path(directory.name) / "chunk.wav"
                    result = self.render(SpeechRequest(text, voice.id, voice.language, rate=rate, volume=volume),
                                         target, cancelled=cancelled,
                                         timeout_seconds=self.budget.chunk_timeout_seconds,
                                         max_output_bytes=self.budget.chunk_bytes,
                                         configuration_root=self.root)
                    self._check()
                    self.source.check(section, self._stopped, full=True)
                    duration = result.duration_seconds
                    if (type(duration) not in (int, float) or not math.isfinite(duration)
                            or not 0 < duration <= self.budget.chunk_audio_seconds
                            or total_seconds + duration > self.budget.session_audio_seconds
                            or target.stat().st_size > self.budget.chunk_bytes):
                        raise ValueError("The speech chunk exceeded the live-reading audio budget.")
                    with target.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != result.sha256:
                            raise ValueError("The generated speech changed before playback.")
                    self._check()
                    self._await_display(section)
                    opened = player.open(target)
                    if abs(opened["duration_seconds"] - duration) > .02:
                        raise ValueError("The measured playback duration differs from the checked speech chunk.")
                    self._remember(section, chunk_start, end, voice, rate, volume)
                    self._check()
                    self.source.check(section, self._stopped)
                    started = False
                    while True:
                        self._check()
                        if self.clock() - began > self.budget.session_wall_seconds:
                            raise ValueError("The live-reading session reached its time budget. Resume explicitly to continue.")
                        self.source.check(section, self._stopped)
                        with self._lock:
                            paused = self._pause
                            seek = self._pending_seek
                            self._pending_seek = None
                        if seek is not None:
                            if seek[:3] != (section, chunk_start, chunk_end):
                                self._emit(warning='The previous speech chunk ended before seeking; the new chunk was not moved.')
                            else:
                                player.seek(seek[3])
                        if paused:
                            status = player.pause() if started else player.status()
                            state = "paused"
                        else:
                            status = player.status()
                            if not started:
                                status = player.play()
                                started = True
                            elif status["state"] == "paused":
                                status = player.resume()
                            state = status["state"]
                        self._emit(state="paused" if paused else "playing", audio_seconds=status["position_seconds"],
                                   duration_seconds=status["duration_seconds"],
                                   message=("Paused at the measured audio position." if paused else
                                            "Reading the current text chunk; word alignment is unavailable."))
                        if started and not paused and state == "ended":
                            break
                        if started and not paused and state not in ("playing", "paused", "ended"):
                            raise ValueError("The native audio player stopped before the chunk ended.")
                        self._wake.wait(.1)
                        self._wake.clear()
                    self._check()
                    self.source.check(section, self._stopped)
                    self._remember(section, chunk_end, end, voice, rate, volume)
                    player.close()
                    target.unlink()
                    total_characters += len(text)
                    total_seconds += duration
                if not continuous or end != len(section.text) or section.ordinal >= section.total:
                    self._emit(state="complete", chunk_start=end, chunk_end=end, audio_seconds=0.0,
                               duration_seconds=0.0, message="The requested reading range is complete.")
                    return
                self._emit(state="loading", message="Continuing to the next source location you enabled…")
                section = self.source.load(section.ordinal + 1, self._stopped)
                start, end = 0, len(section.text)
        finally:
            cleanup_errors = []
            try:
                if player.status().get("state") not in ("closed", "unavailable"):
                    player.stop()
            except Exception as exc:
                cleanup_errors.append(str(exc))
            try:
                player.close()
            except Exception as exc:
                cleanup_errors.append(str(exc))
            if directory is not None:
                try:
                    directory.cleanup()
                except OSError as exc:
                    cleanup_errors.append("Temporary speech could not be removed: " + str(exc))
            if cleanup_errors:
                self._emit(warning="Playback cleanup needs attention: " + "; ".join(cleanup_errors))
