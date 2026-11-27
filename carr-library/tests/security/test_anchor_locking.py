"""Real cross-process serialization of the protected-anchor publication seam."""
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import time

import pytest
import carr_library

from carr_library.security._files import BusyError
from carr_library.security.entitlements import EntitlementStore, ReplayError
from .conftest import MemoryAnchor, accept, envelope, grant


def test_other_process_holding_client_lock_cannot_race_anchor_advance(tmp_path, signer, policy, clock):
    anchor = MemoryAnchor()
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    first = envelope(signer, grant(clock))
    accept(store, first)
    higher = envelope(signer, grant(clock, sequence=3))
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    # This process exercises the real OS lock. The synthetic anchor stays in
    # the parent; no plaintext anchor backend is introduced in product code.
    code = '''import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from carr_library.security._files import locked
with locked(sys.argv[2]):
    Path(sys.argv[3]).write_text('locked', encoding='ascii')
    deadline = time.monotonic() + 4
    while not Path(sys.argv[4]).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Test parent did not release the fixture lock.')
        time.sleep(.005)
'''
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
        str(Path(carr_library.__file__).resolve().parent.parent), str(store.root), str(ready), str(release)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.005)
        assert ready.exists(), 'The child must hold the actual client lock.'
        with pytest.raises(BusyError):
            accept(store, higher)
        assert anchor.load().sequence == 1
        assert anchor.load().digest == sha256(first).hexdigest()
        assert store.current_path.read_bytes() == first
        assert not store.pending_path.exists()
    finally:
        release.write_text('release', encoding='ascii')
        try:
            stdout, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise
    assert child.returncode == 0, stderr.decode(errors='replace')
    accept(store, higher)
    with pytest.raises(ReplayError):
        accept(store, envelope(signer, grant(clock, sequence=2)))
    assert anchor.load().sequence == 3
    assert store.current_path.read_bytes() == higher
