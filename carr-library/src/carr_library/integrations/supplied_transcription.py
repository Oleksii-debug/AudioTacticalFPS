"""Opt-in adapter for the supplied V4.3 ``worker.py --job-dir`` protocol.

The private worker, interpreter, SDK and model remain external installations.
This module performs no recognition and never acquires a model. One fresh job
contains one immutable input; the existing assistant checkpoint owns recovery.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from filelock import FileLock, Timeout

from ..audio.models import AudioBudget, AudioCancelled, AudioError
from ..audio.process import publish_file, run_process
from ..durable_state import (atomic_json, digest_file, encode, local_path,
                             read_json, regular)
from ..studio.render import _stamp


SOURCE_SHA256 = '1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9'
PREPARED_SHA256 = 'd718866a468f087d5714d7595f7c82921711164c0defebbcedf56afeafcc0992'
AUDIO_EXTENSIONS = frozenset({'.mp3', '.wav', '.m4a', '.mp4', '.ogg', '.opus',
    '.flac', '.aac', '.wma', '.webm', '.mkv', '.3gp', '.amr', '.m4b'})
MAX_INVENTORY = 100_000
MAX_TRANSCRIPT = 16 * 1024 * 1024


def prepare_worker(raw):
    """Reuse f20f9cb's exact, hash-gated ASR-03 correction on a private copy."""
    if sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError('The supplied transcription worker revision is unreviewed.')
    source = raw.decode('utf-8')
    tree = ast.parse(source)
    retry = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == '_v43_settings_attempts')
    worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Worker')
    transcribe = next(n for n in worker.body if isinstance(n, ast.FunctionDef)
                      and n.name == 'transcribe_one')
    fallback = next(n for n in transcribe.body if isinstance(n, ast.Try))
    if (len(fallback.body) != 1 or len(fallback.handlers) != 1
            or not isinstance(fallback.handlers[0].type, ast.Name)
            or fallback.handlers[0].type.id != 'TypeError'):
        raise ValueError('The reviewed worker correction does not match this source.')
    edits = [(retry.body[0].lineno, retry.end_lineno,
              '    return [("original", dict(base))]\n'),
             (fallback.lineno, fallback.end_lineno,
              '        ' + ast.unparse(fallback.body[0]) + '\n')]
    lines = source.splitlines(keepends=True)
    for first, last, replacement in sorted(edits, reverse=True):
        lines[first - 1:last] = [replacement]
    prepared = ''.join(lines)
    compile(prepared, '<prepared-supplied-worker>', 'exec')
    result = prepared.encode('utf-8')
    if sha256(result).hexdigest() != PREPARED_SHA256:
        raise ValueError('The prepared worker differs from the reviewed ASR-03 correction.')
    return result


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _inventory(root, expected, cancelled):
    root = local_path(root, exists=True)
    regular(root, directory=True)
    if not isinstance(expected, dict) or not 1 <= len(expected) <= MAX_INVENTORY:
        raise ValueError('A complete bounded runtime/model inventory is required.')
    actual, count = set(), 0
    for directory, directories, files in os.walk(root, followlinks=False):
        if cancelled():
            raise InterruptedError('Transcription installation verification stopped.')
        count += len(directories) + len(files)
        if count > MAX_INVENTORY:
            raise ValueError('The transcription installation exceeds its file allowance.')
        for name in directories:
            local_path(Path(directory) / name, root=root, exists=True)
        for name in files:
            path = local_path(Path(directory) / name, root=root, exists=True)
            actual.add(path.relative_to(root).as_posix())
            if len(actual) > MAX_INVENTORY:
                raise ValueError('The transcription installation exceeds its file allowance.')
    if actual != set(expected):
        raise ValueError('The transcription installation file inventory changed.')
    for name, item in expected.items():
        if not isinstance(name, str):
            raise ValueError('Invalid transcription installation inventory entry.')
        relative = PurePosixPath(name)
        if (relative.is_absolute() or str(relative) != name
                or '..' in relative.parts or ':' in name or '\\' in name
                or type(item) is not dict or set(item) != {'size', 'sha256'}
                or type(item['size']) is not int or item['size'] < 0 or not _digest(item['sha256'])):
            raise ValueError('Invalid transcription installation inventory entry.')
        path = local_path(root / name, root=root, exists=True)
        if (regular(path).st_size != item['size']
                or digest_file(path, cancelled=cancelled) != item['sha256']):
            raise ValueError('A transcription runtime/model file failed its checksum.')


@dataclass(frozen=True)
class SuppliedWorkerInstallation:
    python: Path
    worker: Path
    packages: Path
    model: Path
    manifest: Path
    manifest_sha256: str

    def verify(self, cancelled=lambda: False):
        if not _digest(self.manifest_sha256):
            raise ValueError('Retain the reviewed installation manifest SHA256 separately.')
        for name in ('python', 'worker', 'packages', 'model', 'manifest'):
            value = getattr(self, name)
            if not Path(value).is_absolute():
                raise ValueError('Transcription installations require explicit absolute local paths.')
            local_path(value, exists=True)
        if digest_file(self.manifest, cancelled=cancelled) != self.manifest_sha256:
            raise ValueError('The transcription installation manifest changed.')
        value = read_json(self.manifest)
        if (type(value) is not dict or set(value) != {'schema', 'engine', 'python_sha256',
                'python_version', 'runtime_files', 'model_files', 'runtime_notice', 'model_notice'}
                or value['schema'] != 1 or value['engine'] != 'supplied-whisper-v43'
                or not _digest(value['python_sha256'])
                or not isinstance(value['python_version'], str)
                or not re.fullmatch(r'\d+\.\d+\.\d+', value['python_version'])):
            raise ValueError('Unsupported supplied transcription installation manifest.')
        for key in ('runtime_notice', 'model_notice'):
            if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 8192:
                raise ValueError('Record the exact runtime/model provenance and rights notices.')
        if digest_file(self.python, cancelled=cancelled) != value['python_sha256']:
            raise ValueError('The selected transcription interpreter changed.')
        if regular(self.worker).st_size != 37974 or digest_file(self.worker, cancelled=cancelled) != SOURCE_SHA256:
            raise ValueError('The selected supplied worker is not the reviewed V4.3 revision.')
        _inventory(self.packages, value['runtime_files'], cancelled)
        _inventory(self.model, value['model_files'], cancelled)
        if not {'config.json', 'model.bin', 'tokenizer.json'} <= value['model_files'].keys():
            raise ValueError('The selected local Whisper model is incomplete.')
        # The original worker receives an absolute local model path, so the
        # name-based Hub branch cannot be selected when these files are present.
        if 'faster_whisper/__init__.py' not in value['runtime_files']:
            raise ValueError('The reviewed runtime must contain faster-whisper and its dependencies.')
        return value


def recognition_settings(options, model):
    if type(options) is not dict or set(options) - {
            'language', 'vad_filter', 'beam_size', 'condition_on_previous_text'}:
        raise ValueError('Unsupported transcription settings.')
    language = options.get('language', 'auto')
    beam = options.get('beam_size', 1)
    if not isinstance(language, str) or not re.fullmatch('auto|[a-z]{2,3}', language):
        raise ValueError('Choose auto detection or an explicit engine language code.')
    if type(beam) is not int or not 1 <= beam <= 10:
        raise ValueError('Transcription beam size must be an integer from 1 to 10.')
    for key in ('vad_filter', 'condition_on_previous_text'):
        if key in options and type(options[key]) is not bool:
            raise ValueError('Transcription switches must be true or false.')
    return {'output_mode': 'separate', 'task': 'transcribe', 'translation_target': 'en',
        'language': language, 'model': str(model), 'device': 'cpu', 'compute_type': 'int8',
        'beam_size': beam, 'parallel_files': 1, 'vad_filter': options.get('vad_filter', True),
        'include_headers': False, 'output_txt': True, 'output_srt': True,
        'output_vtt': True, 'output_json': True, 'multilingual': False,
        'condition_on_previous_text': options.get('condition_on_previous_text', True)}


# No site startup hooks or inherited PYTHONPATH. Only the explicitly inventoried
# package directory is added after the audit guard; no worker source is bundled.
# The guard is defense in depth, not an OS sandbox for native extensions.
_BOOTSTRAP = r'''
import json, os, pathlib, runpy, sys
stage = pathlib.Path(sys.argv[1]).resolve()
packages, version = sys.argv[2], sys.argv[3]
if '.'.join(map(str, sys.version_info[:3])) != version:
    raise RuntimeError('The selected transcription Python version changed.')
def audit(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise PermissionError('Transcription denies network and child commands.')
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
        if writing and not pathlib.Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
            raise PermissionError('Transcription writes only to its private staging directory.')
sys.addaudithook(audit)
sys.path.insert(0, packages)
sys.argv = [str(stage / 'worker.py'), '--job-dir', str(stage / 'job')]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


def _worker_results(stage, settings, input_file):
    job = stage / 'job'
    status = read_json(job / 'job_status.json')
    if (type(status) is not dict
            or any(type(status.get(key)) is not int for key in ('done', 'failed', 'total', 'active_count'))
            or status.get('state') != 'завершено' or status.get('done') != 1
            or status.get('failed') != 0 or status.get('total') != 1
            or status.get('active_count') != 0):
        raise AudioError('The supplied worker did not complete this one input successfully.')
    base = job / 'results' / 'inputs' / 'source'
    for suffix in ('.txt', '.srt', '.vtt', '.json'):
        if regular(base.with_suffix(suffix)).st_size > MAX_TRANSCRIPT:
            raise AudioError('A transcript exceeds the bounded export allowance.')
    meta = read_json(base.with_suffix('.json'))
    if (meta.get('source') != str(input_file) or meta.get('group') != 'inputs'
            or meta.get('relative') != input_file.name or meta.get('settings') != settings
            or meta.get('selected_language') != settings['language']
            or meta.get('v43_attempt') != 'original' or meta.get('v43_attempt_no') != 1):
        raise AudioError('Transcription output is not bound to the selected source and settings.')
    detected = meta.get('detected_language')
    if (not isinstance(detected, str) or not re.fullmatch('[a-z]{2,3}', detected)
            or settings['language'] != 'auto' and detected != settings['language']):
        raise AudioError('The worker did not preserve the selected recognition language.')
    duration = meta.get('duration')
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise AudioError('The worker returned invalid audio duration.')
    segments = meta.get('segments')
    if type(segments) is not list or len(segments) > 100000:
        raise AudioError('The worker returned invalid transcript segments.')
    texts, srt, vtt, last = [], [], [], 0
    for index, segment in enumerate(segments, 1):
        start, end, text = segment.get('start'), segment.get('end'), segment.get('text')
        if (any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end))
                or not last <= start <= end <= duration + 1 or not isinstance(text, str)):
            raise AudioError('The worker returned invalid or unordered timestamped segments.')
        last = start
        text = text.strip()
        if text:
            texts.append(text)
            srt.extend((str(index), f'{_stamp(start, comma=True)} --> {_stamp(end, comma=True)}', text, ''))
            vtt.extend((f'{_stamp(start)} --> {_stamp(end)}', text, ''))
    text = '\n'.join(line.strip() for line in '\n'.join(texts).splitlines() if line.strip())
    expected = {'.txt': text + ('\n' if text else ''),
        '.srt': '\n'.join(srt).strip() + ('\n' if srt else ''),
        '.vtt': 'WEBVTT\n\n' + '\n'.join(vtt).strip() + ('\n' if vtt else '')}
    if meta.get('text') != text:
        raise AudioError('The transcript text disagrees with its timestamped segments.')
    for suffix, payload in expected.items():
        if base.with_suffix(suffix).read_text(encoding='utf-8') != payload:
            raise AudioError('A requested transcript export is incomplete or inconsistent.')
    return base, meta


class SuppliedTranscriptionAdapter:
    """Use one existing assistant intent and persist before publishing artifacts."""
    def __init__(self, installation):
        if not isinstance(installation, SuppliedWorkerInstallation):
            raise TypeError('An explicitly reviewed local transcription installation is required.')
        self.installation = installation

    def transcribe(self, source, destination, *, options, intent, persist, state_dir,
                   max_bytes, max_seconds, cancelled=lambda: False, progress=None):
        source = local_path(source, exists=True)
        destination = local_path(destination)
        if source.suffix.lower() not in AUDIO_EXTENSIONS or regular(source).st_size > max_bytes:
            raise ValueError('Choose a supported local audio file within the approved byte budget.')
        installation = self.installation
        manifest = installation.verify(cancelled)
        settings = recognition_settings(options, installation.model)
        binding = {'adapter': 'supplied-v43-single-file/1', 'worker_sha256': SOURCE_SHA256,
            'prepared_worker_sha256': PREPARED_SHA256,
            'source': str(source), 'size': regular(source).st_size,
            'sha256': digest_file(source, cancelled=cancelled),
            'manifest_sha256': installation.manifest_sha256, 'settings': settings,
            'destination': str(destination)}
        if 'transcription' in intent and intent['transcription'] != binding:
            raise ValueError('The transcription input, installation or approved settings changed; review a new plan.')
        if 'transcription' not in intent:
            if os.path.lexists(destination):
                raise FileExistsError('The transcript destination already exists.')
            intent['transcription'] = binding
            persist()
        expected = intent.get('transcript_artifacts')
        if expected:
            self._verify_expected(expected, destination, max_bytes, missing=True)
            if all(Path(item['path']).exists() for item in expected):
                return self._result(expected, binding)
        lock = local_path(Path(state_dir) / ('asr-' + installation.manifest_sha256[:32] + '.lock'))
        if os.path.lexists(lock):
            regular(lock)
        try:
            with FileLock(lock, timeout=0, mode=0o600):
                return self._run(source, destination, binding, manifest, settings, intent,
                    persist, max_bytes, max_seconds, cancelled, progress)
        except Timeout:
            raise RuntimeError('This transcription runtime is busy; retry the saved job after it finishes.') from None

    def _run(self, source, destination, binding, manifest, settings, intent, persist,
             max_bytes, max_seconds, cancelled, progress):
        installation = self.installation
        with tempfile.TemporaryDirectory(prefix='.transcription-', dir=destination.parent) as temporary:
            stage = Path(temporary)
            if shutil.disk_usage(stage).free < binding['size'] + 32 * 1024 * 1024:
                raise AudioError('Insufficient free space for an immutable transcription input and disk reserve.')
            inputs = stage / 'inputs'
            inputs.mkdir()
            job = stage / 'job'
            job.mkdir()
            input_file = inputs / ('source' + source.suffix.lower())
            with source.open('rb') as read, input_file.open('xb') as write:
                total = 0
                while block := read.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('Transcription stopped before starting the worker.')
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError('The transcription input exceeded its approved byte budget.')
                    if shutil.disk_usage(stage).free < len(block) + 32 * 1024 * 1024:
                        raise AudioError('Transcription stopped to preserve its free disk reserve.')
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
            if total != binding['size'] or digest_file(input_file, cancelled=cancelled) != binding['sha256']:
                raise ValueError('The transcription input changed while copying its immutable snapshot.')
            (stage / 'worker.py').write_bytes(prepare_worker(Path(installation.worker).read_bytes()))
            atomic_json(job / 'job_config.json', {'job_name': 'Resonance transcription',
                'folders': [str(inputs)], 'results_root': str(job / 'results'),
                'models_dir': str(stage / 'models'), 'settings': settings})
            atomic_json(job / 'settings_live.json', settings)
            env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'LANG') if key in os.environ}
            env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                PYTHONDONTWRITEBYTECODE='1', DO_NOT_TRACK='1',
                TEMP=str(stage), TMP=str(stage), TMPDIR=str(stage),
                HF_HOME=str(stage / 'hf-cache'), XDG_CACHE_HOME=str(stage / 'cache'))
            if progress:
                progress('Transcribing one local file; cancellation stops the owned worker process.')
            try:
                run_process([str(installation.python), '-I', '-S', '-B', '-c', _BOOTSTRAP,
                    str(stage), str(installation.packages), manifest['python_version']],
                    budget=AudioBudget(timeout=max_seconds,
                        max_output_bytes=max_bytes + 8 * MAX_TRANSCRIPT,
                        max_log_bytes=2 * 1024 * 1024),
                    cancelled=cancelled, watch=(stage,), env=env, cwd=str(stage))
            except AudioCancelled:
                raise InterruptedError('Transcription stopped; the incomplete file will restart on resume.') from None
            base, meta = _worker_results(stage, settings, input_file)
            installation.verify(cancelled)
            if digest_file(source, cancelled=cancelled) != binding['sha256']:
                raise ValueError('The selected source changed during transcription; no output was published.')
            # Published JSON records original-source provenance, not disposable
            # staging paths. No word-alignment or diarization claim is inferred.
            meta = {key: meta[key] for key in ('text', 'segments', 'detected_language', 'duration')}
            meta.update(schema='resonance-transcript/1', source=binding,
                        word_timestamps=False, diarization=False)
            base.with_suffix('.json').write_bytes(encode(meta))
            artifacts = [{'path': str(destination / ('transcript' + suffix)),
                'size': regular(base.with_suffix(suffix)).st_size,
                'sha256': digest_file(base.with_suffix(suffix), cancelled=cancelled)}
                for suffix in ('.txt', '.srt', '.vtt', '.json')]
            if sum(item['size'] for item in artifacts) > max_bytes:
                raise ValueError('Transcript exports exceed the approved output byte budget.')
            previous = intent.get('transcript_artifacts')
            if previous is not None and previous != artifacts:
                raise ValueError('Retried transcription differs from an interrupted publication; existing outputs were kept.')
            if cancelled():
                raise InterruptedError('Transcription stopped before artifact publication.')
            intent['transcript_artifacts'] = artifacts
            persist()
            destination.mkdir(mode=0o700, exist_ok=True)
            local_path(destination, exists=True)
            self._verify_expected(artifacts, destination, max_bytes, missing=True)
            # All four bounded payloads were verified before this commit phase.
            # A later cancellation does not discard already published exports.
            for item in artifacts:
                target = Path(item['path'])
                if not target.exists():
                    staged = base.with_suffix(target.suffix)
                    publish_file(staged, target)
                    staged.unlink()
            self._verify_expected(artifacts, destination, max_bytes)
            return self._result(artifacts, binding)

    @staticmethod
    def _verify_expected(artifacts, destination, max_bytes, *, missing=False):
        names = {str(destination / ('transcript' + suffix)) for suffix in ('.txt', '.srt', '.vtt', '.json')}
        if (type(artifacts) is not list or len(artifacts) != 4
                or any(type(item) is not dict or set(item) != {'path', 'size', 'sha256'} for item in artifacts)
                or {item['path'] for item in artifacts} != names
                or any(type(item['size']) is not int or item['size'] < 0 or not _digest(item['sha256']) for item in artifacts)
                or sum(item['size'] for item in artifacts) > max_bytes):
            raise ValueError('The saved transcript artifact inventory is invalid.')
        for item in artifacts:
            path = local_path(item['path'])
            if missing and not os.path.lexists(path):
                continue
            if regular(path).st_size != item['size'] or digest_file(path) != item['sha256']:
                raise ValueError('A completed transcript was changed; it will not be overwritten.')

    @staticmethod
    def _result(artifacts, binding):
        return {'artifacts': [dict(item) for item in artifacts], 'source_sha256': binding['sha256'],
            'runtime_manifest_sha256': binding['manifest_sha256'], 'status': 'complete',
            'backup': {'included': 'Assistant plan, source/output identities and checkpoints',
                'external': ['source audio', 'transcript exports', 'private worker', 'runtime', 'model'],
                'restore': 'Review and reselect external inputs and installation before resuming'},
            'notice': 'Local supplied-worker transcript; review recognition accuracy. Segment timing only.'}
