"""Explicit, source-pinned import of canonical Studio project schemas one and two.

This is a conversion into the existing editable script and managed project.
No legacy runtime, executable setting, render job or cache is activated. Exact
selected JSON remains inert provenance inside the new document.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
from uuid import uuid4

from filelock import FileLock

from ..ingest.writer_guard import exclusive_writer
from ..reader.narration_text import NarrationDraft, decode_json, text_digest
from .jobs import (checked_directory, file_identity, regular_file, sha256_file,
                   verified_reader, publish_noreplace, _sync_directory)
from .managed import ManagedProjects
from .models import identity, number

KIND = 'canonical-studio-project'
MAX_JSON_BYTES = 32 * 1024 ** 2
MAX_SCENES = 10000
MAX_LINES = 100000
MAX_ASSETS = 1000
MAX_ASSET_BYTES = 8 * 1024 ** 3
_PROJECT = {'id', 'title', 'roles', 'scenes', 'original_script', 'mode',
            'sample_rate', 'revision', 'schema'}
_ROLE = {'id', 'name', 'voice_id', 'language', 'pan', 'gain_db', 'rate', 'volume'}
_LINE = {'id', 'role', 'text', 'pause_after', 'source_ref'}
_SCENE = {'id', 'title', 'lines', 'music', 'cues', 'minimum_duration'}
_MUSIC = {'mood', 'tempo', 'gain_db', 'engine', 'seed'}
_CUE = {'id', 'path', 'start', 'gain_db', 'pan', 'role'}
_CUE_V2 = _CUE | {'trim_start', 'duration', 'fade_in', 'fade_out', 'title'}


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Canonical Studio import cancelled; the original is unchanged.')


def _object(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError('Unsupported or incomplete canonical ' + label + ' fields.')
    return value


def _text(value, label, maximum, *, empty=False):
    if (not isinstance(value, str) or '\x00' in value or len(value) > maximum
            or (not empty and not value.strip())):
        raise ValueError('Invalid canonical ' + label + '.')
    text_digest(value)
    return value


def decode_project(raw):
    """Validate only the published data schema; no paths are followed here."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JSON_BYTES:
        raise ValueError('Choose a bounded canonical Studio JSON document.')
    try:
        value = _object(decode_json(raw.decode('utf-8')), _PROJECT, 'project')
    except UnicodeError as exc:
        raise ValueError('The canonical project must be UTF-8 JSON.') from exc
    identity(value['id'])
    if (type(value['schema']) is not int or value['schema'] not in (1, 2)
            or type(value['revision']) is not int or value['revision'] < 1
            or value['mode'] not in ('faithful', 'authored')
            or type(value['sample_rate']) is not int
            or value['sample_rate'] not in (16000, 22050, 24000, 44100, 48000)):
        raise ValueError('Unsupported canonical Studio schema, revision or production mode.')
    _text(value['title'], 'title', 512)
    _text(value['original_script'], 'original script', 16_000_000, empty=True)
    if (type(value['roles']) is not list or len(value['roles']) > 64
            or type(value['scenes']) is not list or not 1 <= len(value['scenes']) <= MAX_SCENES):
        raise ValueError('The canonical project exceeds the role or scene budget.')
    roles = set()
    for role in value['roles']:
        _object(role, _ROLE, 'role')
        identity(role['id'])
        if role['id'] in roles:
            raise ValueError('Duplicate canonical role identity.')
        roles.add(role['id'])
        _text(role['name'], 'role name', 256)
        _text(role['voice_id'], 'voice identity', 2048, empty=True)
        _text(role['language'], 'language', 64, empty=True)
        number(role['pan'], -1, 1, 'Canonical role pan')
        number(role['gain_db'], -60, 12, 'Canonical role gain')
        if type(role['rate']) is not int or type(role['volume']) is not int:
            raise ValueError('Canonical speech controls must be integers.')
        number(role['rate'], -10, 10, 'Canonical speech rate')
        number(role['volume'], 0, 100, 'Canonical speech volume')
    scenes, lines, assets = set(), 0, 0
    for scene in value['scenes']:
        _object(scene, _SCENE, 'scene')
        identity(scene['id'])
        if scene['id'] in scenes:
            raise ValueError('Duplicate canonical scene identity.')
        scenes.add(scene['id'])
        _text(scene['title'], 'scene title', 512)
        number(scene['minimum_duration'], 0, 31_536_000, 'Canonical scene duration')
        if type(scene['lines']) is not list or type(scene['cues']) is not list:
            raise ValueError('Canonical scene lines and cues must be lists.')
        lines += len(scene['lines'])
        assets += len(scene['cues'])
        if lines > MAX_LINES or assets > MAX_ASSETS:
            raise ValueError('The canonical project exceeds the line or input budget.')
        ids = set()
        for line in scene['lines']:
            _object(line, _LINE, 'line')
            identity(line['id'])
            identity(line['role'])
            if line['id'] in ids or line['role'] not in roles:
                raise ValueError('Duplicate canonical line or unknown speaking role.')
            ids.add(line['id'])
            _text(line['text'], 'spoken text', 100000, empty=True)
            _text(line['source_ref'], 'source reference', 2048, empty=True)
            number(line['pause_after'], 0, 86400, 'Canonical line pause')
        if not scene['lines'] and not scene['cues'] and scene['music'] is None and scene['minimum_duration'] <= 0:
            raise ValueError('A canonical scene cannot be empty.')
        music = scene['music']
        if music is not None:
            _object(music, _MUSIC, 'music')
            if music['mood'] not in ('calm', 'suspense', 'hopeful', 'sad', 'energetic'):
                raise ValueError('Unknown canonical music mood.')
            if music['engine'] not in ('procedural', 'midi', 'musicgen', 'ace-step'):
                raise ValueError('Unknown canonical music engine.')
            number(music['tempo'], 20, 300, 'Canonical tempo')
            number(music['gain_db'], -60, 0, 'Canonical music gain')
            if type(music['seed']) is not int:
                raise ValueError('A canonical seed must be an integer.')
            number(music['seed'], 0, 2 ** 31 - 1, 'Canonical music seed')
        ids = set()
        for cue in scene['cues']:
            _object(cue, _CUE_V2 if value['schema'] == 2 else _CUE, 'cue')
            identity(cue['id'])
            if cue['id'] in ids:
                raise ValueError('Duplicate canonical cue identity.')
            ids.add(cue['id'])
            _text(cue['path'], 'cue path', 8192)
            number(cue['start'], 0, 31_536_000, 'Canonical cue start')
            number(cue['gain_db'], -60, 12, 'Canonical cue gain')
            number(cue['pan'], -1, 1, 'Canonical cue pan')
            tracks = ('sfx', 'ambience', 'music', 'microphone') if value['schema'] == 2 else ('sfx', 'ambience', 'music')
            if cue['role'] not in tracks:
                raise ValueError('Unknown canonical cue track.')
            if value['schema'] == 2:
                number(cue['trim_start'], 0, 31_536_000, 'Canonical clip trim')
                if cue['duration'] is not None:
                    number(cue['duration'], .000001, 31_536_000, 'Canonical clip duration')
                number(cue['fade_in'], 0, 31_536_000, 'Canonical clip fade in')
                number(cue['fade_out'], 0, 31_536_000, 'Canonical clip fade out')
                _text(cue['title'], 'clip title', 512, empty=True)
                if cue['duration'] is not None and max(cue['fade_in'], cue['fade_out']) > cue['duration']:
                    raise ValueError('A canonical fade exceeds its selected duration.')
    return value


def validate_reference(reference, draft):
    """Authenticate inert source bytes without admitting any old path or runtime."""
    fields = {'kind', 'version', 'bytes_sha256', 'document_utf8', 'original_path'}
    _object(reference, fields, 'source reference')
    if (reference['kind'] != KIND or type(reference['version']) is not int
            or reference['version'] != 1 or not isinstance(reference['document_utf8'], str)
            or not isinstance(reference['bytes_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', reference['bytes_sha256'])):
        raise ValueError('Invalid canonical Studio source reference.')
    _text(reference['original_path'], 'original path', 8192)
    try:
        raw = reference['document_utf8'].encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Invalid canonical Studio source encoding.') from exc
    source = decode_project(raw)
    if (sha256(raw).hexdigest() != reference['bytes_sha256']
            or not isinstance(draft, NarrationDraft)
            or draft.source_id != 'canonical-studio:' + reference['bytes_sha256']
            or draft.source_text != source['original_script']):
        raise ValueError('The canonical project provenance differs from its exact original bytes.')
    draft.assert_source()
    return source


def _read_source(path, cancelled):
    selected = Path(path).absolute()
    info = regular_file(selected)
    if info.st_nlink != 1 or not 0 < info.st_size <= MAX_JSON_BYTES:
        raise ValueError('Choose one regular, unaliased canonical project within the byte budget.')
    stamp = file_identity(info)
    _cancel(cancelled)
    with verified_reader(selected, expected_identity=stamp) as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    _cancel(cancelled)
    decode_project(raw)
    return selected, stamp, raw



def inspect_project(path, *, cancelled=lambda: False):
    """Read selected metadata for native review; this grants no import authority."""
    selected, _, raw = _read_source(path, cancelled)
    return selected, sha256(raw).hexdigest(), decode_project(raw)


def _cue_key(scene, cue):
    return scene['id'] + ':' + cue['id']


def _script(source, asset_names, *, default_voice, default_language, voice_overrides):
    """Serialize then compare every represented control through the current parser."""
    from .clips import clip_directive
    from .models import Clip, Effect, Line, MusicCue, Pause, Role
    from .production import parse_script

    if type(voice_overrides) is not dict or set(voice_overrides) - {r['id'] for r in source['roles']}:
        raise ValueError('Voice replacements must name exact canonical role identities.')
    if (len(source['title']) > 256 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in source['title'])):
        raise ValueError('The original title cannot be represented in the current Studio title field.')
    # These are the caller's selected local controls, never loaded executables.
    Role('Narrator', voice=default_voice, language=default_language)
    expected_roles, output = {}, [f"[output format=wav sample_rate={source['sample_rate']} stems=true]"]
    for role in source['roles']:
        name = role['name']
        if name in expected_roles or name != name.strip() or len(name.splitlines()) != 1:
            raise ValueError('Canonical roles require distinct, representable names.')
        converted = Role(name, voice=voice_overrides.get(role['id'], role['voice_id'] or default_voice),
            language=role['language'] or default_language, pan=role['pan'],
            rate=role['rate'], volume=role['volume'] / 100,
            effects=(Effect('gain', (('gain_db', role['gain_db']),)),))
        expected_roles[name] = converted
        options = {'voice': converted.voice, 'language': converted.language, 'pan': converted.pan,
                   'rate': converted.rate, 'volume': converted.volume}
        output.append('[role ' + shlex.quote(name) + ''.join(
            ' ' + key + '=' + shlex.quote(str(value)) for key, value in options.items()) + ']')
        output.append('[effect ' + shlex.quote(name) + ' gain gain_db=' + repr(role['gain_db']) + ']')
    by_id = {row['id']: row['name'] for row in source['roles']}
    expected = []
    for scene_index, scene in enumerate(source['scenes'], 1):
        title = scene['title']
        if title != title.strip() or len(title.splitlines()) != 1:
            raise ValueError('A canonical scene title cannot be represented without changing it.')
        output.append('# ' + title)
        music = None
        if scene['music'] is not None:
            row = scene['music']
            if row['engine'] != 'procedural':
                raise ValueError('This canonical project requires an installed music engine. Import a reviewed procedural version or explicitly recreate that music cue; old runtime settings are not activated.')
            music = MusicCue(mood=row['mood'], tempo=row['tempo'],
                gain=10 ** (row['gain_db'] / 20), seed=row['seed'], engine='procedural')
            output.append('[music mood=' + music.mood + ' tempo=' + str(music.tempo)
                          + ' gain=' + repr(music.gain) + ' seed=' + str(music.seed) + ']')
        # The old renderer inserted a one-second base for scenes with no lines.
        minimum = scene['minimum_duration'] if scene['lines'] else max(1, scene['minimum_duration'])
        if minimum:
            output.append('[duration ' + repr(minimum) + ']')
        clips, events = [], []
        for cue_index, cue in enumerate(scene['cues'], 1):
            key = _cue_key(scene, cue)
            clip = Clip(f'canonical-cue-{scene_index:06d}-{cue_index:06d}', asset_names[key],
                        start=cue['start'], gain_db=cue['gain_db'], pan=cue['pan'],
                        track=cue['role'], trim_start=cue.get('trim_start', 0),
                        duration=cue.get('duration'), fade_in=cue.get('fade_in', 0),
                        fade_out=cue.get('fade_out', 0), title=cue.get('title', cue['id']))
            clips.append(clip)
            output.append(clip_directive(clip))
        for line in scene['lines']:
            if line['text']:
                if len(line['text'].splitlines()) != 1:
                    raise ValueError('A canonical spoken line contains a line break the current script cannot preserve.')
                output.append(by_id[line['role']] + ': ' + line['text'])
                events.append(('line', by_id[line['role']], line['text']))
            pause = line['pause_after']
            if not line['text'] and pause == 0:
                raise ValueError('A zero-length canonical silent line cannot be represented without a timing change.')
            if pause:
                Pause('checked-pause', pause)
                output.append('[pause ' + repr(pause) + ']')
                events.append(('pause', pause))
        expected.append((title, tuple(events), music, tuple(clips), minimum))
    script = '\n'.join(output) + '\n'
    plan = parse_script(script, title=source['title'], voice=default_voice, language=default_language)
    actual_roles = {role.name: role for role in plan.roles}
    if any(actual_roles.get(name) != role for name, role in expected_roles.items()):
        raise ValueError('The current script parser changed an imported voice control.')
    actual = []
    for scene in plan.scenes:
        events = []
        for event in scene.events:
            if isinstance(event, Line):
                events.append(('line', event.role, event.text))
            elif isinstance(event, Pause):
                events.append(('pause', event.seconds))
            else:
                raise ValueError('An imported source introduced an unexpected production instruction.')
        actual.append((scene.title, tuple(events), scene.music, scene.clips, scene.minimum_duration))
    if tuple(actual) != tuple(expected) or plan.sample_rate != source['sample_rate']:
        raise ValueError('The current script cannot preserve this canonical project without changes.')
    return script


@dataclass(frozen=True)
class Asset:
    key: str
    path: Path
    identity: tuple
    sha256: str


@dataclass(frozen=True)
class ImportReview:
    library: Path
    library_identity: tuple
    source: Path
    source_identity: tuple
    raw: bytes
    input_roots: tuple
    assets: tuple
    default_voice: str
    default_language: str
    voice_overrides: tuple
    script: str
    max_input_bytes: int


def review_project(path, *, private_root, default_voice, default_language,
                   input_roots=(), asset_sources=None, voice_overrides=None,
                   max_input_bytes=32 * 1024 ** 3, expected_sha256=None, cancelled=lambda: False):
    """Capture explicit inputs and a validated proposal without creating a project."""
    from . import ffmpeg as audio
    if type(max_input_bytes) is not int or max_input_bytes <= 0:
        raise ValueError('Choose a positive input byte budget.')
    library = checked_directory(private_root)
    info = library.stat()
    selected, stamp, raw = _read_source(path, cancelled)
    if expected_sha256 is not None and sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('The canonical project changed after inspection. Inspect it again.')
    source = decode_project(raw)
    overrides = {} if asset_sources is None else asset_sources
    voices = {} if voice_overrides is None else voice_overrides
    keys = {_cue_key(scene, cue) for scene in source['scenes'] for cue in scene['cues']}
    if type(overrides) is not dict or set(overrides) - keys:
        raise ValueError('Asset replacements must name exact canonical scene/cue identities.')
    _script(source, {key: '0' * 64 + '.wav' for key in keys},
            default_voice=default_voice, default_language=default_language, voice_overrides=voices)
    roots = tuple(checked_directory(root) for root in input_roots)
    root_stamps = tuple((root, (root.stat().st_dev, root.stat().st_ino)) for root in roots)
    assets, names, pcm_info, total = [], {}, {}, 0
    for scene in source['scenes']:
        for cue in scene['cues']:
            _cancel(cancelled)
            key = _cue_key(scene, cue)
            candidate = Path(overrides.get(key, cue['path']))
            if not candidate.is_absolute():
                raise ValueError('Select the current local file for a relative or foreign-platform cue path.')
            candidate = candidate.absolute()
            if not roots or not any(candidate.is_relative_to(root) for root in roots):
                raise ValueError('Select an input folder explicitly before importing its canonical sound cues.')
            current = regular_file(candidate)
            total += current.st_size
            if (current.st_nlink != 1 or not 0 < current.st_size <= MAX_ASSET_BYTES
                    or total > max_input_bytes):
                raise ValueError('Canonical sound inputs exceed their budget or use a file alias.')
            identity_before = file_identity(current)
            pcm = audio.wav_info(candidate)
            digest = sha256_file(candidate, cancelled=cancelled, expected_identity=identity_before)
            assets.append(Asset(key, candidate, identity_before, digest))
            names[key] = digest + '.wav'
            pcm_info[names[key]] = pcm
    script = _script(source, names, default_voice=default_voice,
                     default_language=default_language, voice_overrides=voices)
    from .clip_audio import clip_window
    from .production import parse_script
    parsed = parse_script(script, title=source['title'], voice=default_voice, language=default_language)
    for scene in parsed.scenes:
        for clip in scene.clips:
            clip_window(clip, pcm_info[clip.asset], parsed.sample_rate)
    _cancel(cancelled)
    return ImportReview(library, (info.st_dev, info.st_ino), selected, stamp, raw,
                        root_stamps, tuple(assets), default_voice, default_language,
                        tuple(sorted(voices.items())), script, max_input_bytes)


def _current(review, cancelled):
    _cancel(cancelled)
    current = checked_directory(review.library).stat()
    if (current.st_dev, current.st_ino) != review.library_identity:
        raise ValueError('The selected library changed after canonical import review.')
    for root, expected in review.input_roots:
        current = checked_directory(root).stat()
        if (current.st_dev, current.st_ino) != expected:
            raise ValueError('An approved canonical input folder changed after review.')
    _, stamp, raw = _read_source(review.source, cancelled)
    if stamp != review.source_identity or raw != review.raw:
        raise ValueError('The canonical project changed after review. Review it again.')
    for asset in review.assets:
        if not any(asset.path.is_relative_to(root) for root, _ in review.input_roots):
            raise ValueError('A reviewed sound input is outside its selected folders.')
        if file_identity(regular_file(asset.path)) != asset.identity:
            raise ValueError('A canonical sound input changed after review.')


def import_reviewed(review, *, cancelled=lambda: False):
    """Publish a fresh current managed project; no old project is overwritten."""
    from .clips import retain_pcm_asset
    from .backup import _studio
    if not isinstance(review, ImportReview):
        raise TypeError('Review one canonical project before importing it.')
    source = decode_project(review.raw)
    names = {asset.key: asset.sha256 + '.wav' for asset in review.assets}
    script = _script(source, names, default_voice=review.default_voice,
        default_language=review.default_language, voice_overrides=dict(review.voice_overrides))
    if script != review.script:
        raise ValueError('The approved canonical import proposal changed.')
    _current(review, cancelled)
    identity_new = uuid4().hex
    managed = ManagedProjects(review.library)
    root, document, output = managed.paths(identity_new)
    with exclusive_writer(review.library, timeout=0):
        _current(review, cancelled)
        checked_directory(root.parent, create=True)
        stage_root = Path(tempfile.mkdtemp(prefix='.canonical-import-', dir=root.parent))
        owned = stage_root.stat()
        try:
            staged_document = stage_root / 'studio' / 'project.json'
            staged_output = stage_root / 'audio'
            staged_document.parent.mkdir(mode=0o700)
            staged_output.mkdir(mode=0o700)
            # Same writer order as current managed saves and complete backups.
            # Release both nested native locks before moving the tree on Windows.
            with FileLock(stage_root / '.draft.lock', timeout=0, mode=0o600):
                for asset in review.assets:
                    _cancel(cancelled)
                    name, _ = retain_pcm_asset(asset.path, staged_output, cancelled=cancelled,
                        expected_sha256=asset.sha256,
                        disk_budget_bytes=review.max_input_bytes + 64 * 1024 ** 2)
                    if name != names[asset.key]:
                        raise ValueError('A retained sound cue differs from the reviewed input.')
                _current(review, cancelled)
                digest = sha256(review.raw).hexdigest()
                draft = NarrationDraft(source['original_script'], source_id='canonical-studio:' + digest)
                draft.edit_text(script, reason='Explicit canonical Studio project import')
                reference = {'kind': KIND, 'version': 1, 'bytes_sha256': digest,
                             'document_utf8': review.raw.decode('utf-8'),
                             'original_path': str(review.source)}
                validate_reference(reference, draft)
                payload = {'kind': 'research-audio-studio', 'version': 2, 'mode': 'LOCAL ONLY',
                    'project_id': identity_new, 'title': source['title'],
                    'voice': review.default_voice, 'language': review.default_language,
                    'input_kind': 'script', 'output_dir': str(output),
                    'source_reference': reference, 'draft': draft.to_dict()}
                _studio(payload)
                raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                  indent=2, allow_nan=False) + '\n').encode('utf-8')
                if len(raw) > 64 * 1024 ** 2:
                    raise ValueError('The imported editable document exceeds the current Studio byte budget.')
                with staged_document.open('xb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_directory(staged_document.parent)
                _sync_directory(staged_output)
            _current(review, cancelled)
            _sync_directory(stage_root)
            publish_noreplace(stage_root, root)
        except BaseException:
            # The publication helper may move the whole tree and then report a
            # sync failure. Remove only this exact private inode at either name.
            for candidate in (stage_root, root):
                try:
                    current = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    shutil.rmtree(candidate)
                    _sync_directory(root.parent)
            raise
    return document

