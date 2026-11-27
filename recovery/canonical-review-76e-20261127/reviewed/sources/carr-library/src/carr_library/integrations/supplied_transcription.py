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
import stat
import tempfile

from ..audio import transcription as core
from ..audio import supplied_whisper as preparation
from ..audio.models import AudioBudget, AudioCancelled
from ..audio.process import publish_file
from ..durable_state import (MAX_JSON_BYTES, _open_json_file, _unique,
                             digest_file, encode, local_path, regular)

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



def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _read_bound_bytes(path, maximum, cancelled=lambda: False):
    """Read one bounded, ordinary local file; reject replacement before I/O."""
    core._cancel(cancelled)
    path = local_path(path, exists=True)
    before = regular(path)
    parent = _identity(regular(path.parent, directory=True))
    if before.st_size > maximum:
        raise ValueError('The selected file exceeds its bounded byte allowance.')
    if os.name == 'nt':
        stream = _open_json_file(path)
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK
                             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
        try:
            stream = os.fdopen(descriptor, 'rb')
        except BaseException:
            os.close(descriptor)
            raise
    with stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or _stamp(opened) != _stamp(before)):
            raise ValueError('The selected file changed before its bytes were read.')
        local_path(path, exists=True)
        chunks, total = [], 0
        while True:
            core._cancel(cancelled)
            block = stream.read(min(1024 * 1024, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise ValueError('The selected file grew beyond its byte allowance.')
        if _stamp(os.fstat(stream.fileno())) != _stamp(opened):
            raise ValueError('The selected file changed while its bytes were read.')
    if (total != before.st_size or _stamp(regular(path)) != _stamp(before)
            or _identity(regular(path.parent, directory=True)) != parent):
        raise ValueError('The selected file or its parent changed while being read.')
    return b''.join(chunks)


def _manifest_value(path, expected, cancelled=lambda: False):
    raw = _read_bound_bytes(path, MAX_JSON_BYTES, cancelled)
    if sha256(raw).hexdigest() != expected:
        raise ValueError('The selected transcription runtime manifest changed.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          ValueError('Non-finite transcription installation value.')))


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
        value = _manifest_value(self.manifest, self.manifest_sha256, cancelled)
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
            raw = _read_bound_bytes(installation.manifest_path, MAX_JSON_BYTES)
            self._manifest_sha256 = sha256(raw).hexdigest()
            selected = _manifest_value(installation.manifest_path, self._manifest_sha256)
            if selected != installation.data:
                raise ValueError('The selected runtime manifest changed during enrollment.')
        else:
            self._manifest_sha256 = installation.manifest_sha256

    def _runtime(self, cancelled):
        if isinstance(self.installation, SuppliedWorkerInstallation):
            return self.installation.runtime(cancelled)
        path = self.installation.manifest_path
        data = _manifest_value(path, self._manifest_sha256, cancelled)
        # Use the current validator on the exact hash-checked snapshot. A
        # second pathname parse must not select different runtime metadata.
        runtime = core.SuppliedWhisperRuntime.__new__(core.SuppliedWhisperRuntime)
        runtime.manifest_path = path
        runtime._configure(data)
        return runtime

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
            core._cancel(stopped)
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
        parent = _identity(regular(local_path(destination.parent, exists=True), directory=True))
        stage = Path(tempfile.mkdtemp(prefix='.transcription-', dir=destination.parent))
        stage_identity = _identity(regular(stage, directory=True))
        staged_nodes, targets, artifacts = {}, {}, []
        destination_identity = None
        owned_publication = False
        failure = cleanup_error = None
        try:
            for suffix in ('.txt', '.srt', '.vtt', '.json'):
                core._cancel(cancelled)
                source = generated / ('transcript' + suffix)
                payload = _read_bound_bytes(source, MAX_TRANSCRIPT, cancelled)
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
                if (_identity(regular(stage, directory=True)) != stage_identity
                        or _identity(regular(destination.parent, directory=True)) != parent):
                    raise ValueError('The selected transcript staging or output parent changed.')
                target = stage / ('transcript' + suffix)
                with target.open('xb') as output:
                    staged_nodes[target.name] = _identity(os.fstat(output.fileno()))
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
            if _identity(regular(destination.parent, directory=True)) != parent:
                raise ValueError('The selected transcript output parent changed.')
            if previous is None and os.path.lexists(destination):
                raise FileExistsError('The transcript destination appeared before this publication.')
            intent['transcript_artifacts'] = artifacts
            persist()
            destination.mkdir(mode=0o700, exist_ok=previous is not None)
            local_path(destination, exists=True)
            destination_identity = _identity(regular(destination, directory=True))
            self._verify_expected(artifacts, destination, max_bytes, missing=True, cancelled=cancelled)
            for item in artifacts:
                target = Path(item['path'])
                if os.path.lexists(target):
                    # Only a previous prepared receipt can authorize reopening
                    # an existing export. Fresh competing files are not ours.
                    if previous is None:
                        raise FileExistsError('A competing transcript appeared before publication.')
                    targets[target.name] = _identity(regular(target))
            for item in artifacts:
                core._cancel(cancelled)
                target = Path(item['path'])
                if (_identity(regular(destination.parent, directory=True)) != parent
                        or _identity(regular(destination, directory=True)) != destination_identity):
                    raise ValueError('The selected transcript destination changed.')
                if target.name in targets:
                    if _identity(regular(target)) != targets[target.name]:
                        raise ValueError('An existing transcript changed during publication.')
                    continue
                staged = stage / target.name
                targets[target.name] = staged_nodes[target.name]
                publish_file(staged, target, cancelled=cancelled)
                staged.unlink()
        except (Exception, KeyboardInterrupt) as exc:
            failure = exc
        finally:
            # Establish the exact names while any unfinished staged aliases
            # still keep their original inodes alive. Cleanup is not evidence
            # that a competing output belongs to this attempt.
            if len(artifacts) == 4 and len(targets) == 4 and destination_identity is not None:
                try:
                    nodes = [Path(item['path']).lstat() for item in artifacts]
                    owned_publication = (
                        _identity(regular(destination.parent, directory=True)) == parent
                        and _identity(regular(destination, directory=True)) == destination_identity
                        and all(stat.S_ISREG(node.st_mode) and node.st_nlink in (1, 2)
                            and not getattr(node, 'st_file_attributes', 0) & 0x400
                            and _identity(node) == targets[Path(item['path']).name]
                            for item, node in zip(artifacts, nodes)))
                except (Exception, KeyboardInterrupt):
                    owned_publication = False
            try:
                self._cleanup_stage(stage, stage_identity, staged_nodes, parent)
            except (Exception, KeyboardInterrupt) as exc:
                cleanup_error = exc

        # This is deliberately outside both publication and cleanup. A late
        # cancellation cannot revoke verified, committed bytes; a foreign inode,
        # incomplete set or changed payload can never become a completed result.
        verified = False
        if owned_publication:
            try:
                if (_identity(regular(destination.parent, directory=True)) != parent
                        or _identity(regular(destination, directory=True)) != destination_identity
                        or any(_identity(regular(Path(item['path']))) != targets[Path(item['path']).name]
                               for item in artifacts)):
                    raise ValueError('The committed transcript identities changed.')
                self._verify_expected(artifacts, destination, max_bytes, cancelled=lambda: False)
                if (_identity(regular(destination.parent, directory=True)) != parent
                        or _identity(regular(destination, directory=True)) != destination_identity
                        or any(_identity(regular(Path(item['path']))) != targets[Path(item['path']).name]
                               for item in artifacts)):
                    raise ValueError('The committed transcript identities changed during verification.')
                verified = True
            except (Exception, KeyboardInterrupt) as exc:
                if failure is None:
                    failure = exc
        if verified:
            result = self._result(artifacts, binding, job_id)
            if failure is not None:
                result.setdefault('notices', []).append(
                    'All transcript exports were committed and independently verified after a late publication interruption.')
            if cleanup_error is not None:
                result.setdefault('notices', []).append(
                    'Verified transcript export is complete; private staging cleanup could not be confirmed: ' + str(stage))
            return result
        failure = failure or cleanup_error or ValueError('The transcript publication is incomplete.')
        if cleanup_error is not None and failure is not cleanup_error:
            failure.add_note('Private transcript staging cleanup remains necessary: ' + str(stage))
        if isinstance(failure, AudioCancelled):
            raise InterruptedError('Transcription export stopped; verified completed files remain available for resume.') from failure
        raise failure

    @staticmethod
    def _cleanup_stage(stage, identity, owned, parent):
        """Remove only this call's exact temporary files; never receipt paths."""
        if (_identity(regular(stage.parent, directory=True)) != parent
                or _identity(regular(stage, directory=True)) != identity):
            raise ValueError('The private transcription staging directory changed; it was preserved.')
        entries = []
        with os.scandir(stage) as children:
            for child in children:
                if len(entries) >= 4 or child.name not in owned:
                    raise ValueError('Unexpected private transcription staging contents were preserved.')
                path = stage / child.name
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in (1, 2)
                        or getattr(info, 'st_file_attributes', 0) & 0x400
                        or _identity(info) != owned[child.name]):
                    raise ValueError('A private transcription staging file changed; it was preserved.')
                entries.append(path)
        for path in entries:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in (1, 2)
                    or getattr(info, 'st_file_attributes', 0) & 0x400
                    or _identity(info) != owned[path.name]
                    or _identity(regular(stage, directory=True)) != identity):
                raise ValueError('Private transcription staging changed before cleanup; it was preserved.')
            path.unlink()
        if (_identity(regular(stage, directory=True)) != identity
                or _identity(regular(stage.parent, directory=True)) != parent):
            raise ValueError('The private transcription staging directory changed before cleanup.')
        stage.rmdir()

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
            raw = _read_bound_bytes(path, MAX_TRANSCRIPT, cancelled)
            if len(raw) != item['size'] or sha256(raw).hexdigest() != item['sha256']:
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
