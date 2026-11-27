"""Required POSIX nonblocking/non-following listening descriptor checks.

Each hostile open happens in the existing owned child process so a regression
cannot hang the test runner. Windows uses its separately tested reparse opener.
"""
from hashlib import sha256
import json
from pathlib import Path
import sys
import sysconfig

import pytest

from carr_library.audio.models import AudioBudget
from carr_library.audio.process import run_process
from carr_library.speech import listening


@pytest.mark.parametrize('kind', ['fifo', 'symlink'])
def test_private_listening_open_refuses_substitution_before_reading(tmp_path, kind):
    selected = Path(listening.__file__).resolve()
    installed = Path(sysconfig.get_path('purelib')) / 'carr_library' / 'speech' / 'listening.py'
    path = tmp_path / 'checked.wav'
    path.write_bytes(b'bounded initial ordinary file')
    other = tmp_path / 'unrelated.txt'
    other.write_bytes(b'foreign bytes must not be read')
    code = r'''
from hashlib import sha256
import json, os, sys
from pathlib import Path
from carr_library.speech import listening
actual = Path(listening.__file__).resolve()
assert str(actual) in json.loads(sys.argv[1])
assert sha256(actual.read_bytes()).hexdigest() == sys.argv[2]
path, kind, other = Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5])
identity = listening.stamp(path)
original = listening._open_stream
observed = []
def substituted(selected):
    observed.append(True)
    selected.unlink()
    if kind == 'fifo':
        os.mkfifo(selected)
    else:
        selected.symlink_to(other)
    return original(selected)
listening._open_stream = substituted
try:
    with listening._opened(path, identity):
        raise AssertionError('Substituted path admitted a PCM reader.')
except (ValueError, OSError):
    pass
else:
    raise AssertionError('Substitution was not rejected.')
assert observed == [True]
assert other.read_bytes() == b'foreign bytes must not be read'
sys.stdout.buffer.write(b'checked-without-reading\n')
'''
    result = run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c', code,
        json.dumps(sorted({str(selected), str(installed.resolve())})),
        sha256(selected.read_bytes()).hexdigest(), str(path), kind, str(other)],
        cwd=tmp_path, owned_tree=True,
        budget=AudioBudget(timeout=10, max_log_bytes=16384, reserve_disk_bytes=0))
    assert result.stdout == b'checked-without-reading\n' and result.stderr == b''
