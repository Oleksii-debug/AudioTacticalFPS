"""Explicit optional real-upstream checks; excluded from default discovery.

Run pytest against this filename with CARR_TEST_DORDIAI_RUNTIME set to an
existing private kx47Runtime. No private files are copied or modified. These
checks do not synthesize speech or establish Windows/NVDA acceptance.
"""

import hashlib
import os
from pathlib import Path
import sys

import pytest

from carr_library.integrations import dordiai_voices as api


def _runtime():
    configured = os.environ.get("CARR_TEST_DORDIAI_RUNTIME")
    if not configured:
        pytest.fail("This optional command requires CARR_TEST_DORDIAI_RUNTIME; it is not a default test gate")
    return Path(configured).resolve(strict=True)


def test_real_upstream_worker_no_model_handshake_error_cancel_drop():
    runtime = _runtime()
    worker = runtime / "nvda_worker.py"
    assert hashlib.sha256(worker.read_bytes()).hexdigest() == api.REVIEWED_WORKER_SHA256
    child = api._JsonLinesProcess([sys.executable, "-I", "-B", "-u", str(worker), str(runtime / "models")], runtime)
    try:
        child.send({"mode": "preload", "runtimeOnly": True})
        reply = child.receive(10)
        assert reply["ready"] is True and "workerBootstrap" in reply
        child.send({"mode": "reference", "profile": "carr_protocol_fixture", "language": "uk", "text": "", "speed": 1.0})
        reply = child.receive(10)
        assert reply["error"] == "ValueError" and isinstance(reply["diagnostics"], dict)
        child.send({"mode": "cancel"})
        child.send({"mode": "drop"})
        assert child.receive(10) == {"finished": True}
        with pytest.raises(api.DordiaiTimeout):
            child.receive(0.05)
    finally:
        child.close()
    assert child.process.returncode == 0


def test_real_upstream_inventory_uses_actual_helpers_without_inference():
    with api.DordiaiVoicesAdapter(_runtime(), python_executable=sys.executable) as adapter:
        assert all(voice.languages for voice in adapter.voices())
        assert adapter.capabilities().worker_sha256 == api.REVIEWED_WORKER_SHA256
        assert adapter.language_statuses()
        assert adapter._worker is None
