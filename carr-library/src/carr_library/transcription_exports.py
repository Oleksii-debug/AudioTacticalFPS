"""Independent, new-only exports of already verified retained transcripts.

No runtime or original source path is opened. Publication is a single owned
directory move; the receipt records saved inference, never a new model run.
"""
from contextlib import ExitStack
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from .audio import jobs
from .audio.model_manifest import digest_file, local_path
from .audio.paths import filename_component
from .backup_files import optional_present
from .operations import ResourceBroker, ResourceRequest
from .transcription_state import DIRECTORY, check_cancelled, relative_path, regular

FORMATS = ('txt', 'srt', 'vtt', 'json')
SCHEMA = 'carr-transcription-export/1'


class PublishedExportError(OSError):
    """The complete directory was moved, but durability is not confirmed."""
    def __init__(self, destination):
        self.destination = Path(destination)
        super().__init__('The transcript export was published but its directory sync failed. '
                         'Keep the destination for verification; do not retry over it.')


def _directory_identity(path):
    info = jobs.checked_directory(path).stat()
    return info.st_dev, info.st_ino


def export_results(service, identity, destination, *, formats=None, cancelled=lambda: False):
    """Copy exact accepted formats under current job/worker guards, in bounded blocks."""
    chosen = None if formats is None else tuple(formats)
    if chosen is not None and (not chosen or any(type(value) is not str or value not in FORMATS for value in chosen)
            or len(set(chosen)) != len(chosen)):
        raise ValueError('Choose distinct saved TXT, SRT, VTT or JSON formats.')
    destination = local_path(destination, must_exist=False)
    filename_component(destination.name)
    parent = jobs.checked_directory(destination.parent)
    parent_identity = _directory_identity(parent)
    root = jobs.checked_directory(service.library_root)
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('Choose a new export directory outside the private library.')
    if optional_present(destination):
        raise FileExistsError('Choose a new export directory; existing files are never replaced.')
    job = service._job(identity)
    stage = None
    published = False
    result = None
    warning = None
    try:
        # Match the supervisor's namespace -> job -> standalone-worker lock order.
        with jobs.project_lock(root / DIRECTORY), jobs.project_lock(job), ExitStack() as guards:
            guards.enter_context(jobs.project_lock(job / 'logs', lock_name='.worker-v5.lock'))
            summary = service.completed(identity, cancelled=cancelled)
            rows = [dict(row) for row in summary['outputs'] if chosen is None or row['format'] in chosen]
            if not rows:
                raise ValueError('The saved completion contains none of the selected formats.')
            if chosen is not None and any(format not in {row['format'] for row in rows} for format in chosen):
                raise ValueError('A selected format is absent; export only formats in this saved completion.')
            amount = sum(row['bytes'] for row in rows)
            request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2,
                disk_path=parent, disk_bytes=amount + 16 * 1024**2)
            broker = ResourceBroker.for_app(root, cancelled=cancelled)
            with broker.reserve(request, operation_id='document:' + uuid4().hex,
                                cancelled=cancelled, timeout=60) as lease:
                def stopping():
                    check_cancelled(cancelled)
                    lease.checkpoint()
                    if _directory_identity(parent) != parent_identity:
                        raise ValueError('The selected export parent changed after review.')
                    if stage is not None and _directory_identity(stage) != stage_identity:
                        raise ValueError('The owned export stage changed before publication.')
                    return False
                stopping()
                if (_directory_identity(parent) != parent_identity or optional_present(destination)):
                    raise ValueError('The selected export destination changed before copying.')
                stage = Path(tempfile.mkdtemp(prefix='.transcription-export-', dir=parent))
                stage_identity = _directory_identity(stage)
                try:
                    originals = jobs.checked_directory(summary['results_root'])
                    for row in rows:
                        stopping()
                        parts = relative_path(row['path']).parts
                        source = originals.joinpath(*parts)
                        captured = jobs.file_identity(regular(source))
                        target = stage.joinpath('files', *parts)
                        jobs.checked_directory(target.parent, create=True)
                        jobs.publish_new(source, target, expected_identity=captured, cancelled=stopping)
                        if (regular(target).st_size != row['bytes']
                                or digest_file(target, max_bytes=row['bytes'], cancelled=stopping) != row['sha256']):
                            raise ValueError('A transcript changed after its completion was checked.')
                    receipt = dict(schema=SCHEMA, job_id=identity, protocol=summary['protocol'],
                        run_id=summary['run_id'], completion_sha256=summary['completion_sha256'],
                        job_config_sha256=summary['job_config_sha256'],
                        runtime_manifest_sha256=summary['runtime_manifest_sha256'],
                        model_sha256=summary['model_sha256'], historical=True,
                        runtime_current_verified=False, outputs=rows)
                    jobs.atomic_json(stage / 'transcription-export.json', receipt)
                    # Recheck source receipt/owned bytes while holding the same native guards.
                    after = service.completed(identity, cancelled=stopping)
                    if any(after.get(key) != summary.get(key) for key in
                           ('run_id', 'completion_sha256', 'job_config_sha256', 'outputs')):
                        raise ValueError('The accepted transcription changed during export.')
                    if _directory_identity(parent) != parent_identity:
                        raise ValueError('The selected export directory changed during copying.')
                    stopping()
                    try:
                        jobs.publish_noreplace(stage, destination, cancelled=stopping)
                    except BaseException as error:
                        # The shared publisher documents a possible post-move fsync failure.
                        # A retained directory is reported explicitly, never deleted or called cancelled.
                        if (not optional_present(stage) and optional_present(destination)
                                and _directory_identity(destination) == stage_identity):
                            published = True
                            raise PublishedExportError(destination) from error
                        raise
                    published = True
                    result = dict(status='complete', path=str(destination), job_id=identity,
                        completion_sha256=summary['completion_sha256'], files=len(rows),
                        historical=True, runtime_current_verified=False)
                finally:
                    if stage is not None and optional_present(stage):
                        try:
                            if _directory_identity(stage) != stage_identity:
                                raise ValueError('The owned export stage was replaced; preserve it for inspection.')
                            shutil.rmtree(stage)
                        except OSError:
                            if published:
                                warning = 'The export is complete; an owned temporary directory needs cleanup.'
                            else:
                                raise
    except BaseException:
        if result is None:
            raise
        warning = 'The export is complete; resource or temporary-file cleanup needs attention.'
    if warning:
        result['cleanup_warning'] = warning
    return result
