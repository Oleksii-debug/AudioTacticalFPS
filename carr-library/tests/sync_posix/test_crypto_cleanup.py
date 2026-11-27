"""Required actual POSIX descriptor-anchored crypto cleanup; no Windows substitute."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
from threading import Event

import pytest
from carr_library.sync import crypto

_path = Path(__file__).parents[1] / 'sync' / 'test_crypto.py'
_spec = importlib.util.spec_from_file_location('_sync_crypto_cleanup_fixture', _path)
_fixture = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _fixture
exec(compile(_path.read_bytes(), str(_path), 'exec'), _fixture.__dict__)
encrypted = _fixture.encrypted
PASSWORD = _fixture.PASSWORD

def test_private_stage_permissions_and_cancelled_parent_move_cleanup(tmp_path, encrypted):
    assert os.name == 'posix', 'This required case is selected only in the POSIX lane.'
    plain, _ = encrypted
    source = tmp_path / 'source'
    source.write_bytes(plain)
    parent, moved = tmp_path / 'target', tmp_path / 'moved'
    parent.mkdir()
    stop = Event()
    def progress(done, total):
        if done and not stop.is_set():
            staged = tuple(parent.glob('.carr-crypto-*'))
            assert len(staged) == 1 and stat.S_IMODE(staged[0].stat().st_mode) == 0o600
            parent.rename(moved)
            stop.set()
    with pytest.raises(InterruptedError):
        crypto.encrypt_file(source, parent / 'output', PASSWORD,
                            cancelled=stop.is_set, on_progress=progress)
    assert not (moved / 'output').exists() and list(moved.iterdir()) == []

