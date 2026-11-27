"""Allowlisted assistant checkpoints for the existing complete-library backup.

Call while holding the application writer and assistant state guards. The complete
backup also locks jobs, borrowing only its own live same-process/thread execution
guard. Only durable publications are captured, with original permissions/paths.
"""
import os
from pathlib import Path
import re
import stat

from ..backup_files import optional_present
from .state import read_bytes, regular
from .validation import validate_state_document


DIRECTORY = 'state/assistant'
MAX_JOBS = 1000
MAX_JOB_BYTES = 64 * 1024 * 1024
MAX_CONTROL_BYTES = 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_DOCUMENT = re.compile(r'([0-9a-f]{32})(\.control)?\.json')
_TRANSIENT = re.compile(r'(?:\.state\.lock|[0-9a-f]{32}\.lock|\.[0-9a-f]{32}(?:\.control)?\.json\.[0-9a-f]{32}\.tmp)')


def is_state_path(relative):
    return (isinstance(relative, str) and relative.startswith(DIRECTORY + '/')
            and _DOCUMENT.fullmatch(relative[len(DIRECTORY) + 1:]) is not None)


def state_limit(relative):
    if not is_state_path(relative):
        raise ValueError('Unsupported assistant backup sidecar.')
    return MAX_CONTROL_BYTES if relative.endswith('.control.json') else MAX_JOB_BYTES


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Assistant state needs ordinary local directories.')


def state_paths(root, *, cancelled=lambda: False):
    """Return a bounded inventory; do not silently omit unknown committed files."""
    root = Path(root)
    folder = root
    for part in DIRECTORY.split('/'):
        folder = folder / part
        if not optional_present(folder):
            return ()
        _directory(folder)
    paths, jobs, controls, total, entries = [], set(), set(), 0, 0
    with os.scandir(folder) as children:
        for child in children:
            path = Path(child.path)
            if cancelled():
                raise InterruptedError('Assistant checkpoint snapshot cancelled.')
            entries += 1
            if entries > MAX_JOBS * 4 + 16:
                raise ValueError('Assistant checkpoint directory exceeds its entry budget.')
            # A disposable-looking name never permits following a link or
            # ignoring a directory inside this private namespace.
            info = regular(path)
            if _TRANSIENT.fullmatch(path.name):
                continue
            match = _DOCUMENT.fullmatch(path.name)
            if match is None:
                raise ValueError('Unexpected file in the assistant checkpoint directory.')
            relative = path.relative_to(root).as_posix()
            if not 0 < info.st_size <= state_limit(relative):
                raise ValueError('Assistant checkpoint exceeds its file budget.')
            total += info.st_size
            (controls if match[2] else jobs).add(match[1])
            if len(jobs) > MAX_JOBS or len(controls) > MAX_JOBS or total > MAX_SNAPSHOT_BYTES:
                raise ValueError('Assistant checkpoints exceed their snapshot budget.')
            paths.append(path)
    if controls - jobs:
        raise ValueError('Assistant control checkpoint has no saved job.')
    return tuple(sorted(paths))


def _workspace(db, workspace):
    if not isinstance(workspace, str) or not db.execute(
            'SELECT 1 FROM workspace WHERE id=?', (workspace,)).fetchone():
        raise ValueError('Assistant checkpoint workspace disagrees with the backup database.')


def _edition(db, workspace, edition, *, revision=None, asset=None, work=None):
    if not isinstance(edition, str):
        raise ValueError('Invalid source edition in assistant checkpoint.')
    row = db.execute('SELECT asset, work FROM catalog_edition WHERE workspace=? AND id=?',
                     (workspace, edition)).fetchone()
    if row is None or (asset is not None and row[0] != asset) or (work is not None and row[1] != work):
        raise ValueError('Assistant source edition disagrees with its approved workspace.')
    if revision is not None and (not isinstance(revision, str) or not db.execute('''
            SELECT 1 FROM revision r JOIN sealed s ON s.revision=r.id
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?''',
            (workspace, edition, revision, row[0])).fetchone()):
        raise ValueError('Assistant source revision disagrees with the backup database.')


def _rows(value, name):
    rows = value.get(name)
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise ValueError('Invalid assistant source-result inventory.')
    return rows


def validate_sources(db, record):
    """Check committed source identities; unexecuted requests stay requests.

    Archived workspaces and sealed historical revisions remain valid history.
    This never substitutes the active revision or promotes a saved result to a
    newly verified quotation. External inputs/outputs are not opened here.
    """
    workspace = record['workspace']
    if workspace is not None:
        _workspace(db, workspace)
    for step, checkpoint in zip(record['plan']['steps'], record['steps']):
        result = checkpoint['result']
        if result is None:
            continue
        tool = step['tool']
        if tool == 'library.create':
            if result.get('workspace') != workspace or workspace is None:
                raise ValueError('Created-library result disagrees with its checkpoint.')
        elif tool == 'library.list':
            scoped = record['plan']['scope']['workspace_id']
            for row in _rows(result, 'libraries'):
                _workspace(db, row.get('id'))
                if scoped is not None and row['id'] != scoped:
                    raise ValueError('Assistant listed a library outside its approved scope.')
        elif tool == 'library.catalog':
            for row in _rows(result, 'books'):
                _edition(db, workspace, row.get('edition_id'), asset=row.get('asset'), work=row.get('work_id'))
        elif tool == 'library.search':
            for row in _rows(result, 'results'):
                if row.get('workspace') != workspace or not isinstance(row.get('revision'), str):
                    raise ValueError('Assistant search result is outside its approved source scope.')
                _edition(db, workspace, row.get('edition_id'), revision=row['revision'])
        elif tool.startswith('library.import'):
            editions = result.get('editions')
            if type(editions) is not list or result.get('batch_id') != checkpoint['intent'].get('batch'):
                raise ValueError('Assistant import result disagrees with its saved intent.')
            for edition in editions:
                _edition(db, workspace, edition)
        elif tool == 'library.extract':
            if result.get('reading_copy') is not True or not isinstance(result.get('source_revision'), str):
                raise ValueError('Invalid assistant reading-copy source result.')
            _edition(db, workspace, step['arguments']['edition'], revision=result['source_revision'])


def validate_state_tree(db, root, *, cancelled=lambda: False):
    """Validate one document at a time; return no external file capabilities."""
    root = Path(root)
    for path in state_paths(root, cancelled=cancelled):
        relative = path.relative_to(root).as_posix()
        raw = read_bytes(path, maximum=state_limit(relative))
        value = validate_state_document(path.name, raw)
        if not path.name.endswith('.control.json'):
            validate_sources(db, value)
