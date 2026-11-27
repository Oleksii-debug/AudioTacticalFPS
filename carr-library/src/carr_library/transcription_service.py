"""Supervise the supplied local Whisper worker through its reviewed protocol.

There is no ASR implementation, model downloader, or remote fallback here.
Portable definitions retain source copies. Each execution binds a fresh run ID,
exact runtime, input hashes and completion receipt before accepting success.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unicodedata
from uuid import uuid4

from .audio.jobs import (atomic_json, checked_directory, file_identity, project_lock,
                         publish_new, publish_noreplace, verified_reader)
from .audio.model_manifest import (LocalModelManifest, _read_json_snapshot, digest_file,
                                   local_path, offline_environment)
from .audio.models import canonical
from .ingest.writer_guard import exclusive_writer
from .transcription_receipt import PROTOCOL, validate_settings, verify_completion
from .transcription_state import (
    DIRECTORY, SCHEMA, MAX_INPUTS, MAX_ENTRIES, MAX_JOB_BYTES, MAX_JSON_BYTES,
    MAX_LOG_BYTES, EXTENSIONS, JOB_ID, check_cancelled, regular, relative_path,
    read_definition, validate_definition,
)

_STATES = frozenset(('queued', 'verifying', 'running', 'paused', 'cancelled', 'failed', 'complete'))


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ReviewedRuntime:
    manifest: LocalModelManifest
    interpreter: Path
    worker: Path
    models: Path
    model_sha256: str
    settings: dict


def review_runtime(path, settings, *, expected_sha256=None, cancelled=lambda: False):
    """Reuse the existing complete local inventory and explicit acceptance flags."""
    settings = validate_settings(settings)
    manifest = LocalModelManifest.load(path, engine='supplied-whisper', expected_sha256=expected_sha256)
    declared = manifest.settings
    required = {'protocol', 'interpreter', 'worker', 'models_dir', 'model', 'languages'}
    optional = {'devices', 'compute_types', 'tasks'}
    if (set(declared) - required - optional or not required <= set(declared)
            or declared['protocol'] != PROTOCOL or declared['model'] != settings['model']):
        raise ValueError('The reviewed runtime does not declare the selected worker protocol and model.')
    for field, chosen, defaults in (
        ('languages', settings['language'], None), ('devices', settings['device'], ['cpu']),
        ('compute_types', settings['compute_type'], ['int8']), ('tasks', settings['task'], ['transcribe']),
    ):
        choices = declared.get(field, defaults)
        if (type(choices) is not list or not 1 <= len(choices) <= 200
                or any(type(value) is not str or not value or len(value) > 64 for value in choices)
                or len(set(choices)) != len(choices)
                or ((chosen != 'auto' or field != 'languages') and chosen not in choices)):
            raise ValueError('The reviewed transcription runtime does not support the selected ' + field + '.')
    manifest.verify(cancelled=cancelled, require_complete=True)
    interpreter, worker = manifest.asset(declared['interpreter']), manifest.asset(declared['worker'])
    models = manifest.directory(declared['models_dir'])
    prefix = declared['models_dir'] + '/' + settings['model'] + '/'
    inventory = {name[len(prefix):]: checksum for name, checksum in manifest.files if name.startswith(prefix)}
    if not {'model.bin', 'config.json', 'tokenizer.json'} <= set(inventory):
        raise ValueError('The selected local Whisper model is incomplete.')
    for name, _ in manifest.files:
        regular(manifest.asset(name))
    model_sha256 = hashlib.sha256(canonical(inventory).encode('utf-8')).hexdigest()
    return ReviewedRuntime(manifest, interpreter, worker, models, model_sha256, settings)


def selected_inputs(sources, *, cancelled=lambda: False):
    """Capture explicit local selections without links, aliases or unbounded scans."""
    sources = tuple(sources)
    if not sources or len(sources) > MAX_INPUTS:
        raise ValueError('Choose one to 1,000 local files or folders.')
    selected, aliases, total, entries = [], set(), 0, 0
    for source in sources:
        check_cancelled(cancelled)
        source = local_path(source)
        root = source if source.is_dir() else source.parent
        candidates = [source]
        while candidates:
            check_cancelled(cancelled)
            path = local_path(candidates.pop())
            entries += 1
            if entries > MAX_ENTRIES:
                raise ValueError('The transcription selection exceeds its directory-entry budget.')
            if path.is_dir():
                # Count while enumerating rather than allocating an unlimited list.
                children = []
                for child in path.iterdir():
                    if entries + len(candidates) + len(children) >= MAX_ENTRIES:
                        raise ValueError('The transcription selection exceeds its directory-entry budget.')
                    children.append(child)
                candidates.extend(sorted(children, key=lambda item: item.name, reverse=True))
                continue
            if path.suffix.lower() not in EXTENSIONS:
                if path == source:
                    raise ValueError('The selected file has no supported audio or video extension.')
                continue
            info = regular(path)
            alias = unicodedata.normalize('NFC', str(path)).casefold()
            if alias in aliases:
                raise ValueError('The selection contains overlapping or aliased audio files.')
            aliases.add(alias)
            total += info.st_size
            if not info.st_size or len(selected) >= MAX_INPUTS or total > MAX_JOB_BYTES:
                raise ValueError('Transcription inputs exceed their file/byte budget or contain an empty file.')
            relative = path.relative_to(root).as_posix()
            relative_path(relative)
            selected.append((path, root, relative, info.st_size, file_identity(info)))
    if not selected:
        raise ValueError('No supported audio or video files were found in the selected folders.')
    return tuple(selected)


class TranscriptionService:
    def __init__(self, library_root):
        self.library_root = checked_directory(library_root)
        self.root = self.library_root / DIRECTORY

    def _job(self, identity):
        if type(identity) is not str or not JOB_ID.fullmatch(identity):
            raise ValueError('Invalid transcription job identity.')
        return checked_directory(self.root / identity)

    def definition(self, identity, *, verify_inputs=True, cancelled=lambda: False):
        job = self._job(identity)
        if verify_inputs:
            return read_definition(job, cancelled=cancelled)
        regular(job / 'job.json')
        value, _ = _read_json_snapshot(job / 'job.json', max_bytes=MAX_JSON_BYTES)
        return validate_definition(value, identity=identity)

    def prepare(self, sources, runtime_manifest, settings, *, title='Local transcription', cancelled=lambda: False):
        runtime = review_runtime(runtime_manifest, settings, cancelled=cancelled)
        selected = selected_inputs(sources, cancelled=cancelled)
        identity = uuid4().hex
        # Match complete-backup lock order. No partially published job is visible.
        with exclusive_writer(self.library_root), project_lock(self.root):
            for count, _ in enumerate(self.root.iterdir(), 1):
                if count > MAX_INPUTS:
                    raise ValueError('The retained transcription job-count budget is full.')
            if shutil.disk_usage(self.root).free < sum(row[3] for row in selected) + max(row[3] for row in selected) + 32 * 1024**2:
                raise OSError('Not enough space to retain the selected transcription inputs.')
            stage = Path(tempfile.mkdtemp(prefix='.prepare-', dir=self.root))
            try:
                inputs, origins, roots = [], [], {}
                for path, source_root, relative, size, snapshot in selected:
                    check_cancelled(cancelled)
                    root = roots.setdefault(str(source_root), 'inputs/root-' + str(len(roots)))
                    owned = root + '/' + relative
                    target = stage.joinpath(*relative_path(owned).parts)
                    checked_directory(target.parent, create=True)
                    publish_new(path, target, cancelled=cancelled, expected_identity=snapshot)
                    checksum = digest_file(target, cancelled=cancelled, max_bytes=size)
                    inputs.append(dict(path=owned, root=root, relative=relative, bytes=size, sha256=checksum))
                    origins.append(dict(path=str(path), root=str(source_root), relative=relative, bytes=size, sha256=checksum))
                value = dict(schema=SCHEMA, id=identity, title=title, created=_now(),
                             settings=runtime.settings, runtime_manifest_sha256=runtime.manifest.manifest_sha256,
                             model_sha256=runtime.model_sha256, inputs=inputs, source_provenance=origins)
                validate_definition(value)
                atomic_json(stage / 'job.json', value)
                check_cancelled(cancelled)
                publish_noreplace(stage, self.root / identity)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        return identity

    def jobs(self, *, cancelled=lambda: False):
        if not self.root.exists():
            return ()
        result = []
        for count, path in enumerate(checked_directory(self.root).iterdir(), 1):
            check_cancelled(cancelled)
            if count > MAX_INPUTS + 2:
                raise ValueError('The transcription job count exceeds its budget.')
            if JOB_ID.fullmatch(path.name):
                value = self.definition(path.name, verify_inputs=False)
                result.append(dict(id=value['id'], title=value['title'], inputs=len(value['inputs'])))
        return tuple(sorted(result, key=lambda value: value['id']))

    def control(self, identity, action):
        job = self._job(identity)
        if action not in ('pause', 'resume', 'stop'):
            raise ValueError('Unknown transcription control.')
        flag = job / ('stop.flag' if action == 'stop' else 'pause.flag')
        if flag.exists() or flag.is_symlink():
            regular(flag)
        if action == 'resume':
            flag.unlink(missing_ok=True)
        elif not flag.exists():
            with flag.open('xb') as stream:
                stream.flush()
                os.fsync(stream.fileno())

    def _state(self, job, state, run_id):
        if state not in _STATES:
            raise ValueError('Invalid transcription operation state.')
        atomic_json(job / 'execution.json', dict(schema=SCHEMA, state=state, run_id=run_id,
                                                 exit_code=0 if state == 'complete' else None, updated=_now()))

    def _archive_execution(self, job):
        """Retain old absolute bindings as history, including after a restore."""
        names = ('job_config.json', 'job_status.json', 'execution.json', 'settings_live.json', 'logs/completion-v1.json')
        paths = [(name, job / name) for name in names if os.path.lexists(job / name)]
        if not paths:
            return
        for _, path in paths:
            if regular(path).st_size > MAX_JSON_BYTES:
                raise ValueError('A previous execution record exceeds its archival budget.')
        target = checked_directory(job / 'history' / uuid4().hex, create=True)
        for name, path in paths:
            publish_noreplace(path, target / Path(name).name)

    def run(self, identity, runtime_manifest, *, cancelled=lambda: False, progress=None, timeout=24 * 3600):
        if type(timeout) not in (int, float) or not 1 <= timeout <= 7 * 24 * 3600:
            raise ValueError('Choose a transcription deadline between one second and seven days.')
        job = self._job(identity)
        with project_lock(self.root), project_lock(job):
            definition = read_definition(job, cancelled=cancelled)
            runtime = review_runtime(runtime_manifest, definition['settings'],
                                     expected_sha256=definition['runtime_manifest_sha256'], cancelled=cancelled)
            if runtime.model_sha256 != definition['model_sha256']:
                raise ValueError('The selected transcription model no longer matches the job.')
            check_cancelled(cancelled)
            if shutil.disk_usage(job).free < max(row['bytes'] for row in definition['inputs']) + 32 * 1024**2:
                raise OSError('Not enough space for the worker to capture one immutable input and its results.')
            self._archive_execution(job)
            run_id = uuid4().hex
            self._state(job, 'verifying', run_id)
            inputs = [dict(row, path=str(job / row['path']), root=str(job / row['root']))
                      for row in definition['inputs']]
            results = checked_directory(job / 'results', create=True)
            checked_directory(job / 'logs', create=True)
            config = dict(run_id=run_id, job_name=definition['title'],
                          folders=list(dict.fromkeys(row['root'] for row in inputs)), inputs=inputs,
                          source_provenance=definition['source_provenance'], settings=definition['settings'],
                          results_root=str(results), models_dir=str(runtime.models),
                          runtime_manifest=str(runtime.manifest.path),
                          runtime_manifest_sha256=runtime.manifest.manifest_sha256)
            atomic_json(job / 'job_config.json', config)
            config_sha = digest_file(job / 'job_config.json', max_bytes=MAX_JSON_BYTES)
            for name in ('pause.flag', 'stop.flag'):
                path = job / name
                if path.exists() or path.is_symlink():
                    regular(path)
                    path.unlink()
            check_cancelled(cancelled)
            environment = offline_environment()
            for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT'):
                environment.pop(name, None)
            options = ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
                       if os.name == 'nt' else {'start_new_session': True})
            process, started, last_status = None, time.monotonic(), None
            self._state(job, 'running', run_id)
            try:
                with tempfile.TemporaryFile() as output:
                    process = subprocess.Popen([str(runtime.interpreter), '-B', '-I', str(runtime.worker),
                                                '--job-dir', str(job)], stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=output, cwd=job, env=environment,
                                               shell=False, **options)
                    while True:
                        check_cancelled(cancelled)
                        if time.monotonic() - started >= timeout:
                            raise TimeoutError('The local transcription exceeded its selected time budget.')
                        if os.fstat(output.fileno()).st_size > MAX_LOG_BYTES:
                            raise RuntimeError('The transcription worker exceeded its diagnostic byte budget.')
                        status_path = job / 'job_status.json'
                        if status_path.exists():
                            # Progress is observational. An atomic status replacement
                            # can race its snapshot; the final completion read below
                            # remains strict and must validate the complete receipt.
                            try:
                                status, _ = _read_json_snapshot(status_path, max_bytes=1024 * 1024)
                            except (OSError, ValueError):
                                status = None
                            if (type(status) is dict and status.get('run_id') == run_id
                                    and status.get('protocol') == PROTOCOL):
                                pair = status.get('done'), status.get('total'), status.get('state')
                                if (pair != last_status and type(pair[0]) is int and type(pair[1]) is int
                                        and 0 <= pair[0] <= pair[1] == len(inputs)):
                                    if progress is not None:
                                        progress(pair[0], pair[1], str(pair[2])[:100])
                                    last_status = pair
                        exit_code = process.poll()
                        if exit_code is not None:
                            break
                        time.sleep(.05)
                    if exit_code == 130 and (job / 'stop.flag').exists():
                        if regular(job / 'stop.flag').st_size == 0:
                            raise InterruptedError('The supplied transcription worker acknowledged the requested stop.')
                    if exit_code != 0:
                        raise RuntimeError('The supplied transcription worker failed with exit code ' + str(exit_code) + '.')
                    status, _ = _read_json_snapshot(job / 'job_status.json', max_bytes=1024 * 1024)
                    verified = verify_completion(job, config, status, expected_config_sha256=config_sha,
                                                 expected_runtime_sha256=runtime.manifest.manifest_sha256,
                                                 expected_model_sha256=runtime.model_sha256, cancelled=cancelled)
                    if read_definition(job, cancelled=cancelled) != definition:
                        raise ValueError('The transcription job changed during execution.')
                    self._state(job, 'complete', run_id)
                    return verified
            except BaseException as error:
                try:
                    if process is not None and process.poll() is None:
                        self.control(identity, 'stop')
                finally:
                    self._state(job, 'cancelled' if isinstance(error, InterruptedError) else 'failed', run_id)
                raise
            finally:
                if process is not None:
                    from .audio.engines import _stop
                    _stop(process)

    def completed(self, identity, *, cancelled=lambda: False):
        """Read a saved accepted execution without activating its external runtime.

        The historical verifier opens only the current job's retained bytes.
        It does not grant launch permission or establish new ASR inference.
        """
        job = self._job(identity)
        definition = read_definition(job, cancelled=cancelled)
        from .transcription_backup import verify_saved_completion
        return verify_saved_completion(job, definition, cancelled=cancelled)

    def reading_copy(self, identity, output_relative, *, cancelled=lambda: False):
        """Use the existing reading-copy editor; preserve the accepted transcript."""
        verified = self.completed(identity, cancelled=cancelled)
        chosen = next((row for row in verified['outputs'] if row['path'] == output_relative), None)
        if chosen is None or chosen['format'] not in ('txt', 'srt', 'vtt', 'json') or chosen['bytes'] > 8 * 1024**2:
            raise ValueError('Choose a verified text output of at most 8 MiB for the reading editor.')
        check_cancelled(cancelled)
        path = Path(verified['results_root']).joinpath(*relative_path(chosen['path']).parts)
        with verified_reader(path) as stream:
            raw = stream.read(8 * 1024**2 + 1)
        if len(raw) != chosen['bytes'] or hashlib.sha256(raw).hexdigest() != chosen['sha256']:
            raise ValueError('The selected transcript changed before opening the editor.')
        from .reader.narration import new_project
        definition = self.definition(identity, verify_inputs=False)
        return new_project(raw.decode('utf-8-sig'), title=definition['title'][:170] + ' — reading copy',
                           original_path=str(path))
