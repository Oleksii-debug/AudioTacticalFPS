"""Independent, receipt-bound copies of existing managed book-part production."""
from hashlib import sha256
import os
from pathlib import Path
import shutil
from tempfile import mkdtemp

from ..backup_files import optional_present
from ..operations import ResourceRequest
from .jobs import (checked_directory, regular_file, file_identity, read_json,
                   publish_new, publish_noreplace, _sync_directory)
from .models import digest
from .production import _verified_output
from .book_plan import encode


def _directory_identity(path):
    selected = checked_directory(path)
    info = selected.stat()
    return selected, (info.st_dev, info.st_ino)


def output_parent(value, private_root):
    parent, identity = _directory_identity(value)
    private = checked_directory(private_root)
    if parent == private or parent.is_relative_to(private) or private.is_relative_to(parent):
        raise ValueError('Choose an existing export folder outside and not containing private library data.')
    return parent, identity


def output_name(plan, index):
    title = plan.project(index).title
    safe = ''.join('_' if c in '/\\:*?"<>|' or ord(c) < 32 or 127 <= ord(c) < 160 else c for c in title)
    safe = safe.strip(' .')[:110].rstrip(' .') or 'Book part'
    return safe + '-' + plan.job_id(index)[:12]


def _source(plan, index, result, root, cancelled):
    root = checked_directory(root)
    run_key = result.get('run_key') if type(result) is dict else None
    if type(run_key) is not str or len(run_key) != 64 or any(c not in '0123456789abcdef' for c in run_key):
        raise ValueError('The managed production returned no accepted run identity.')
    directory = checked_directory(root / 'renders' / run_key)
    receipt = _verified_output(directory, run_key, cancelled)
    if (receipt is None or receipt.get('plan_sha256') != digest(plan.project(index).to_dict())
            or receipt.get('source_sha256') != plan.project(index).source_sha256):
        raise ValueError('The completed managed audio differs from this reviewed part.')
    return directory, receipt


def _provenance(plan, index, receipt):
    return {'schema': 'carr-book-part-export/1', 'reference': plan.reference(index),
            'production_plan_sha256': receipt['plan_sha256'], 'run_key': receipt['run_key'],
            'completion_sha256': sha256(encode(receipt)).hexdigest(),
            'source': plan.provenance(index), 'quotation_authority': False}


def _existing(path, receipt, provenance, cancelled):
    checked_directory(path)
    names = {row['name'] for row in receipt['files']} | {'complete.json', 'book-source.json'}
    actual = set()
    with os.scandir(path) as entries:
        for entry in entries:
            if cancelled():
                raise InterruptedError('Book export verification cancelled.')
            if len(actual) >= len(names) or entry.name not in names:
                raise ValueError('The existing part export contains changed or unrelated files.')
            actual.add(entry.name)
            regular_file(path / entry.name)
    if (actual != names or read_json(path / 'complete.json', limit=16 * 1024**2) != receipt
            or read_json(path / 'book-source.json', limit=4 * 1024**2) != provenance
            or _verified_output(path, receipt['run_key'], cancelled) != receipt):
        raise ValueError('The existing part export differs from the accepted completed production.')
    _sync_directory(path.parent)


def export_part(plan, index, result, root, parent, parent_identity, *,
                broker, cancelled=lambda: False, source_guard=lambda: None):
    """No stored output authority; caller must pass this invocation's reviewed parent."""
    parent = Path(parent)
    target = parent / output_name(plan, index)
    stage = None
    stage_identity = None
    committed = False
    active_lease = None
    completed = None
    def stopping():
        if cancelled():
            return True
        if active_lease is not None:
            active_lease.checkpoint()
        selected, identity = _directory_identity(parent)
        if selected != parent or identity != parent_identity:
            raise ValueError('The reviewed export parent changed.')
        if stage is not None and optional_present(stage):
            _, current = _directory_identity(stage)
            if current != stage_identity:
                raise ValueError('The owned book export stage changed.')
        return False
    def final_guard():
        if stopping():
            return True
        source_guard()
        return stopping()
    if final_guard():
        raise InterruptedError('Book export cancelled before copying.')
    source, receipt = _source(plan, index, result, root, stopping)
    provenance = _provenance(plan, index, receipt)
    if optional_present(target):
        _existing(target, receipt, provenance, stopping)
        if final_guard():
            raise InterruptedError('Book export reuse cancelled before acknowledgement.')
        return {'status': 'complete', 'destination': str(target), 'reused': True,
                'run_key': receipt['run_key'], 'project': plan.job_id(index)}
    size = sum(row['size'] for row in receipt['files']) + len(encode(receipt)) + len(encode(provenance))
    request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2,
                              disk_bytes=size + 1024**2, disk_path=parent)
    try:
        with broker.reserve(request, operation_id='studio-export:' + plan.job_id(index),
                            cancelled=cancelled, timeout=60) as lease:
            active_lease = lease
            if final_guard():
                raise InterruptedError('Book export cancelled before copying.')
            stage = Path(mkdtemp(prefix='.book-export-', dir=parent))
            _, stage_identity = _directory_identity(stage)
            try:
                for row in receipt['files']:
                    if stopping():
                        raise InterruptedError('Book export cancelled during bounded copy.')
                    publish_new(source / row['name'], stage / row['name'],
                                cancelled=stopping, expected_identity=file_identity(regular_file(source / row['name'])))
                from .jobs import atomic_json
                atomic_json(stage / 'complete.json', receipt)
                atomic_json(stage / 'book-source.json', provenance)
                if _source(plan, index, result, root, stopping)[1] != receipt:
                    raise ValueError('The accepted production changed during export.')
                _existing(stage, receipt, provenance, stopping)
                try:
                    publish_noreplace(stage, target, cancelled=final_guard)
                    committed = True
                except BaseException:
                    # A post-move directory-sync failure has a visible publication.
                    # Never overwrite it or misreport it as an uncommitted retry.
                    if not optional_present(stage) and optional_present(target):
                        _, current = _directory_identity(target)
                        if current == stage_identity:
                            committed = True
                            raise BookExportPublished(target)
                    raise
                completed = {'status': 'complete', 'destination': str(target), 'reused': False,
                             'run_key': receipt['run_key'], 'project': plan.job_id(index)}
            finally:
                if not committed and stage is not None and optional_present(stage):
                    try:
                        if _directory_identity(stage)[1] == stage_identity:
                            shutil.rmtree(stage)
                    except OSError:
                        pass
    
    except BaseException:
        if completed is None:
            raise
        completed['cleanup_warning'] = 'The export is complete; resource cleanup needs attention.'
    return completed


class BookExportPublished(OSError):
    def __init__(self, destination):
        self.destination = Path(destination)
        super().__init__('The part export is visible, but its durability acknowledgement failed. '
                         'Do not replace it; review the existing receipt before retrying.')
