"""Owned parser requests settle native sharing without hiding other failures."""
import json
import os
from pathlib import Path
import threading
import time

import pytest

from carr_library.audio import engines
from carr_library.ingest import worker


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows delete sharing is required.')
def test_owned_request_waits_for_native_read_handle_release(tmp_path):
    unrelated = tmp_path / 'unrelated.txt'
    unrelated.write_bytes(b'Preserve this independent file.')
    released = threading.Event()
    stream = None
    thread = None
    directory = None
    try:
        with worker._request_directory() as name:
            directory = Path(name)
            path = directory / 'request.json'
            path.write_bytes(b'{"synthetic":true}')
            stream = path.open('rb')

            def close_later():
                time.sleep(.15)
                stream.close()
                released.set()

            thread = threading.Thread(target=close_later)
            thread.start()
        thread.join(5)
        assert released.is_set()
        assert not directory.exists()
        assert unrelated.read_bytes() == b'Preserve this independent file.'
    finally:
        if thread is not None:
            thread.join(5)
        if stream is not None:
            stream.close()


def test_request_access_error_remains_visible_and_preserves_owned_bytes(monkeypatch):
    actual_factory = worker.TemporaryDirectory
    temporary = actual_factory(prefix='carr-request-denied-')
    attempts = []

    class DeniedDirectory:
        name = temporary.name

        def cleanup(self):
            attempts.append('cleanup')
            error = PermissionError('Synthetic access denial; not a sharing violation.')
            error.winerror = 5
            raise error

    monkeypatch.setattr(worker, 'TemporaryDirectory', lambda **kwargs: DeniedDirectory())
    request = Path(temporary.name) / 'request.json'
    try:
        with pytest.raises(PermissionError, match='access denial'):
            with worker._request_directory() as name:
                assert name == temporary.name
                request.write_bytes(b'{"preserved":true}')
        assert attempts == ['cleanup']
        assert request.read_bytes() == b'{"preserved":true}'
    finally:
        temporary.cleanup()


def test_constructor_failure_closes_request_before_directory_cleanup(monkeypatch):
    actual_factory = worker.TemporaryDirectory
    paths = []

    def temporary(**kwargs):
        value = actual_factory(**kwargs)
        paths.append(Path(value.name))
        return value

    def failed_launch(command, **options):
        assert json.loads(options['stdin'].read()) == {'probe': 'construction'}
        assert options['stdin'].readable() and not options['stdin'].writable()
        raise OSError('Synthetic process construction failure')

    monkeypatch.setattr(worker, 'TemporaryDirectory', temporary)
    monkeypatch.setattr(engines, '_start_process', failed_launch)
    with pytest.raises(OSError, match='construction failure'):
        worker._invoke({'probe': 'construction'}, timeout=5)
    assert len(paths) == 1 and not paths[0].exists()
