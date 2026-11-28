"""Typed Studio input snapshots inside the existing application-state backup.

Only persisted Project/Cue fields are interpreted. Unrelated application state,
original documents and external output folders are never rewritten. The backup
retains original project bytes; restore creates a verified local projection.
"""
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from uuid import uuid4

from ..durable_state import (atomic_json, digest_file, encode, local_path,
                             read_json, regular, state_lock)
from .project import Project

RECEIPT = 'studio/backup-inputs.json'
REVIEW = 'studio/legacy-input-review.json'
ASSET = re.compile(r'studio/inputs/([0-9a-f]{64})\.audio')
_IDENTIFIER = r'[a-zA-Z0-9_-]{1,80}'
_CURRENT = re.compile(rf'studio/projects/({_IDENTIFIER})/project\.json', re.IGNORECASE)
_REVISION = re.compile(rf'studio/projects/({_IDENTIFIER})/revisions/([0-9]{{8,}})-([0-9a-f]{{64}})\.json',
                       re.IGNORECASE)
_JOB = re.compile(rf'studio/jobs/({_IDENTIFIER})/job\.json', re.IGNORECASE)
_JOB_PROJECT = re.compile(rf'studio/jobs/({_IDENTIFIER})/project\.json', re.IGNORECASE)


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _checked_digest(source, before, cancelled):
    digest, count = sha256(), 0
    if cancelled():
        raise InterruptedError('Studio input verification cancelled.')
    from ..speech.listening import _opened
    with _opened(source, _stamp(before)) as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A required Studio input changed before verification.')
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('Studio input verification cancelled.')
            count += len(block)
            if count > before.st_size:
                raise ValueError('A required Studio input grew during verification.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A required Studio input changed during verification.')
    if count != before.st_size or _stamp(regular(source)) != _stamp(before):
        raise ValueError('A required Studio input changed during verification.')
    return digest.hexdigest()


def _reserved_names(entries):
    for name in entries:
        if name.casefold() in {RECEIPT.casefold(), REVIEW.casefold()} and name not in {RECEIPT, REVIEW}:
            raise ValueError('Studio backup metadata must use its exact reserved filename.')


def _record(root, name):
    current, revision = _CURRENT.fullmatch(name), _REVISION.fullmatch(name)
    job, exported = _JOB.fullmatch(name), _JOB_PROJECT.fullmatch(name)
    if not any((current, revision, job, exported)):
        return None
    value = read_json(Path(root) / name)
    # Historical opaque sidecars are still copied exactly. A schema-1 Studio
    # record, however, must be a valid project/job before its paths are used.
    if job and isinstance(value, dict) and 'schema' not in value and 'project' not in value:
        return None
    try:
        project = Project.from_dict(value['project'] if job else value)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('Invalid typed Studio project in application state.') from exc
    if current and project.id != current[1]:
        raise ValueError('Studio project directory does not match its identity.')
    if revision and (project.id != revision[1] or project.revision != int(revision[2])
                     or project.fingerprint != revision[3]):
        raise ValueError('Studio revision filename does not match its content.')
    if job and (type(value.get('schema')) is not int or value['schema'] != 1
                or value.get('id') != job[1] or value.get('fingerprint') != project.fingerprint):
        raise ValueError('Studio job does not match its project identity.')
    return ('job' if job else 'revision' if revision else 'project'), value, project


def _input_paths(project):
    return {cue.path for scene in project.scenes for cue in scene.cues}


def input_review(studio_root):
    """Local approval state, never inherited as authority from a backup."""
    path = Path(studio_root) / Path(REVIEW).name
    if not os.path.lexists(path):
        return {'schema': 1, 'pending': [], 'approved': {}}
    value = read_json(path)
    if (not isinstance(value, dict) or set(value) != {'schema', 'pending', 'approved'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['pending'], list)
            or any(not isinstance(item, str) or not item for item in value['pending'])
            or len(set(value['pending'])) != len(value['pending'])
            or not isinstance(value['approved'], dict)
            or set(value['pending']) & set(value['approved'])):
        raise ValueError('Invalid restored Studio input-review state.')
    for source, identity in value['approved'].items():
        if (not isinstance(source, str) or not source or not isinstance(identity, dict)
                or set(identity) != {'size_bytes', 'sha256'}
                or type(identity['size_bytes']) is not int or identity['size_bytes'] <= 0
                or not isinstance(identity['sha256'], str)
                or re.fullmatch('[0-9a-f]{64}', identity['sha256']) is None):
            raise ValueError('Invalid restored Studio input approval.')
    return value



def register_pending_inputs(studio_root, selected_paths):
    """Withdraw inherited path authority without opening those declarations."""
    if isinstance(selected_paths, (str, bytes)):
        raise ValueError('Select an explicit collection of Studio input paths.')
    selected = set()
    for number, raw in enumerate(selected_paths):
        if number >= 100_000:
            raise ValueError('Studio input review exceeds its item budget.')
        if (type(raw) is not str or not raw or len(raw) > 32768
                or any(ord(char) < 32 for char in raw)
                or raw.startswith(('//', '\\\\'))
                or not (PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute())):
            raise ValueError('Studio input review needs absolute local path declarations.')
        selected.add(raw)
    if not selected:
        return input_review(studio_root)
    with state_lock(studio_root, 'input-review'):
        value = input_review(studio_root)
        pending = set(value['pending']) | selected
        if len(pending) + len(set(value['approved']) - selected) > 100_000:
            raise ValueError('Studio input review exceeds its item budget.')
        value['pending'] = sorted(pending)
        for raw in selected:
            value['approved'].pop(raw, None)
        atomic_json(Path(studio_root) / Path(REVIEW).name, value, replace=True)
        return value

def approve_input_review(studio_root, selected, *, cancelled=lambda: False):
    """Record explicit local file selection, bound to the approved current bytes."""
    selected = tuple(str(path) for path in selected)
    with state_lock(studio_root, 'input-review'):
        value = input_review(studio_root)
        known = set(value['pending']) | set(value['approved'])
        if not selected or any(path not in known for path in selected):
            raise ValueError('Select the original local files listed for restored input review.')
        identities = {}
        for raw in selected:
            if not Path(raw).is_absolute():
                raise ValueError('Select an explicit absolute local Studio input path.')
            source = local_path(raw, exists=True)
            before = regular(source)
            digest = _checked_digest(source, before, cancelled)
            if before.st_size <= 0 or _stamp(regular(source)) != _stamp(before):
                raise ValueError('A selected Studio input is empty or changed during review.')
            identities[raw] = {'size_bytes': before.st_size, 'sha256': digest}
        value['approved'].update(identities)
        value['pending'] = sorted(set(value['pending']) - set(selected))
        atomic_json(Path(studio_root) / Path(REVIEW).name, value, replace=True)
        return value


def _waiting_job(value, original, rebound, *, retained):
    value = dict(value)
    previous = {'project_fingerprint': original.fingerprint,
                'status': value.get('status'), 'destination': value.get('destination'),
                'artifacts': value.get('artifacts', [])}
    recovery = value.get('recovery')
    if value.get('status') == 'waiting-input' and recovery is not None:
        if (type(recovery) is not dict
                or set(recovery) != {'requires_output_directory', 'inputs_retained', 'previous'}
                or recovery['requires_output_directory'] is not True
                or type(recovery['inputs_retained']) is not bool
                or type(recovery['previous']) is not dict
                or set(recovery['previous']) != {'project_fingerprint', 'status', 'destination', 'artifacts'}):
            raise ValueError('Invalid historical Studio recovery provenance.')
        saved = recovery['previous']
        if (type(saved['project_fingerprint']) is not str
                or re.fullmatch('[0-9a-f]{64}', saved['project_fingerprint']) is None
                or type(saved['status']) is not str or len(saved['status']) > 80
                or type(saved['destination']) is not str or len(saved['destination']) > 32768
                or type(saved['artifacts']) is not list):
            raise ValueError('Invalid historical Studio recovery provenance.')
        previous = dict(saved)
    value.update(project=rebound.to_dict(), fingerprint=rebound.fingerprint,
                 status='waiting-input', destination='', artifacts=[], nodes={}, runtime=None,
                 recovery={'requires_output_directory': True, 'inputs_retained': retained,
                           'previous': previous},
                 progress=('Inputs restored. ' if retained else
                           'Older backup did not retain external inputs; reselect available originals. ')
                          + 'Choose a new output directory before rendering.', error='')
    value.pop('result', None)
    return value


def snapshot_inputs(target, entries, *, max_bytes, max_files, cancelled):
    """Retain every required Cue input and bind it to copied project snapshots."""
    target = Path(target)
    by_name = {entry['filename']: entry for entry in entries}
    _reserved_names(by_name)
    if RECEIPT in by_name:
        raise ValueError('The Studio backup-input receipt is reserved for backup snapshots.')
    records, paths = [], set()
    for name in sorted(by_name):
        found = _record(target, name)
        if found is None:
            continue
        kind, _value, project = found
        records.append({'filename': name, 'kind': kind,
                        'source_sha256': by_name[name]['sha256'],
                        'project_fingerprint': project.fingerprint})
        paths.update(_input_paths(project))
    if not records:
        return entries
    review = input_review(target / 'studio')
    if paths & set(review['pending']):
        raise ValueError('Review and explicitly approve legacy Studio inputs before backing them up.')
    total = sum(entry['size_bytes'] for entry in entries)
    sources, inputs = {}, {}
    for raw in sorted(paths):
        if cancelled():
            raise InterruptedError('Backup cancelled.')
        if not Path(raw).is_absolute():
            raise ValueError('A Studio cue needs an explicit absolute local input path.')
        source = local_path(raw, exists=True)
        before = regular(source)
        if not 0 < before.st_size <= max_bytes:
            raise ValueError('Studio input is empty or exceeds the backup byte budget.')
        directory = target / 'studio' / 'inputs'
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ('.pending-' + uuid4().hex)
        digest, count = sha256(), 0
        try:
            with source.open('rb') as read, temporary.open('xb') as write:
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Studio input changed before backup; retry.')
                while block := read.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('Backup cancelled.')
                    count += len(block)
                    if count > before.st_size or count > max_bytes:
                        raise ValueError('Studio input changed or exceeds the backup byte budget.')
                    digest.update(block)
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Studio input changed during backup; retry.')
            if count != before.st_size or _stamp(regular(source)) != _stamp(before):
                raise ValueError('Studio input changed during backup; retry.')
            name = 'studio/inputs/' + digest.hexdigest() + '.audio'
            entry = {'filename': name, 'size_bytes': count, 'sha256': digest.hexdigest()}
            previous = sources.get(source)
            if previous is not None and (_stamp(previous[0]), previous[1]) != (
                    _stamp(before), entry['sha256']):
                raise ValueError('Aliased Studio input paths changed during backup; retry.')
            sources[source] = (before, entry['sha256'])
            if raw in review['approved'] and review['approved'][raw] != {
                    'size_bytes': count, 'sha256': entry['sha256']}:
                raise ValueError('A reviewed legacy Studio input changed; select it again before backup.')
            destination = target / name
            if name in by_name:
                if by_name[name] != entry or _checked_digest(destination, regular(destination), cancelled) != entry['sha256']:
                    raise ValueError('A retained Studio input differs from its content identity.')
            else:
                if len(by_name) + 1 >= max_files or total + count > max_bytes:
                    raise ValueError('Studio inputs exceed the backup budget.')
                os.link(temporary, destination)
                by_name[name] = entry
                total += count
            inputs[raw] = entry
        finally:
            temporary.unlink(missing_ok=True)
    for source, (before, expected) in sources.items():
        if (_stamp(regular(source)) != _stamp(before)
                or _checked_digest(source, before, cancelled) != expected):
            raise ValueError('A required Studio input changed before backup completion; retry.')
    receipt = {'schema': 1, 'projects': records, 'inputs': inputs,
               'external_outputs': 'not-copied; choose a new output directory after restore'}
    raw = encode(receipt)
    if total + len(raw) > max_bytes or len(by_name) + 1 > max_files:
        raise ValueError('Studio input receipt exceeds the backup budget.')
    atomic_json(target / RECEIPT, receipt)
    by_name[RECEIPT] = {'filename': RECEIPT, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
    return sorted(by_name.values(), key=lambda entry: entry['filename'])


def _cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Studio input recovery cancelled.')


def _validated_inputs(stage, entries, *, cancelled=lambda: False):
    """Validate only copied state; recorded external source paths stay unopened."""
    _cancelled(cancelled)
    stage = Path(stage)
    by_name = {entry['filename']: entry for entry in entries}
    _reserved_names(by_name)
    if RECEIPT not in by_name:
        return None, {}
    receipt = read_json(stage / RECEIPT)
    if (type(receipt) is not dict or set(receipt) != {'schema', 'projects', 'inputs', 'external_outputs'}
            or type(receipt['schema']) is not int or receipt['schema'] != 1
            or not isinstance(receipt['projects'], list) or not isinstance(receipt['inputs'], dict)
            or receipt['external_outputs'] != 'not-copied; choose a new output directory after restore'):
        raise ValueError('Invalid Studio input backup receipt.')
    actual = {}
    for name in sorted(by_name):
        _cancelled(cancelled)
        record = _record(stage, name)
        if record is not None:
            actual[name] = record
    listed, required = set(), set()
    for record in receipt['projects']:
        _cancelled(cancelled)
        if (not isinstance(record, dict) or set(record) != {
                'filename', 'kind', 'source_sha256', 'project_fingerprint'}
                or not isinstance(record['filename'], str) or record['filename'] in listed
                or record['filename'] not in actual):
            raise ValueError('Invalid or duplicate Studio project binding.')
        name = record['filename']
        kind, _value, project = actual[name]
        if (record['kind'] != kind or record['source_sha256'] != by_name[name]['sha256']
                or record['project_fingerprint'] != project.fingerprint):
            raise ValueError('Studio project binding differs from its backed-up bytes.')
        listed.add(name)
        required.update(_input_paths(project))
    if listed != set(actual) or set(receipt['inputs']) != required:
        raise ValueError('Missing or extra Studio input/project bindings.')
    for original, entry in receipt['inputs'].items():
        _cancelled(cancelled)
        if (not isinstance(original, str) or not isinstance(entry, dict)
                or set(entry) != {'filename', 'size_bytes', 'sha256'}
                or not isinstance(entry['filename'], str)
                or not (match := ASSET.fullmatch(entry['filename']))
                or match[1] != entry['sha256'] or by_name.get(entry['filename']) != entry
                or type(entry['size_bytes']) is not int or entry['size_bytes'] <= 0
                or _checked_digest(stage / entry['filename'], regular(stage / entry['filename']), cancelled) != entry['sha256']):
            raise ValueError('A required Studio input is missing or differs from its receipt.')
    return receipt, actual


def inspect_snapshot_inputs(stage, entries, *, cancelled=lambda: False):
    """Return a verified retained-input receipt, or None for historical state."""
    receipt, _ = _validated_inputs(stage, entries, cancelled=cancelled)
    return receipt


def restore_inputs(stage, destination, entries, *, cancelled=lambda: False):
    """Rebind typed project inputs only after the complete state hashes pass."""
    _cancelled(cancelled)
    stage, destination = Path(stage), Path(destination)
    by_name = {entry['filename']: entry for entry in entries}
    _reserved_names(by_name)
    if RECEIPT not in by_name:
        # Legacy snapshots have no retained input bindings. Keep their project
        # paths intact, but never carry old external output authority forward.
        pending = set()
        for name in by_name:
            _cancelled(cancelled)
            record = _record(stage, name)
            if record is not None:
                kind, value, project = record
                pending.update(_input_paths(project))
                if kind == 'job':
                    atomic_json(stage / name, _waiting_job(value, project, project, retained=False),
                                replace=True)
        # Paths in an old backup describe its source machine. A later backup
        # or render must not treat them as local file-selection permission.
        if pending or REVIEW in by_name:
            atomic_json(stage / REVIEW, {'schema': 1, 'pending': sorted(pending), 'approved': {}},
                        replace=REVIEW in by_name)
        return
    receipt, actual = _validated_inputs(stage, entries, cancelled=cancelled)
    for name, (kind, value, project) in actual.items():
        _cancelled(cancelled)
        revised = project.to_dict()
        for scene in revised['scenes']:
            for cue in scene['cues']:
                cue['path'] = str(destination / receipt['inputs'][cue['path']]['filename'])
        rebound = Project.from_dict(revised)
        output_name = name
        if kind == 'job':
            value = _waiting_job(value, project, rebound, retained=True)
        else:
            value = revised
            if kind == 'revision':
                output_name = str(Path(name).with_name(
                    f'{rebound.revision:08d}-{rebound.fingerprint}.json')).replace(os.sep, '/')
        target = stage / output_name
        if output_name != name and target.exists():
            if read_json(target) != value:
                raise ValueError('Restored Studio revision identities collide.')
        else:
            atomic_json(target, value, replace=output_name == name)
        if output_name != name:
            (stage / name).unlink()
    # The receipt describes the immutable backup, not these rebound live paths.
    # Removing it also ensures the next backup creates a fresh exact receipt.
    _cancelled(cancelled)
    (stage / RECEIPT).unlink()
    # Approvals apply to the source machine only; retained inputs need none.
    (stage / REVIEW).unlink(missing_ok=True)
