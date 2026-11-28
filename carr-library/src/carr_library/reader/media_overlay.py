"""Read the exact verified Resonance EPUB media-overlay graph.

This is deliberately not a general EPUB or DAISY engine. XML and archive
validation reuse the publisher; MP3 decoding uses the existing AudioRuntime,
and playback uses the existing WavePlayer. Loading never starts audio.
"""
from bisect import bisect_right
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import os
import shutil
import wave
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import ZipFile

from defusedxml import ElementTree as SafeET

from ..audio.models import AudioBudget, AudioCancelled, finite, integer
from ..durable_state import local_path, regular
from ..studio.publishing import inspect_epub, _digest, _read, XHTML, OPF, DC


# The shared CPython3.11 WavePlayer reads ordinary RIFF PCM, not RF64.
# Keep a full 64KiB header allowance below the unsigned RIFF32 file boundary.
MAX_RIFF_BYTES = 2**32 - 1
RIFF_HEADER_RESERVE = 65536


def pcm_playback_bytes(duration, sample_rate, channels):
    duration = finite(duration, 'Measured playback duration', .000001, 365 * 86400)
    integer(sample_rate, 'Playback sample rate', 8000, 192000)
    integer(channels, 'Playback channels', 1, 2)
    needed = round(duration * sample_rate) * channels * 2 + RIFF_HEADER_RESERVE
    if needed > MAX_RIFF_BYTES:
        raise ValueError('This decoded recording exceeds the shared player\'s RIFF32 PCM limit '
                         '(below 4 GiB including headers). Publish or select shorter chapter recordings; '
                         'RF64 playback is not supported by this reader.')
    return needed


@dataclass
class PreparedOverlay:
    title: str
    publication_sha256: str
    provenance: dict
    chapters: tuple
    intervals: tuple
    audio: Path
    audio_identity: dict
    duration: float
    _temporary: object

    def close(self):
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def prepare_overlay(publication, *, library_root, budget=None, cancelled=lambda: False, progress=None):
    """Capture, authenticate and decode an explicitly selected publication."""
    budget = budget or AudioBudget()
    if not isinstance(budget, AudioBudget):
        raise TypeError('Use the shared AudioBudget for synchronized reading.')
    root = local_path(library_root, exists=True)
    source = local_path(publication, exists=True)
    if source.suffix.casefold() != '.epub':
        raise ValueError('Select a Resonance synchronized .epub publication.')
    started = monotonic()

    def check():
        if cancelled():
            raise AudioCancelled('Synchronized EPUB loading cancelled.')
        if monotonic() - started > budget.timeout:
            raise TimeoutError('Synchronized EPUB loading exceeded its time budget.')

    def stopping():
        return cancelled() or monotonic() - started > budget.timeout

    def report(message):
        check()
        if progress is not None:
            try:
                progress(message)
            except Exception:
                pass

    temporary = TemporaryDirectory(prefix='.pending-resonance-overlay-')
    try:
        stage = local_path(temporary.name, exists=True)
        report('Capturing the selected EPUB and its exact byte identity')
        identity = _digest(source, check, budget.max_output_bytes)
        if shutil.disk_usage(stage).free < identity['size'] * 2 + budget.reserve_disk_bytes:
            raise OSError('Not enough free local disk for the selected EPUB and reserve.')
        snapshot = stage / 'publication.epub'
        _read(source, identity, check, copy_to=snapshot)
        report('Verifying text, source provenance and measured media overlays')
        provenance = inspect_epub(snapshot, cancelled=stopping, max_bytes=budget.max_output_bytes)
        if _digest(snapshot, check, budget.max_output_bytes) != identity:
            raise ValueError('The captured EPUB changed during verification.')
        intervals = provenance['intervals']
        duration = finite(provenance['duration'], 'Measured publication duration', .000001, 365 * 86400)
        # Fail before invoking a decoder for a recording the existing player cannot open.
        pcm_playback_bytes(duration, provenance['audio'].get('sample_rate'), provenance['audio'].get('channels'))
        with ZipFile(snapshot) as archive:
            content = SafeET.fromstring(archive.read('EPUB/content.xhtml'),
                forbid_dtd=True, forbid_entities=True, forbid_external=True)
            package = SafeET.fromstring(archive.read('EPUB/package.opf'),
                forbid_dtd=True, forbid_entities=True, forbid_external=True)
            nodes = {node.get('id'): ''.join(node.itertext()) for node in content.iter() if node.get('id')}
            chapters, chapter_names, first_intervals = [], set(), {}
            for index, row in enumerate(intervals):
                first_intervals.setdefault(row['chapter'], index)
            for section in content.findall('.//{' + XHTML + '}section'):
                name = section.get('id')
                heading = section.find('{' + XHTML + '}h2')
                if heading is None or name is None or name in chapter_names:
                    raise ValueError('Invalid synchronized chapter navigation.')
                selected = first_intervals.get(name)
                if selected is None:
                    raise ValueError('A synchronized chapter has no measured intervals.')
                chapter_names.add(name)
                chapters.append({'id': name, 'title': ''.join(heading.itertext()), 'first_interval': selected})
            if not chapters or set(first_intervals) != chapter_names:
                raise ValueError('Synchronized intervals refer to missing chapters.')
            enriched = tuple({**row, 'text': nodes[row['id']]} for row in intervals)
            title_node = package.find('{' + OPF + '}metadata/{' + DC + '}title')
            title = ''.join(title_node.itertext()) if title_node is not None else 'Synchronized publication'
            mp3 = stage / 'verified-master.mp3'
            report('Preparing the verified embedded MP3 for local playback')
            count, digest = 0, sha256()
            with archive.open('EPUB/audio/master.mp3') as incoming, mp3.open('xb') as outgoing:
                while block := incoming.read(1024 * 1024):
                    check()
                    count += len(block)
                    if count + identity['size'] > budget.max_output_bytes:
                        raise ValueError('EPUB and embedded audio exceed the selected reading byte budget.')
                    digest.update(block); outgoing.write(block)
                outgoing.flush(); os.fsync(outgoing.fileno())
            if {'size': count, 'sha256': digest.hexdigest()} != provenance['audio']['identity']:
                raise ValueError('The embedded MP3 changed during verified extraction.')
        from ..runtime_settings import audio_runtime
        runtime = audio_runtime(stage, state_root=root / 'state', input_roots=(stage,), budget=budget)
        observed = runtime.inspect(mp3, cancelled=stopping)
        audio = provenance['audio']
        if (observed.codec != 'mp3' or observed.sample_rate != audio.get('sample_rate')
                or observed.channels != audio.get('channels') or observed.duration + 1e-6 < duration):
            raise ValueError('Actual embedded audio differs from its publication codec or measured timeline.')
        needed = pcm_playback_bytes(duration, observed.sample_rate, observed.channels)
        available = budget.max_output_bytes - identity['size'] - count
        if available < max(4096, needed):
            raise ValueError('Decoded PCM and captured EPUB exceed the selected reading byte budget.')
        if shutil.disk_usage(stage).free < needed + budget.reserve_disk_bytes:
            raise OSError('Not enough free local disk for decoded playback and the selected reserve.')
        runtime.budget = replace(budget, max_output_bytes=min(available, MAX_RIFF_BYTES))
        wav = stage / 'playback.wav'
        report('Decoding verified MP3 with the configured local audio runtime')
        runtime.convert(mp3, wav, sample_rate=observed.sample_rate, channels=observed.channels,
                        duration=duration, bit_depth=16, cancelled=stopping)
        measured = runtime.inspect(wav, cancelled=stopping)
        if (not measured.codec.startswith('pcm_') or measured.sample_rate != observed.sample_rate
                or measured.channels != observed.channels
                or abs(measured.duration - duration) > 2 / observed.sample_rate):
            raise ValueError('Decoded playback no longer matches the measured publication timeline.')
        with wav.open('rb') as stream:
            header = stream.read(12)
        if header[:4] != b'RIFF' or header[8:12] != b'WAVE':
            raise ValueError('The shared playback backend requires ordinary RIFF PCM, not RF64.')
        with wave.open(str(wav), 'rb') as stream:
            if (stream.getcomptype() != 'NONE' or stream.getsampwidth() != 2
                    or stream.getframerate() != observed.sample_rate
                    or stream.getnchannels() != observed.channels):
                raise ValueError('Decoded audio is incompatible with the existing PCM playback backend.')
        wave_identity = _digest(wav, check, min(available, MAX_RIFF_BYTES))
        if sum(regular(path).st_size for path in stage.iterdir() if path.is_file()) > budget.max_output_bytes:
            raise ValueError('Synchronized playback staging exceeded the selected byte budget.')
        # Playback only needs the authenticated PCM; remove extra staging bytes.
        snapshot.unlink(); mp3.unlink()
        check()
        result = PreparedOverlay(title, identity['sha256'], provenance, tuple(chapters), enriched,
                                 wav, wave_identity, duration, temporary)
        report('Synchronized EPUB prepared. Choose an interval and press Play explicitly.')
        return result
    except BaseException:
        temporary.cleanup()
        raise


class OverlaySession:
    """Small navigation layer over an existing WavePlayer; no automatic play."""

    def __init__(self, document, player):
        if not isinstance(document, PreparedOverlay) or not document.intervals:
            raise ValueError('Load a verified synchronized publication first.')
        self.document, self.player = document, player
        self.cursor, self.index = 0.0, 0
        self._transport_active, self._verified_stamp = False, None
        self._starts = tuple(row['start'] for row in document.intervals)

    @property
    def position(self):
        state = self.player.state
        if self._transport_active and state in ('playing', 'paused', 'stopped'):
            self.cursor = min(self.document.duration, max(0.0, self.player.position_seconds))
        if state not in ('playing', 'paused'):
            self._transport_active = False
        self.index = min(len(self._starts) - 1, max(0, bisect_right(self._starts, self.cursor) - 1))
        return self.cursor

    def select(self, index):
        if type(index) is not int or not 0 <= index < len(self.document.intervals):
            raise ValueError('Select a measured publication interval.')
        self.player.stop()
        self._transport_active, self._verified_stamp = False, None
        self.index, self.cursor = index, self._starts[index]
        return self.document.intervals[index]

    def seek(self, seconds):
        seconds = finite(seconds, 'Playback position', 0, self.document.duration)
        self.player.stop()
        self._transport_active, self._verified_stamp = False, None
        self.cursor = seconds
        self.index = min(len(self._starts) - 1, max(0, bisect_right(self._starts, seconds) - 1))
        return self.document.intervals[self.index]

    def _stamp(self):
        info = regular(local_path(self.document.audio, exists=True))
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def verify_audio(self, *, cancelled=lambda: False):
        # The UI runs this bounded streaming check in its shared worker, not
        # synchronously in a Play button handler for a multi-hour recording.
        def check():
            if cancelled():
                raise AudioCancelled('Playback verification cancelled.')
        before = self._stamp()
        if _digest(self.document.audio, check, self.document.audio_identity['size']) != self.document.audio_identity:
            raise ValueError('Verified playback audio changed; reopen the publication.')
        check()
        if self._stamp() != before:
            raise ValueError('Playback audio changed during verification.')
        self._verified_stamp = before

    def play(self):
        if self._verified_stamp is None or self._stamp() != self._verified_stamp:
            raise ValueError('Verify the prepared audio before starting playback.')
        self._verified_stamp = None
        if self.player.state == 'paused':
            self.player.resume()
        else:
            self.player.play(self.document.audio, start_seconds=self.cursor)
        self._transport_active = True

    def pause(self):
        self.position
        self.player.pause()

    def stop(self):
        self.position
        self.player.stop()
        self._transport_active, self._verified_stamp = False, None

    def close(self):
        # If the device cannot confirm close, the UI retains both handles.
        self.player.close()
        self.document.close()
