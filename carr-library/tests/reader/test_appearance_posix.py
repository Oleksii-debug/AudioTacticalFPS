"""POSIX-only real FIFO substitution; selected by the Linux portable gate."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.reader import appearance

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX FIFO substitution requires a POSIX filesystem.')


def test_owned_fifo_substitution_is_rejected_without_blocking_reader(tmp_path):
    # Select the same package already imported by the ordinary parent test.
    # In the installed gate this is the installed prefix, never checkout/src.
    module = Path(appearance.__file__).resolve(strict=True)
    environment = dict(os.environ, PYTHONPATH=str(module.parents[2]), PYTHONDONTWRITEBYTECODE='1')
    child = r'''
import json
import os
from pathlib import Path
import sys
from carr_library.reader import appearance
assert Path(appearance.__file__).resolve(strict=True) == Path(sys.argv[2])
root = Path(sys.argv[1]); root.mkdir()
appearance.save_appearance(root, appearance.ReaderAppearancePreferences(), expected_token=None)
target = root / 'state/ui/reader-appearance.json'
if hasattr(appearance, '_open_preferences'):
    opened = appearance._open_preferences
    def raced(path):
        assert path == target
        path.unlink(); os.mkfifo(path)
        print('owned FIFO substituted before open', flush=True)
        return opened(path)
    appearance._open_preferences = raced
else:
    # This branch reproduces the original blocking Path.open implementation.
    opened = Path.open
    def raced(path, *args, **kwargs):
        if path == target:
            path.unlink(); os.mkfifo(path)
            print('owned FIFO substituted before open', flush=True)
        return opened(path, *args, **kwargs)
    Path.open = raced
try:
    appearance.load_appearance(root)
except (OSError, ValueError):
    print(json.dumps({'status': 'refused', 'origin': str(Path(appearance.__file__).resolve())}), flush=True)
else:
    raise AssertionError('A substituted FIFO was accepted as reader appearance.')
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', child, str(tmp_path / 'owned-library'), str(module)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, errors = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
    assert process.returncode == 0, errors
    assert 'owned FIFO substituted before open' in output
    assert json.loads(output.splitlines()[-1]) == {'status': 'refused', 'origin': str(module)}
