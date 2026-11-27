"""Real Linux leader/descendant lifetimes; no engine or model is used."""
import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from carr_library.operations import ResourceBroker, ResourceRequest
from carr_library.operations import process


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux owned-group lifecycle contract.")

CHILD = r'''
import json, os, pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = pathlib.Path(sys.argv[1])
fields = (pathlib.Path('/proc') / str(os.getpid()) / 'stat').read_text().rsplit(') ', 1)[1].split()
pending = path.with_suffix('.pending')
pending.write_text(json.dumps({'pid': os.getpid(), 'starttime': fields[19],
                               'group': os.getpgrp(), 'leader': os.getppid()}))
os.replace(pending, path)
time.sleep(90)
'''

LEADER = r'''
import json, pathlib, subprocess, sys, time
ready = pathlib.Path(sys.argv[1])
subprocess.Popen([sys.executable, '-I', '-B', '-c', sys.argv[3], str(ready)])
deadline = time.monotonic() + 5
while not ready.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('Synthetic descendant did not become ready.')
    time.sleep(.01)
print('Synthetic native worker result.', flush=True)
if int(sys.argv[2]):
    print('Synthetic native worker rejected its result.', file=sys.stderr, flush=True)
raise SystemExit(int(sys.argv[2]))
'''


def _owned_state(record):
    try:
        fields = (Path('/proc') / str(record['pid']) / 'stat').read_text().rsplit(') ', 1)[1].split()
    except FileNotFoundError:
        return None
    return fields[0] if fields[19] == record['starttime'] else None


@pytest.mark.parametrize("return_code", [0, 7], ids=["valid-outcome", "rejected-outcome"])
def test_exited_leader_descendant_stops_before_result_or_error(tmp_path, return_code):
    """The synchronized child ignores TERM and holds both inherited pipes."""
    ready = tmp_path / "descendant.json"
    library = tmp_path / "library"
    library.mkdir()
    broker = ResourceBroker.for_app(library)
    record = None

    def cancellation_observation():
        # Once ready, observe the leader's exit without reaping it. This makes
        # the original parent-exits/child-lives boundary deterministic while
        # leaving all termination and drain behavior to the real runner.
        if ready.exists():
            child = json.loads(ready.read_text())
            deadline = time.monotonic() + 5
            while True:
                try:
                    fields = (Path('/proc') / str(child['leader']) / 'stat').read_text().rsplit(') ', 1)[1].split()
                except FileNotFoundError:
                    break
                if fields[0] in {'Z', 'X', 'x'}:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError('The synthetic leader did not exit.')
                time.sleep(.01)
        return False

    try:
        with broker.reserve(ResourceRequest(devices=("heavy-media",)),
                            operation_id="actual-process-group") as lease:
            args = [str(Path(sys.executable).absolute()), "-I", "-B", "-c", LEADER,
                    str(ready), str(return_code), CHILD]
            if return_code:
                with pytest.raises(process.ProcessError, match="exit 7"):
                    process.run_process(args, cancelled=cancellation_observation,
                                        budget=process.ProcessBudget(timeout=15))
            else:
                result = process.run_process(args, cancelled=cancellation_observation,
                                             budget=process.ProcessBudget(timeout=15))
                assert result.stdout == b"Synthetic native worker result.\n"
            record = json.loads(ready.read_text())
            assert record["group"] == record["leader"]
            assert _owned_state(record) in {None, "Z", "X", "x"}
            assert broker.snapshot()["leases"][0]["id"] == lease.id
        assert broker.snapshot()["leases"] == []
    finally:
        # The unpatched baseline leaves this exact fixture alive. A failing
        # assertion still cleans only the PID with its recorded kernel start.
        if record is None and ready.exists():
            record = json.loads(ready.read_text())
        if record is not None and _owned_state(record) not in {None, "Z", "X", "x"}:
            os.kill(record["pid"], signal.SIGKILL)


def test_unconfirmed_group_shutdown_cannot_return_a_normal_result(tmp_path, monkeypatch):
    children = []
    native_popen = process.subprocess.Popen

    def start(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        return child

    def unconfirmed(*args, **kwargs):
        raise TimeoutError("Synthetic inability to confirm group shutdown.")

    monkeypatch.setattr(process.subprocess, "Popen", start)
    monkeypatch.setattr(process, "_wait_linux_group", unconfirmed)
    with pytest.raises(process.ProcessError, match="shutdown could not be confirmed"):
        process.run_process([str(Path(sys.executable).absolute()), "-I", "-B", "-c",
                             "print('A normal output is insufficient evidence of shutdown.')"],
                            budget=process.ProcessBudget(timeout=15))
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdout.closed and children[0].stderr.closed
