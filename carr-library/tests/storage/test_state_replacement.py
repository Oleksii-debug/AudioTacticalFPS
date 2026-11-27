"""Filesystem and audit boundaries shared by host and supplied-worker state."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library import windows_state
from carr_library.windows_state import replace_file


@pytest.mark.parametrize('existing', [False, True])
def test_state_replacement_preserves_exact_bytes_and_removes_only_stage(tmp_path, existing):
    source, target = tmp_path / 'Новий.tmp', tmp_path / 'Стан.json'
    payload = b'{"exact":"\\u044f"}\r\n'
    source.write_bytes(payload)
    if existing:
        target.write_bytes(b'previous')
    unrelated = tmp_path / 'original.txt'
    unrelated.write_bytes(b'original')
    replace_file(source, target)
    assert target.read_bytes() == payload and not source.exists()
    assert unrelated.read_bytes() == b'original'
    assert target.stat().st_nlink == 1


@pytest.mark.parametrize('kind', ['same', 'non-sibling', 'directory', 'source-link', 'target-link'])
def test_invalid_replacement_preserves_both_names(tmp_path, kind):
    source, target = tmp_path / 'stage', tmp_path / 'target'
    source.write_bytes(b'new')
    if kind == 'same':
        target = source
    elif kind == 'non-sibling':
        (tmp_path / 'other').mkdir()
        target = tmp_path / 'other' / 'target'
        target.write_bytes(b'old')
    elif kind == 'directory':
        target.mkdir()
    else:
        target.write_bytes(b'old')
        os.link(source if kind == 'source-link' else target, tmp_path / 'second-name')
    with pytest.raises(ValueError):
        replace_file(source, target)
    assert source.read_bytes() == b'new'
    if kind not in ('same', 'directory'):
        assert target.read_bytes() == b'old'


def test_state_replacement_honors_real_python_audit_before_native_publication(tmp_path):
    source, target = tmp_path / 'stage', tmp_path / 'target'
    source.write_bytes(b'new')
    target.write_bytes(b'old')
    program = r'''
import pathlib, sys
sys.path.insert(0, sys.argv[3])
from carr_library.windows_state import replace_file
def audit(event, args):
    if event == 'os.rename':
        assert pathlib.Path(args[0]) == pathlib.Path(sys.argv[1])
        assert pathlib.Path(args[1]) == pathlib.Path(sys.argv[2])
        raise PermissionError('Synthetic audit refused publication')
sys.addaudithook(audit)
try:
    replace_file(sys.argv[1], sys.argv[2])
except PermissionError as exc:
    assert 'Synthetic audit' in str(exc)
    raise SystemExit(42)
raise AssertionError('Publication bypassed the audit guard')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(source), str(target),
        str(Path(windows_state.__file__).resolve().parents[1])], capture_output=True, timeout=10)
    assert result.returncode == 42, result.stderr.decode(errors='replace')
    assert source.read_bytes() == b'new' and target.read_bytes() == b'old'


@pytest.mark.parametrize('escape', ['source', 'destination'])
def test_actual_worker_status_bootstrap_refuses_outside_job_replacement(tmp_path, escape):
    from carr_library.transcription import _worker_bootstrap

    job = tmp_path / 'job'
    job.mkdir()
    source = (tmp_path if escape == 'source' else job) / 'stage'
    target = (tmp_path if escape == 'destination' else job) / 'job_status.json'
    source.write_bytes(b'new')
    target.write_bytes(b'old')
    worker = tmp_path / 'selected-worker.py'
    worker.write_text('import os\nos.replace(' + repr(str(source)) + ', ' + repr(str(target)) + ')\n')
    original = worker.read_bytes()
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', _worker_bootstrap(),
        str(worker), str(job), 'receipts-v1'], cwd=job, capture_output=True, timeout=15)
    assert result.returncode != 0
    assert b'Transcription writes must remain inside its private job.' in result.stderr
    assert source.read_bytes() == b'new' and target.read_bytes() == b'old'
    assert worker.read_bytes() == original
