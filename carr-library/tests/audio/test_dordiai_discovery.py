"""Synthetic installed-helper contracts; no private voice data or neural models.

The subprocess uses the test interpreter. The hashed installation's interpreter
file is deliberately a marker: tests redirect only that executable selection.
All discovery, JSON parsing, package filtering and worker I/O then execute in
real isolated processes against original synthetic helper implementations.
"""
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
import wave

import pytest

from carr_library.audio import engines
from carr_library.audio.dordiai_discovery import DISCOVERY_PROTOCOL, MAX_RESPONSE_BYTES, parse_discovery
from carr_library.audio.engines import DordiaiVoicesEngine, EngineUnavailable
from carr_library.audio.model_manifest import ModelManifestError


POLICY = '''
def filter_voice_catalog(voices):
    return {identity: label for identity, label in voices.items() if identity != 'blocked-demo'}
'''

LANGUAGES = '''
import json
def canonical_code(code):
    return code.lower().replace('_', '-').split('-')[0]
def data(models):
    return json.loads((models / 'availability.json').read_text(encoding='utf-8'))
def installed_languages(models):
    return {code: code for code in data(models)['installed']}
def compatible_voice_ids(models, language, allowed_voice_ids):
    return tuple(identity for identity in data(models)['builtin'].get(language, [])
                 if identity in allowed_voice_ids)
'''

PACKAGES = '''
import json
def discover_voices(references, engine_id=None):
    assert engine_id == 'holos'
    return json.loads((references / 'packages.json').read_text(encoding='utf-8'))
'''

WORKER = '''
import base64,json,sys
request=json.loads(sys.stdin.readline())
assert request['mode']=='reference' and request['profile']=='fixture-alpha'
assert request['language']=='uk' and request['text']=='Original synthetic sentence.'
print(json.dumps({'chunk':base64.b64encode(b'\\x01\\x00'*48).decode()}),flush=True)
assert json.loads(sys.stdin.readline())=={'mode':'next'}
print(json.dumps({'finished':True}),flush=True)
'''


def write_manifest(root, runtime, **settings):
    path = root / 'installation.json'
    files = {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
             for p in root.rglob('*') if p.is_file() and p != path}
    prefix = runtime.relative_to(root).as_posix()
    path.write_text(json.dumps({
        'schema_version': 1, 'engine': 'dordiai-voices', 'model_id': 'synthetic-runtime',
        'revision': 'a' * 40, 'license': 'synthetic-test-only', 'license_accepted': True,
        'files': files, 'settings': {
            'protocol': 'nvda-worker-jsonl-pcm24k-v1',
            'python': f'{prefix}/python/python.exe', 'worker': f'{prefix}/nvda_worker.py',
            'catalog': f'{prefix}/models/voices/catalog.json', **settings}}), encoding='utf-8')
    return path


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / 'Installed synthetic voices'
    runtime = root / 'runtime Україна'
    refs = runtime / 'models/voices'
    refs.mkdir(parents=True)
    (runtime / 'models/Holos').mkdir()
    (runtime / 'python').mkdir()
    interpreter = runtime / 'python/python.exe'
    interpreter.write_bytes(b'Synthetic marker; the fixture explicitly selects the test interpreter.')
    for name, source in (('voice_policy.py', POLICY), ('language_catalog.py', LANGUAGES),
                         ('voice_assets.py', PACKAGES), ('nvda_worker.py', WORKER)):
        (runtime / name).write_text(source, encoding='utf-8')
    (refs / 'catalog.json').write_text(json.dumps({
        'fixture-alpha': 'Synthetic Alpha', 'fixture-beta': 'Synthetic Beta',
        'fixture-uninstalled': 'Unavailable target', 'blocked-demo': 'Excluded synthetic entry'}), encoding='utf-8')
    (refs / 'packages.json').write_text('{}', encoding='utf-8')
    (runtime / 'models/availability.json').write_text(json.dumps({
        'installed': ['uk', 'sk'],
        'builtin': {'uk': ['fixture-alpha', 'blocked-demo'], 'sk': ['fixture-beta']}}), encoding='utf-8')
    original = engines._binary

    def test_interpreter(path):
        if Path(path) == interpreter:
            return Path(sys.executable).resolve()
        return original(path)

    monkeypatch.setattr(engines, '_binary', test_interpreter)
    return root, runtime


def engine(installed, **kwargs):
    root, runtime = installed
    return DordiaiVoicesEngine(write_manifest(root, runtime), **kwargs)


def package(identity, *, languages=('uk', 'sk'), roles=('holos_profile', 'target_embedding')):
    return {'id': identity, 'name': 'Synthetic installed package',
            'engine': {'id': 'holos', 'compatibility_version': 1},
            'languages': list(languages),
            'files': {f'synthetic-{number}.bin': {'role': role}
                      for number, role in enumerate(roles)}}


def test_constructor_and_real_discovery_accept_actual_flat_catalog_shape(installed):
    provider = engine(installed)
    rows = provider.voices()
    assert [(row.id, row.name, row.languages) for row in rows] == [
        ('dordiai-voices:fixture-alpha', 'Synthetic Alpha', ('uk',)),
        ('dordiai-voices:fixture-beta', 'Synthetic Beta', ('sk',))]
    assert provider._languages == ('uk', 'sk')
    assert provider.cache_identity(rows[0].id)['discovery'] == DISCOVERY_PROTOCOL
    assert not list(installed[0].rglob('*.pyc'))


def test_constructor_discovery_and_existing_pcm_transport_work_together(installed, tmp_path):
    provider = engine(installed)
    voice = provider.voices()[0]
    output = provider.synthesize('Original synthetic sentence.', tmp_path / 'actual-protocol.wav',
                                voice=voice.id, language='uk')
    with wave.open(str(output), 'rb') as data:
        assert data.getframerate() == 24000 and data.getnframes() == 48
        assert data.readframes(48) == b'\x01\x00' * 48


def test_missing_resources_do_not_advertise_catalog_targets(installed):
    _, runtime = installed
    (runtime / 'models/availability.json').write_text('{"installed":[],"builtin":{}}', encoding='utf-8')
    provider = engine(installed)
    with pytest.raises(EngineUnavailable, match='No installed.*compatible language resources'):
        provider.voices()
    assert provider._profiles == {}


def test_managed_package_overlays_and_required_roles_are_respected(installed):
    _, runtime = installed
    documents = {
        'fixture-package': package('fixture-package', languages=('uk', 'sk', 'en')),
        # Overrides the built-in uk profile, but lacks sk's target embedding.
        'fixture-alpha': package('fixture-alpha', languages=('sk',), roles=('holos_profile',)),
        'blocked-demo': package('blocked-demo'),
    }
    (runtime / 'models/voices/packages.json').write_text(json.dumps(documents), encoding='utf-8')
    rows = engine(installed).voices()
    assert {row.id: row.languages for row in rows} == {
        'dordiai-voices:fixture-beta': ('sk',), 'dordiai-voices:fixture-package': ('uk', 'sk')}


def test_unsupported_voice_language_fails_before_render(installed, tmp_path, monkeypatch):
    provider = engine(installed)
    provider.voices()
    monkeypatch.setattr(provider, '_stream', lambda *a, **k: pytest.fail('Unsupported language cannot render.'))
    with pytest.raises(EngineUnavailable, match='profile has no compatible'):
        provider.synthesize('Synthetic text', tmp_path / 'absent.wav',
                            voice='dordiai-voices:fixture-alpha', language='sk')
    assert not (tmp_path / 'absent.wav').exists()


def test_wrong_model_catalog_path_is_rejected_before_any_process(installed, monkeypatch):
    root, runtime = installed
    elsewhere = root / 'other-models'
    elsewhere.mkdir()
    (elsewhere / 'catalog.json').write_text('{}', encoding='utf-8')
    path = write_manifest(root, runtime, catalog='other-models/catalog.json')
    monkeypatch.setattr(engines, '_run', lambda *a, **k: pytest.fail('Invalid layout cannot start a process.'))
    with pytest.raises(ModelManifestError, match='worker model directory'):
        DordiaiVoicesEngine(path)


def test_missing_required_installed_helper_fails_before_discovery(installed):
    root, runtime = installed
    (runtime / 'voice_policy.py').unlink()
    with pytest.raises(ModelManifestError, match='not in the verified manifest'):
        DordiaiVoicesEngine(write_manifest(root, runtime))


@pytest.mark.parametrize('payload', [
    b'not-json', b'[]', b'{"protocol":"other","voices":[]}',
    b'{"protocol":"carr.dordiai-installed-voices.v1","voices":[],"voices":[]}',
    json.dumps({'protocol': DISCOVERY_PROTOCOL, 'voices': [
        {'id': '../escape', 'name': 'Synthetic', 'languages': ['uk']}]}).encode(),
    json.dumps({'protocol': DISCOVERY_PROTOCOL, 'voices': [
        {'id': 'demo', 'name': 'Synthetic', 'languages': ['https://remote.invalid']}]}).encode(),
    json.dumps({'protocol': DISCOVERY_PROTOCOL, 'voices': [
        {'id': 'demo', 'name': 'Synthetic', 'languages': []}]}).encode(),
    pytest.param(b'x' * (MAX_RESPONSE_BYTES + 1), id='oversized-response'),
])
def test_malformed_discovery_response_never_publishes_voices(installed, monkeypatch, payload):
    provider = engine(installed)
    monkeypatch.setattr(engines, '_run', lambda *a, **k: payload)
    with pytest.raises(ModelManifestError, match='discovery response'):
        provider.voices()
    assert provider._profiles == {}


def test_discovery_cancels_a_real_hung_helper_process(installed):
    _, runtime = installed
    (runtime / 'voice_policy.py').write_text('import time\ntime.sleep(20)\n' + POLICY, encoding='utf-8')
    provider = engine(installed)
    started = time.monotonic()
    with pytest.raises(InterruptedError):
        provider.voices(cancelled=lambda: time.monotonic() - started > .15)
    assert time.monotonic() - started < 4


def test_discovery_timeout_is_bounded(installed):
    _, runtime = installed
    (runtime / 'voice_policy.py').write_text('import time\ntime.sleep(20)\n' + POLICY, encoding='utf-8')
    provider = engine(installed, timeout=.1)
    with pytest.raises(TimeoutError):
        provider.voices()


@pytest.mark.parametrize('attempt', [
    'import socket; socket.socket()',
    'import subprocess; subprocess.Popen(["unexpected-child"] )',
])
def test_discovery_does_not_allow_network_or_child_processes(installed, attempt):
    _, runtime = installed
    (runtime / 'voice_policy.py').write_text(attempt + '\n' + POLICY, encoding='utf-8')
    provider = engine(installed)
    with pytest.raises(EngineUnavailable, match='exit code'):
        provider.voices()


def test_discovery_redacts_helper_errors(installed):
    _, runtime = installed
    (runtime / 'voice_policy.py').write_text('raise RuntimeError("PRIVATE TEST LABEL")', encoding='utf-8')
    with pytest.raises(EngineUnavailable) as caught:
        engine(installed).voices()
    assert 'PRIVATE TEST LABEL' not in str(caught.value)


def test_oversized_real_process_output_is_stopped(installed):
    _, runtime = installed
    (runtime / 'voice_policy.py').write_text('print("x" * (1024 * 1024 + 4096))\n' + POLICY, encoding='utf-8')
    with pytest.raises(RuntimeError, match='byte budget'):
        engine(installed).voices()


def test_source_runtime_change_after_discovery_does_not_leave_usable_cached_voice(installed, monkeypatch):
    _, runtime = installed
    provider = engine(installed)
    original = engines._run

    def change_after_process(*args, **kwargs):
        output = original(*args, **kwargs)
        (runtime / 'models/availability.json').write_text('{}', encoding='utf-8')
        return output

    monkeypatch.setattr(engines, '_run', change_after_process)
    with pytest.raises(ModelManifestError, match='Checksum mismatch'):
        provider.voices()
    assert provider._profiles == {}
