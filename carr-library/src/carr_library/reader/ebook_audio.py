"""Recorded EPUB media overlays over the existing, freshly authorized reader.

The selected archive remains immutable. SMIL supplies synchronization; this
module does not infer word timings or approve extracted text for quotation.
Untrusted ZIP/XML is parsed in a bounded child, using the existing EPUB parser.
Only local recorded-audio seq/par overlays are supported; no remote resources,
encrypted content, TTS substitution or executable markup is admitted.
"""
from dataclasses import asdict, dataclass, field
from copy import copy
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import threading
import wave
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending
from ..audio.process import run_process
from ..catalog.office_archive import validate_office
from ..durable_state import digest_file, local_path, regular
from ..extract import epub_ordered as epub
from ..runtime_settings import audio_runtime

SMIL = '{http://www.w3.org/ns/SMIL}'
MAX_CUES = 2048
MAX_CUE_SECONDS = 3600
MAX_REPLY = 2 * 1024 * 1024
_AUDIO = {'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/aac': '.aac',
          'audio/ogg': '.ogg', 'audio/wav': '.wav', 'audio/x-wav': '.wav',
          'audio/flac': '.flac'}


def _check(cancelled):
    if cancelled():
        raise AudioCancelled('EPUB audio reading cancelled.')


def _retain_temporary(error, temporary):
    """The existing process owner keeps its files until native quiescence.

    Do not let TemporaryDirectory finalization race a child which is still
    holding the archive or decoded audio. The owner already has the canonical
    retry lifetime; this callback adds no process supervisor or scheduler.
    """
    owner = error.cleanup_owner
    owner.add_quiescent_callback(temporary.cleanup)


def _clock(value, *, default=None):
    if value is None:
        return default
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise ValueError('Invalid EPUB audio clip time.')
    if ':' in value:
        if not re.fullmatch(r'(?:\d{1,3}:)?[0-5]\d:[0-5]\d(?:\.\d{1,9})?', value):
            raise ValueError('Invalid EPUB clock time.')
        seconds = Decimal(0)
        for part in value.split(':'):
            seconds = seconds * 60 + Decimal(part)
    else:
        match = re.fullmatch(r'(\d{1,9}(?:\.\d{1,9})?)(h|min|s|ms)?', value)
        if match is None:
            raise ValueError('Unsupported EPUB audio clip time.')
        seconds = Decimal(match[1]) * {'h': 3600, 'min': 60, 's': 1,
                                       'ms': Decimal('.001'), None: 1}[match[2]]
    if not 0 <= seconds <= 7 * 86400:
        raise ValueError('EPUB audio time exceeds the supported limit.')
    return float(seconds)


@dataclass(frozen=True)
class OverlayCue:
    index: int
    fragment: str
    start: int
    end: int
    member: str
    suffix: str
    begin: float
    finish: float | None


@dataclass(frozen=True)
class OverlayChapter:
    asset_sha256: str
    ordinal: int
    resource: str
    text_sha256: str
    cues: tuple[OverlayCue, ...]


def _ranges(archive, member, fragments, raw, budget, warnings):
    """Use the existing text normalizer for exact start AND end positions.

    Empty span markers in a private XML copy mark fragment ends without adding
    text. Re-extraction must be byte-identical to the sealed chapter. Repeated
    phrases never use substring guesses, and this copy is never persisted.
    """
    document = epub._xml(archive, member, budget, chapter=True)
    nodes = {}
    for node in document.iter():
        for identity in {node.get('id'), node.get(epub.XML + 'id')} - {None}:
            nodes[identity] = None if identity in nodes else node
    prefix = 'carr-overlay-end-'
    while any(name.startswith(prefix) for name in nodes):
        prefix += 'x'
    ends = {}
    for index, fragment in enumerate(sorted(fragments - {''})):
        node = nodes.get(fragment)
        if node is None:
            raise ValueError('An EPUB audio text fragment is missing or ambiguous.')
        if node.tag in {epub.XHTML + name for name in ('img', 'br', 'link', 'meta', 'source', 'col')}:
            raise ValueError('This EPUB audio fragment has no supported source text.')
        marker = prefix + str(index)
        ET.SubElement(node, epub.XHTML + 'span', {'id': marker})
        ends[fragment] = marker
    memory = BytesIO()
    with ZipFile(memory, 'w') as staged:
        staged.writestr(member, ET.tostring(document, encoding='utf-8'))
    with ZipFile(BytesIO(memory.getvalue())) as staged:
        extracted, _, _, _, anchors = epub._chapter(staged, member, budget, warnings)
    if extracted != raw:
        raise ValueError('The EPUB synchronization text differs from the imported chapter.')
    result = {'': (0, len(raw))}
    for fragment, marker in ends.items():
        start, end = anchors.get(fragment), anchors.get(marker)
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
            raise ValueError('An EPUB audio text fragment has no visible source text.')
        if not raw[start:end].strip():
            raise ValueError('An EPUB audio text fragment is empty.')
        result[fragment] = start, end
    return result


def parse_overlay(data, ordinal):
    """Pure parser, called by the isolated child and explicit parser tests."""
    if type(data) is not bytes or not 0 < len(data) <= epub.MAX_SOURCE_BYTES:
        raise ValueError('Invalid EPUB audio source snapshot.')
    if type(ordinal) is not int or not 1 <= ordinal <= epub.MAX_SPINE:
        raise ValueError('Choose a source section in the imported EPUB.')
    validate_office(data, '.epub')
    budget, warnings = epub._Budget(), set()
    with ZipFile(BytesIO(data)) as archive:
        if 'META-INF/encryption.xml' in archive.namelist():
            raise ValueError('Encrypted or obfuscated EPUB resources are not supported for recorded-audio reading.')
        package_name, items, order, _, _ = epub._package(archive, budget, warnings)
        if ordinal > len(order):
            raise ValueError('The EPUB source section is unavailable.')
        selected_id, member, _ = order[ordinal - 1]
        package = epub._xml(archive, package_name, budget)
        if package.get('version') != '3.0':
            raise ValueError('Recorded overlays require an EPUB 3 package.')
        manifest = epub._one(package, epub.OPF + 'manifest', 'EPUB manifest is ambiguous.')
        selected = next(item for item in manifest if item.get('id') == selected_id)
        overlay_id = selected.get('media-overlay')
        if overlay_id not in items or items[overlay_id][1] != 'application/smil+xml':
            raise ValueError('This source section has no local recorded-audio media overlay.')
        overlay = items[overlay_id][0]
        if overlay is None:
            raise ValueError('Remote EPUB audio overlays are not loaded.')
        document = epub._xml(archive, overlay, budget)
        if document.tag != SMIL + 'smil' or document.get('version') != '3.0':
            raise ValueError('Unsupported EPUB media-overlay document.')
        if any(node.tag not in (SMIL + 'head', SMIL + 'body') for node in document):
            raise ValueError('Unsupported EPUB media-overlay structure.')
        body = epub._one(document, SMIL + 'body', 'EPUB audio body is missing or ambiguous.')
        rows, sequence_fragments = [], set()
        common = {'id', epub.XML + 'lang', '{http://www.idpf.org/2007/ops}type'}
        def attributes(node, allowed):
            if set(node.attrib) - common - allowed:
                raise ValueError('Additional SMIL timing, conditions or attributes are not supported.')
        def walk(node):
            attributes(node, {'{http://www.idpf.org/2007/ops}textref'}
                       if node.tag in (SMIL + 'body', SMIL + 'seq') else set())
            textref = node.get('{http://www.idpf.org/2007/ops}textref')
            if textref is not None:
                target, fragment = epub._navigation_link(overlay, textref)
                if target != member:
                    raise ValueError('The EPUB audio sequence points outside its source section.')
                sequence_fragments.add(fragment)
            if node.tag in (SMIL + 'body', SMIL + 'seq'):
                for child in node:
                    if child.tag not in (SMIL + 'seq', SMIL + 'par'):
                        raise ValueError('Only recorded EPUB seq/par overlays are supported.')
                    walk(child)
                return
            if node.tag != SMIL + 'par' or len(node) != 2:
                raise ValueError('Each supported EPUB audio segment needs one text and one audio reference.')
            text = epub._one(node, SMIL + 'text', 'EPUB audio text reference is missing or ambiguous.')
            audio = epub._one(node, SMIL + 'audio', 'EPUB recorded audio reference is missing or ambiguous.')
            attributes(text, {'src'})
            attributes(audio, {'src', 'clipBegin', 'clipEnd'})
            if len(text) or len(audio):
                raise ValueError('Nested EPUB media references are not supported.')
            target, fragment = epub._navigation_link(overlay, text.get('src', ''))
            if target != member:
                raise ValueError('The EPUB audio text points outside its source section.')
            sound = epub._resource(overlay, audio.get('src', ''))
            matches = [media for path, media, _ in items.values() if path == sound]
            if len(matches) != 1 or matches[0] not in _AUDIO:
                raise ValueError('The EPUB audio resource is not uniquely declared in a supported local format.')
            if sound not in archive.namelist():
                raise ValueError('The EPUB recorded audio resource is missing.')
            begin, finish = _clock(audio.get('clipBegin'), default=0.0), _clock(audio.get('clipEnd'))
            if finish is not None and (finish <= begin or finish - begin > MAX_CUE_SECONDS):
                raise ValueError('An EPUB audio clip is empty, reversed or longer than one hour.')
            rows.append((fragment, sound, _AUDIO[matches[0]], begin, finish))
            if len(rows) > MAX_CUES:
                raise ValueError('This EPUB section exceeds the 2048 recorded-segment limit.')
        walk(body)
        if not rows:
            raise ValueError('The EPUB source section has no recorded audio segments.')
        raw, _, _, _, _ = epub._chapter(archive, member, budget, warnings)
        ranges = _ranges(archive, member, {row[0] for row in rows} | sequence_fragments, raw, budget, warnings)
        cues = tuple(OverlayCue(index, fragment, *ranges[fragment], sound, suffix, begin, finish)
                     for index, (fragment, sound, suffix, begin, finish) in enumerate(rows))
        return OverlayChapter(sha256(data).hexdigest(), ordinal, member,
                              sha256(raw.encode('utf-8')).hexdigest(), cues)


def _decode_chapter(value, page, identity):
    if (not isinstance(value, dict) or set(value) != set(OverlayChapter.__dataclass_fields__)
            or value['asset_sha256'] != identity.asset_sha256
            or value['ordinal'] != page.ordinal
            or value['text_sha256'] != sha256(page.raw_text.encode('utf-8')).hexdigest()
            or not isinstance(value['resource'], str) or not value['resource']
            or not isinstance(value['cues'], list) or not 1 <= len(value['cues']) <= MAX_CUES):
        raise ValueError('The EPUB audio result differs from the authorized source section.')
    cues = []
    for index, row in enumerate(value['cues']):
        if not isinstance(row, dict) or set(row) != set(OverlayCue.__dataclass_fields__):
            raise ValueError('Invalid EPUB audio segment descriptor.')
        cue = OverlayCue(**row)
        if (type(cue.index) is not int or cue.index != index
                or type(cue.start) is not int or type(cue.end) is not int
                or not 0 <= cue.start < cue.end <= len(page.raw_text)
                or not isinstance(cue.fragment, str) or len(cue.fragment) > 1024
                or cue.suffix not in _AUDIO.values()
                or not isinstance(cue.member, str) or epub._resource('', cue.member) != cue.member
                or type(cue.begin) not in (int, float) or not 0 <= cue.begin <= 7 * 86400
                or cue.finish is not None and (type(cue.finish) not in (int, float)
                    or not cue.begin < cue.finish <= min(7 * 86400, cue.begin + MAX_CUE_SECONDS))):
            raise ValueError('The EPUB audio segment is outside its supported source bounds.')
        cues.append(cue)
    return OverlayChapter(value['asset_sha256'], page.ordinal, value['resource'], value['text_sha256'], tuple(cues))


@dataclass
class PreparedCue:
    cue: OverlayCue
    path: Path
    sha256: str
    frames: int
    sample_rate: int
    _temporary: object
    _admission: tuple | None = None
    _reading: object = field(default_factory=threading.Lock, repr=False)
    _closing: object = field(default_factory=threading.Event, repr=False)

    def close(self):
        # Windows cannot remove an open verification stream. Cancellation is
        # immediate; the last worker reader performs deferred file cleanup.
        self._closing.set()
        if self._reading.acquire(blocking=False):
            try:
                self._temporary.cleanup()
            finally:
                self._reading.release()


class EbookAudioSession:
    """One pinned section and temporary recordings, independent of quotations."""
    def __init__(self, desk, ordinal, *, guard=lambda: None, cancelled=lambda: False):
        self.guard, self.cancelled = guard, cancelled
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._temporary = None
        # Preserve the existing reader's immutable identity/descriptors while
        # giving this child transport its own cancellation lifetime.
        self.desk = desk = copy(desk)
        desk.cancelled = self.stopped
        self.guard(); _check(self.stopped)
        self.page = desk.page(ordinal)
        self.guard(); _check(self.stopped)
        with desk.library._search_read(desk.workspace, desk.timeout, self.stopped) as (db, budget):
            identity, metadata = desk._header(db)
            if identity != desk.identity or metadata != desk.metadata:
                raise ValueError('The EPUB source changed before recorded-audio reading.')
            self.original, mime, _ = desk.library._retained_source(
                db, desk.workspace, identity, cancelled=budget.stopped)
            if mime != 'application/epub+zip':
                raise ValueError('Recorded media overlays require an imported EPUB source.')
            data = desk.library._asset_bytes(db, desk.workspace, identity, cancelled=budget.stopped)
        self.guard(); _check(self.stopped)
        try:
            self._temporary = TemporaryDirectory(prefix='carr-epub-audio-')
            self.root = local_path(self._temporary.name, exists=True)
            self.source = self.root / 'source.epub'
            self.source.write_bytes(data)
            package_root = Path(__file__).resolve().parents[2]
            roots = [str(package_root)]
            if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
                roots.append(str(package_root.parent))
            bootstrap = ('import sys,runpy; count=int(sys.argv.pop(1)); '
                         'sys.path[:0]=[sys.argv.pop(1) for _ in range(count)]; '
                         "runpy.run_module('carr_library.reader.ebook_audio',run_name='__main__')")
            result = run_process([sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap,
                str(len(roots)), *roots, str(self.source), identity.asset_sha256, str(ordinal)],
                budget=AudioBudget(timeout=60, max_log_bytes=MAX_REPLY, max_output_bytes=epub.MAX_SOURCE_BYTES,
                                   reserve_disk_bytes=0), cancelled=self.stopped,
                owned_windows_job=os.name == 'nt')
            value = json.loads(result.stdout)
            self.chapter = _decode_chapter(value, self.page, identity)
            self.check()
        except AudioCleanupPending as error:
            temporary, self._temporary = self._temporary, None
            if temporary is not None:
                _retain_temporary(error, temporary)
            raise
        except BaseException:
            self.close()
            raise

    def stopped(self):
        return self._closed.is_set() or self.cancelled()

    def check(self):
        self.guard(); _check(self.stopped)
        page = self.desk.page(self.page.ordinal)
        if page != self.page:
            raise ValueError('The EPUB source section changed. Reopen recorded-audio reading.')
        self.guard(); _check(self.stopped)

    def prepare(self, index, *, cancelled=lambda: False):
        if type(index) is not int or not 0 <= index < len(self.chapter.cues):
            raise ValueError('Choose a recorded EPUB segment.')
        if not self._lock.acquire(blocking=False):
            raise ValueError('Wait for the current EPUB audio preparation.')
        temporary = None
        stopped = lambda: self.stopped() or cancelled()
        try:
            self.check(); _check(stopped)
            cue = self.chapter.cues[index]
            if digest_file(self.source) != self.chapter.asset_sha256:
                raise ValueError('The private EPUB source snapshot changed.')
            temporary = TemporaryDirectory(prefix='carr-epub-clip-')
            root = local_path(temporary.name, exists=True)
            sound = root / ('recording' + cue.suffix)
            with ZipFile(self.source) as archive, archive.open(cue.member) as source, sound.open('xb') as target:
                total = 0
                while block := source.read(64 * 1024):
                    _check(stopped)
                    total += len(block)
                    if total > 32 * 1024 * 1024:
                        raise ValueError('The EPUB recorded audio exceeds its member budget.')
                    target.write(block)
            runtime = audio_runtime(root, state_root=self.desk.library.root / 'state',
                budget=AudioBudget(timeout=120, max_output_bytes=512 * 1024 * 1024))
            info = runtime.inspect(sound, cancelled=stopped)
            finish = info.duration if cue.finish is None else cue.finish
            if finish > info.duration + 1 / info.sample_rate or not 0 < finish - cue.begin <= MAX_CUE_SECONDS:
                raise ValueError('The EPUB clip timing exceeds the measured audio or one-hour segment limit.')
            destination = root / 'clip.wav'
            runtime.convert(sound, destination, start=cue.begin, duration=finish-cue.begin,
                            sample_rate=24000, channels=2, cancelled=stopped)
            self.check(); _check(stopped)
            with wave.open(str(destination), 'rb') as rendered:
                frames, sample_rate = rendered.getnframes(), rendered.getframerate()
            expected = round((finish-cue.begin) * sample_rate)
            if frames != expected or sample_rate != 24000:
                raise ValueError('The prepared EPUB clip differs from its authored time interval.')
            result = PreparedCue(cue, destination, digest_file(destination), frames, sample_rate, temporary)
            self.admit(result)
            _check(stopped)
            temporary = None
            return result
        except AudioCleanupPending as error:
            if temporary is not None:
                _retain_temporary(error, temporary)
                temporary = None
            raise
        finally:
            if temporary is not None:
                temporary.cleanup()
            if self._closed.is_set() and self._temporary is not None:
                self._temporary.cleanup()
            self._lock.release()

    def admit(self, prepared):
        """Full content admission on the worker, before scheduling native start."""
        if not isinstance(prepared, PreparedCue) or prepared.cue not in self.chapter.cues:
            raise ValueError('The prepared EPUB audio segment differs from this section.')
        if not prepared._reading.acquire(blocking=False):
            raise ValueError('Wait for the current EPUB recording verification.')
        try:
            _check(lambda: self.stopped() or prepared._closing.is_set())
            before = self._stamp(self.original), self._stamp(prepared.path)
            self.check()
            if digest_file(prepared.path) != prepared.sha256:
                raise ValueError('The prepared EPUB audio segment changed before playback.')
            _check(lambda: self.stopped() or prepared._closing.is_set())
            after = self._stamp(self.original), self._stamp(prepared.path)
            if before != after:
                raise ValueError('The EPUB source or prepared recording changed during verification.')
            prepared._admission = (id(self), *after)
            return prepared
        finally:
            try:
                if prepared._closing.is_set():
                    prepared._temporary.cleanup()
            finally:
                prepared._reading.release()

    @staticmethod
    def _stamp(path):
        value = regular(local_path(path, exists=True))
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

    def start_check(self, prepared):
        """Fresh scope and file identity at GUI dispatch, without large reads.

        Full hashes belong to ``admit`` on the worker. These private staging
        files are not an OS sandbox against another same-user process forging
        metadata or racing the final native open.
        """
        self.guard(); _check(self.stopped)
        if not isinstance(prepared, PreparedCue) or prepared.cue not in self.chapter.cues:
            raise ValueError('The prepared EPUB audio segment differs from this section.')
        _check(prepared._closing.is_set)
        with self.desk.library._search_read(self.desk.workspace, min(self.desk.timeout, .5), self.stopped) as (db, _):
            identity, metadata = self.desk._header(db)
            if identity != self.desk.identity or metadata != self.desk.metadata:
                raise ValueError('The EPUB source changed. Reopen recorded-audio reading.')
        if prepared._admission != (id(self), self._stamp(self.original), self._stamp(prepared.path)):
            raise ValueError('The EPUB source or prepared recording changed after verification.')
        self.guard(); _check(self.stopped)

    def close(self):
        self._closed.set()
        if self._lock.acquire(blocking=False):
            try:
                if self._temporary is not None:
                    self._temporary.cleanup()
            finally:
                self._lock.release()


def _child():
    from ..ingest.worker import _resource_limits
    _resource_limits(60)
    if len(sys.argv) != 4:
        raise ValueError('Invalid EPUB audio parser request.')
    source = local_path(sys.argv[1], exists=True)
    if not 0 < regular(source).st_size <= epub.MAX_SOURCE_BYTES:
        raise ValueError('The EPUB source snapshot is outside its size allowance.')
    data = source.read_bytes()
    if sha256(data).hexdigest() != sys.argv[2]:
        raise ValueError('The EPUB source changed before isolated parsing.')
    result = json.dumps(asdict(parse_overlay(data, int(sys.argv[3]))), ensure_ascii=False, allow_nan=False)
    if len(result.encode('utf-8')) > MAX_REPLY:
        raise ValueError('The EPUB audio index exceeds its output budget.')
    print(result)


if __name__ == '__main__':
    _child()
