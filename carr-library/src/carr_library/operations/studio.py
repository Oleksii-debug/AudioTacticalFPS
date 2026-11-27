"""Read an explicitly opened product Audio Studio journal; never discover output paths."""
from pathlib import Path
import re

from ..durable_state import local_path, read_json, regular


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
    if not path.exists():
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
