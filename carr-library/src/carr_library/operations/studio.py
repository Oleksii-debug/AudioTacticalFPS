"""Read an explicitly opened product Audio Studio journal; never discover output paths."""
from pathlib import Path
import re

from ..durable_state import digest_file, local_path, read_json, regular


def studio_journal_identity(root):
    root = local_path(root, exists=True)
    path = local_path(root / 'render-journal.json', root=root)
    if not path.exists():
        return None
    if regular(path).st_size > 65536:
        raise ValueError('Audio Studio journal exceeds its bounded view budget.')
    return digest_file(path)


def read_studio_operation(controller):
    if controller is None or getattr(controller, '_operation_output', None) is None:
        return None
    from .facade import OperationView
    root = local_path(controller._operation_output, exists=True)
    regular(root, directory=True)
    identity = controller.project_id
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('Invalid open Audio Studio identity.')
    path = local_path(root / 'render-journal.json', root=root)
    render_lock = getattr(controller, '_render_lock', None)
    previous = getattr(controller, '_operation_previous_journal', None)
    if (not path.exists() or (render_lock is not None and render_lock.locked()
            and studio_journal_identity(root) == previous)):
        return OperationView('studio', identity, None, 'preparing', None, None,
            'Explicitly opened Audio Studio; waiting for its first render checkpoint',
            '', (), 'Audio Studio / render-journal.json')
    if regular(path).st_size > 65536:
        raise ValueError('Audio Studio journal exceeds its bounded view budget.')
    value = read_json(path)
    required = {'schema_version', 'run_key', 'state', 'completed_steps', 'scene_count', 'source_sha256'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {'message', 'output', 'duration_seconds'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['state'] not in ('running', 'complete', 'cancelled', 'failed')
            or any(not isinstance(value[name], str) or not re.fullmatch('[0-9a-f]{64}', value[name])
                   for name in ('run_key', 'source_sha256'))
            or type(value['completed_steps']) is not int or value['completed_steps'] < 0
            or type(value['scene_count']) is not int or value['scene_count'] < 0):
        raise ValueError('The Audio Studio render journal is invalid.')
    # The renderer records completed processing steps, not a measured total.
    # Output paths and source text are neither opened nor shown by this view.
    return OperationView('studio', identity, None, value['state'], value['completed_steps'],
        None, 'Stored Audio Studio render checkpoint; completed processing steps',
        '', (), 'Audio Studio / render-journal.json')


def render_with_resources(private_root, plan, project_dir, *, engine, ffmpeg,
                          cancelled=lambda: False, on_status=lambda message: None,
                          operation_id=None, renderer=None, admission_timeout=60):
    """Admit existing product rendering without replacing its journal or renderer."""
    from ..audio.jobs import checked_directory, RenderCancelled
    from ..data_paths import guard_data_directory
    from ..audio.models import digest
    from . import ResourceBroker, ResourceRequest, ResourceCancelled
    root = checked_directory(guard_data_directory(private_root), create=True)
    broker = ResourceBroker.for_app(root)
    identity = operation_id or ('audio:' + digest(plan.to_dict())[:32])
    request = ResourceRequest(cpu_slots=1, devices=('heavy-media',))
    if renderer is None:
        from ..audio.production import render_project
        renderer = render_project
    try:
        with broker.reserve(request, operation_id=identity, cancelled=cancelled,
                            timeout=admission_timeout) as lease:
            return renderer(plan, project_dir, engine=engine, ffmpeg=ffmpeg,
                cancelled=lambda: cancelled() or lease.cancelled(), on_status=on_status)
    except ResourceCancelled as exc:
        raise RenderCancelled(str(exc)) from exc
