"""Real worker-process observations for the Windows closed-status policy."""
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest


@pytest.fixture
def deferred_status_probe(monkeypatch):
    from carr_library import transcription
    from carr_library.audio import process as process_runner

    def attach(service, identity, *, release=None, stop_on_progress=False):
        # Linux also exercises the deferred-read branch with real children.
        # Actual Windows qualification uses this same native default.
        assert getattr(transcription, '_READ_LIVE_WORKER_STATUS', os.name != 'nt') == (os.name != 'nt')
        monkeypatch.setattr(transcription, '_READ_LIVE_WORKER_STATUS', False, raising=False)
        status = service._path(identity) / 'job_status.json'
        children, reads, notices, live = [], [], [], []
        stop = Event()
        public_reads, public_read_errors = [], []
        source = service.get(identity)['sources'][0]['filename']
        original_popen = process_runner.subprocess.Popen
        original_read = transcription.read_json

        def spawn(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child

        def read_after_exit(path):
            if Path(path) == status:
                assert children, 'Worker status was opened before its child was launched.'
                assert all(child.poll() is not None for child in children), (
                    'Host opened changing worker status while the actual child was alive.')
                reads.append(tuple(child.returncode for child in children))
            return original_read(path)

        def progress(done, total, phase):
            notices.append((done, total, phase))
            if phase == 'running':
                assert done is None and total is None, 'Deferred progress must remain indeterminate.'
            if children and children[-1].poll() is None:
                live.append((done, total, phase))
                assert done is None and total is None
                # Exercise the public read boundary while a real child is held
                # before publication; both APIs must refuse without a status open.
                for name, read in (('verify', lambda: service.verify(identity)),
                                   ('text', lambda: service.text(identity, source))):
                    try:
                        read()
                    except RuntimeError as exc:
                        if 'already being changed' in str(exc):
                            public_reads.append(name)
                        else:
                            public_read_errors.append((name, str(exc)))
                    except BaseException as exc:
                        public_read_errors.append((name, type(exc).__name__, str(exc)))
                    else:
                        public_read_errors.append((name, 'Active worker read was not refused.'))
                if release is not None:
                    (status.parent / release).write_bytes(b'Release the synthetic worker.\n')
                if stop_on_progress:
                    stop.set()

        monkeypatch.setattr(process_runner.subprocess, 'Popen', spawn)
        monkeypatch.setattr(transcription, 'read_json', read_after_exit)
        return SimpleNamespace(children=children, reads=reads, notices=notices, live=live,
                               stop=stop, progress=progress, public_reads=public_reads,
                               public_read_errors=public_read_errors)
    return attach
