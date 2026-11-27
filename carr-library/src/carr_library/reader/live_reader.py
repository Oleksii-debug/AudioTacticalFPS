"""Literal source narration through the existing local production service.

Native text positions can count CRLF and UTF-16 differently. The UI therefore
supplies actual prefix text from GetRange; no wx position is a Python offset.
"""
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import shutil
import tempfile

from ..audio.playback_resume import PlaybackOwner
from ..audio.runtime import available_tool


def _display(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _raw_offset(raw, prefix):
    normalized = _display(prefix)
    if not _display(raw).startswith(normalized):
        raise ValueError('The displayed source changed. Open its reading position again.')
    wanted, seen, offset = len(normalized), 0, 0
    while seen < wanted and offset < len(raw):
        offset += 2 if raw[offset:offset + 2] == '\r\n' else 1
        seen += 1
    if seen != wanted:
        raise ValueError('The native source selection does not match the original text.')
    return offset


@dataclass(frozen=True)
class ReadingSelection:
    owner: PlaybackOwner
    text: str
    title: str
    status: str

    def validate(self, page):
        if (page.location != self.owner.source or self.owner.end > len(page.raw_text)
                or page.raw_text[self.owner.start:self.owner.end] != self.text
                or sha256(page.raw_text.encode('utf-8')).hexdigest() != page.location.text_sha256):
            raise ValueError('The source or selected text changed. Choose the narration again.')
        return self


def select_source(page, *, mode='page', prefix='', through_selection='', displayed=None):
    raw = page.raw_text
    if displayed is not None and _display(displayed) != _display(raw):
        raise ValueError('The displayed source differs from the authorized original.')
    if mode == 'page':
        start, end = 0, len(raw)
    elif mode in ('selection', 'cursor'):
        start = _raw_offset(raw, prefix)
        end = _raw_offset(raw, through_selection) if mode == 'selection' else len(raw)
    else:
        raise ValueError('Choose this page, selected text, or text from the cursor.')
    if not 0 <= start < end <= len(raw) or not raw[start:end].strip():
        raise ValueError('Choose a nonempty source passage to read aloud.')
    if end - start > 2_000_000:
        raise ValueError('Select at most two million characters for one narration; other source pages remain available.')
    selection = ReadingSelection(PlaybackOwner('source', source=page.location, start=start, end=end),
                                 raw[start:end], page.title, page.status)
    return selection.validate(page)


class ReaderNarrator:
    """A temporary derived recording; no second editor or saved text history."""
    def __init__(self, selection, resolve, *, guard=lambda: None, registry=None, ffmpeg=None):
        if type(selection) is not ReadingSelection:
            raise ValueError('Choose an authorized source passage.')
        self.selection, self.resolve, self.guard = selection, resolve, guard
        self.registry, self.ffmpeg = registry, ffmpeg
        self.directory = None
        self._directory_identity = None
        self.closed = False

    def check(self, cancelled=lambda: False):
        self.guard()
        if self.closed or cancelled():
            raise InterruptedError('Source narration cancelled.')
        return self.selection.validate(self.resolve(self.selection.owner.source))

    def voices(self, *, refresh=False):
        self.check()
        if self.registry is None:
            from ..audio.engines import default_registry
            self.registry = default_registry(ffmpeg=self.ffmpeg)
        result = tuple(self.registry.voices(refresh=refresh))
        self.check()
        return result

    def render(self, *, voice, language='en', rate=0, pitch=0, volume=1,
               cancelled=lambda: False, on_status=lambda message: None):
        from ..audio.production import plan_narration, render_project
        self.check(cancelled)
        if self.registry is None:
            self.voices()
        selected = next((row for row in self.voices(refresh=True)
                         if row.id == voice and getattr(row, 'available', True)), None)
        if selected is None:
            raise ValueError('Choose one currently installed local voice; no substitute is selected automatically.')
        supported = getattr(selected, 'controls', ())
        if any(value != default and name not in supported for name, value, default in
               (('rate', rate, 0), ('pitch', pitch, 0), ('volume', volume, 1))):
            raise ValueError('The selected local voice does not support that performance control.')
        binary = available_tool('ffmpeg', self.ffmpeg)
        if binary is None:
            raise ValueError('Reading aloud requires the configured local FFmpeg runtime.')
        plan = plan_narration(self.selection.text, title=self.selection.title[:256], voice=voice, language=language)
        plan = replace(plan, roles=tuple(replace(role, rate=rate, pitch=pitch, volume=volume) for role in plan.roles))
        if self.directory is None:
            self.directory = Path(tempfile.mkdtemp(prefix='carr-reader-audio-'))
            info = self.directory.stat()
            self._directory_identity = info.st_dev, info.st_ino
        self.check(cancelled)
        result = render_project(plan, self.directory, engine=self.registry, ffmpeg=Path(binary),
            cancelled=cancelled, on_status=on_status)
        self.check(cancelled)
        if result.get('status') != 'complete':
            raise ValueError('The narration service did not confirm completed audio.')
        return result

    def close(self):
        """Call only after render and playback both stop; failure keeps the files."""
        self.closed = True
        if self.directory is not None:
            info = self.directory.lstat()
            if self.directory.is_symlink() or (info.st_dev, info.st_ino) != self._directory_identity:
                raise ValueError('The private narration directory changed; it was not removed.')
            shutil.rmtree(self.directory)
            self.directory = None
