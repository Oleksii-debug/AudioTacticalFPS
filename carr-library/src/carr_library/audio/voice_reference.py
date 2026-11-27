"""Consent-bound local reference review for the installed OmniVoice recipe.

The existing owner package API creates the archive. This module measures a
captured PCM copy and binds the exact audio/text and declared provenance to a
review. It does not infer a speaker, verify consent, train a model or synthesize.
"""
from array import array
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4
import wave
import zipfile

from .cleanup import retained_context, retained_temporary_directory
from .dsp import AudioRuntime
from .models import AudioBudget, AudioCancelled
from .voice_packages import (VoicePackageManager, _document, _hash, _json,
                             _language, _query, runtime_settings)
from ..durable_state import local_path, regular, _open_json_file as _open_read
from ..operations import ResourceBroker, ResourceRequest
from .package_publication import publish_package

MAX_REFERENCE_BYTES = 32 * 1024**2
MAX_REFERENCE_SECONDS = 120
MAX_TRANSCRIPT_CHARACTERS = 16_000
MAX_TRANSCRIPT_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 64 * 1024**2
_CHUNK = 256 * 1024


def _text(value, label, maximum, *, multiline=False):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 and (not multiline or char not in '\r\n\t') for char in value)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError('Enter a bounded explicit ' + label + '.')
    return value


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(path):
    info = regular(local_path(path, exists=True), directory=True)
    return (info.st_dev, info.st_ino)


def _notify(progress, completed, phase):
    if progress is not None:
        try:
            progress(completed, 3, phase)
        except Exception:
            pass  # An observer cannot change a durable publication outcome.


@dataclass(frozen=True)
class ReferenceDescription:
    profile_id: str
    version: str
    name: str
    language: str
    provenance_source: str
    rights_notice: str
    reference_text: str

    def __post_init__(self):
        from .voice_packages import ID, VERSION
        if (type(self.profile_id) is not str or not ID.fullmatch(self.profile_id)
                or type(self.version) is not str or len(self.version) > 80
                or not VERSION.fullmatch(self.version)):
            raise ValueError('Use an explicit profile ID and major.minor.patch version.')
        _text(self.name, 'profile display name', 100)
        if type(self.language) is not str or _language(self.language) != self.language:
            raise ValueError('Choose one explicit lowercase language code, for example uk or en-us.')
        _text(self.provenance_source, 'reference provenance', 4096, multiline=True)
        _text(self.rights_notice, 'reference rights and license notice', 4096, multiline=True)
        _text(self.reference_text, 'text matching this recording', MAX_TRANSCRIPT_CHARACTERS, multiline=True)
        if len(self.reference_text.encode('utf-8')) > MAX_TRANSCRIPT_BYTES:
            raise ValueError('The reference transcript exceeds its UTF-8 byte limit.')


@dataclass(frozen=True)
class ReferenceReview:
    source: str
    source_identity: tuple
    source_sha256: str
    source_size: int
    description: ReferenceDescription
    library_identity: tuple
    manifest_sha256: str
    settings_revision: int
    tools_json: str
    pcm_json: str
    document_json: str

    @property
    def document(self):
        return json.loads(self.document_json)

    @property
    def pcm(self):
        return json.loads(self.pcm_json)

    @property
    def fingerprint(self):
        return _hash({
            'source': self.source, 'source_identity': self.source_identity,
            'source_sha256': self.source_sha256, 'source_size': self.source_size,
            'library_identity': self.library_identity,
            'manifest_sha256': self.manifest_sha256, 'settings_revision': self.settings_revision,
            'tools': json.loads(self.tools_json), 'pcm': self.pcm,
            'document': self.document})

    def summary(self):
        pcm = self.pcm
        rms = 'digital silence' if pcm['rms_dbfs'] is None else f"{pcm['rms_dbfs']:.2f} dBFS"
        peak = 'digital silence' if pcm['peak_dbfs'] is None else f"{pcm['peak_dbfs']:.2f} dBFS"
        return (
            f'{self.description.name}; ID {self.description.profile_id}; version {self.description.version}\n'
            f'Declared language: {self.description.language}; backend: omnivoice\n'
            f"Captured PCM: {pcm['frames']} frames, {pcm['sample_rate']} Hz, "
            f"{pcm['channels']} channel(s), 16-bit; {pcm['seconds']:.3f} seconds.\n"
            f"Measured peak: {peak}; RMS: {rms}; full-scale samples: {pcm['full_scale_samples']}; "
            f"zero samples: {pcm['zero_samples']} / {pcm['samples']}.\n"
            + ('Warning: all PCM samples are zero.\n' if pcm['peak'] == 0 else '')
            + ('Warning: full-scale PCM samples are present; listen for clipping.\n'
               if pcm['full_scale_samples'] else '')
            + f'Audio SHA-256: {self.source_sha256}\n'
            + f"Transcript SHA-256: {self.document['files']['reference.txt']['sha256']}\n"
            + f'Provenance: {self.description.provenance_source}\n'
            + f'Rights/license: {self.description.rights_notice}\n\n'
            + 'Reference transcript, exactly as supplied:\n' + self.description.reference_text + '\n\n'
            + 'Review the recording and transcript yourself. These measurements do not verify speech content, '
            + 'speaker identity, permission, transcript agreement, pronunciation or speaker similarity. '
            + 'No training or inference has run. Create saves a LOCAL UNENCRYPTED data-only package; '
            + 'the original recording is not modified. Installation and production-language approval '
            + 'remain separate explicit reviews in Reference voice packages.')


@dataclass(frozen=True)
class ReferencePackageResult:
    path: str
    sha256: str
    size_bytes: int
    review_sha256: str
    profile_id: str
    version: str
    cleanup_warning: str = ''
    encrypted: bool = False
    model_inference: str = 'NOT_RUN'


def _capture(path, *, maximum, cancelled, target=None):
    """Hash the exact bounded descriptor bytes; optionally create a private copy."""
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size <= 0 or before.st_size > maximum:
        raise ValueError('The selected local file is empty or exceeds the reference byte limit.')
    digest, count = sha256(), 0
    with _open_read(path) as stream:
        opened = os.fstat(stream.fileno())
        if _stamp(opened) != _stamp(before):
            raise ValueError('The selected reference file changed before capture.')
        destination = target.open('xb') if target is not None else None
        try:
            while True:
                if cancelled():
                    raise AudioCancelled('Reference review cancelled; original files are unchanged.')
                chunk = stream.read(min(_CHUNK, before.st_size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > before.st_size or count > maximum:
                    raise ValueError('The reference file grew during bounded capture.')
                digest.update(chunk)
                if destination is not None:
                    destination.write(chunk)
            if count != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise ValueError('The reference file changed during capture.')
            if destination is not None:
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            if destination is not None:
                destination.close()
    if _stamp(regular(path)) != _stamp(before):
        raise ValueError('The selected reference path changed during capture.')
    return _stamp(before), digest.hexdigest(), count


def _measure_pcm(path, *, cancelled):
    samples = square_sum = signed_sum = zero = full_scale = peak = 0
    try:
        with wave.open(str(path), 'rb') as source:
            channels, width, rate, frames = (source.getnchannels(), source.getsampwidth(),
                                             source.getframerate(), source.getnframes())
            if (source.getcomptype() != 'NONE' or width != 2 or channels not in (1, 2)
                    or not 8000 <= rate <= 192000 or frames <= 0
                    or frames > MAX_REFERENCE_SECONDS * rate):
                raise ValueError('Choose a standard 16-bit mono/stereo PCM WAV of at most 120 seconds.')
            remaining = frames
            while remaining:
                if cancelled():
                    raise AudioCancelled('Reference PCM review cancelled.')
                wanted = min(remaining, 32768)
                chunk = source.readframes(wanted)
                if len(chunk) != wanted * channels * width:
                    raise ValueError('The reference WAV has truncated PCM samples.')
                values = array('h')
                values.frombytes(chunk)
                if sys.byteorder != 'little':
                    values.byteswap()
                samples += len(values)
                square_sum += sum(int(value) * int(value) for value in values)
                signed_sum += sum(values)
                zero += values.count(0)
                full_scale += sum(value in (-32768, 32767) for value in values)
                peak = max(peak, max(abs(int(value)) for value in values))
                remaining -= wanted
    except (wave.Error, EOFError) as exc:
        raise ValueError('Choose a standard, readable 16-bit PCM WAV; convert a copy with Audio tools if needed.') from exc
    rms = math.sqrt(square_sum / samples)
    return {'sample_rate': rate, 'channels': channels, 'frames': frames,
            'seconds': frames / rate, 'samples': samples, 'peak': peak,
            'peak_dbfs': None if peak == 0 else 20 * math.log10(peak / 32768),
            'rms_dbfs': None if rms == 0 else 20 * math.log10(rms / 32768),
            'dc_mean': signed_sum / samples, 'zero_samples': zero,
            'full_scale_samples': full_scale}


def _archive_bytes(path, document, *, cancelled):
    """Bind the captured archive bytes to this exact two-asset reviewed recipe."""
    before = _capture(path, maximum=MAX_PACKAGE_BYTES, cancelled=cancelled)
    with _open_read(path) as source:
        payload = source.read(MAX_PACKAGE_BYTES + 1)
    if len(payload) != before[2] or sha256(payload).hexdigest() != before[1]:
        raise ValueError('The prepared archive changed before its immutable capture.')
    expected = {'voice-package.json', *document['files']}
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        members = archive.infolist()
        if len(members) != len(expected) or {item.filename for item in members} != expected:
            raise ValueError('The owner package contains unexpected or duplicate members.')
        for item in members:
            if (item.orig_filename != item.filename or item.is_dir() or item.flag_bits & 1
                    or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG)
                    or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError('The reference package contains an unsupported member.')
            if cancelled():
                raise AudioCancelled('Reference package verification cancelled.')
            limit = 128 * 1024 if item.filename == 'voice-package.json' else document['files'][item.filename]['size_bytes']
            if item.file_size > limit:
                raise ValueError('The reference package member exceeds its reviewed bound.')
            digest, count, metadata = sha256(), 0, []
            with archive.open(item) as source:
                while True:
                    if cancelled():
                        raise AudioCancelled('Reference package verification cancelled.')
                    chunk = source.read(min(_CHUNK, limit - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > limit:
                        raise ValueError('The reference archive exceeds its actual member bound.')
                    digest.update(chunk)
                    if item.filename == 'voice-package.json':
                        metadata.append(chunk)
            if item.filename == 'voice-package.json':
                if _document(json.loads(b''.join(metadata).decode('utf-8')), backend='omnivoice') != document:
                    raise ValueError('The packaged document differs from the reviewed reference.')
            else:
                row = document['files'][item.filename]
                if count != row['size_bytes'] or digest.hexdigest() != row['sha256']:
                    raise ValueError('The packaged reference bytes differ from the reviewed recording/text.')
    return payload


class VoiceReferenceService:
    """Synchronous work on the caller's existing worker; no store or model runner."""
    def __init__(self, root, *, runtime=None, scope_guard=lambda: None):
        if runtime is not None and not isinstance(runtime, AudioRuntime):
            raise TypeError('Use the existing reviewed AudioRuntime for reference inspection.')
        if not callable(scope_guard):
            raise TypeError('A callable reference scope guard is required.')
        self.root = Path(root).absolute()
        self.runtime, self.scope_guard = runtime, scope_guard

    def _check(self, cancelled, expected_root=None):
        self.scope_guard()
        if cancelled():
            raise AudioCancelled('Reference preparation cancelled before publication.')
        identity = _directory_identity(self.root)
        if expected_root is not None and identity != expected_root:
            raise ValueError('The selected library changed after reference review.')
        return identity

    def _current(self, cancelled):
        self._check(cancelled)
        manager = VoicePackageManager(self.root)
        previous, manifest = manager._current(cancelled=cancelled)
        settings = runtime_settings(manifest.settings)
        if settings['reference_engine'] != 'omnivoice' or manifest.settings.get('managed_profiles') is not True:
            raise ValueError('Configure and review the OmniVoice backend in Reference voice packages first.')
        return manager, previous, manifest

    def _tools(self, cancelled):
        from .render import tool_path
        if self.runtime is None:
            from ..runtime_settings import _enabled_settings
            settings = _enabled_settings(self.root, cancelled=cancelled)
            selected = {'ffmpeg': settings['ffmpeg'], 'ffprobe': settings['ffprobe']}
        else:
            selected = {'ffmpeg': self.runtime.ffmpeg, 'ffprobe': self.runtime.ffprobe}
        tools = {}
        for name, value in selected.items():
            path = tool_path(name, value)
            identity, checksum, size = _capture(path, maximum=512 * 1024**2, cancelled=cancelled)
            tools[name] = {'path': str(path), 'identity': identity, 'sha256': checksum, 'size_bytes': size}
        return tools

    @contextmanager
    def _admit(self, cancelled, *, destination=None, manifest_sha256, library_identity):
        broker = ResourceBroker.for_app(self.root)
        staging_parent = self.root / 'runtime' if destination is None else destination.parent
        regular(staging_parent, directory=True)
        request = ResourceRequest(cpu_slots=1, memory_bytes=384 * 1024**2,
            disk_bytes=MAX_REFERENCE_BYTES + MAX_PACKAGE_BYTES + 2 * 1024**2,
            disk_path=staging_parent,
            devices=('voice-maintenance:' + manifest_sha256,))
        with retained_context(broker.reserve(request, operation_id='voice-reference:' + uuid4().hex,
                                             cancelled=cancelled, timeout=60, run_timeout=600)) as lease:
            def stopped():
                if cancelled():
                    return True
                lease.checkpoint()
                self._check(lambda: False, library_identity)
                return False
            yield staging_parent, stopped

    def _review(self, source, description, stage, previous, manifest, tools, library_identity, cancelled):
        self._check(cancelled, library_identity)
        audio = stage / 'reference.wav'
        identity, checksum, size = _capture(source, maximum=MAX_REFERENCE_BYTES,
                                             cancelled=cancelled, target=audio)
        pcm = _measure_pcm(audio, cancelled=cancelled)
        runtime = AudioRuntime(stage, ffmpeg=tools['ffmpeg']['path'], ffprobe=tools['ffprobe']['path'],
            budget=AudioBudget(timeout=30, threads=1, max_alloc_bytes=128 * 1024**2,
                               max_output_bytes=MAX_PACKAGE_BYTES, max_log_bytes=1024**2,
                               max_inputs=1, max_staging_files=16))
        info = runtime.inspect(audio, cancelled=cancelled)
        if (info.sample_rate != pcm['sample_rate'] or info.channels != pcm['channels']
                or info.frames != pcm['frames'] or info.codec != 'pcm_s16le'
                or abs(info.duration - pcm['seconds']) > 1 / pcm['sample_rate']):
            raise ValueError('Native audio inspection disagrees with the captured PCM frames.')
        if _capture(source, maximum=MAX_REFERENCE_BYTES, cancelled=cancelled) != (identity, checksum, size):
            raise ValueError('The original reference changed during review.')
        transcript = description.reference_text.encode('utf-8')
        (stage / 'reference.txt').write_bytes(transcript)
        document = _document({'format': 'dordiai-voice-package', 'format_version': 1,
            'id': description.profile_id, 'version': description.version, 'name': description.name,
            'engine': {'id': 'omnivoice', 'compatibility_version': 1},
            'languages': [description.language], 'styles': ['neutral'],
            'provenance': {'source': description.provenance_source, 'license': description.rights_notice},
            'files': {'reference.wav': {'sha256': checksum, 'size_bytes': size, 'role': 'reference_audio'},
                      'reference.txt': {'sha256': sha256(transcript).hexdigest(),
                                        'size_bytes': len(transcript), 'role': 'reference_text'}}},
            backend='omnivoice')
        return ReferenceReview(str(local_path(source, exists=True)), identity, checksum, size, description,
            self._check(cancelled, library_identity), manifest.manifest_sha256, previous['revision'],
            _json(tools), _json(pcm), _json(document))

    def review(self, source, description, *, consent=False, cancelled=lambda: False, progress=None):
        if consent is not True or type(description) is not ReferenceDescription:
            raise ValueError('Explicit reference permission and a complete description are required before review.')
        library_identity = self._check(cancelled)
        manager, previous, manifest = self._current(cancelled)
        with self._admit(cancelled, manifest_sha256=manifest.manifest_sha256,
                         library_identity=library_identity) as (parent, stopped):
            tools = self._tools(stopped)
            _notify(progress, 0, 'Capturing the selected local reference')
            with retained_temporary_directory(prefix='.voice-reference-review-', dir=parent) as temporary:
                result = self._review(source, description, Path(temporary), previous, manifest, tools, library_identity, stopped)
                if manager.store.read() != previous or self._tools(stopped) != tools:
                    raise ValueError('The selected voice/audio configuration changed during review.')
                manifest.verify(cancelled=stopped, require_complete=True)
                self._check(stopped, result.library_identity)
                _notify(progress, 3, 'Reference measurements are ready for explicit review')
                return result

    def create(self, review, destination, *, consent=False, quality_reviewed=False,
               cancelled=lambda: False, progress=None):
        if type(review) is not ReferenceReview or consent is not True or quality_reviewed is not True:
            raise ValueError('Review the exact reference and explicitly confirm permission and the recording/text review.')
        from ..studio.publishing import capture_publication_destination
        self._check(cancelled, review.library_identity)
        approval = capture_publication_destination(destination, private_root=self.root, cancelled=cancelled)
        if approval.target.suffix.lower() not in ('.zip', '.dordiai-voice'):
            raise ValueError('Choose a new local .zip or .dordiai-voice data package.')
        manager, previous, manifest = self._current(cancelled)
        if (previous['revision'] != review.settings_revision
                or manifest.manifest_sha256 != review.manifest_sha256):
            raise ValueError('Voice registration changed after the reference review.')
        if approval.target.is_relative_to(manifest.root):
            raise ValueError('Export the reference outside the installed engine directory.')
        result = None
        try:
            with self._admit(cancelled, destination=approval.target,
                             manifest_sha256=manifest.manifest_sha256,
                             library_identity=review.library_identity) as (parent, stopped):
                tools = self._tools(stopped)
                with retained_temporary_directory(prefix='.voice-reference-package-', dir=parent) as temporary:
                    stage = Path(temporary)
                    current = self._review(review.source, review.description, stage, previous, manifest, tools, review.library_identity, stopped)
                    if current != review:
                        raise ValueError('The selected reference, transcript or audio configuration changed after review.')
                    _notify(progress, 1, 'Creating the reviewed local data-only reference package')
                    archive = stage / 'created.dordiai-voice'
                    returned = _document(_query(manifest, 'create_reference', source=str(stage),
                        document=review.document, output=str(archive),
                        expected_document_sha256=_hash(review.document), timeout=600, cancelled=stopped),
                        backend='omnivoice')
                    if returned != review.document:
                        raise ValueError('The installed owner returned a different reference document.')
                    payload = _archive_bytes(archive, review.document, cancelled=stopped)
                    checksum, size = sha256(payload).hexdigest(), len(payload)
                    del payload  # The shared publication owner rechecks these exact bytes at commit.
                    if manager.store.read() != previous or self._tools(stopped) != tools:
                        raise ValueError('Voice/audio settings changed before reference publication.')
                    manifest.verify(cancelled=stopped, require_complete=True)
                    if _capture(review.source, maximum=MAX_REFERENCE_BYTES, cancelled=stopped) != (
                            review.source_identity, review.source_sha256, review.source_size):
                        raise ValueError('The original recording changed before package publication.')
                    _notify(progress, 2, 'Publishing the selected local unencrypted package')
                    def publication_cancelled():
                        if stopped():
                            return True
                        self._check(lambda: False, review.library_identity)
                        if manager.store.read() != previous:
                            raise ValueError('Voice registration changed before package publication.')
                        return False
                    # Keep this exact stage alive until the shared owner proves
                    # the post-fsync bytes, parent and atomically linked target.
                    receipt = publish_package(archive, approval.requested, private_root=self.root,
                        expected_sha256=checksum, maximum_bytes=MAX_PACKAGE_BYTES,
                        approval=approval, cancelled=publication_cancelled)
                    result = ReferencePackageResult(str(receipt.path), checksum, size,
                        review.fingerprint, review.description.profile_id, review.description.version,
                        cleanup_warning=receipt.cleanup_warning or '')
        except BaseException:
            if result is None:
                raise
            # A lease-release diagnostic cannot turn a proven committed file
            # into an apparent cancellation or invite duplicate publication.
            result = replace(result, cleanup_warning=(result.cleanup_warning + '\n'
                + 'The package was published; local temporary or reservation cleanup needs review.').strip())
        _notify(progress, 3, 'Local unencrypted reference package saved; installation still requires review')
        return result
