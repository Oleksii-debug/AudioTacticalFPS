"""Non-destructive clip edits in the existing Studio script and PCM cache.

The directive contract and native controls adapt canonical PR152 ClipEditor.
There is no second project store, runtime, renderer or history implementation.
"""
from dataclasses import asdict, replace
from pathlib import Path
import shlex
import shutil
import tempfile
from uuid import uuid4

from . import ffmpeg as audio
from .clip_audio import CLIP_MIX_REVISION, clip_window, process_clip
from .jobs import (ChunkCache, check_cancelled, checked_directory, file_identity,
                   project_lock, publish_new, regular_file, sha256_file, _sync_directory)
from .models import Clip, digest, number, MAX_CLIP_SECONDS
from .production import PIPELINE_VERSION, parse_script


MAX_SOURCE_BYTES = 8 * 1024 ** 3
CLIP_FIELDS = ('start', 'gain_db', 'pan', 'track', 'trim_start', 'duration',
               'fade_in', 'fade_out', 'title')


def script_fingerprint(script, *, title, voice, language, input_kind, project_dir):
    return digest({'script': script, 'title': title, 'voice': voice,
                   'language': language, 'input_kind': input_kind, 'project_dir': str(Path(project_dir).absolute())})


def clip_directive(clip):
    if not isinstance(clip, Clip):
        raise ValueError('Choose a validated clip.')
    tokens = ['clip', shlex.quote(clip.asset), 'id=' + clip.id]
    for key in CLIP_FIELDS:
        value = getattr(clip, key)
        if value is not None and not (key == 'title' and value == ''):
            tokens.append(key + '=' + shlex.quote(str(value)))
    return '[' + ' '.join(tokens) + ']'


def retain_pcm_asset(source, project_dir, *, cancelled=lambda: False, expected_sha256=None,
                     disk_budget_bytes=32 * 1024 ** 3):
    """Retain one checked independent PCM copy under the existing project lock."""
    root = checked_directory(project_dir)
    with project_lock(root):
        return _retain_pcm_asset(source, root, cancelled=cancelled,
                                 expected_sha256=expected_sha256, disk_budget_bytes=disk_budget_bytes)


def _retain_pcm_asset(source, root, *, cancelled, expected_sha256=None, disk_budget_bytes):
    path = Path(source).absolute()
    info = regular_file(path)
    if not 0 < info.st_size <= MAX_SOURCE_BYTES:
        raise ValueError('Choose a local PCM WAV/RF64 file of at most eight GiB.')
    stamp = file_identity(info)
    pcm = audio.wav_info(path)
    content = sha256_file(path, cancelled=cancelled, expected_identity=stamp)
    if expected_sha256 is not None and content != expected_sha256:
        raise ValueError('The selected saved recording no longer has its verified content.')
    assets = checked_directory(root / 'assets', create=True)
    target = assets / (content + '.wav')
    if target.exists() or target.is_symlink():
        actual = regular_file(target)
        if actual.st_nlink != 1 or actual.st_size != info.st_size or sha256_file(target, cancelled=cancelled) != content:
            raise ValueError('A retained clip filename already has different or aliased content.')
    else:
        cache = ChunkCache(root / 'cache', cancelled=cancelled,
                           budget_root=root, disk_budget_bytes=disk_budget_bytes)
        cache.reserve(info.st_size + 64 * 1024 ** 2)
        if shutil.disk_usage(assets).free < info.st_size + 64 * 1024 ** 2:
            raise OSError('There is not enough free space to retain this clip independently.')
        publish_new(path, target, cancelled=cancelled, expected_identity=stamp)
    if file_identity(regular_file(path)) != stamp or sha256_file(target, cancelled=cancelled) != content:
        raise ValueError('A clip input changed while retaining the independent project copy.')
    # A prior publish may have moved the bytes before its directory flush
    # failed. Matching content alone must not acknowledge that uncertain entry.
    _sync_directory(assets)
    return target.name, pcm


class ClipEditor:
    def __init__(self, script, project_dir, *, title='Audio project', voice='sapi5:default',
                 language='en', input_kind='script'):
        if input_kind != 'script':
            raise ValueError('Choose Structured production script explicitly before editing clips.')
        self.root = checked_directory(project_dir)
        self.original = script
        self.title, self.voice, self.language, self.input_kind = title, voice, language, input_kind
        self.fingerprint = script_fingerprint(script, title=title, voice=voice,
                                             language=language, input_kind=input_kind, project_dir=self.root)
        # A deliberately blank structured project can begin as a clip chapter.
        self.script = script if script.strip() else '[duration 1]\n'
        self.ranges = []
        self.plan = parse_script(self.script, title=title, voice=voice, language=language,
                                 _ranges=self.ranges)
        self.lines = self.script.splitlines(keepends=True)
        self.newline = '\r\n' if '\r\n' in self.script else '\n'

    def _scene(self, identity):
        for index, scene in enumerate(self.plan.scenes):
            if scene.id == identity:
                return index, scene
        raise ValueError('The selected chapter no longer exists.')

    def _clip(self, identity):
        for index, scene in enumerate(self.plan.scenes):
            for clip in scene.clips:
                if clip.id == identity:
                    return index, scene, clip
        raise ValueError('The selected clip no longer exists.')

    def _clip_line(self, identity):
        scene_index, scene, wanted = self._clip(identity)
        start, end = self.ranges[scene_index]
        ordinal = 0
        for index in range(start, end):
            line = self.lines[index].strip()
            if not line.startswith('[') or not line.endswith(']'):
                continue
            tokens = shlex.split(line[1:-1], posix=True)
            if tokens and tokens[0] == 'clip':
                clip = scene.clips[ordinal]
                ordinal += 1
                if clip.id == identity:
                    return index
        raise ValueError('The clip directive no longer matches its parsed chapter.')

    def _replace(self, edits):
        # Index edits are computed from one captured parse, never a refreshed UI
        # list index. Preserve every unrelated source line and its line ending.
        lines = list(self.lines)
        for start, end, replacement in sorted(edits, reverse=True):
            lines[start:end] = replacement
        result = ''.join(lines)
        parse_script(result, title=self.title, voice=self.voice, language=self.language)
        return result

    def _append(self, scene_index, directive):
        end = self.ranges[scene_index][1]
        text = directive + self.newline
        if end > 0 and not self.lines[end - 1].endswith(('\n', '\r')):
            text = self.newline + text
        return end, end, [text]

    def _retained(self, clip, *, cancelled):
        path = checked_directory(self.root / 'assets') / clip.asset
        stamp = file_identity(regular_file(path))
        info = audio.wav_info(path)
        clip_window(clip, info, self.plan.sample_rate)
        check_cancelled(cancelled)
        return path, stamp, info

    def _retain(self, source, *, cancelled, expected_sha256=None):
        return _retain_pcm_asset(source, self.root, cancelled=cancelled,
            expected_sha256=expected_sha256, disk_budget_bytes=self.plan.disk_budget_bytes)

    def apply(self, request, *, cancelled=lambda: False):
        """Return a new script; the existing draft owner performs the final edit."""
        if type(request) is not dict or set(request) - {'action', 'scene_id', 'clip_id', 'source', 'values', 'minimum_duration'}:
            raise ValueError('Invalid clip editing request.')
        action = request.get('action')
        values = request.get('values', {})
        if type(values) is not dict or set(values) - set(CLIP_FIELDS):
            raise ValueError('Unknown clip control.')
        check_cancelled(cancelled)
        with project_lock(self.root):
            if action == 'add':
                index, scene = self._scene(request['scene_id'])
                if len(scene.clips) >= 32:
                    raise ValueError('A chapter supports at most thirty-two clips.')
                # Validate controls before retaining any bytes.
                candidate = Clip(uuid4().hex, 'pending.wav', **values)
                clip_window(candidate, audio.wav_info(Path(request['source']).absolute()), self.plan.sample_rate)
                name, info = self._retain(request['source'], cancelled=cancelled)
                candidate = replace(candidate, asset=name)
                clip_window(candidate, info, self.plan.sample_rate)
                updated = self._replace([self._append(index, clip_directive(candidate))])
                selected = candidate.id
            elif action in ('edit', 'remove', 'move'):
                index, scene, original = self._clip(request['clip_id'])
                line = self._clip_line(original.id)
                if action == 'edit':
                    candidate = replace(original, **values)
                    self._retained(candidate, cancelled=cancelled)
                    ending = '\r\n' if self.lines[line].endswith('\r\n') else ('\n' if self.lines[line].endswith('\n') else '')
                    updated = self._replace([(line, line + 1, [clip_directive(candidate) + ending])])
                    selected = candidate.id
                else:
                    replacement = []
                    if len(scene.clips) == 1 and not scene.events and not scene.minimum_duration:
                        replacement = ['[duration 1]' + self.newline]
                    edits = [(line, line + 1, replacement)]
                    if action == 'move':
                        destination, target = self._scene(request['scene_id'])
                        if target.id == scene.id:
                            raise ValueError('Choose another chapter for this clip.')
                        if len(target.clips) >= 32:
                            raise ValueError('The destination chapter already has thirty-two clips.')
                        self._retained(original, cancelled=cancelled)
                        edits.append(self._append(destination, clip_directive(original)))
                    updated = self._replace(edits)
                    selected = original.id if action == 'move' else None
            elif action == 'duration':
                index, scene = self._scene(request['scene_id'])
                seconds = number(request.get('minimum_duration'), 0.000001,
                                 MAX_CLIP_SECONDS, 'Chapter minimum duration')
                start, end = self.ranges[index]
                line = next((row for row in range(start, end)
                             if self.lines[row].strip().startswith('[duration ')), None)
                directive = '[duration ' + str(seconds) + ']'
                edit = (line, line + 1, [directive + self.newline]) if line is not None else self._append(index, directive)
                updated, selected = self._replace([edit]), None
            else:
                raise ValueError('Choose add, edit, remove, move or chapter duration.')
            check_cancelled(cancelled)
            return {'script': updated, 'fingerprint': self.fingerprint, 'clip_id': selected}

    def preview(self, identity, *, ffmpeg, cancelled=lambda: False, broker=None, operation_id=None):
        """Render only the selected trim/gain/pan/fades through the shared cache."""
        with project_lock(self.root):
            _, _, clip = self._clip(identity)
            source, stamp, info = self._retained(clip, cancelled=cancelled)
            window = clip_window(clip, info, self.plan.sample_rate)
            source_hash = sha256_file(source, cancelled=cancelled, expected_identity=stamp)
            settings = {'pipeline_version': PIPELINE_VERSION, 'kind': 'clip_processing',
                'source_sha256': source_hash, 'clip': asdict(clip),
                'sample_rate': self.plan.sample_rate, 'processor': audio.ffmpeg_identity(ffmpeg, cancelled=cancelled),
                'clip_revision': CLIP_MIX_REVISION}
            def render(stopping):
                if sha256_file(source, cancelled=stopping, expected_identity=stamp) != source_hash:
                    raise ValueError('The retained clip changed while waiting for resources.')
                cache = ChunkCache(self.root / 'cache', cancelled=stopping,
                    budget_root=self.root, disk_budget_bytes=self.plan.disk_budget_bytes)
                cache.reserve(0)
                ready = cache.get(digest(settings))
                if ready is None:
                    cache.reserve(window.frames * 8 + 65536)
                    with tempfile.TemporaryDirectory(prefix='.clip-preview-', dir=self.root) as temporary:
                        target = Path(temporary) / 'clip.wav'
                        process_clip(source, target, clip, ffmpeg=ffmpeg, sample_rate=self.plan.sample_rate,
                                     cancelled=stopping, expected_identity=stamp)
                        if sha256_file(source, cancelled=stopping, expected_identity=stamp) != source_hash:
                            raise ValueError('The retained input changed during clip preview.')
                        ready = cache.seal(digest(settings), target, provenance={'kind': 'clip_preview',
                                                                              'source_sha256': source_hash})
                check_cancelled(stopping)
                return ready[0]

            if broker is None:
                return render(cancelled)
            from ..operations import ResourceBroker
            from ..operations.admission import production_request
            from .models import Scene
            if not isinstance(broker, ResourceBroker):
                raise TypeError('Use the shared typed resource coordinator.')
            preview_plan = replace(self.plan, schema_version=2,
                scenes=(Scene('preview', 'Selected clip', (), clips=(replace(clip, start=0),)),))
            request = production_request(preview_plan, self.root, broker.limits,
                                         sound_seconds={clip.asset: info.seconds})
            with broker.reserve(request, operation_id=operation_id or 'studio:' + digest(str(self.root))[:32], cancelled=cancelled, timeout=60) as lease:
                return render(lease.cancelled)
