"""Manifest-bound retained Scene inputs, presets and passive recovery notices.

This adapter consumes only the existing state snapshot. A Cue path is data, not
permission to open an external file. Source-root bindings are captured by the
backup producer; current projects may acquire one checked relocation revision.
Historical project revisions and job receipts remain unchanged.
"""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from ..durable_state import atomic_json, encode, local_path, read_json_snapshot, regular

SNAPSHOT = 'studio/backup-inputs.json'
RECOVERY = 'studio/input-recovery.json'
SNAPSHOT_SCHEMA = 'carr-studio-input-snapshot/1'
RECOVERY_SCHEMA = 'carr-studio-input-recovery/1'
MAX_BYTES = 32 * 1024**2
MAX_INPUT_BYTES = 8 * 1024**3
MAX_PRESETS = 256
MAX_PRESET_BYTES = 64 * 1024
NOTICE = ('Only manifest-bound retained Studio inputs are included. External audio references '
          'remain unavailable until explicitly selected and retained again. Historical jobs '
          'keep their original receipts and require a new output destination.')
_ID = r'[A-Za-z0-9_-]{1,80}'
_CURRENT = re.compile(r'studio/projects/(' + _ID + r')/project\.json\Z', re.IGNORECASE)
_REVISION = re.compile(r'studio/projects/(' + _ID + r')/revisions/([0-9]{8,})-([0-9a-f]{64})\.json\Z',
                       re.IGNORECASE)
_JOB = re.compile(r'studio/jobs/(' + _ID + r')/job\.json\Z', re.IGNORECASE)
_JOB_PROJECT = re.compile(r'studio/jobs/(' + _ID + r')/project\.json\Z', re.IGNORECASE)
_INPUT = re.compile(r'studio/inputs/([0-9a-f]{64})\.audio\Z')
_PRESET = re.compile(r'studio/effect-presets/([0-9a-f]{32})\.json\Z')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Studio input backup or recovery cancelled.')


def _digest(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('Invalid Studio content identity.')
    return value


def _text(value, maximum=8192):
    if type(value) is not str or not value or len(value) > maximum or '\x00' in value:
        raise ValueError('Invalid retained Studio reference.')
    value.encode('utf-8', 'strict')
    return value


def _number(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid retained Studio size or revision.')
    return value


def _state_root(value):
    """Validate a captured local path spelling without resolving or opening it."""
    value = _text(value, 32767)
    windows = re.match(r'^[A-Za-z]:[\\/]', value) is not None
    path = PureWindowsPath(value) if windows else PurePosixPath(value)
    parts = value.replace('\\', '/').split('/') if windows else value.split('/')
    if (not path.is_absolute() or value.startswith(('//', '\\\\'))
            or '..' in parts or '.' in parts
            or any(ord(char) < 32 for char in value)
            or ':' in (value[2:] if windows else value)):
        raise ValueError('A captured Studio state root must be an explicit local absolute path.')
    return path


def namespace(name):
    """These reserved root-relative state families never include primary Studio."""
    if type(name) is not str or not name.startswith('state/'):
        return False
    folded = name[len('state/'):].casefold()
    return (folded in (SNAPSHOT, RECOVERY)
            or folded.startswith(('studio/inputs/', 'studio/effect-presets/')))


def limit(name):
    if not namespace(name):
        return None
    relative = name[len('state/'):]
    if relative != relative.casefold():
        raise ValueError('Retained Studio inputs and presets require their exact canonical names.')
    if _INPUT.fullmatch(relative):
        return MAX_INPUT_BYTES
    if _PRESET.fullmatch(relative):
        return MAX_PRESET_BYTES
    if relative in (SNAPSHOT, RECOVERY):
        return MAX_BYTES
    raise ValueError('Unsupported retained Studio input or preset pathname.')


def is_input(name):
    return namespace(name) and _INPUT.fullmatch(name[len('state/'):]) is not None


def _inventory(entries, cancelled):
    from ..state_backup import validate_state
    validate_state(entries)
    result, presets = {}, 0
    for entry in entries:
        _check(cancelled)
        name = entry['filename']
        folded = name.casefold()
        if folded in result:
            raise ValueError('Ambiguous retained Studio state inventory.')
        result[folded] = entry
        if namespace('state/' + name):
            maximum = limit('state/' + name)
            if not 0 < entry['size_bytes'] <= maximum:
                raise ValueError('Retained Studio state exceeds its data-format budget.')
            if _INPUT.fullmatch(name) and entry['sha256'] != _INPUT.fullmatch(name)[1]:
                raise ValueError('Retained Studio input name differs from its manifest content hash.')
            if _PRESET.fullmatch(name):
                presets += 1
                if presets > MAX_PRESETS:
                    raise ValueError('Too many retained effect presets.')
    return result


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _verify_input(root, entry, cancelled):
    """Stream only the manifest-listed private file; never follow a Cue path."""
    _check(cancelled)
    path = local_path(Path(root) / entry['filename'], root=root, exists=True)
    before = regular(path)
    if before.st_size != entry['size_bytes']:
        raise ValueError('Retained Studio input size changed.')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Retained Studio input changed before verification.')
        while block := stream.read(1024 * 1024):
            _check(cancelled)
            count += len(block)
            if count > entry['size_bytes']:
                raise ValueError('Retained Studio input grew during verification.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Retained Studio input changed during verification.')
    if (count != entry['size_bytes'] or digest.hexdigest() != entry['sha256']
            or _stamp(regular(local_path(path, root=root, exists=True))) != _stamp(before)):
        raise ValueError('Retained Studio input differs from its manifest content identity.')


def _json(root, entry):
    if not 0 < entry['size_bytes'] <= MAX_BYTES:
        raise ValueError('Typed Studio metadata exceeds its JSON budget.')
    value, identity = read_json_snapshot(Path(root) / entry['filename'],
                                         max_bytes=entry['size_bytes'])
    if identity != entry['sha256']:
        raise ValueError('Typed Studio metadata differs from its captured manifest bytes.')
    return value


def _record(root, entry):
    from .project import Project
    name = entry['filename']
    current, revision = _CURRENT.fullmatch(name), _REVISION.fullmatch(name)
    job, exported = _JOB.fullmatch(name), _JOB_PROJECT.fullmatch(name)
    if not any((current, revision, job, exported)):
        return None
    try:
        value = _json(root, entry)
    except (UnicodeError, json.JSONDecodeError):
        # Existing opaque diagnostic job files cannot be active typed plans.
        if job:
            return None
        raise ValueError('Invalid typed Studio project JSON.') from None
    if job and (type(value) is not dict or 'schema' not in value and 'project' not in value):
        return None
    project = Project.from_dict(value['project'] if job else value)
    if current and project.id != current[1]:
        raise ValueError('Studio project directory differs from its identity.')
    if revision and (project.id != revision[1] or project.revision != int(revision[2])
                     or project.fingerprint != revision[3]):
        raise ValueError('Studio revision filename differs from its content identity.')
    if job and (type(value.get('schema')) is not int or value['schema'] != 1
                or value.get('id') != job[1] or value.get('fingerprint') != project.fingerprint):
        raise ValueError('Historical Studio job differs from its bound project.')
    return ('current' if current else 'revision' if revision else 'job' if job else 'job-project'), project


def _owned(raw, source_state_root):
    root = _state_root(source_state_root)
    path = type(root)(_text(raw))
    # A similar suffix on another volume/root is deliberately not a binding.
    if '..' in path.parts or path.parent != root / 'studio' / 'inputs':
        return None
    if re.fullmatch(r'[0-9a-f]{64}\.audio', path.name) is None:
        return None
    return 'studio/inputs/' + path.name


def _snapshot(root, inventory, source_state_root, cancelled):
    _state_root(source_state_root)
    # A previous restoration's missing authority survives another backup.
    # A familiar path underneath this machine's input directory is not fresh
    # user selection. Read only the exact captured passive notice.
    blocked = set()
    if RECOVERY in inventory:
        recovery = validate_recovery(_json(root, inventory[RECOVERY]))
        blocked = {(item['cue'], item['path']) for row in recovery['projects']
                   for item in row['unresolved']}
    records, total = [], 0
    for entry in sorted(inventory.values(), key=lambda row: row['filename']):
        _check(cancelled)
        found = _record(root, entry)
        if found is None:
            continue
        kind, project = found
        raw_paths = sorted({cue.path for scene in project.scenes for cue in scene.cues})
        if not raw_paths:
            continue
        retained, external = {}, []
        unavailable = {cue.path for scene in project.scenes for cue in scene.cues
                       if (cue.id, cue.path) in blocked} if kind == 'current' else set()
        for raw in raw_paths:
            _check(cancelled)
            relative = None if raw in unavailable else _owned(raw, source_state_root)
            if relative is None:
                external.append(raw)
                continue
            incoming = inventory.get(relative)
            if incoming is None or not _INPUT.fullmatch(incoming['filename']):
                raise ValueError('A managed Studio input is missing from the complete snapshot.')
            if incoming['sha256'] != Path(relative).stem or incoming['size_bytes'] <= 0:
                raise ValueError('A managed Studio input lost its content binding.')
            retained[raw] = dict(incoming)
        row = {'filename': entry['filename'], 'kind': kind, 'sha256': entry['sha256'],
               'project_id': project.id, 'revision': project.revision,
               'fingerprint': project.fingerprint, 'inputs': retained, 'unretained': external}
        total += len(encode(row))
        if total > MAX_BYTES - 65536:
            raise ValueError('Studio input bindings exceed their metadata budget.')
        records.append(row)
    return {'schema': SNAPSHOT_SCHEMA, 'source_state_root': source_state_root,
            'projects': records, 'notice': NOTICE}


def validate_metadata(name, value):
    relative = name.removeprefix('state/')
    maximum = limit('state/' + relative)
    if maximum is None or _INPUT.fullmatch(relative):
        raise ValueError('A typed Studio metadata path is required.')
    if len(encode(value)) > maximum:
        raise ValueError('Studio metadata exceeds its data-format budget.')
    if match := _PRESET.fullmatch(relative):
        from ..audio.effect_presets import EffectPreset
        preset = EffectPreset.from_dict(value)
        if preset.id != match[1]:
            raise ValueError('Effect preset identity differs from its filename.')
        return value
    if relative == SNAPSHOT:
        if (type(value) is not dict or set(value) !=
                {'schema', 'source_state_root', 'projects', 'notice'}
                or value['schema'] != SNAPSHOT_SCHEMA or value['notice'] != NOTICE
                or type(value['projects']) is not list or len(value['projects']) > 100000):
            raise ValueError('Invalid Studio input snapshot receipt.')
        _state_root(value['source_state_root'])
        # Exact record/member/type bindings are validated against the complete
        # manifest in validate_state, before any restored projection is made.
        return value
    return validate_recovery(value)


def validate_recovery(value):
    if (type(value) is not dict or set(value) != {'schema', 'projects', 'notice'}
            or value['schema'] != RECOVERY_SCHEMA or value['notice'] != NOTICE
            or type(value['projects']) is not list or len(value['projects']) > 100000):
        raise ValueError('Invalid Studio input recovery notice.')
    identities = set()
    for row in value['projects']:
        if (type(row) is not dict or set(row) != {'id', 'source_revision', 'source_fingerprint',
                'revision', 'fingerprint', 'retained_inputs', 'unresolved'}
                or type(row['id']) is not str or re.fullmatch(_ID, row['id']) is None
                or row['id'] in identities):
            raise ValueError('Invalid Studio recovery project identity.')
        identities.add(row['id'])
        _number(row['source_revision'], 1)
        _number(row['revision'], row['source_revision'], row['source_revision'] + 1)
        _digest(row['source_fingerprint'])
        _digest(row['fingerprint'])
        if (type(row['retained_inputs']) is not list or len(row['retained_inputs']) > 100000
                or any(type(name) is not str or _INPUT.fullmatch(name) is None for name in row['retained_inputs'])
                or len(set(row['retained_inputs'])) != len(row['retained_inputs'])
                or type(row['unresolved']) is not list or len(row['unresolved']) > 100000):
            raise ValueError('Invalid Studio input recovery inventory.')
        seen = set()
        for item in row['unresolved']:
            if (type(item) is not dict or set(item) != {'scene', 'cue', 'path'}
                    or any(type(item[key]) is not str or re.fullmatch(_ID, item[key]) is None
                           for key in ('scene', 'cue'))):
                raise ValueError('Invalid unresolved Studio clip reference.')
            _text(item['path'])
            identity = item['scene'], item['cue'], item['path']
            if identity in seen:
                raise ValueError('Duplicate unresolved Studio clip reference.')
            seen.add(identity)
    return value


def validate_state(root, entries, *, cancelled=lambda: False):
    """Validate only the complete copied state; return a passive projection plan."""
    inventory = _inventory(entries, cancelled)
    for entry in inventory.values():
        _check(cancelled)
        name = entry['filename']
        if _INPUT.fullmatch(name):
            _verify_input(root, entry, cancelled)
        elif namespace('state/' + name):
            validate_metadata('state/' + name, _json(root, entry))
    receipt_entry = inventory.get(SNAPSHOT)
    if receipt_entry is not None:
        value = _json(root, receipt_entry)
        expected = _snapshot(root, inventory, value['source_state_root'], cancelled)
        if encode(value) != encode(expected):
            raise ValueError('Studio input receipt differs from its exact project/manifest bindings.')
        return expected
    # Even a legacy snapshot must reject malformed typed project identities.
    for entry in inventory.values():
        _check(cancelled)
        _record(root, entry)
    return None


def _entry(name, raw):
    return {'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


def _written(entries, name, value):
    raw = encode(value)
    row = _entry(name, raw)
    result = [item for item in entries if item['filename'].casefold() != name.casefold()]
    result.append(row)
    from ..state_backup import validate_state
    validate_state(result)
    return sorted(result, key=lambda item: item['filename'])


def snapshot_inputs(root, entries, *, source_state_root, cancelled=lambda: False):
    """Add an explicit binding receipt after the existing streaming snapshot."""
    inventory = _inventory(entries, cancelled)
    if SNAPSHOT in inventory:
        raise ValueError('The Studio input snapshot receipt is reserved for complete backups.')
    # The existing snapshot already streamed and rehashed its sources. The
    # complete copied-state validator verifies these bindings and owned bytes
    # before publication; do not open any original Cue path here.
    value = _snapshot(root, inventory, str(source_state_root), cancelled)
    if not value['projects']:
        return entries
    updated = _written(entries, SNAPSHOT, value)
    _check(cancelled)
    atomic_json(Path(root) / SNAPSHOT, value)
    return updated


def _revision_name(name, project, inventory):
    wanted = str(PurePosixPath(name).parent / 'revisions' /
                 f'{project.revision:08d}-{project.fingerprint}.json')
    if wanted.casefold() in inventory:
        return inventory[wanted.casefold()]['filename']
    parents = {str(parent).casefold(): str(parent)
               for entry in inventory.values() for parent in PurePosixPath(entry['filename']).parents}
    parent = str(PurePosixPath(wanted).parent)
    return str(PurePosixPath(parents.get(parent.casefold(), parent)) / PurePosixPath(wanted).name)


def restore_inputs(root, entries, receipt, *, destination_state_root=None, cancelled=lambda: False):
    """Append local editable revisions; leave original histories and jobs intact."""
    from .project import Project
    root = Path(root)
    inventory = _inventory(entries, cancelled)
    destination = (_state_root(str(destination_state_root))
                   if destination_state_root is not None else None)
    bindings = {row['filename'].casefold(): row for row in receipt['projects']} if receipt else {}
    notices, result = [], list(entries)
    for entry in sorted(inventory.values(), key=lambda row: row['filename']):
        _check(cancelled)
        if not _CURRENT.fullmatch(entry['filename']):
            continue
        found = _record(root, entry)
        _, project = found
        mapping = bindings.get(entry['filename'].casefold(), {}).get('inputs', {})
        bound = mapping if destination is not None else {}
        unresolved = [{'scene': scene.id, 'cue': cue.id, 'path': cue.path}
                      for scene in project.scenes for cue in scene.cues if cue.path not in bound]
        retained = sorted({item['filename'] for item in bound.values()})
        if not retained and not unresolved:
            continue
        scenes = tuple(replace(scene, cues=tuple(replace(cue,
            path=str(destination / bound[cue.path]['filename'])) if cue.path in bound else cue
            for cue in scene.cues)) for scene in project.scenes)
        revised = replace(project, scenes=scenes)
        if revised.fingerprint != project.fingerprint:
            revised = replace(revised, revision=project.revision + 1)
            history = _revision_name(entry['filename'], project, inventory)
            existing = inventory.get(history.casefold())
            if existing is not None:
                prior = Project.from_dict(_json(root, existing))
                if prior.fingerprint != project.fingerprint:
                    raise ValueError('Restored Studio revision identities collide.')
            else:
                result = _written(result, history, project.to_dict())
                _check(cancelled)
                atomic_json(root / history, project.to_dict())
            # A private-stage publication still checks the captured current
            # record before replacing it. Later edits use normal revision/CAS.
            _json(root, entry)
            result = _written(result, entry['filename'], revised.to_dict())
            _check(cancelled)
            atomic_json(root / entry['filename'], revised.to_dict(), replace=True)
            # Keep the projection itself as an immutable revision receipt as
            # well as the editable current record. Ordinary later ProjectStore
            # CAS saves can reuse this exact historical identity.
            projected = _revision_name(entry['filename'], revised, inventory)
            existing = inventory.get(projected.casefold())
            if existing is not None:
                prior = Project.from_dict(_json(root, existing))
                if prior.fingerprint != revised.fingerprint:
                    raise ValueError('Restored Studio projection revision identities collide.')
            else:
                result = _written(result, projected, revised.to_dict())
                _check(cancelled)
                atomic_json(root / projected, revised.to_dict())
        notices.append({'id': project.id, 'source_revision': project.revision,
            'source_fingerprint': project.fingerprint, 'revision': revised.revision,
            'fingerprint': revised.fingerprint, 'retained_inputs': retained,
            'unresolved': unresolved})
    if notices:
        value = {'schema': RECOVERY_SCHEMA, 'projects': notices, 'notice': NOTICE}
        validate_recovery(value)
        if len(encode(value)) > MAX_BYTES:
            raise ValueError('Studio recovery notice exceeds its metadata budget.')
        result = _written(result, RECOVERY, value)
        _check(cancelled)
        atomic_json(root / RECOVERY, value, replace=RECOVERY in inventory)
    if receipt is not None:
        _check(cancelled)
        (root / inventory[SNAPSHOT]['filename']).unlink()
        result = [entry for entry in result if entry['filename'].casefold() != SNAPSHOT]
    return result


def _recovery(studio_root):
    path = Path(studio_root) / PurePosixPath(RECOVERY).name
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    value, _ = read_json_snapshot(path, max_bytes=MAX_BYTES)
    return validate_recovery(value)


def recovery_notice(studio_root, project=None):
    """Read a bounded passive notice, without probing any retained/external asset."""
    value = _recovery(studio_root)
    if value is None:
        return ''
    rows = value['projects']
    if project is not None:
        rows = [row for row in rows if row['id'] == project.id]
        present = {(scene.id, cue.id, cue.path) for scene in project.scenes for cue in scene.cues}
        unresolved = sum((item['scene'], item['cue'], item['path']) in present
                         for row in rows for item in row['unresolved'])
    else:
        unresolved = sum(len(row['unresolved']) for row in rows)
    retained = len({name for row in rows for name in row['retained_inputs']})
    return (f'Restored Studio inputs: {retained} retained original(s); '
            f'{unresolved} unavailable clip reference(s). '
            + ('Import the selected originals again in Clips before rendering. ' if unresolved else '')
            + 'Historical production jobs require a new explicitly selected output.')


def require_reviewed_input(studio_root, cue):
    """Reject restored unresolved clip identities before any external path access."""
    value = _recovery(studio_root)
    if value is not None and any(item['cue'] == cue.id and item['path'] == cue.path
            for row in value['projects'] for item in row['unresolved']):
        raise ValueError('This restored clip has no retained input binding. Import its selected '
                         'original again in Clips before rendering.')
