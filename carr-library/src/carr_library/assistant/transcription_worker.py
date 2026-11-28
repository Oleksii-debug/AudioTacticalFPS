"""Private bounded entry point for one explicitly approved STT Gateway call.

The existing supervisor owns this child. Its short-lived request contains no
credential; the current provider profile resolves its own vault reference just
before the existing Gateway transmits the selected retained bytes.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic

from ..assistant.models import canonical
from ..assistant.profiles import ProfileStore
from ..assistant.providers import Authorization, Gateway, UsageLedger
from ..assistant.transcription_provider import OPERATION, PROTOCOL, AudioUpload
from ..catalog.intake import _open_file_source, _stream_change_time
from ..durable_state import atomic_bytes, encode, local_path, read_json, regular
from ..integrations import transcription_remote_values as values


def execute(message, *, request_path, gateway_factory=Gateway):
    values.fields(message, ('schema', 'library_root', 'library_identity', 'workspace',
                            'job', 'job_sha256', 'authorization', 'output', 'timeout'))
    if message['schema'] != 'resonance-remote-transcription-call/1':
        raise ValueError('Unsupported remote transcription child request.')
    values.identifier(message['job'])
    values.checksum(message['job_sha256'])
    values.text(message['workspace'], 200)
    timeout = message['timeout']
    if type(timeout) not in (int, float) or not .05 <= timeout <= 120:
        raise ValueError('Invalid remote transcription deadline.')
    root = local_path(message['library_root'], exists=True)
    expected_root = message['library_identity']
    if (type(expected_root) is not list or len(expected_root) != 2
            or any(type(number) is not int or number < 0 for number in expected_root)):
        raise ValueError('Invalid captured library directory identity.')
    output = local_path(message['output'])
    if output != request_path.with_name('response.json') or os.path.lexists(output):
        raise ValueError('Remote worker output must be one new private sibling file.')
    auth = Authorization(**message['authorization'])
    job_path = root / 'state' / 'transcription' / sha256(message['workspace'].encode('utf-8')).hexdigest() / 'jobs' / message['job']
    started = monotonic()

    def root_guard():
        info = regular(local_path(root, exists=True), directory=True)
        if [info.st_dev, info.st_ino] != expected_root:
            raise PermissionError('The original library directory changed.')
        if monotonic() - started >= timeout:
            raise InterruptedError('Remote transcription exceeded its cooperative network deadline.')

    root_guard()
    saved = read_json(job_path / 'job.json')
    values.job(saved, identity=message['job'], workspace=message['workspace'])
    if (values.fingerprint(saved) != message['job_sha256'] or saved['status'] != 'running'
            or saved['remote']['restored']):
        raise PermissionError('The selected remote transcription job changed.')
    binding = saved['remote']
    if (values.fingerprint(message['authorization']) != binding['approval_sha256']
            or auth.id != binding['authorization_id'] or auth.task_id != saved['id'] or auth.max_calls != 1
            or tuple(auth.request_hashes) != (binding['request_sha256'],)):
        raise PermissionError('The ephemeral approval does not match this retained job.')
    source = saved['sources'][0]

    def guard():
        root_guard()
        current = read_json(job_path / 'job.json')
        if values.fingerprint(current) != message['job_sha256']:
            raise PermissionError('The remote transcription changed during its call.')
        profile = ProfileStore(root / 'state' / 'providers').get(binding['profile']['id'])
        if (values.fingerprint(profile.to_dict()) != binding['profile_sha256']
                or not profile.enabled or not profile.credential_ref):
            raise PermissionError('The approved remote transcription profile changed.')
        root_guard()
        return False

    guard()
    profile = ProfileStore(root / 'state' / 'providers').get(binding['profile']['id'])
    class ScopedLedger(UsageLedger):
        def reserve(self, *args, **kwargs):
            guard()
            result = super().reserve(*args, **kwargs)
            guard()
            return result
        def finish(self, *args, **kwargs):
            guard()
            result = super().finish(*args, **kwargs)
            guard()
            return result
    gateway = gateway_factory(root / 'state' / 'providers', mode='APPROVED_REMOTE')
    guard()
    gateway.ledger = ScopedLedger(root / 'state' / 'providers' / 'usage')
    guard()
    result = gateway.request(profile, OPERATION, binding['payload'], task_id=saved['id'],
        data_class='selected-audio', authorization=auth, max_cost=binding['max_cost'],
        operation_id=binding['operation_id'], upload=AudioUpload(job_path / 'input' / source['filename']),
        cancelled=guard, timeout=timeout)
    guard()
    receipt, _ = values.make_receipt(saved, result)
    values.verify_usage(job_path, saved, receipt)
    raw = encode(result)
    if len(raw) > values.MAX_RESULT_BYTES:
        raise ValueError('Remote transcript exceeds its retained response allowance.')
    atomic_bytes(output, raw)
    return {'schema': 'resonance-remote-transcription-return/1', 'sha256': sha256(raw).hexdigest()}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        raise ValueError('The private remote transcription worker requires an exact request and checksum.')
    selected = local_path(argv[0], exists=True)
    values.checksum(argv[1])
    before = regular(selected)
    if not 0 < before.st_size <= 131072:
        raise ValueError('Remote transcription request exceeds its finite allowance.')
    with _open_file_source(selected) as stream:
        opened = os.fstat(stream.fileno())
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != identity:
            raise ValueError('Remote transcription request changed while opening.')
        changed = _stream_change_time(stream)
        raw = stream.read(131073)
        after = regular(local_path(selected, exists=True))
        if (len(raw) != before.st_size or len(raw) > 131072
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != identity
                or _stream_change_time(stream) != changed or sha256(raw).hexdigest() != argv[1]):
            raise ValueError('Remote transcription request changed before decoding.')
    message = json.loads(raw)
    result = execute(message, request_path=selected)
    print(canonical(result), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # No response body, source name, request or credential enters diagnostics.
        print('Remote transcription worker failed: ' + type(error).__name__, file=sys.stderr, flush=True)
        raise SystemExit(1) from None
