"""Reviewed local Whisper transcription using the existing audio job primitives.

The faster-whisper call, segment extraction and TXT/SRT/VTT workflow adapt the
owner-supplied accessible Whisper V4.3 worker, SHA256
1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9.
Its GUI, queue, basename adoption, language substitution and monkey patches are
not imported. No upstream licence or rights clearance is inferred from receipt.

This optional in-process backend cancels between yielded inference segments;
it cannot interrupt a blocked native inference call. No model is downloaded.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import tempfile
import threading

from .engines import EngineUnavailable
from .jobs import (atomic_json, checked_directory, project_lock, publish_new,
                   regular_file, sha256_file)
from .model_manifest import LocalModelManifest, ModelManifestError, digest_file, read_json
from .models import digest


ADAPTER_VERSION = 'owner-whisper-adapter-1'
MAX_RESULT_BYTES = 64 * 1024 ** 2
MAX_SOURCE_BYTES = 32 * 1024 ** 3
MAX_SEGMENTS = 250000
RUNTIME_DISTRIBUTIONS = ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime',
                         'numpy', 'huggingface-hub', 'tokenizers')
FORMATS = ('txt', 'srt', 'vtt', 'json')


def receipt_notes(receipt):
    """Keep any retained private audio snapshot actionable in every frontend."""
    notes = [str(value) for value in receipt.get('warnings', ())]
    if receipt.get('cleanup_pending'):
        notes.append('Temporary audio snapshot awaiting cleanup: ' + str(receipt['cleanup_pending']))
    return tuple(notes)


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Transcription cancelled. Completed verified results remain available.')


@contextmanager
def _staging_directory(root):
    """Cleanup cannot turn an already committed transcript into a failure."""
    temporary = tempfile.TemporaryDirectory(prefix='.transcription-', dir=root)
    receipt, active_error = {}, None
    try:
        yield Path(temporary.name), receipt
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        try:
            temporary.cleanup()
        except OSError:
            warning = 'The transcript is saved, but its temporary audio snapshot could not be removed.'
            if receipt.get('status') == 'complete':
                receipt['warnings'] = [warning]
                receipt['cleanup_pending'] = temporary.name
            elif active_error is not None:
                active_error.add_note('The temporary audio snapshot also needs cleanup: ' + temporary.name)
            else:
                raise


@dataclass(frozen=True)
class TranscriptionSettings:
    language: str = 'auto'
    task: str = 'transcribe'
    beam_size: int = 5
    vad_filter: bool = True
    word_timestamps: bool = False
    condition_on_previous_text: bool = True
    device: str = 'cpu'
    compute_type: str = 'int8'

    def __post_init__(self):
        if not isinstance(self.language, str) or not re.fullmatch('auto|[a-z]{2,3}', self.language):
            raise ValueError('Choose auto or a supported Whisper language code, such as uk or en.')
        if self.task not in ('transcribe', 'translate'):
            raise ValueError('Whisper supports transcription or translation into English only.')
        if type(self.beam_size) is not int or not 1 <= self.beam_size <= 10:
            raise ValueError('Beam size must be an integer from 1 to 10.')
        for name in ('vad_filter', 'word_timestamps', 'condition_on_previous_text'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be a boolean.')
        if self.device not in ('cpu', 'cuda'):
            raise ValueError('Select CPU or CUDA explicitly; no device fallback is performed.')
        if self.compute_type not in ('int8', 'int8_float16', 'int8_float32', 'float16', 'float32'):
            raise ValueError('Select a supported explicit compute type.')


def transcription_runtime_identity(*, cancelled=lambda: False):
    """Fingerprint the installed ASR components, without importing model code.

    This identifies actual installed files, not only their declared versions.
    It is not a redistributable runtime lock or a hardware quality qualification.
    """
    result = {'python': platform.python_version(), 'implementation': platform.python_implementation(),
              'platform': platform.system(), 'machine': platform.machine(),
              'executable_sha256': digest_file(Path(sys.executable).resolve(), cancelled=cancelled),
              'distributions': {}}
    try:
        for name in RUNTIME_DISTRIBUTIONS:
            distribution = metadata.distribution(name)
            files = distribution.files
            if files is None:
                raise EngineUnavailable('The installed ASR runtime has no verifiable file inventory.')
            entries = []
            for entry in sorted(files, key=str):
                _cancel(cancelled)
                # Generated bytecode and launcher paths outside the distribution
                # are not imported ASR code; fingerprint its installed payload.
                if entry.hash is None or '..' in entry.parts or entry.suffix == '.pyc':
                    continue
                actual = Path(distribution.locate_file(entry)).resolve(strict=True)
                entries.append((str(entry), digest_file(actual, cancelled=cancelled)))
                if len(entries) > 65536:
                    raise EngineUnavailable('The installed ASR runtime exceeds its inventory budget.')
            if not entries:
                raise EngineUnavailable('The installed ASR runtime has an empty file inventory.')
            result['distributions'][name] = {'version': distribution.version, 'files_sha256': digest(entries)}
    except InterruptedError:
        raise
    except (metadata.PackageNotFoundError, OSError) as exc:
        raise EngineUnavailable('Install a compatible local faster-whisper runtime before transcription; no download was attempted.') from exc
    return result


def _default_model_factory(path, **options):
    try:
        from faster_whisper import WhisperModel
    except (ImportError, OSError) as exc:
        raise EngineUnavailable('The selected local faster-whisper runtime cannot be imported.') from exc
    return WhisperModel(str(path), local_files_only=True, num_workers=1, **options)


def _time(value):
    # Round once to milliseconds before dividing: 59.9996 must become 00:01:00,
    # rather than the invalid subtitle time 00:00:60 produced by the old worker.
    milliseconds = round(value * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30 * 86400:
        raise ValueError('The transcription engine returned an invalid timestamp.')
    return float(value)


def _text(value, *, maximum=100000):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError('The transcription engine returned invalid or excessive text.')
    return value.strip()


def _validated_result(value, *, requested_language='auto'):
    if not isinstance(value, dict) or set(value) != {'language', 'duration', 'segments', 'text'}:
        raise ValueError('Invalid transcription result fields.')
    language = value['language']
    if not isinstance(language, str) or not re.fullmatch('[a-z]{2,3}', language):
        raise ValueError('The transcription engine returned an invalid language code.')
    # Whisper's translate task still reports the SOURCE language here; English
    # is the output target. A cache digest does not establish this relationship.
    if requested_language != 'auto' and language != requested_language:
        raise ValueError('The transcript does not match the requested source language '
                         f'({requested_language}); the engine reported {language}.')
    _seconds(value['duration'])
    segments = value['segments']
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        raise ValueError('The transcription exceeds its segment budget.')
    previous = 0.0
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {'start', 'end', 'text', 'words'}:
            raise ValueError('Invalid transcription segment.')
        start, end = _seconds(segment['start']), _seconds(segment['end'])
        if start < previous or end < start:
            raise ValueError('The transcription engine returned out-of-order timestamps.')
        previous = start
        _text(segment['text'])
        words = segment['words']
        if not isinstance(words, list) or len(words) > 10000:
            raise ValueError('The transcription exceeds its word timestamp budget.')
        word_start = start
        for word in words:
            if not isinstance(word, dict) or set(word) != {'start', 'end', 'word'}:
                raise ValueError('Invalid transcription word.')
            beginning, ending = _seconds(word['start']), _seconds(word['end'])
            if beginning < word_start or ending < beginning or ending > end + 0.01:
                raise ValueError('The transcription engine returned invalid word timestamps.')
            word_start = beginning
            _text(word['word'])
    expected = '\n'.join(segment['text'].strip() for segment in segments if segment['text'].strip())
    if value['text'] != expected:
        raise ValueError('The transcript text does not match its verified segments.')
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_RESULT_BYTES:
        raise ValueError('The transcription exceeds its result byte budget.')
    return value


def _encoded_outputs(result, provenance):
    segments = [segment for segment in result['segments'] if segment['text'].strip()]
    srt, vtt = [], ['WEBVTT', '']
    for index, segment in enumerate(segments, 1):
        line = f"{_time(segment['start'])} --> {_time(segment['end'])}"
        text = segment['text'].strip()
        srt.extend((str(index), line, text, ''))
        vtt.extend((line.replace(',', '.'), text, ''))
    return {
        'txt': (result['text'] + ('\n' if result['text'] else '')).encode('utf-8'),
        'srt': '\n'.join(srt).encode('utf-8'),
        'vtt': ('\n'.join(vtt) + '\n').encode('utf-8'),
        'json': json.dumps({'schema_version': 1, **provenance, **result},
                           ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode('utf-8'),
    }


class TranscriptionSession:
    """One reviewed model selection, serialized through all yielded segments."""

    def __init__(self, model_manifest, *, _model_factory=None, _runtime_identity=None):
        self.manifest = LocalModelManifest.load(model_manifest, engine='faster-whisper')
        if not self.manifest.trusted_weights:
            raise ModelManifestError('Review the exact local Whisper model before execution.')
        options = self.manifest.settings
        if set(options) - {'model_directory', 'runtime_versions'}:
            raise ModelManifestError('Unsupported local Whisper installation settings.')
        relative = options.get('model_directory', '.')
        if not isinstance(relative, str) or (relative != '.' and (
                PurePosixPath(relative).is_absolute() or any(part in ('', '.', '..') for part in relative.split('/'))
                or '\\' in relative or ':' in relative)):
            raise ModelManifestError('Choose a relative local model directory.')
        prefix = '' if relative == '.' else relative + '/'
        for required in ('model.bin', 'config.json', 'tokenizer.json'):
            self.manifest.asset(prefix + required)
        self.model_directory = self.manifest.asset(prefix + 'model.bin').parent
        self._factory = _model_factory or _default_model_factory
        self._runtime_identity = _runtime_identity
        self._model = None
        self._signature = None
        self._lock = threading.Lock()

    def _runtime(self, cancelled):
        identity = (self._runtime_identity() if self._runtime_identity is not None
                    else transcription_runtime_identity(cancelled=cancelled))
        versions = self.manifest.settings.get('runtime_versions', {})
        if not isinstance(versions, dict) or set(versions) - set(RUNTIME_DISTRIBUTIONS):
            raise ModelManifestError('Invalid pinned ASR runtime versions.')
        for name, version in versions.items():
            if not isinstance(version, str) or identity['distributions'].get(name, {}).get('version') != version:
                raise EngineUnavailable('The installed ASR runtime differs from the selected pinned version.')
        return identity

    def _infer(self, source, settings, runtime, cancelled, on_status):
        signature = digest({'runtime': runtime, 'device': settings.device, 'compute_type': settings.compute_type})
        if self._model is None or self._signature != signature:
            on_status('Loading the verified local Whisper model.')
            self._model = None
            self._signature = None
            _cancel(cancelled)
            self._model = self._factory(self.model_directory, device=settings.device,
                                        compute_type=settings.compute_type)
            self._signature = signature
        if settings.language != 'auto':
            # faster-whisper otherwise silently uses English on an English-only
            # model after logging a warning. Capability comes from the loaded
            # model, never its directory name or an assumed model family.
            supported = getattr(self._model, 'supported_languages', None)
            if (not isinstance(supported, (list, tuple)) or not supported
                    or any(not isinstance(code, str) or not re.fullmatch('[a-z]{2,3}', code)
                           for code in supported)):
                raise EngineUnavailable('The selected Whisper runtime does not expose valid '
                                        'supported source languages; use a compatible reviewed runtime.')
            if settings.language not in supported:
                raise EngineUnavailable('The selected Whisper model does not support the '
                                        f'requested source language ({settings.language}). '
                                        'Choose a compatible local model or a supported source language.')
        on_status('Transcribing locally. Cancellation takes effect after the current inference segment.')
        _cancel(cancelled)
        segments, info = self._model.transcribe(str(source),
            language=None if settings.language == 'auto' else settings.language,
            task=settings.task, beam_size=settings.beam_size, vad_filter=settings.vad_filter,
            word_timestamps=settings.word_timestamps,
            condition_on_previous_text=settings.condition_on_previous_text)
        collected, text_bytes = [], 0
        iterator = iter(segments)
        try:
            while True:
                _cancel(cancelled)
                try:
                    segment = next(iterator)
                except StopIteration:
                    break
                _cancel(cancelled)
                text = _text(segment.text)
                words = []
                for word in getattr(segment, 'words', None) or ():
                    words.append({'start': _seconds(word.start), 'end': _seconds(word.end),
                                  'word': _text(word.word)})
                    if len(words) > 10000:
                        raise ValueError('The transcription exceeds its word timestamp budget.')
                collected.append({'start': _seconds(segment.start), 'end': _seconds(segment.end),
                                  'text': text, 'words': words})
                text_bytes += len(json.dumps(collected[-1], ensure_ascii=False).encode('utf-8'))
                if len(collected) > MAX_SEGMENTS or text_bytes > MAX_RESULT_BYTES:
                    raise ValueError('The transcription exceeds its result budget.')
                on_status(f'Transcribed through {_time(segment.end)}; {len(collected)} segments.')
        finally:
            close = getattr(iterator, 'close', None)
            if close is not None:
                close()
        return _validated_result({'language': info.language, 'duration': _seconds(info.duration),
            'segments': collected, 'text': '\n'.join(s['text'] for s in collected if s['text'])},
            requested_language=settings.language)

    def transcribe(self, source, output_directory, *, settings=None, formats=FORMATS,
                   cancelled=lambda: False, on_status=lambda value: None):
        settings = TranscriptionSettings() if settings is None else settings
        if not isinstance(settings, TranscriptionSettings):
            raise TypeError('Use validated TranscriptionSettings.')
        if (not isinstance(formats, (tuple, list)) or not formats
                or any(not isinstance(format, str) or format not in FORMATS for format in formats)
                or len(set(formats)) != len(formats)):
            raise ValueError('Choose one or more distinct TXT, SRT, VTT or JSON formats.')
        selected_formats = tuple(format for format in FORMATS if format in formats)
        # Waiting for another call does not ignore cancellation.
        while not self._lock.acquire(timeout=0.1):
            _cancel(cancelled)
        try:
            _cancel(cancelled)
            source = Path(source).absolute()
            source_info = regular_file(source)
            if source_info.st_size <= 0 or source_info.st_size > MAX_SOURCE_BYTES:
                raise ValueError('Choose a nonempty local audio file within the 32 GiB source budget.')
            proposed_root = Path(output_directory).absolute()
            if proposed_root.resolve(strict=False).is_relative_to(self.manifest.root):
                raise ValueError('Choose a transcript output folder outside the reviewed model installation.')
            root = checked_directory(proposed_root, create=True)
            with project_lock(root), _staging_directory(root) as (staging, completed):
                if shutil.disk_usage(root).free < source_info.st_size + MAX_RESULT_BYTES:
                    raise OSError('Not enough free space to snapshot the audio and save the transcript.')
                snapshot = staging / ('source' + source.suffix)
                source_hash = hashlib.sha256()
                copied = 0
                on_status('Verifying the selected audio and local Whisper model.')
                with source.open('rb') as incoming, snapshot.open('xb') as outgoing:
                    while block := incoming.read(1024 * 1024):
                        _cancel(cancelled)
                        copied += len(block)
                        if copied > MAX_SOURCE_BYTES:
                            raise ValueError('The audio source grew beyond its selected byte budget.')
                        source_hash.update(block)
                        outgoing.write(block)
                after = regular_file(source)
                if (source_info.st_dev, source_info.st_ino, source_info.st_size, source_info.st_mtime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or copied != source_info.st_size:
                    raise ValueError('The selected audio changed while its snapshot was captured.')
                self.manifest.verify(cancelled=cancelled, require_complete=True)
                runtime = self._runtime(cancelled)
                provenance = {'adapter': ADAPTER_VERSION, 'source_sha256': source_hash.hexdigest(),
                    'source_bytes': copied, 'model': self.manifest.cache_identity(),
                    'runtime': runtime, 'settings': asdict(settings),
                    'translation_target': 'en' if settings.task == 'translate' else None,
                    'diarization': False}
                key = digest(provenance)
                cache = checked_directory(root / '.transcription-cache', create=True) / (key + '.json')
                result = None
                if cache.exists():
                    saved = read_json(cache, max_bytes=MAX_RESULT_BYTES)
                    if (not isinstance(saved, dict) or set(saved) != {'schema_version', 'identity', 'provenance', 'result', 'result_sha256'}
                            or saved['schema_version'] != 1 or saved['identity'] != key or saved['provenance'] != provenance
                            or digest(saved['result']) != saved['result_sha256']):
                        raise ValueError('The saved transcript does not match the selected audio, model and settings.')
                    result = _validated_result(saved['result'], requested_language=settings.language)
                cached = result is not None
                if result is None:
                    result = self._infer(snapshot, settings, runtime, cancelled, on_status)
                    _cancel(cancelled)
                    checkpoint = {'schema_version': 1, 'identity': key, 'provenance': provenance,
                        'result': result, 'result_sha256': digest(result)}
                    if len(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True,
                            indent=2, allow_nan=False).encode('utf-8')) > MAX_RESULT_BYTES:
                        raise ValueError('The complete transcription checkpoint exceeds its byte budget.')
                    atomic_json(cache, checkpoint)
                _cancel(cancelled)
                on_status('Saving and verifying every requested transcript format.')
                destination = checked_directory(root / ('transcript-' + key), create=True)
                outputs = _encoded_outputs(result, provenance)
                paths, records = {}, {}
                for format in selected_formats:
                    _cancel(cancelled)
                    data = outputs[format]
                    target = destination / ('transcript.' + format)
                    expected = hashlib.sha256(data).hexdigest()
                    if not os.path.lexists(target):
                        staged = staging / ('transcript.' + format)
                        staged.write_bytes(data)
                        publish_new(staged, target)
                        staged.unlink()
                    if regular_file(target).st_size != len(data) or sha256_file(target, cancelled=cancelled) != expected:
                        raise ValueError('A transcript output already exists with different content; it was preserved.')
                    paths[format] = str(target)
                    records[target.name] = {'sha256': expected, 'bytes': len(data)}
                _cancel(cancelled)
                # The completion marker is last. A failed format never becomes
                # success, and the verified cache can retry missing publication.
                receipt = {'schema_version': 1, 'status': 'complete', 'identity': key, 'source_sha256': source_hash.hexdigest(),
                    'formats': list(selected_formats), 'outputs': records}
                atomic_json(destination / ('complete-' + digest(selected_formats)[:16] + '.json'), receipt)
                completed.update({**receipt, 'cached': cached, 'files': paths, 'language': result['language'],
                    'duration': result['duration'], 'segments': len(result['segments'])})
                return completed
        finally:
            self._lock.release()


def transcribe_file(source, output_directory, *, model_manifest, settings=None, formats=FORMATS,
                    cancelled=lambda: False, on_status=lambda value: None):
    """Convenience call; keep a TranscriptionSession for repeated file requests."""
    return TranscriptionSession(model_manifest).transcribe(source, output_directory,
        settings=settings, formats=formats, cancelled=cancelled, on_status=on_status)
