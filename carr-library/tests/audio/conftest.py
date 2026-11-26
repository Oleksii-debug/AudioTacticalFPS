"""Exercise the Windows file-flush requirement on every test host."""
import os
import stat

import pytest


@pytest.fixture
def writable_file_fsync(monkeypatch):
    real_fsync = os.fsync
    flushed = []

    def require_write_access(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            # A zero-byte write leaves the content and position unchanged, but
            # rejects a read-only descriptor even on Linux. Windows _commit
            # requires write access; its actual fsync still runs below.
            os.write(descriptor, b'')
            flushed.append((info.st_dev, info.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(os, 'fsync', require_write_access)
    return flushed
