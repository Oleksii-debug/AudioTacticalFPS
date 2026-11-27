"""Read existing EPUB3/DAISY 2.02 publications with the shared local player.

Navigation is adapted from PR127. The current publisher's validators, verified
readers, FFmpeg owner and resource broker remain the only format/audio owners.
Loading never starts an output device or approves source quotations.
"""
from bisect import bisect_right
from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
import math
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4
from zipfile import ZipFile

from defusedxml import ElementTree as SafeET

from ..audio.daisy import HTML_DTD, inspect_daisy
from ..audio.ffmpeg import run_ffmpeg, wav_info
from ..audio.capture_types import AudioBudget
from ..audio.playback_processing import PlaybackControls, prepare_playback
from ..audio.jobs import checked_directory, file_identity, regular_file, verified_reader
from ..audio.models import number
from ..audio.publishing import (DC, OPF, XHTML, MAX_XML_BYTES, PublicationCancelled,
                                _digest, _read, inspect_epub, local_path)
from ..audio.runtime import resolve_ffmpeg
from ..operations import ResourceBroker, ResourceRequest


@dataclass(frozen=True)
class ReadingBudget:
    max_bytes: int = 2 * 1024**3
    timeout: float = 3600
    reserve_bytes: int = 64 * 1024**2

    def __post_init__(self):
        for name, low, high in (('max_bytes', 1024**2, 2 * 1024**4),
                                ('reserve_bytes', 0, 2 * 1024**4)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('Choose a bounded publication reading ' + name + '.')
        number(self.timeout, .001, 86400, 'Publication reading time allowance')


class _DirectoryReader:
    """Bound ZipFile's initial central-directory read before it allocates it."""
    def __init__(self, stream):
        self.stream = stream

    def read(self, count=-1):
        if type(count) is not int or count < -1 or count > 1024**2:
            raise ValueError('The publication ZIP directory exceeds its bounded profile.')
        # ZipFile reads the bounded end-of-directory region with read(-1).
        # Cap even that request before allocating bytes from a hostile archive.
        data = self.stream.read(1024**2 + 1 if count == -1 else count)
        if len(data) > 1024**2:
            raise ValueError('The publication ZIP directory exceeds its bounded profile.')
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _directory(snapshot, kind, check):
    """The supported profiles contain a small fixed member inventory."""
    from ..audio.publishing import _NAMES
    from ..audio.daisy import NAMES
    with verified_reader(snapshot) as stream, ZipFile(_DirectoryReader(stream)) as archive:
        check()
        expected = _NAMES if kind == 'epub' else NAMES
        entries = archive.infolist()
        if len(entries) != len(expected) or {entry.filename for entry in entries} != expected:
            raise ValueError('Choose a supported synchronized EPUB or DAISY publication.')


def _document(archive, name, check, *, doctype=None):
    if archive.getinfo(name).file_size > MAX_XML_BYTES:
        raise ValueError('Publication text exceeds its metadata budget.')
    chunks, size = [], 0
    with archive.open(name) as source:
        while block := source.read(1024**2):
            check()
            size += len(block)
            if size > MAX_XML_BYTES:
                raise ValueError('Publication text exceeds its metadata budget.')
            chunks.append(block)
    raw = b''.join(chunks)
    if doctype is not None:
        if raw.count(doctype) != 1:
            raise ValueError('The DAISY text type declaration changed.')
        raw = raw.replace(doctype, b'', 1)
    return SafeET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def _navigation(archive, provenance, kind, check):
    content = _document(archive, 'EPUB/content.xhtml' if kind == 'epub' else 'content.html',
                        check, doctype=None if kind == 'epub' else HTML_DTD)
    nodes = {node.get('id'): ''.join(node.itertext()) for node in content.iter() if node.get('id')}
    intervals = provenance['intervals']
    if type(intervals) is not list or not 1 <= len(intervals) <= 201000:
        raise ValueError('Publication reading requires bounded measured intervals.')
    starts, chapters, names = {}, [], set()
    for index, row in enumerate(intervals):
        check()
        starts.setdefault(row['chapter'], index)
        if sha256(nodes[row['id']].encode('utf-8')).hexdigest() != row['text_sha256']:
            raise ValueError('Publication text differs from its measured interval.')
        if len(nodes[row['id']]) > 2_000_000:
            raise ValueError('Split this publication into bounded reading intervals before opening it.')
    tag = 'section' if kind == 'epub' else 'div'
    for section in content.findall('.//{' + XHTML + '}' + tag):
        check()
        identity = section.get('id')
        heading = section.find('{' + XHTML + '}h2')
        if heading is None or identity is None or identity in names:
            raise ValueError('Invalid synchronized chapter navigation.')
        names.add(identity)
        if identity in starts:
            chapters.append({'id': identity, 'title': ''.join(heading.itertext()),
                             'first_interval': starts[identity]})
    if not chapters or set(starts) != {row['id'] for row in chapters} or len(chapters) > 10000:
        raise ValueError('Measured intervals require bounded matching chapters.')
    if kind == 'epub':
        package = _document(archive, 'EPUB/package.opf', check)
        node = package.find('{' + OPF + '}metadata/{' + DC + '}title')
        title = ''.join(node.itertext()) if node is not None else 'Synchronized publication'
    else:
        title = provenance['title']
    return title, tuple(chapters), tuple({**row, 'text': nodes[row['id']]} for row in intervals)


@dataclass
class PreparedOverlay:
    title: str
    kind: str
    publication_sha256: str
    provenance: dict
    chapters: tuple
    intervals: tuple
    audio: Path
    audio_identity: tuple
    audio_sha256: str
    duration: float
    sample_rate: int
    frames: int
    _temporary: object

    def close(self):
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def prepare_overlay(publication, *, library_root, budget=None, ffmpeg=None, broker=None,
                    cancelled=lambda: False, progress=lambda message: None, temporary_parent=None):
    """Capture exact bytes, verify the existing graph, and decode only its MP3."""
    budget = ReadingBudget() if budget is None else budget
    if type(budget) is not ReadingBudget:
        raise TypeError('A typed publication reading budget is required.')
    root, source = checked_directory(library_root), local_path(publication)
    kind = {'.epub': 'epub', '.zip': 'daisy'}.get(source.suffix.casefold())
    if kind is None:
        raise ValueError('Choose a synchronized .epub or DAISY .zip publication.')
    binary = resolve_ffmpeg(ffmpeg)
    if binary is None:
        raise ValueError('Configure a local FFmpeg runtime before preparing recorded audio.')
    broker = broker or ResourceBroker.for_app(root)
    started, leases = monotonic(), []
    if temporary_parent is not None:
        temporary_parent = checked_directory(temporary_parent)
    temporary = TemporaryDirectory(prefix='.carr-publication-reading-', dir=temporary_parent)
    try:
        stage = checked_directory(temporary.name)
    except BaseException:
        temporary.cleanup()
        raise
    operation = 'publication-reading-' + uuid4().hex
    last_budget_check = 0.0

    def check():
        nonlocal last_budget_check
        if cancelled():
            raise PublicationCancelled('Publication reading preparation cancelled.')
        now = monotonic()
        if now - started >= budget.timeout:
            raise TimeoutError('Publication reading preparation exceeded its time allowance.')
        if any(lease.cancelled() for lease in leases):
            raise PublicationCancelled('Publication reading resource admission stopped.')
        if now - last_budget_check >= .1:
            last_budget_check = now
            total = 0
            with os.scandir(stage) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > 3:
                        raise ValueError('Publication staging contains unexpected files.')
                    total += regular_file(stage / entry.name).st_size
            if total > budget.max_bytes:
                raise ValueError('Publication capture and decoded PCM exceed the selected byte budget.')
            if shutil.disk_usage(stage).free < budget.reserve_bytes:
                raise OSError('Publication preparation reached its selected disk reserve.')

    def stopping():
        check()
        return False

    def external_stop():
        # A lease must never call its own checkpoint through this callback.
        return cancelled() or monotonic() - started >= budget.timeout

    def report(message):
        check()
        try:
            progress(message)
        except Exception:
            pass
        check()

    try:
        source_bytes = regular_file(source).st_size
        if not 0 < source_bytes <= budget.max_bytes:
            raise ValueError('Publication exceeds the selected captured byte budget.')
        with ExitStack() as resources:
            leases.append(resources.enter_context(broker.reserve(
                ResourceRequest(cpu_slots=1, memory_bytes=512 * 1024**2,
                                disk_bytes=source_bytes, disk_path=stage),
                operation_id=operation, cancelled=external_stop, run_timeout=budget.timeout)))
            report('Capturing the selected publication and its complete byte identity')
            identity = _digest(source, check, budget.max_bytes)
            snapshot = stage / ('publication.epub' if kind == 'epub' else 'publication.zip')
            _read(source, identity, check, copy_to=snapshot)
            with verified_reader(snapshot) as publication_owner:
                _directory(snapshot, kind, check)
                report('Verifying the packaged text, navigation and measured audio intervals')
                inspector = inspect_epub if kind == 'epub' else inspect_daisy
                provenance = inspector(snapshot, cancelled=stopping, max_bytes=budget.max_bytes)
                duration = number(provenance['duration'], .000001, 365 * 86400, 'Publication duration')
                audio_proof = provenance['audio']
                rate, channels = audio_proof.get('sample_rate'), audio_proof.get('channels')
                if (type(rate) is not int or rate not in (16000, 22050, 24000, 32000, 44100, 48000)
                        or type(channels) is not int or channels not in (1, 2)):
                    raise ValueError('Publication audio requires the supported measured MP3 profile.')
                frames = math.ceil(duration * rate)
                pcm_bytes = frames * channels * 2 + 65536
                encoded_bytes = audio_proof['identity']['size']
                if identity['size'] + encoded_bytes + pcm_bytes > budget.max_bytes:
                    raise ValueError('Publication and decoded PCM exceed the selected byte budget.')
                leases.append(resources.enter_context(broker.reserve(
                    ResourceRequest(cpu_slots=0, disk_bytes=encoded_bytes + pcm_bytes, disk_path=stage),
                    operation_id=operation + '-pcm', cancelled=external_stop, run_timeout=budget.timeout)))
                with ZipFile(publication_owner) as archive:
                    title, chapters, intervals = _navigation(archive, provenance, kind, check)
                    member = 'EPUB/audio/master.mp3' if kind == 'epub' else 'master.mp3'
                    encoded = stage / 'verified.mp3'
                    hashed, size = sha256(), 0
                    with archive.open(member) as incoming, encoded.open('xb') as outgoing:
                        while block := incoming.read(1024**2):
                            check()
                            size += len(block)
                            if size > encoded_bytes:
                                raise ValueError('Embedded audio exceeds its exact verified size.')
                            hashed.update(block)
                            outgoing.write(block)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if {'size': size, 'sha256': hashed.hexdigest()} != audio_proof['identity']:
                        raise ValueError('The embedded audio differs from its publication identity.')
                if _digest(snapshot, check, budget.max_bytes) != identity:
                    raise ValueError('The captured publication changed during verification.')
            report('Decoding the verified MP3 with the selected local audio runtime')
            audio = stage / 'playback.wav'
            encoded_identity = file_identity(regular_file(encoded))
            with verified_reader(encoded, expected_identity=encoded_identity):
                if _digest(encoded, check, encoded_bytes) != audio_proof['identity']:
                    raise ValueError('The prepared encoded audio changed before decoding.')
                run_ffmpeg(binary, ['-threads', '1', '-filter_threads', '1',
                    '-protocol_whitelist', 'file', '-f', 'mp3', '-i', str(encoded),
                    '-map', '0:a:0', '-map_metadata', '-1', '-vn', '-sn', '-dn',
                    '-af', f'atrim=end_sample={frames},asetpts=PTS-STARTPTS',
                    '-ac', str(channels), '-ar', str(rate), '-c:a', 'pcm_s16le',
                    '-rf64', 'auto', '-fs', str(pcm_bytes), '-f', 'wav', str(audio)], cancelled=stopping)
            info = wav_info(audio)
            if (info.rate, info.channels, info.frames) != (rate, channels, frames):
                raise ValueError('Decoded PCM does not cover the exact measured publication timeline.')
            wave_identity = file_identity(regular_file(audio))
            audio_digest = _digest(audio, check, pcm_bytes)['sha256']
            check()
            if sum(regular_file(path).st_size for path in stage.iterdir()) > budget.max_bytes:
                raise ValueError('Publication staging exceeded the selected byte budget.')
            snapshot.unlink()
            encoded.unlink()
            report('Publication prepared. Choose a measured interval and press Play.')
        leases.clear()
        check()
        return PreparedOverlay(title, kind, identity['sha256'], provenance, chapters, intervals,
                               audio, wave_identity, audio_digest, duration, rate, frames, temporary)
    except BaseException:
        temporary.cleanup()
        raise


class OverlaySession:
    """Measured interval navigation; callers serialize work for this session."""
    def __init__(self, document, player):
        if type(document) is not PreparedOverlay or not document.intervals:
            raise ValueError('Load a verified synchronized publication first.')
        self.document, self.player = document, player
        self.cursor, self.index = 0.0, 0
        self._transport_active, self._reader = False, None
        self._prepared_playback = None
        self.invalid_audio = False
        self._starts = tuple(row['start'] for row in document.intervals)

    @property
    def position(self):
        snapshot = self.player.status()
        if (self._transport_active and snapshot.position_accuracy in ('sample_frames', 'driver_reported', 'completed_buffers')
                and snapshot.state in ('starting', 'playing', 'paused', 'stopped', 'completed', 'error')):
            observed = max(0.0, snapshot.position_seconds)
            if self._prepared_playback is not None:
                observed = self._prepared_playback.audio_to_source(
                    min(self._prepared_playback.audio_duration, observed))
            self.cursor = min(self.document.duration, observed)
        self.index = min(len(self._starts) - 1, max(0, bisect_right(self._starts, self.cursor) - 1))
        return self.cursor

    @property
    def source_time_accuracy(self):
        return ('measured_intervals' if self._prepared_playback is None
                else self._prepared_playback.source_time_accuracy)

    def _release_prepared(self):
        if self._prepared_playback is not None:
            try:
                self._prepared_playback.release()
            except BaseException:
                self.invalid_audio = True
                raise

    def _discard_prepared(self):
        prepared = self._prepared_playback
        if prepared is not None:
            try:
                self._release_prepared()
            finally:
                # Keep the owner for a retry if private-directory removal fails.
                prepared.close()
                self._prepared_playback = None

    def prepare_controls(self, controls, *, library_root, budget=None, ffmpeg=None,
                         broker=None, cancelled=lambda: False, progress=lambda message: None):
        """Verify and prepare on the current worker; never open a device.

        The existing reader's selected byte allowance covers both its decoded
        original and the controlled derivative. Changing controls requires an
        acknowledged Stop. Paused playback can only resume the same controls.
        """
        if type(controls) is not PlaybackControls:
            raise TypeError('Choose typed independent playback controls.')
        budget = ReadingBudget() if budget is None else budget
        if type(budget) is not ReadingBudget:
            raise TypeError('Choose the existing bounded publication reading allowance.')
        prepared = self._prepared_playback
        previous = PlaybackControls() if prepared is None else prepared.controls
        if self._transport_active and controls != previous:
            raise ValueError('Stop recorded playback before changing its controls.')
        self.verify_audio(cancelled=cancelled)
        original_bytes = regular_file(self.document.audio).st_size
        if original_bytes > budget.max_bytes:
            raise ValueError('The original decoded audio exceeds the selected preparation allowance.')
        if controls == previous and prepared is not None:
            if original_bytes + regular_file(prepared.audio).st_size > budget.max_bytes:
                raise ValueError('Original and controlled audio exceed the selected preparation allowance.')
            prepared.pin(cancelled=cancelled)
            prepared.ready()
            return
        if controls.original:
            if prepared is not None:
                self._discard_prepared()
            return
        if prepared is not None:
            self._discard_prepared()
        remaining = budget.max_bytes - original_bytes
        if remaining < 4096:
            raise ValueError('The selected allowance has no room for controlled playback PCM.')
        self._prepared_playback = prepare_playback(self.document.audio, controls,
            library_root=library_root, ffmpeg=ffmpeg, broker=broker,
            budget=AudioBudget(timeout=budget.timeout, max_output_bytes=remaining,
                reserve_disk_bytes=budget.reserve_bytes, threads=1),
            cancelled=cancelled, progress=progress)
        self.check_playback()

    def _release(self):
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.__exit__(None, None, None)

    def halt(self):
        """Stop the native owner; an in-flight verifier may still own its reader."""
        position = self.position
        self.player.stop()
        self._transport_active = False
        self.cursor = position
        return position

    def stop(self):
        # Call after the verifier settles. A failed close cannot authorize saving.
        position = self.halt()
        try:
            try:
                self._release()
            finally:
                self._release_prepared()
        except Exception:
            self.invalid_audio = True
            raise
        return position

    def select(self, index):
        if type(index) is not int or not 0 <= index < len(self._starts):
            raise ValueError('Choose a measured publication interval.')
        self.stop()
        self.index, self.cursor = index, self._starts[index]
        return self.document.intervals[index]

    def seek(self, seconds):
        seconds = number(seconds, 0, self.document.duration, 'Publication playback position')
        self.stop()
        self.cursor = seconds
        self.index = min(len(self._starts) - 1, max(0, bisect_right(self._starts, seconds) - 1))
        return self.document.intervals[self.index]

    def verify_audio(self, *, cancelled=lambda: False):
        """Run off the UI thread; keep the Windows no-write lease until Stop."""
        if self.invalid_audio:
            raise ValueError('Prepared playback validation failed; reopen the publication.')
        if self._reader is not None:
            if file_identity(regular_file(self.document.audio)) != self.document.audio_identity:
                raise ValueError('Prepared playback audio changed; reopen the publication.')
            return
        document = self.document
        owner = verified_reader(document.audio, expected_identity=document.audio_identity)
        stream = owner.__enter__()
        try:
            digest = sha256()
            while block := stream.read(1024**2):
                if cancelled():
                    raise PublicationCancelled('Playback verification cancelled.')
                digest.update(block)
            if digest.hexdigest() != document.audio_sha256:
                raise ValueError('Prepared playback audio changed; reopen the publication.')
            if cancelled():
                raise PublicationCancelled('Playback verification cancelled.')
        except BaseException:
            owner.__exit__(None, None, None)
            raise
        self._reader = owner

    def check_playback(self):
        if (self.invalid_audio or self._reader is None
                or file_identity(regular_file(self.document.audio)) != self.document.audio_identity):
            raise ValueError('Verify the unchanged prepared audio before playback.')
        if self._prepared_playback is not None:
            self._prepared_playback.ready()

    def play(self, *, device_id=None):
        self.check_playback()
        if self.player.status().state == 'paused':
            self.player.resume()
        else:
            path, position = self.document.audio, self.cursor
            if self._prepared_playback is not None:
                path = self._prepared_playback.audio
                position = self._prepared_playback.source_to_audio(position)
            self.player.play(path, start_seconds=position, device_id=device_id)
        self._transport_active = True

    def pause(self):
        self.position
        self.player.pause()

    def remember(self, store, *, guard=lambda: None):
        if self.invalid_audio or self._transport_active or self._reader is not None:
            raise ValueError('Stop verified playback before saving the publication cursor.')
        return store.remember(self.document, self.cursor, guard=guard)

    def dispose_document(self):
        """Release a stopped publication without closing the shared WavePlayer."""
        if self._transport_active:
            raise RuntimeError('Stop the publication before replacing its owned files.')
        try:
            self._release()
        finally:
            # The output owner is quiescent. A failed private cleanup retains its
            # exact object; a later Close can retry without losing ownership.
            self._discard_prepared()
            self.document.close()

    def close(self):
        self.player.close()
        self._transport_active = False
        self.dispose_document()
