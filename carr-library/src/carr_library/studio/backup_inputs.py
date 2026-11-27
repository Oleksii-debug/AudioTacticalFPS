"""Typed Studio input snapshots inside the existing application-state backup.

Only persisted Project/Cue fields are interpreted. Unrelated application state,
original documents and external output folders are never rewritten. The backup
retains original project bytes; restore creates a verified local projection.
"""
from hashlib import sha256
import os
from pathlib import Path
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
    with source.open('rb') as stream:
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


def validate_input_review(value):
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


def input_review(studio_root):
    """Local approval state, never inherited as authority from a backup."""
    path = Path(studio_root) / Path(REVIEW).name
    if not os.path.lexists(path):
        return {'schema': 1, 'pending': [], 'approved': {}}
    value = read_json(path)
    return validate_input_review(value)


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


def review_project_inputs(studio_root, project, *, input_roots=(), cancelled=lambda: False):
    """Record path references without treating an imported project as permission.

    Explicitly selected input directories may admit a new local file. Pending
    references and existing approvals retain their meaning until the user
    separately reviews them; opening or resubmitting a project cannot renew them.
    """
    studio_root = Path(studio_root)
    selected_roots = tuple(local_path(root, exists=True) for root in input_roots)
    with state_lock(studio_root, 'input-review'):
        value = input_review(studio_root)
        before = encode(value)
        pending = set(value['pending'])
        for raw in sorted(_input_paths(project)):
            if cancelled():
                raise InterruptedError('Studio input verification cancelled.')
            if raw in pending or raw in value['approved']:
                continue
            path = Path(raw)
            if (path.is_absolute() and path.parent == studio_root / 'inputs'
                    and re.fullmatch(r'[0-9a-f]{64}\.audio', path.name)):
                # Managed bytes are checked against their content identity by
                # render and by the existing state snapshot, not approved here.
                continue
            if (not path.is_absolute()
                    or not any(path.is_relative_to(root) for root in selected_roots)):
                pending.add(raw)
                continue
            source = local_path(path, exists=True)
            if not any(source.is_relative_to(root) for root in selected_roots):
                pending.add(raw)
                continue
            stamp = regular(source)
            if stamp.st_size <= 0:
                raise ValueError('A selected Studio input is empty.')
            value['approved'][raw] = {
                'size_bytes': stamp.st_size, 'sha256': _checked_digest(source, stamp, cancelled)}
        value['pending'] = sorted(pending)
        if encode(value) != before:
            atomic_json(studio_root / Path(REVIEW).name, value, replace=True)
        return value


def _waiting_job(value, original, rebound, *, retained):
    value = dict(value)
    previous = {'project_fingerprint': original.fingerprint,
                'status': value.get('status'), 'destination': value.get('destination'),
                'artifacts': value.get('artifacts', [])}
    value.update(project=rebound.to_dict(), fingerprint=rebound.fingerprint,
                 status='waiting-input', destination='', artifacts=[], nodes={}, runtime=None,
                 recovery={'requires_output_directory': True, 'inputs_retained': retained,
                           'previous': previous},
                 progress=('Inputs restored. ' if retained else
                           'Older backup did not retain external inputs; reselect available originals. ')
                          + 'Choose a new output directory before rendering.', error='')
    value.pop('result', None)
    return value


def snapshot_inputs(target, entries, *, source_root, max_bytes, max_files, cancelled):
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
        raise ValueError('Review and explicitly approve Studio inputs before backing them up.')
    total = sum(entry['size_bytes'] for entry in entries)
    sources, inputs = {}, {}
    for raw in sorted(paths):
        if cancelled():
            raise InterruptedError('Backup cancelled.')
        if not Path(raw).is_absolute():
            raise ValueError('A Studio cue needs an explicit absolute local input path.')
        if raw not in review['approved']:
            # A restored managed input is already copied and verified by the
            # state snapshot. A filename from any other directory grants no
            # authority to open it, even when a project was saved locally.
            try:
                retained_name = Path(raw).relative_to(source_root).as_posix()
            except ValueError:
                retained_name = ''
            retained = by_name.get(retained_name)
            match = ASSET.fullmatch(retained_name)
            if (match is None or retained is None or retained['sha256'] != match[1]
                    or retained['size_bytes'] <= 0):
                raise ValueError('Review and explicitly approve Studio inputs before backing them up.')
            inputs[raw] = retained
            continue
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
                if by_name[name] != entry or digest_file(destination) != entry['sha256']:
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


def validate_backup_inputs(stage, entries, *, cancelled=lambda: False):
    """Authenticate staged project/input bindings without following external paths."""
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
        record = _record(stage, name)
        if record is not None:
            actual[name] = record
    listed, required = set(), set()
    for record in receipt['projects']:
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
        if (not isinstance(original, str) or not isinstance(entry, dict)
                or set(entry) != {'filename', 'size_bytes', 'sha256'}
                or not isinstance(entry['filename'], str)
                or not (match := ASSET.fullmatch(entry['filename']))
                or match[1] != entry['sha256'] or by_name.get(entry['filename']) != entry
                or type(entry['size_bytes']) is not int or entry['size_bytes'] <= 0
                or digest_file(stage / entry['filename'], cancelled=cancelled) != entry['sha256']):
            raise ValueError('A required Studio input is missing or differs from its receipt.')
    return receipt, actual


def restore_inputs(stage, destination, entries, *, cancelled=lambda: False):
    """Rebind typed project inputs only after the complete state hashes pass."""
    stage, destination = Path(stage), Path(destination)
    by_name = {entry['filename']: entry for entry in entries}
    _reserved_names(by_name)
    if RECEIPT not in by_name:
        # Legacy snapshots have no retained input bindings. Keep their project
        # paths intact, but never carry old external output authority forward.
        pending = set()
        for name in by_name:
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
    receipt, actual = validate_backup_inputs(stage, entries, cancelled=cancelled)
    for name, (kind, value, project) in actual.items():
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
    (stage / RECEIPT).unlink()
    # Approvals apply to the source machine only; retained inputs need none.
    (stage / REVIEW).unlink(missing_ok=True)
