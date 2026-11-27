"""Compatibility tool for the existing supplied-worker transcription service.

The Assistant keeps its scoped plan and external export receipts. Recognition,
retained inputs, cancellation, resources and native cleanup belong exclusively
to audio.transcription.TranscriptionService and its existing typed job store.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from ..audio import transcription as core
from ..audio import supplied_whisper as preparation
from ..audio.models import AudioBudget, AudioCancelled
from ..audio.process import publish_file
from ..durable_state import digest_file, encode, local_path, read_json, regular

SOURCE_SHA256 = preparation.SOURCE_SHA256
AUDIO_EXTENSIONS = frozenset(core.AUDIO_EXTENSIONS)
MAX_INVENTORY = 100_000
MAX_TRANSCRIPT = 16 * 1024 * 1024


def prepare_worker(raw):
    """Use the shared strict settings/timestamp/newline preparation unchanged."""
    try:
        return preparation.prepare(raw)
    except ValueError as exc:
        raise ValueError('The supplied transcription worker revision is unreviewed.') from exc


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _inventory(root, expected, cancelled, *, runtime=False):
    if type(expected) is not dict or not 1 <= len(expected) <= MAX_INVENTORY:
        raise ValueError('A complete bounded runtime/model inventory is required.')
    files = {}
    for name, entry in expected.items():
        relative = PurePosixPath(name) if isinstance(name, str) else None
        if (relative is None or relative.is_absolute() or str(relative) != name
                or '..' in relative.parts or ':' in name or '\\' in name
                or type(entry) is not dict or set(entry) != {'size', 'sha256'}
                or type(entry['size']) is not int or entry['size'] < 0
                or not _digest(entry['sha256'])):
            raise ValueError('Invalid transcription installation inventory entry.')
        files[name] = entry['sha256']
    # Reuse the same cancellation, link rejection and complete bounded scan.
    # Executable Python caches never become an alternative unverified source.
    root = core._inventory(root, files, cancelled, runtime=runtime)
    for name, entry in expected.items():
        core._cancel(cancelled)
        if regular(local_path(root / name, root=root, exists=True)).st_size != entry['size']:
            raise ValueError('A transcription runtime/model file changed size.')
    return files


@dataclass(frozen=True)
class SuppliedWorkerInstallation:
    python: Path
    worker: Path
    packages: Path
    model: Path
    manifest: Path
    manifest_sha256: str
    runtime_reviewed: bool = False
    rights_reviewed: bool = False

    def verify(self, cancelled=lambda: False):
        if self.runtime_reviewed is not True or self.rights_reviewed is not True:
            raise ValueError('Explicitly review this legacy runtime and its rights before enrollment; notices and hashes are not approval.')
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
                or type(value['schema']) is not int or value['schema'] != 1
                or value['engine'] != 'supplied-whisper-v43'
                or not _digest(value['python_sha256'])
                or not isinstance(value['python_version'], str)
                or re.fullmatch(r'\d+\.\d+\.\d+', value['python_version']) is None):
            raise ValueError('Unsupported supplied transcription installation manifest.')
        for key in ('runtime_notice', 'model_notice'):
            if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 8192:
                raise ValueError('Record the exact runtime/model provenance and rights notices.')
        if digest_file(self.python, cancelled=cancelled) != value['python_sha256']:
            raise ValueError('The selected transcription interpreter changed.')
        raw = core._bounded_bytes(self.worker, maximum=128 * 1024)
        if sha256(raw).hexdigest() not in (SOURCE_SHA256, preparation.HARDENED_SHA256):
            raise ValueError('The selected supplied worker is not a reviewed revision.')
        prepare_worker(raw)
        _inventory(self.packages, value['runtime_files'], cancelled, runtime=True)
        _inventory(self.model, value['model_files'], cancelled)
        if not {'config.json', 'model.bin', 'tokenizer.json'} <= value['model_files'].keys():
            raise ValueError('The selected local Whisper model is incomplete.')
        return value

    def runtime(self, cancelled=lambda: False):
        value = self.verify(cancelled)
        model = local_path(self.model, exists=True)
        data = {'schema': 1, 'engine': value['engine'],
            'python': str(self.python), 'python_sha256': value['python_sha256'],
            'worker': str(self.worker), 'runtime_directory': str(self.packages),
            'runtime_files': {name: item['sha256'] for name, item in value['runtime_files'].items()},
            'models_directory': str(model.parent), 'model': model.name,
            'model_files': {name: item['sha256'] for name, item in value['model_files'].items()},
            'runtime_reviewed': self.runtime_reviewed, 'rights_reviewed': self.rights_reviewed,
            'rights_notice': value['runtime_notice'] + '\n' + value['model_notice']}
        return core.SuppliedWhisperRuntime.from_reviewed_data(data,
            source_manifest_sha256=self.manifest_sha256, python_version=value['python_version'])


def recognition_settings(options, model):
    if type(options) is not dict or set(options) - {
            'language', 'vad_filter', 'beam_size', 'condition_on_previous_text'}:
        raise ValueError('Unsupported transcription settings.')
    settings = core.TranscriptionSettings(language=options.get('language', 'auto'),
        beam_size=options.get('beam_size', 1), vad_filter=options.get('vad_filter', True),
        condition_on_previous_text=options.get('condition_on_previous_text', True))
    return settings.worker(str(model), core.FORMATS)


class SuppliedTranscriptionAdapter:
    """Keep the scoped Assistant API; delegate all recognition to typed ASR jobs."""
    def __init__(self, installation):
        if not isinstance(installation, (SuppliedWorkerInstallation, core.SuppliedWhisperRuntime)):
            raise TypeError('An explicitly reviewed local transcription installation is required.')
        self.installation = installation
        if isinstance(installation, core.SuppliedWhisperRuntime):
            if installation.manifest_path is None:
                raise ValueError('Select a retained reviewed runtime manifest.')
            self._manifest_sha256 = digest_file(installation.manifest_path)
        else:
            self._manifest_sha256 = installation.manifest_sha256

    def _runtime(self, cancelled):
        if isinstance(self.installation, SuppliedWorkerInstallation):
            return self.installation.runtime(cancelled)
        path = self.installation.manifest_path
        if digest_file(path, cancelled=cancelled) != self._manifest_sha256:
            raise ValueError('The selected transcription runtime manifest changed.')
        return core.SuppliedWhisperRuntime(path)

    def transcribe(self, source, destination, *, options, intent, persist, state_dir,
                   max_bytes, max_seconds, cancelled=lambda: False, progress=None,
                   application_root=None, control=None):
        try:
            return self._transcribe(source, destination, options=options, intent=intent,
                persist=persist, state_dir=state_dir, max_bytes=max_bytes, max_seconds=max_seconds,
                cancelled=cancelled, progress=progress, application_root=application_root, control=control)
        except AudioCancelled as exc:
            raise InterruptedError('Transcription stopped before completion; verified checkpoints remain available.') from exc

    def _transcribe(self, source, destination, *, options, intent, persist, state_dir,
                    max_bytes, max_seconds, cancelled, progress, application_root, control):
        source = local_path(source, exists=True)
        destination = local_path(destination)
        if (type(max_bytes) is not int or not 1 <= max_bytes <= core.MAX_SOURCE_BYTES
                or type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
                or not 0 < max_seconds <= 86400):
            raise ValueError('Use a bounded approved transcription budget.')
        if source.suffix.lower() not in AUDIO_EXTENSIONS or not 0 < regular(source).st_size <= max_bytes:
            raise ValueError('Choose a supported local audio file within the approved byte budget.')
        state_dir = local_path(state_dir, exists=True)
        if application_root is None:
            if state_dir.name != 'assistant' or state_dir.parent.name != 'state':
                raise ValueError('Bind transcription to this application data directory explicitly.')
            application_root = state_dir.parent.parent
        application_root = local_path(application_root, exists=True)
        local_path(state_dir, root=application_root, exists=True)
        regular(application_root, directory=True)
        runtime = self._runtime(cancelled)
        runtime_id, prepared, _, _, _ = runtime.verify(cancelled)
        worker_settings = recognition_settings(options, runtime.data['model'])
        settings = core.TranscriptionSettings(**{key: worker_settings[key]
            for key in core.TranscriptionSettings.__dataclass_fields__})
        binding = {'adapter': 'shared-supplied-whisper/2',
            'worker_sha256': digest_file(runtime.data['worker'], cancelled=cancelled),
            'prepared_worker_sha256': sha256(prepared).hexdigest(), 'runtime': runtime_id,
            'source': str(source), 'size': regular(source).st_size,
            'sha256': digest_file(source, cancelled=cancelled),
            'manifest_sha256': self._manifest_sha256, 'settings': worker_settings,
            'destination': str(destination)}
        if 'transcription' in intent and intent['transcription'] != binding:
            raise ValueError('The transcription input, installation or approved settings changed; an older adapter intent requires a new reviewed plan.')
        if 'transcription' not in intent:
            if os.path.lexists(destination):
                raise FileExistsError('The transcript destination already exists.')
            intent['transcription'] = binding
            persist()
        expected = intent.get('transcript_artifacts')
        if expected:
            if not _digest(intent.get('transcription_job')):
                raise ValueError('This saved export has no shared transcription job; review a new plan.')
            self._verify_expected(expected, destination, max_bytes, missing=True, cancelled=cancelled)
        service = core.TranscriptionService(application_root / 'state', runtime,
            budget=AudioBudget(timeout=max_seconds, max_output_bytes=max_bytes + 8 * MAX_TRANSCRIPT,
                               max_log_bytes=2 * 1024 * 1024))
        job_id = intent.get('transcription_job')
        if job_id is None:
            job_id = service.create([source], settings=settings, formats=core.FORMATS, cancelled=cancelled)
            intent['transcription_job'] = job_id
            persist()
        record = service.load(job_id, cancelled=cancelled)
        request = record['request']
        if (request['runtime'] != runtime_id or request['settings'] != asdict(settings)
                or request['formats'] != list(core.FORMATS) or len(request['inputs']) != 1
                or request['inputs'][0]['sha256'] != binding['sha256']
                or request['inputs'][0]['bytes'] != binding['size']):
            raise ValueError('The saved Assistant step does not match its retained transcription job.')
        if expected and record['status'] == 'complete' and all(Path(item['path']).exists() for item in expected):
            return self._result(expected, binding, job_id)
        forwarded = 'run'
        def stopped():
            nonlocal forwarded
            command = control() if control is not None else 'run'
            if command not in ('run', 'pause', 'cancel'):
                raise ValueError('The Assistant transcription control is invalid.')
            if command != 'run' and command != forwarded:
                # This callback runs on the existing operation worker. The same
                # typed ASR control file serves its GUI, CLI and Assistant view.
                service.control(job_id, command)
                forwarded = command
            return bool(cancelled()) or command != 'run'
        try:
            core._cancel(cancelled)
            service.control(job_id, 'run')
            service.run(job_id, cancelled=stopped, on_status=progress or (lambda _message: None))
        except AudioCancelled:
            raise InterruptedError('Transcription stopped; the retained ASR job will resume through its existing checkpoint.') from None
        detail = service.details(job_id, cancelled=stopped)
        if (detail['record']['status'] != 'complete' or len(detail['outputs']) != 1
                or not detail['outputs'][0]['verified']):
            raise ValueError('The shared transcription job has no complete verified output.')
        if digest_file(source, cancelled=stopped) != binding['sha256']:
            raise ValueError('The selected source changed during transcription; no export was published.')
        return self._export(Path(detail['outputs'][0]['directory']), detail['outputs'][0]['checksums'],
                            destination, binding, job_id, intent, persist, max_bytes, stopped)

    def _export(self, generated, checksums, destination, binding, job_id, intent, persist, max_bytes, cancelled):
        stage = Path(tempfile.mkdtemp(prefix='.transcription-', dir=destination.parent))
        result = active_error = None
        try:
            artifacts = []
            for suffix in ('.txt', '.srt', '.vtt', '.json'):
                core._cancel(cancelled)
                source = generated / ('transcript' + suffix)
                payload = core._bounded_bytes(source, maximum=MAX_TRANSCRIPT)
                expected = checksums[suffix[1:]]
                if len(payload) != expected['bytes'] or sha256(payload).hexdigest() != expected['sha256']:
                    raise ValueError('A generated transcript changed after receipt verification; it was preserved.')
                if suffix == '.json':
                    saved = json.loads(payload)
                    meta = {key: saved[key] for key in ('text', 'segments', 'duration')}
                    meta['detected_language'] = saved['language']
                    if 'warnings' in saved:
                        meta['warnings'] = saved['warnings']
                    meta.update(schema='resonance-transcript/1', source=binding,
                                word_timestamps=False, diarization=False, transcription_job=job_id)
                    payload = encode(meta)
                if len(payload) > MAX_TRANSCRIPT:
                    raise ValueError('A transcript exceeds the bounded export allowance.')
                target = stage / ('transcript' + suffix)
                with target.open('xb') as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                artifacts.append({'path': str(destination / target.name), 'size': len(payload),
                                  'sha256': sha256(payload).hexdigest()})
            if sum(item['size'] for item in artifacts) > max_bytes:
                raise ValueError('Transcript exports exceed the approved output byte budget.')
            previous = intent.get('transcript_artifacts')
            if previous is not None and previous != artifacts:
                raise ValueError('Retried transcription differs from an interrupted publication; existing outputs were kept.')
            core._cancel(cancelled)
            intent['transcript_artifacts'] = artifacts
            persist()
            destination.mkdir(mode=0o700, exist_ok=True)
            local_path(destination, exists=True)
            self._verify_expected(artifacts, destination, max_bytes, missing=True, cancelled=cancelled)
            for item in artifacts:
                target = Path(item['path'])
                if not os.path.lexists(target):
                    staged = stage / target.name
                    publish_file(staged, target, cancelled=cancelled)
                    staged.unlink()
            self._verify_expected(artifacts, destination, max_bytes, cancelled=cancelled)
            result = self._result(artifacts, binding, job_id)
            return result
        except AudioCancelled as exc:
            active_error = exc
            raise InterruptedError('Transcription export stopped; verified completed files remain available for resume.') from exc
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            try:
                shutil.rmtree(stage)
            except OSError as cleanup_error:
                if result is not None:
                    result.setdefault('notices', []).append('Verified transcript export is complete; private staging cleanup remains necessary: ' + str(stage))
                elif active_error is not None:
                    active_error.add_note('Private transcript staging cleanup remains necessary: ' + str(stage))
                else:
                    raise cleanup_error

    @staticmethod
    def _verify_expected(artifacts, destination, max_bytes, *, missing=False, cancelled=lambda: False):
        names = {str(destination / ('transcript' + suffix)) for suffix in ('.txt', '.srt', '.vtt', '.json')}
        if (type(artifacts) is not list or len(artifacts) != 4
                or any(type(item) is not dict or set(item) != {'path', 'size', 'sha256'} for item in artifacts)
                or {item['path'] for item in artifacts} != names
                or any(type(item['size']) is not int or item['size'] < 0 or not _digest(item['sha256']) for item in artifacts)
                or sum(item['size'] for item in artifacts) > max_bytes):
            raise ValueError('The saved transcript artifact inventory is invalid.')
        for item in artifacts:
            core._cancel(cancelled)
            path = local_path(item['path'])
            if missing and not os.path.lexists(path):
                continue
            if (regular(path).st_size != item['size']
                    or digest_file(path, cancelled=cancelled) != item['sha256']):
                raise ValueError('A completed transcript was changed; it will not be overwritten.')

    @staticmethod
    def _result(artifacts, binding, job_id):
        return {'artifacts': [item['path'] for item in artifacts], 'source_sha256': binding['sha256'],
            'runtime_manifest_sha256': binding['manifest_sha256'], 'transcription_job': job_id,
            'status': 'complete',
            'backup': {'included': 'Assistant plan, retained input, typed ASR checkpoint and verified generated transcripts',
                'external': ['published export copies', 'private worker', 'runtime', 'model'],
                'restore': 'Review the restored ASR job and reselect its external installation before resuming'},
            'notice': 'Local supplied-worker transcript; review recognition accuracy. Segment timing only.'}
