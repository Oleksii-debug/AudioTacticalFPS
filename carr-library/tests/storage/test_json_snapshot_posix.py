"""POSIX opened-object substitution at the shared bounded JSON boundary.

Selected by Linux portable/installed gates; Windows retains its native sharing
tests and does not select this POSIX-only module.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library import durable_state

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Requires actual POSIX FIFO and symlink semantics.')


@pytest.mark.parametrize('replacement', ('fifo', 'symlink', 'regular'))
def test_owned_preopen_substitution_is_bounded_and_keeps_real_snapshot_semantics(tmp_path, replacement):
    # Import exactly the package selected by the parent. For an installed gate
    # this names only that installed prefix, never an alternate source checkout.
    module = Path(durable_state.__file__).resolve(strict=True)
    environment = dict(os.environ, PYTHONPATH=str(module.parents[1]),
                       PYTHONDONTWRITEBYTECODE='1')
    script = r'''
import json
import os
from pathlib import Path
import sys
from carr_library import durable_state as state

module = Path(state.__file__).resolve(strict=True)
assert module == Path(sys.argv[2])
root = Path(sys.argv[1]); root.mkdir()
target, other = root / 'state.json', root / 'replacement.json'
state.atomic_json(target, {'value': 'original'}, replace=False)
state.atomic_json(other, {'value': 'replacement'}, replace=False)
other_bytes = other.read_bytes()
kind = sys.argv[3]
opened = state._open_json_file
calls = []

def raced(path):
    assert path == target
    calls.append(path)
    if len(calls) == 1:
        target.unlink()
        if kind == 'fifo':
            os.mkfifo(target)
        elif kind == 'symlink':
            target.symlink_to(other)
        else:
            os.replace(other, target)
        print('owned object substituted before open', flush=True)
    return opened(path)

state._open_json_file = raced
try:
    value = state.read_json(target, maximum=64)
except (OSError, ValueError):
    assert kind in ('fifo', 'symlink')
    assert other.read_bytes() == other_bytes
    outcome = 'refused'
else:
    assert kind == 'regular' and value == {'value': 'replacement'}
    assert len(calls) == 2, 'A different inode must be retried before parsing.'
    assert target.read_bytes() == other_bytes
    outcome = 'replacement snapshot'
print(json.dumps({'status': outcome, 'origin': str(module), 'kind': kind}), flush=True)
'''
    process = subprocess.Popen(
        [sys.executable, '-B', '-c', script, str(tmp_path / 'owned-json'), str(module), replacement],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, errors = process.communicate(timeout=5)
    finally:
        # Own only this fixture's child, including the old blocking FIFO defect.
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
    assert process.returncode == 0, errors
    assert 'owned object substituted before open' in output
    assert json.loads(output.splitlines()[-1]) == {
        'status': 'replacement snapshot' if replacement == 'regular' else 'refused',
        'origin': str(module), 'kind': replacement}
