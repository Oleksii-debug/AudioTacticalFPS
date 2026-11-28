"""POSIX-only actual-child proof against a FIFO substituted after selection.

Windows runs the common descriptor/inventory suite. This file is selected only
by the POSIX gate; the selected-environment helper never executes .pth files.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import carr_library
import bootstrap
from carr_library.transcription import runtime as module


CHILD = r'''
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys

target = Path(sys.argv[1])
outside = Path(sys.argv[2])
api, replacement = sys.argv[3:5]
roots, expected = json.loads(sys.argv[5]), json.loads(sys.argv[6])
sys.path[:0] = roots
from carr_library.transcription import runtime as module
actual = Path(module.__file__).resolve()
assert str(actual) == expected["path"]
assert sha256(actual.read_bytes()).hexdigest() == expected["sha256"]
original = module._open_selected_file
replaced = []

def raced(path):
    if path == target and not replaced:
        path.unlink()
        if replacement == "fifo":
            os.mkfifo(path)
        else:
            path.symlink_to(outside)
        replaced.append(True)
    return original(path)

module._open_selected_file = raced
try:
    getattr(module, api)(target)
except (OSError, ValueError):
    assert replaced == [True]
else:
    raise AssertionError("The raced non-regular path was accepted.")
assert stat.S_ISFIFO(outside.lstat().st_mode)
assert target.is_symlink() if replacement == "symlink" else stat.S_ISFIFO(target.lstat().st_mode)
print(json.dumps({"status": "PASS", "api": api, "replacement": replacement,
                  "module": str(actual), "entered_real_open": True}), flush=True)
'''


@pytest.mark.parametrize('api', ['read_file_bytes', 'file_identity'])
@pytest.mark.parametrize('replacement', ['fifo', 'symlink'])
def test_real_child_never_blocks_on_post_selection_fifo(tmp_path, api, replacement):
    assert os.name == 'posix', 'This real FIFO suite belongs only to the POSIX gate.'
    target, outside = tmp_path / 'selected.py', tmp_path / 'outside-fifo'
    target.write_bytes(b'# originally regular\n')
    os.mkfifo(outside)
    roots = list(dict.fromkeys([
        str(Path(carr_library.__file__).resolve().parent.parent),
        str(Path(bootstrap.__file__).resolve().parent.parent),
    ]))
    expected = {'path': str(Path(module.__file__).resolve()),
                'sha256': sha256(Path(module.__file__).read_bytes()).hexdigest()}
    completed = subprocess.run(
        [sys.executable, '-I', '-B', '-X', 'utf8', '-c', CHILD,
         str(target.resolve()), str(outside.resolve()), api, replacement,
         json.dumps(roots), json.dumps(expected)],
        capture_output=True, text=True, encoding='utf-8', timeout=10, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result == {'status': 'PASS', 'api': api, 'replacement': replacement,
                      'module': expected['path'], 'entered_real_open': True}

def test_recorded_symlink_inside_selected_root_is_not_hidden_by_resolution(synthetic_asr_runtime):
    assert os.name == 'posix'
    runtime = synthetic_asr_runtime
    selected = runtime.site / 'tqdm.py'
    target = runtime.site / 'unselected-tqdm-payload.py'
    target.write_bytes(b'# Authored inert optional package\n')
    selected.symlink_to(target)
    info = runtime.site / 'tqdm-0.1.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: tqdm\nVersion: 0.1\n', encoding='utf-8')
    (info / 'RECORD').write_text('tqdm.py,,\ntqdm-0.1.dist-info/METADATA,,\ntqdm-0.1.dist-info/RECORD,,\n',
                                encoding='utf-8')
    before = target.read_bytes()
    with pytest.raises(ValueError, match='without links'):
        module.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    assert selected.is_symlink() and target.read_bytes() == before
