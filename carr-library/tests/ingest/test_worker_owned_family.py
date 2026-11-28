"""Real owned parser family boundaries shared by import and conversion."""
import json
import subprocess
import sys
import time

import pytest

from carr_library.audio import engines
from carr_library.ingest import worker


@pytest.mark.parametrize('outcome', ['complete', 'cancelled'])
def test_parser_owner_settles_descendant_after_leader_exit(tmp_path, monkeypatch, outcome):
    marker = tmp_path / 'descendant-ready'
    heartbeat = tmp_path / 'descendant-writes'
    descendant = (
        "from pathlib import Path; import time\n"
        f"Path({str(marker)!r}).write_text('ready')\n"
        "while True:\n"
        f"    with open({str(heartbeat)!r}, 'ab') as stream: stream.write(b'x')\n"
        "    time.sleep(.01)\n"
    )
    leader = (
        "import json,subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-I', '-B', '-c', {descendant!r}])\n"
        f"marker=Path({str(marker)!r})\n"
        "deadline=time.monotonic()+5\n"
        "while not marker.exists():\n"
        "    if time.monotonic()>deadline: raise RuntimeError('descendant did not start')\n"
        "    time.sleep(.01)\n"
        + ("print(json.dumps({'result': {'state': 'complete'}}), flush=True)\n"
           if outcome == 'complete' else "time.sleep(30)\n")
    )
    launch = engines._start_process
    processes = []

    def substitute(command, **options):
        process = launch([sys.executable, '-I', '-B', '-c', leader], **options)
        processes.append(process)
        return process

    monkeypatch.setattr(engines, '_start_process', substitute)
    result = worker._invoke({'probe': 'owned-family'}, timeout=10,
                            cancelled=marker.exists if outcome == 'cancelled' else lambda: False)
    assert result['state'] == outcome
    assert marker.exists() and all(process.poll() is not None for process in processes)
    before = heartbeat.read_bytes() if heartbeat.exists() else b''
    time.sleep(.15)
    assert (heartbeat.read_bytes() if heartbeat.exists() else b'') == before


def test_parser_request_handle_is_read_only(tmp_path, monkeypatch):
    launch = engines._start_process
    code = (
        "import json,os,sys\n"
        "raw=sys.stdin.buffer.read()\n"
        "try:\n"
        "    os.write(sys.stdin.fileno(), b'changed')\n"
        "except OSError:\n"
        "    blocked=True\n"
        "else:\n"
        "    blocked=False\n"
        "print(json.dumps({'result': {'state':'complete','read_only':blocked,'request':json.loads(raw)}}))\n"
    )

    def substitute(command, **options):
        return launch([sys.executable, '-I', '-B', '-c', code], **options)

    monkeypatch.setattr(engines, '_start_process', substitute)
    result = worker._invoke({'original': 'immutable request'}, timeout=10)
    assert result == {'state': 'complete', 'read_only': True,
                      'request': {'original': 'immutable request'}}
