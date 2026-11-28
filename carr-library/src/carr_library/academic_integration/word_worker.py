"""Fixed owned-process protocol for optional already-open Word automation.

The application invokes this exact script with a request digest. It never accepts
a program, COM class, macro, document filename, or arbitrary method from a request.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    # Fixed installed/source package next to this very script; -I excludes cwd
    # and environment imports. No caller-selected Python import root is accepted.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from carr_library.durable_state import atomic_json, local_path, read_json_snapshot, regular
from carr_library.academic_integration.word_records import (
    MAX_BYTES, exact, fingerprint, identity, integer, require, validate_capture,
    validate_proposal, digest, text,
)


def read_record(path, *, expected=None):
    value, actual = read_json_snapshot(path, max_bytes=MAX_BYTES)
    if expected is not None:
        require(actual == expected, 'The exact Word request bytes changed.')
    return value


def run_protocol(directory, request_digest, *, backend_factory=None):
    from carr_library.academic_integration.word_native import WordNative
    root = local_path(directory, exists=True)
    root_info = regular(root, directory=True)
    root_id = root_info.st_dev, root_info.st_ino
    request = read_record(root / 'request.json', expected=digest(request_digest))
    exact(request, ('schema', 'session', 'mode', 'staging', 'timeout', 'preview_seconds'), 'Word request')
    require(request['schema'] == 'resonance-word-request/1', 'Unsupported Word request.')
    session = identity(request['session'], 'held session')
    require(request['mode'] in ('inspect', 'mutate'), 'Unsupported Word operation.')
    require(type(request['timeout']) in (int, float) and 1 <= request['timeout'] <= 300,
            'Word needs a finite total deadline.')
    integer(request['preview_seconds'], 'preview expiry', 1, 90)
    exact(request['staging'], ('path', 'device', 'inode'), 'Word staging')
    text(request['staging']['path'], 'Word staging path', 8192)
    for key in ('device', 'inode'):
        require(type(request['staging'][key]) is int and request['staging'][key] >= 0,
                'Word staging identity must use exact integer fields.')
    require(request['staging'] == {'path': str(root), 'device': root_id[0], 'inode': root_id[1]},
            'The held Word staging identity changed.')
    deadline = time.monotonic() + request['timeout']
    begun = False
    completed = False
    backend = None

    def check():
        current = regular(local_path(root, exists=True), directory=True)
        require((current.st_dev, current.st_ino) == root_id, 'The Word staging directory changed.')
        if time.monotonic() >= deadline:
            raise TimeoutError('The held Word session expired; inspect and review again.')

    def publish(name, value):
        check()
        atomic_json(root / name, value)
        # A failed directory sync may leave visible bytes. The caller treats
        # begun as possible mutation and a complete verified receipt as committed.
        return value

    def wait(name, until):
        path = root / name
        while not os.path.lexists(path):
            check()
            if time.monotonic() >= until:
                raise TimeoutError('Word preview approval expired; no automatic application is allowed.')
            time.sleep(.05)
        check()
        return read_record(path)

    try:
        check()
        backend = (backend_factory or WordNative)(session)
        capture = validate_capture(backend.capture(), session)
        publish('capture.json', capture)
        if request['mode'] == 'inspect':
            return 0
        proposal = wait('proposal.json', deadline)
        validate_proposal(proposal, capture)
        proposal_sha = fingerprint(proposal)
        approved = wait('approved.json', min(deadline, time.monotonic() + request['preview_seconds']))
        exact(approved, ('schema', 'session', 'proposal_sha256'), 'Word approval')
        require(approved == {'schema': 'resonance-word-approval/1', 'session': session,
                             'proposal_sha256': proposal_sha}, 'Word approval differs from the reviewed preview.')
        check()
        current = validate_capture(backend.capture(), session)
        require(fingerprint(current) == fingerprint(capture), 'Word changed after preview; inspect again.')
        ready = {'schema': 'resonance-word-ready/1', 'session': session,
                 'proposal_sha256': proposal_sha, 'capture_sha256': fingerprint(capture)}
        publish('ready.json', ready)
        grant_deadline = min(deadline, time.monotonic() + 5)
        grant = wait('grant.json', grant_deadline)
        require(grant == {'schema': 'resonance-word-grant/1', 'session': session,
                          'ready_sha256': fingerprint(ready)}, 'The final source attestation differs.')
        # Recheck held COM objects and document bytes after the host's grant.
        require(fingerprint(backend.capture()) == fingerprint(capture),
                'Word changed at the mutation boundary; inspect again.')
        check()
        if time.monotonic() >= grant_deadline:
            raise TimeoutError('The final source attestation expired.')
        publish('begun.json', {'schema': 'resonance-word-begun/1',
                              'session': session, 'proposal_sha256': proposal_sha})
        begun = True
        def first_write():
            check()
            if time.monotonic() >= grant_deadline:
                raise TimeoutError('The final source attestation expired before the first document change.')

        receipt = backend.apply(proposal, capture, check=check, first_write=first_write)
        # No cancellation/source callback after a fully observed native result.
        # Publication uses a create-only immutable record that the parent reads
        # even if the later process shutdown or cleanup reports an error.
        atomic_json(root / 'receipt.json', receipt)
        completed = True
        return 0
    except BaseException as error:
        # Only fixed categories leave this boundary; no document text, COM
        # exception message, filename or citation metadata is printed.
        try:
            atomic_json(root / 'failure.json', {
                'schema': 'resonance-word-failure/1', 'session': session,
                'phase': 'after-mutation-start' if begun or os.path.lexists(root / 'begun.json') else 'before-mutation',
                'category': 'expired' if isinstance(error, TimeoutError) else
                            'cancelled' if isinstance(error, (KeyboardInterrupt, InterruptedError)) else
                            'invalid-or-unavailable' if isinstance(error, (ValueError, OSError)) else 'native-failure',
                'completed_receipt': bool(completed or os.path.lexists(root / 'receipt.json')),
            })
        except BaseException:
            pass
        return 3 if begun or os.path.lexists(root / 'begun.json') else 1
    finally:
        if backend is not None:
            backend.close()


def main():
    require(len(sys.argv) == 2, 'The fixed Word worker accepts one request digest.')
    return run_protocol(Path.cwd(), sys.argv[1])


if __name__ == '__main__':
    raise SystemExit(main())
