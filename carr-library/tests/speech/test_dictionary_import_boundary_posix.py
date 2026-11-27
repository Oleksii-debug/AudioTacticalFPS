"""POSIX-only actual before-open FIFO/link substitution; no synthetic parser."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.ui import pronunciation_dialog as ui


@pytest.mark.parametrize('replacement', ['fifo', 'symlink'])
def test_before_open_substitution_is_bounded_and_cannot_read_foreign_bytes(tmp_path, replacement):
    assert os.name == 'posix', 'Select this POSIX boundary suite only in the Linux gate.'
    source = Path(ui.__file__).resolve()
    child = r'''
from hashlib import sha256
import json, os
from pathlib import Path
import sys
from carr_library.ui import pronunciation_dialog as ui
selected_module = Path(ui.__file__).resolve()
assert str(selected_module) == sys.argv[1]
assert sha256(selected_module.read_bytes()).hexdigest() == sys.argv[2]
root = Path(sys.argv[3])
target = root / 'rules.json'
raw = json.dumps({'schema':'carr.pronunciations.v1','rules':[]}).encode()
target.write_bytes(raw)
foreign = root / 'foreign.json'
foreign.write_bytes(raw)
actual_path_open = Path.open
actual_descriptor = getattr(ui, '_open_json_file', None)
entered = []
def substitute(path):
    if Path(path) == target and not entered:
        target.unlink()
        if sys.argv[4] == 'fifo':
            os.mkfifo(target)
        else:
            target.symlink_to(foreign)
        entered.append(True)
        print('ACTUAL_OWNED_SUBSTITUTION', flush=True)
def selected_open(path, *args, **kwargs):
    substitute(path)
    return actual_path_open(path, *args, **kwargs)
Path.open = selected_open
if actual_descriptor is not None:
    def selected_descriptor(path):
        substitute(path)
        return actual_descriptor(path)
    ui._open_json_file = selected_descriptor
try:
    ui.read_dictionary(target)
except (ValueError, OSError):
    assert entered == [True]
    assert foreign.read_bytes() == raw
    print(json.dumps({'status':'REFUSED','kind':sys.argv[4]}), flush=True)
else:
    raise AssertionError('A substituted dictionary was accepted.')
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', child, str(source),
                                sha256(source.read_bytes()).hexdigest(), str(tmp_path), replacement],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        try:
            output, error = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                output, error = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); output, error = process.communicate(timeout=3)
            pytest.fail('Actual dictionary opening blocked after owned substitution: '
                        + output.decode('utf-8', 'replace') + error.decode('utf-8', 'replace'))
        assert process.returncode == 0, error.decode('utf-8', 'replace')
        lines = output.decode('utf-8').splitlines()
        assert lines[0] == 'ACTUAL_OWNED_SUBSTITUTION'
        assert json.loads(lines[-1]) == {'status': 'REFUSED', 'kind': replacement}
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=3)
