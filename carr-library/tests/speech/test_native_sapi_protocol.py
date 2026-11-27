"""Protocol/failure checks; actual PCM/COM acceptance runs only on Windows."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.speech import NativeSapiProvider, Voice, VoiceRegistry
from carr_library.speech import runtime, sapi_native_worker


def _render_request(directory):
    return {'version': 1, 'operation': 'render', 'token': 'Installed-token',
        'text': 'Literal <voice>Українська</voice> & $(echo unchanged)',
        'output': str(directory / 'speech.wav'), 'rate': 0, 'volume': 50,
        'punctuation': False}


def test_native_protocol_keeps_text_literal_and_bounds_output_to_private_staging(tmp_path):
    request = _render_request(tmp_path)
    checked = sapi_native_worker._validate(request.copy(), tmp_path)
    assert checked['text'] == request['text']
    with pytest.raises(ValueError, match='private staging'):
        sapi_native_worker._validate(dict(request, output=str(tmp_path.parent / 'original.wav')), tmp_path)
    (tmp_path / 'speech.wav').write_bytes(b'Original source bytes')
    with pytest.raises(ValueError):
        sapi_native_worker._validate(request, tmp_path)
    assert (tmp_path / 'speech.wav').read_bytes() == b'Original source bytes'


@pytest.mark.parametrize('field,value', [('operation', 'shell'), ('version', 2), ('rate', 11),
    ('volume', -1), ('punctuation', 1), ('text', ''), ('token', ''), ('code', 'arbitrary')])
def test_invalid_native_requests_stop_before_loading_com(tmp_path, field, value):
    request = _render_request(tmp_path)
    request[field] = value
    with pytest.raises(ValueError):
        sapi_native_worker._validate(request, tmp_path)


def test_fixed_file_worker_reply_works_without_console_streams(tmp_path, monkeypatch):
    request, reply = tmp_path / 'request.json', tmp_path / 'reply.json'
    request.write_text(json.dumps({'version': 1, 'operation': 'invalid'}))
    monkeypatch.setattr(sys, 'stdin', None)
    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', None)
    assert sapi_native_worker.main([str(request), str(reply)]) == 1
    result = json.loads(reply.read_text())
    assert result == {'version': 1, 'status': 'failed', 'phase': 'validating-request', 'error': 'invalid-request'}


def test_worker_does_not_overwrite_an_existing_reply(tmp_path):
    request, reply = tmp_path / 'request.json', tmp_path / 'reply.json'
    request.write_text('{"version":1,"operation":"voices"}')
    reply.write_text('Preserved')
    assert sapi_native_worker.main([str(request), str(reply)]) == 2
    assert reply.read_text() == 'Preserved'


def test_default_windows_discovery_does_not_start_powershell(monkeypatch):
    metadata = Voice('sapi5-64:fixture', 'Metadata-only native test', 'SAPI 5 (64-bit)', 'en-US')
    class FixtureProvider:
        name = 'fixture'
        def voices(self):
            return (metadata,)
    def forbidden(*args, **kwargs):
        pytest.fail('Default discovery attempted the PowerShell/.NET host.')
    monkeypatch.setattr(runtime, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(runtime, 'NativeSapiProvider', FixtureProvider)
    monkeypatch.setattr(runtime, 'SapiProvider', forbidden)
    monkeypatch.setattr(runtime, 'windows_directory', forbidden)
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: None)
    registry = VoiceRegistry.discover(include_flite=False)
    assert registry.voices() == (metadata,)


def test_native_provider_keeps_existing_token_ids_and_language_mapping(monkeypatch):
    provider = NativeSapiProvider()
    monkeypatch.setattr(provider, '_request', lambda *args, **kwargs: {'version': 1, 'voices': [
        {'token': 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens\\Fixture',
         'name': 'Metadata fixture', 'language': '409', 'vendor': 'Fixture'}]})
    voice = provider.voices()[0]
    from hashlib import sha256
    token = provider._tokens[voice.id]
    assert voice.id == 'sapi5-' + provider.architecture + ':' + sha256(token.encode()).hexdigest()[:24]
    assert voice.language == 'en-US'
    assert voice.capabilities == ('rate', 'volume', 'punctuation')


def test_native_worker_source_uses_only_fixed_sapi_classes_and_plain_text_flags():
    source = Path(sapi_native_worker.__file__).read_text()
    assert "CreateObject('SAPI.SpVoice', dynamic=True" in source
    assert "CreateObject('SAPI.SpFileStream', dynamic=True" in source
    assert "clsctx=comtypes.CLSCTX_INPROC_SERVER" in source
    assert 'flags = 16 | (64 if' in source
    assert 'powershell' not in source.lower().split('"""', 2)[-1]



def test_native_sapi_child_preserves_existing_runtime_bytecode(tmp_path, monkeypatch):
    """Run the actual provider transport with a synthetic, file-only COM reply."""
    import importlib.util
    import py_compile

    sealed = tmp_path / 'sealed-runtime'
    sealed.mkdir()
    module = sealed / 'bytecode_probe.py'
    module.write_text("marker = 'old'\n", encoding='utf-8')
    py_compile.compile(str(module), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    module.write_text("marker = 'current approved source'\n", encoding='utf-8')
    before = {path.relative_to(sealed): path.read_bytes()
              for path in sealed.rglob('*') if path.is_file()}
    helper = tmp_path / 'sapi_native_worker.py'
    helper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(sealed)!r})\n"
        "import bytecode_probe\n"
        "import json, struct\n"
        "from pathlib import Path\n"
        "value = dict(version=1, architecture=str(struct.calcsize('P') * 8), "
        "adapter='native-comtypes', voices=[], marker=bytecode_probe.marker, "
        "dont_write=sys.dont_write_bytecode, isolated=sys.flags.isolated)\n"
        "Path(sys.argv[2]).write_text(json.dumps(value), encoding='utf-8')\n",
        encoding='utf-8')
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec',
                        lambda name, *args, **kwargs: object() if name == 'comtypes'
                        else real_find_spec(name, *args, **kwargs))
    monkeypatch.setattr(runtime, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(runtime, '__file__', str(tmp_path / 'runtime.py'))
    monkeypatch.delattr(sys, 'frozen', raising=False)
    # Isolated mode ignores this environment flag; the child must carry -B.
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    provider = NativeSapiProvider()
    result = provider._request({'operation': 'voices'}, timeout=10)

    assert result['marker'] == 'current approved source'
    assert {path.relative_to(sealed): path.read_bytes()
            for path in sealed.rglob('*') if path.is_file()} == before
    assert result['dont_write'] is True and result['isolated'] == 1
