"""Real parser-adapter process-family ownership; no parser-quality claim."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library.audio import capture_process
from carr_library.ingest import worker


@pytest.mark.parametrize('callback_failure', [False, True])
def test_import_waits_for_owned_descendants_before_returning_or_unwinding(tmp_path, monkeypatch, callback_failure):
    ready, late, release = (tmp_path / name for name in ('descendant-ready', 'late-write', 'release'))
    child = """
import sys,time
from pathlib import Path
Path(sys.argv[1]).write_text('ready')
deadline=time.monotonic()+10
while not Path(sys.argv[3]).exists() and time.monotonic()<deadline:
    time.sleep(.01)
if Path(sys.argv[3]).exists():
    Path(sys.argv[2]).write_text('unowned child wrote after supervisor return')
"""
    parent = """
import json,subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-I','-B','-c',sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]],
                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
deadline=time.monotonic()+5
while not Path(sys.argv[2]).exists():
    if time.monotonic()>deadline:
        raise RuntimeError('Synthetic descendant did not start.')
    time.sleep(.005)
print(json.dumps({'result':{'state':'complete','fixture':'owned-family'}}),flush=True)
"""
    actual = capture_process.start_owned_process
    resources, processes = [], []
    def launch(arguments, **options):
        stream = options['stdin_file']
        assert stream.readable() and not stream.writable()
        assert json.loads(stream.read()) == {'action': 'synthetic-owned-family'}
        stream.seek(0)
        resources.extend([stream, options['stdout'], options['stderr']])
        process = actual([sys.executable, '-I', '-B', '-c', parent, child, str(ready), str(late), str(release)],
                         **options)
        processes.append(process)
        return process
    monkeypatch.setattr(capture_process, 'start_owned_process', launch)
    def started():
        if callback_failure:
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert time.monotonic() < deadline, 'The real descendant did not start.'
                time.sleep(.005)
            raise RuntimeError('Synthetic observer failed after the real child started.')
    if callback_failure:
        with pytest.raises(RuntimeError, match='Synthetic observer'):
            worker._invoke({'action': 'synthetic-owned-family'}, timeout=8, on_started=started)
    else:
        assert worker._invoke({'action': 'synthetic-owned-family'}, timeout=8, on_started=started) == {
            'state': 'complete', 'fixture': 'owned-family'}
    assert ready.exists() and len(processes) == 1 and processes[0].poll() is not None
    assert all(stream.closed for stream in resources)
    # Release only after supervisor return: an unowned child would now write.
    release.write_bytes(b'')
    time.sleep(1.35)
    assert not late.exists()
