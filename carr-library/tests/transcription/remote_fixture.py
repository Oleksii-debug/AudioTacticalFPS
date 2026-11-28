"""Synthetic STT protocol fixtures; no paid endpoint or acoustic inference.

The real Gateway, ledger, retained job, serializers, process supervisor and
Windows Job remain in use. Only the remote HTTPS response and credential lookup
are replaced with this closed synthetic protocol.
"""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import os

RAW = b'RIFF synthetic selected recording; protocol bytes only'
TEXT = 'Synthetic selected speech.'
RESPONSE = {
    'language_code': 'eng', 'language_probability': .99,
    'text': TEXT, 'audio_duration_secs': 2.0, 'transcription_id': 'synthetic-one',
    'words': [
        {'type': 'word', 'text': 'Synthetic', 'start': 0, 'end': .5, 'logprob': -.1},
        {'type': 'spacing', 'text': ' ', 'start': .5, 'end': .5, 'logprob': 0},
        {'type': 'word', 'text': 'selected', 'start': .5, 'end': 1.1, 'logprob': -.1},
        {'type': 'spacing', 'text': ' ', 'start': 1.1, 'end': 1.1, 'logprob': 0},
        {'type': 'word', 'text': 'speech.', 'start': 1.1, 'end': 1.8, 'logprob': -.1},
    ],
}


def profile(**changes):
    from carr_library.assistant.profiles import ProviderProfile
    data = dict(id='stt_account', name='Synthetic dedicated STT', adapter='elevenlabs',
        endpoint='https://api.elevenlabs.io/v1', model='scribe_v2',
        capabilities=('speech.transcribe',), enabled=True,
        credential_ref='carr/provider/stt_account/' + 'a' * 32)
    data.update(changes)
    return ProviderProfile(**data)


class Vault:
    def __init__(self): self.calls = []
    def get(self, identity, reference):
        from carr_library.assistant.credentials import validate_reference
        validate_reference(reference, identity)
        self.calls.append((identity, reference))
        return 'synthetic-stt-secret'


class Response:
    def __init__(self, data=RESPONSE, *, status=200, content_type='application/json', declared=None):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode('utf-8')
        self.stream, self.status = BytesIO(self.raw), status
        self.content_type, self.declared = content_type, str(len(self.raw)) if declared is None else declared
    def getheader(self, name, default=None):
        return {'Content-Type': self.content_type, 'Content-Length': self.declared}.get(name, default)
    def read(self, size): return self.stream.read(size)


class Connection:
    requests, instances, response_factory = [], [], Response
    def __init__(self, host, port=None, **options):
        assert host == 'api.elevenlabs.io' and port in (None, 443)
        assert 0 < options['timeout'] <= 30
        self.closed = False
        type(self).instances.append(self)
    def request(self, method, route, *, body, headers):
        type(self).requests.append((method, route, body, dict(headers)))
    def getresponse(self): return type(self).response_factory()
    def close(self): self.closed = True


def install_gateway(monkeypatch):
    from carr_library.assistant import providers
    Connection.requests, Connection.instances = [], []
    Connection.response_factory = Response
    monkeypatch.setattr(providers.http.client, 'HTTPSConnection', Connection)


def prepared(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.assistant.profiles import ProfileStore
    from carr_library.operations import ResourceBroker, ResourceLimits
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Remote synthetic transcription')
    app.add_text(workspace, 'Unrelated retained book', 'Exact source remains unchanged.')
    ProfileStore(app.root / 'state' / 'providers').save(profile())
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    from carr_library.transcription import TranscriptionService
    service = TranscriptionService(app, workspace, broker=broker)
    selected = tmp_path / 'selected.wav'
    selected.write_bytes(RAW)
    remote = service.remote_transcription()
    return SimpleNamespace(app=app, workspace=workspace, service=service, remote=remote, selected=selected, broker=broker)


def approval(fixture):
    from carr_library.integrations.transcription_remote import approve
    review = fixture.remote.review(fixture.selected, profile_id='stt_account',
        title='Reviewed recording', language='auto', max_cost='0.50')
    return approve(review, consent=True)


def in_process(monkeypatch):
    """Actual worker/Gateway/result publication with only native launch omitted."""
    from carr_library.integrations import transcription_remote as service_module
    from carr_library.assistant import transcription_worker as worker_module
    from carr_library.assistant.providers import Gateway
    from carr_library.durable_state import read_json
    calls = []
    install_gateway(monkeypatch)
    def run(command, **options):
        assert command[1:4] == ['-I', '-B', '-c']
        assert command[4] == service_module.WORKER_BOOTSTRAP
        assert options['owned_windows_job'] is (os.name == 'nt')
        assert not options['cancelled']()
        path = Path(command[-2])
        raw = path.read_bytes()
        assert sha256(raw).hexdigest() == command[-1]
        message = read_json(path)
        result = worker_module.execute(message, request_path=path,
            gateway_factory=lambda directory, **kw: Gateway(directory, vault=Vault(), **kw))
        calls.append(message)
        return SimpleNamespace(stdout=json.dumps(result).encode('utf-8'), stderr=b'', returncode=0)
    monkeypatch.setattr(service_module, 'run_process', run)
    return calls


def real_child(monkeypatch, *, delay=False):
    """Run the unchanged supervisor with an explicit exact source/test bootstrap."""
    from carr_library.integrations import transcription_remote as service_module
    from carr_library.assistant import transcription_worker as worker_module
    actual = service_module.run_process
    helper = Path(__file__).resolve(strict=True)
    calls = []
    # Execute the actual production bootstrap and all its origin checks unchanged.
    # The observer runs only when that bootstrap reaches its one reviewed module.
    program = r'''
import hashlib, importlib.util, pathlib, runpy, sys
production, helper, helper_sha, delay = sys.argv[1:5]
sys.argv = [sys.argv[0], *sys.argv[5:]]
actual_run_module = runpy.run_module
def synthetic_worker(name, *args, **kwargs):
    if name != "carr_library.assistant.transcription_worker" or args or kwargs != {"run_name": "__main__"}:
        raise ValueError("Unexpected remote-STT worker dispatch")
    raw = pathlib.Path(helper).read_bytes()
    if hashlib.sha256(raw).hexdigest() != helper_sha:
        raise ValueError("Synthetic fixture origin changed")
    spec = importlib.util.spec_from_file_location("selected_remote_fixture", helper)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    from carr_library.assistant import providers
    providers.http.client.HTTPSConnection = fixture.Connection
    providers.WindowsCredentialVault = fixture.Vault
    if delay == "yes":
        import time
        pathlib.Path(sys.argv[1]).with_name("child-ready").write_text("ready")
        time.sleep(30)
    return actual_run_module(name, **kwargs)
runpy.run_module = synthetic_worker
exec(compile(production, "<current-remote-STT-bootstrap>", "exec"), {"__name__": "__main__"})
'''
    def run(command, **options):
        assert command[1:4] == ['-I', '-B', '-c']
        assert command[4] == service_module.WORKER_BOOTSTRAP
        calls.append((command, options))
        try:
            return actual([command[0], '-I', '-B', '-c', program, command[4],
                str(helper), sha256(helper.read_bytes()).hexdigest(),
                'yes' if delay else 'no', *command[5:]], **options)
        finally:
            options['fixture_ready'] = Path(command[-2]).with_name('child-ready').exists()
    monkeypatch.setattr(service_module, 'run_process', run)
    return calls
