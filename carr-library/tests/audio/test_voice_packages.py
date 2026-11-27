"""Actual child/JSON and file-boundary tests; every voice/model here is synthetic."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time
import wave
import zipfile

import pytest

from carr_library.audio import model_voices, voice_packages
from carr_library.audio.voice_manifest import LocalModelManifest, ModelManifestError
from carr_library.audio.voice_packages import VoicePackageManager
from carr_library.audio.voice_settings import VoiceSettingsStore
from carr_library.audio.voices import VoiceRegistry


POLICY = """def is_voice_allowed(identity): return identity != 'blocked-fixture'
def require_voice_allowed(identity):
    if not is_voice_allowed(identity): raise ValueError('excluded synthetic profile')
"""
PACKAGES = r"""
import json,hashlib,zipfile
from pathlib import Path
from voice_policy import require_voice_allowed
def load_manifest(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def verify_assets(directory,document,hashes=True):
    for name,row in document['files'].items():
        data=(Path(directory)/name).read_bytes()
        assert len(data)==row['size_bytes']
        if hashes: assert hashlib.sha256(data).hexdigest()==row['sha256']
    return document
def inspect_package(path):
    with zipfile.ZipFile(path) as archive:
        value=json.loads(archive.read('voice-package.json'))
        require_voice_allowed(value['id'])
        for name,row in value['files'].items():
            data=archive.read(name)
            assert hashlib.sha256(data).hexdigest()==row['sha256']
    return value
def list_installed_packages(root):
    path=Path(root)/'voice-packages.json'
    if not path.exists(): return []
    index=load_manifest(path)
    return [{'id':key} for key in {*index['active'],*index.get('inactive',{})}]

def _case(root):
    path=Path(root).parents[3]/'voice-maintenance-case.txt'
    return path.read_text() if path.exists() else ''
from contextlib import contextmanager
@contextmanager
def _installation_lock(root):
    # Synthetic owner fixture uses an actual OS lock outside the reviewed
    # installation; the real owner's lock protocol is separately source pinned.
    from filelock import FileLock
    root=Path(root)
    with FileLock(str(root.parents[3]/'.synthetic-voice-maintenance.lock'),timeout=0):
        if _case(root)=='change-index-before-yield':
            path=root/'voice-packages.json'
            value=load_manifest(path)
            value['active']['fixture']='9.0.0'
            path.write_text(json.dumps(value),encoding='utf-8')
        yield
def create_package(source,document,output):
    import time
    source,output=Path(source),Path(output)
    root=source.parents[1] if source.parent.name=='fixture' else None
    case=_case(root) if root is not None and root.name=='packages' else ''
    if case=='slow-export':
        marker=root.parents[3]/'voice-export-started.txt'
        marker.write_text('synthetic owner create entered')
        time.sleep(20)
    verify_assets(source,document)
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('voice-package.json',json.dumps(document,ensure_ascii=False))
        for name in sorted(document['files']):
            archive.writestr(name,(source/name).read_bytes())
    inspect_package(output)
    return output
def uninstall_package(root,profile,keep_files=False):
    import shutil,time
    root=Path(root)
    with _installation_lock(root):
        case=_case(root)
        (root.parents[3]/'voice-removal-entered.txt').write_text('reviewed lock yielded')
        if case=='slow-removal':
            time.sleep(20)
        path=root/'voice-packages.json'
        value=load_manifest(path)
        version=value['active'].get(profile,value.get('inactive',{}).get(profile))
        if version is None: raise ValueError('unknown synthetic profile')
        value['active'].pop(profile,None)
        value.setdefault('inactive',{})[profile]=version
        value['format_version']=2
        path.write_text(json.dumps(value),encoding='utf-8')
        if not keep_files:
            directory=root/profile
            if case=='partial-delete':
                next(path for path in sorted(directory.rglob('*')) if path.is_file()).unlink()
                raise OSError('synthetic interrupted deletion')
            if directory.exists(): shutil.rmtree(directory)
            value['inactive'].pop(profile)
            if not value['inactive']:
                value.pop('inactive')
                value['format_version']=1
            path.write_text(json.dumps(value),encoding='utf-8')
        if case=='change-shared-model':
            (root.parents[2]/'models/Holos/model.bin').write_bytes(b'changed unselected fixture model')
        if case=='add-unreviewed-file':
            (root.parents[2]/'foreign.dat').write_bytes(b'unapproved file')
    return root/profile

"""
ASSETS = r"""
import json,shutil,time
from pathlib import Path
import voice_packages as packages
from voice_policy import require_voice_allowed
inspect_package=packages.inspect_package
def package_root(references): return Path(references)/'packages'
def clear_cache(): pass
def validate_adapter(document): return document
def discover_voices(references,engine_id=None):
    root=package_root(references)
    index=root/'voice-packages.json'
    if not index.exists(): return {}
    found={}
    for identity,version in packages.load_manifest(index)['active'].items():
        document=packages.load_manifest(root/identity/version/'voice-package.json')
        if document['engine']['id']==engine_id:
            found[identity]={**document,'directory':str(root/identity/version)}
    return found
def voice_versions(references,profile):
    root=package_root(references)/profile
    return [packages.load_manifest(path/'voice-package.json') for path in sorted(root.iterdir())] if root.is_dir() else []
def _activate(root,document):
    root.mkdir(parents=True,exist_ok=True)
    (root/'.voice-packages.lock').write_bytes(b'0')
    path=root/'voice-packages.json'
    index=packages.load_manifest(path) if path.exists() else {'format_version':1,'active':{}}
    index['active'][document['id']]=document['version']
    index.get('inactive',{}).pop(document['id'],None)
    path.write_bytes(json.dumps(index,ensure_ascii=False).encode('utf-8'))
def install_voice_package(package,references,engine_id='holos'):
    import zipfile
    document=inspect_package(package)
    assert document['engine']['id']==engine_id
    if document['provenance']['source']=='fixture:slow': time.sleep(20)
    if document['provenance']['source']=='fixture:socket':
        import socket
        socket.create_connection(('127.0.0.1',9))
    if document['provenance']['source']=='fixture:child':
        import subprocess,sys
        subprocess.run([sys.executable,'-c','raise SystemExit(0)'],check=True)
    root=package_root(references)
    directory=root/document['id']/document['version']
    directory.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        for name in ['voice-package.json',*document['files']]:
            target=directory/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(archive.read(name))
    _activate(root,document)
    return directory
def activate_voice_version(references,voice,version,engine_id='holos'):
    root=package_root(references)
    document=packages.load_manifest(root/voice/version/'voice-package.json')
    packages.verify_assets(root/voice/version,document)
    assert document['engine']['id']==engine_id
    _activate(root,document)
    return root/voice/version
"""
WORKER = r"""
import argparse,base64,json,sys
parser=argparse.ArgumentParser()
parser.add_argument('models')
for key in ('reference-model','references','reference-engine','preload-language'):
    parser.add_argument('--'+key)
args=parser.parse_args()
request=json.loads(sys.stdin.readline())
assert request['profile']=='fixture' and request['language'] in {'uk','sk'}
if args.reference_engine=='omnivoice':
    assert request['stream'] is False and request.get('nativeProfile') is False
    assert 'wideLocale' not in request and 'nativeLocale' not in request
print(json.dumps({'pcm':base64.b64encode(b'\x01\x00'*120).decode()}),flush=True)
"""


def write_json(path, value):
    path.write_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / 'reviewed-runtime'
    root.mkdir()
    assets = {'python/python.exe': b'INERT interpreter marker; actual child uses test interpreter',
              'nvda_worker.py': WORKER.encode(), 'voice_policy.py': POLICY.encode(),
              'voice_packages.py': PACKAGES.encode(), 'voice_assets.py': ASSETS.encode(),
              'models/Holos/model.bin': b'SYNTHETIC model, never loaded',
              'models/OmniVoice/model.bin': b'SYNTHETIC model, never loaded',
              'models/OpenVoice/model.bin': b'SYNTHETIC model, never loaded',
              'models/voices/catalog.json': json.dumps({'voices': {
                  'legacy': {'name': 'Synthetic legacy'}, 'blocked-fixture': {'name': 'Excluded fixture'}},
                  'languages': {'uk': 'Ukrainian', 'sk': 'Slovak'}}).encode()}
    for name, content in assets.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    selected = root / 'manifest.json'
    write_json(selected, {'schema_version': 1, 'engine': 'dordiai-voices', 'model_id': 'synthetic-owner-api',
        'revision': 'f' * 40, 'license': 'Synthetic protocol fixture, no voice assets',
        'files': {name: sha256(content).hexdigest() for name, content in assets.items()},
        'settings': {'protocol': 'nvda-worker-jsonl-pcm24k-v1', 'profile_languages': {
            'legacy': {'supported': ['uk'], 'candidates': ['sk']}}},
        'license_accepted': True, 'trusted_weights': True, 'personal_noncommercial': True})
    real_run = model_voices._run
    invocations = []
    def synthetic_interpreter(command, **kwargs):
        assert Path(command[0]) == selected.parent / 'python/python.exe'
        assert command[1:4] == ['-I', '-B', '-u']
        invocations.append(tuple(command))
        return real_run([sys.executable, *command[1:]], **kwargs)
    monkeypatch.setattr(model_voices, '_run', synthetic_interpreter)
    library = tmp_path / 'library'
    store = VoiceSettingsStore(library)
    store.register({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
                    'dordiai_manifest': str(selected)}, expected_revision=0)
    return library, selected, VoicePackageManager(library), invocations


def package(path, *, version='1.0.0', backend='omnivoice', source='fixture:local',
            identity='fixture', extra=None):
    data = {'sample.wav': b'INERT reference bytes; not a voice sample',
            'sample.txt': 'Синтетичний опис.'.encode()}
    if backend == 'holos':
        data = {'profile.dat': b'INERT holos profile'}
        roles = {'profile.dat': 'holos_profile'}
    elif backend == 'openvoice':
        data = {'embedding.dat': b'INERT openvoice profile'}
        roles = {'embedding.dat': 'target_embedding'}
    else:
        roles = {'sample.wav': 'reference_audio', 'sample.txt': 'reference_text'}
    if extra:
        data.update(extra)
        roles.update({key: 'extra_' + str(index) for index, key in enumerate(extra)})
    document = {'format': 'dordiai-voice-package', 'format_version': 1,
        'id': identity, 'version': version, 'name': 'Synthetic owner reference',
        'engine': {'id': backend, 'compatibility_version': 1}, 'languages': ['uk', 'sk'],
        'styles': ['neutral'], 'provenance': {'source': source, 'license': 'Synthetic fixture only'},
        'files': {name: {'sha256': sha256(value).hexdigest(), 'size_bytes': len(value), 'role': roles[name]}
                  for name, value in data.items()}}
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('voice-package.json', json.dumps(document, ensure_ascii=False).encode())
        for name, value in data.items():
            archive.writestr(name, value)
    return document


def configure(manager, backend='omnivoice'):
    return manager.configure(backend, 'models', 'models/' + {
        'holos': 'Holos', 'omnivoice': 'OmniVoice', 'openvoice': 'OpenVoice'}[backend],
        'models/voices', expected_revision=manager.store.read()['revision'])


def test_actual_owner_api_install_explicit_reference_rights_and_registry_language_binding(installed, tmp_path):
    library, manifest, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    document = package(archive)
    review = manager.inspect(archive)
    assert 'reference_audio' in review.summary() and 'Speech, speaker similarity' in review.summary()
    before = len(calls)
    with pytest.raises(ValueError, match='permission'):
        manager.apply(review, rights_approved=False, supported_languages=['uk'])
    assert len(calls) == before and not (manifest.parent / 'models/voices/packages').exists()
    manager.apply(review, rights_approved=True, supported_languages=['uk'])
    runtime = LocalModelManifest.load(manifest)
    runtime.verify(require_complete=True)
    assert runtime.settings['profile_reviews']['fixture']['rights_approved'] is True
    assert runtime.settings['profile_reviews']['fixture']['references'] == {
        row['role']: row['sha256'] for row in document['files'].values()}
    registry = VoiceRegistry.for_library(library)
    voice, = registry.list_voices()
    assert voice.id == 'dordiai-voices:fixture' and voice.languages == ('uk',)
    assert voice.candidate_languages == ('sk',)
    assert registry.voices_for_language('sk') == ()
    assert all('--reference-engine' not in command for command in calls)  # metadata calls never infer


@pytest.mark.parametrize('backend', ['holos', 'openvoice', 'omnivoice'])
def test_all_three_reviewed_backend_commands_use_pinned_relative_paths_and_actual_child_pcm(installed, tmp_path, monkeypatch, backend):
    library, manifest, manager, _ = installed
    configure(manager, backend)
    archive = tmp_path / 'reference.zip'
    package(archive, backend=backend)
    manager.apply(manager.inspect(archive), rights_approved=True, supported_languages=['uk'])
    engine = model_voices.DordiaiVoicesEngine(manifest)
    engine.voices()
    original = engine._stream
    commands = []
    def actual_fixture(command, *args, **kwargs):
        commands.append(command)
        return original([sys.executable, *command[1:]], *args, **kwargs)
    monkeypatch.setattr(engine, '_stream', actual_fixture)
    target = tmp_path / 'synthetic-protocol.wav'
    engine.synthesize('Literal synthetic protocol; no acoustic inference.', target,
                      voice='dordiai-voices:fixture', language='uk')
    command, = commands
    assert command[command.index('--reference-engine') + 1] == backend
    assert command[command.index('--references') + 1] == str(manifest.parent / 'models/voices')
    assert command[1:4] == ['-I', '-B', '-u']
    with wave.open(str(target), 'rb') as reader:
        assert reader.getparams()[:3] == (1, 2, 24000)
        assert reader.getnframes() == 120
    identity = engine.cache_identity('dordiai-voices:fixture')
    assert identity['reference_engine'] == backend and len(identity['owner_package_bridge_sha256']) == 64


def test_version_activation_rebinds_rights_and_asset_identity_without_changing_voice_id(installed, tmp_path):
    library, manifest, manager, _ = installed
    configure(manager)
    for version in ('1.0.0', '2.0.0'):
        archive = tmp_path / (version + '.zip')
        package(archive, version=version)
        manager.apply(manager.inspect(archive), rights_approved=True, supported_languages=['uk'])
    before = VoiceRegistry.for_library(library).fingerprint()
    assert {row['version'] for row in manager.versions('fixture')} == {'1.0.0', '2.0.0'}
    review = manager.review_version('fixture', '1.0.0')
    manager.apply(review, rights_approved=True, supported_languages=['sk'])
    registry = VoiceRegistry.for_library(library)
    assert registry.choose_voice('sk').id == 'dordiai-voices:fixture'
    assert registry.voices_for_language('uk') == ()
    assert registry.fingerprint() != before
    assert LocalModelManifest.load(manifest).settings['profile_reviews']['fixture']['version'] == '1.0.0'


@pytest.mark.parametrize('field,value', [
    ('reference_engine', 'automatic-online'), ('references', '../outside'),
    ('reference_model', '/absolute'), ('models', r'C:\models'),
    ('references', 'models/voices:stream'), ('managed_profiles', 1),
])
def test_backend_settings_reject_unknown_engine_and_path_escape_before_child(installed, field, value):
    _, selected, _, calls = installed
    raw = json.loads(selected.read_bytes())
    raw['settings'][field] = value
    write_json(selected, raw)
    before = len(calls)
    with pytest.raises((ValueError, ModelManifestError)):
        model_voices.DordiaiVoicesEngine(selected)
    assert len(calls) == before


@pytest.mark.parametrize('language', ['en', 'uk,sk', 'UK@'])
def test_review_cannot_promote_language_missing_from_selected_profile(installed, tmp_path, language):
    _, _, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    before = len(calls)
    with pytest.raises(ValueError):
        manager.apply(review, rights_approved=True, supported_languages=[language])
    assert len(calls) == before


@pytest.mark.parametrize('extra', [{'plugin.py': b'raise RuntimeError()'},
                                  {'../outside.wav': b'outside'}, {'bad.exe': b'PE fixture'}])
def test_inspect_rejects_executable_or_traversing_data_even_if_owner_fixture_is_permissive(installed, tmp_path, extra):
    _, _, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'bad.zip'
    package(archive, extra=extra)
    with pytest.raises((ValueError, ModelManifestError)):
        manager.inspect(archive)
    assert not (tmp_path / 'outside.wav').exists()


def test_changed_archive_or_changed_registration_invalidates_review_without_install(installed, tmp_path):
    _, manifest, manager, calls = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    package(archive, version='2.0.0')
    before = len(calls)
    with pytest.raises(ValueError, match='changed'):
        manager.apply(review, rights_approved=True, supported_languages=[])
    assert len(calls) == before and not (manifest.parent / 'models/voices/packages').exists()


def test_restored_registration_blocks_package_inspection_before_external_read(installed, tmp_path, monkeypatch):
    _, manifest, manager, calls = installed
    value = manager.store.read()
    value['restored'] = True
    write_json(manager.store.path, value)
    before = len(calls)
    monkeypatch.setattr(voice_packages, 'local_path', lambda *a, **k: pytest.fail('must not read external package'))
    with pytest.raises(ValueError, match='Restored'):
        manager.inspect(tmp_path / 'missing.zip')
    assert len(calls) == before


@pytest.mark.parametrize('source', ['fixture:socket', 'fixture:child'])
def test_owner_maintenance_keeps_network_and_arbitrary_child_process_denied(installed, tmp_path, source):
    _, manifest, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'denied.zip'
    package(archive, source=source)
    review = manager.inspect(archive)
    with pytest.raises(model_voices.EngineUnavailable):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert not (manifest.parent / 'models/voices/packages').exists()


def test_owner_install_cancellation_reaps_actual_child_before_publication(installed, tmp_path):
    _, manifest, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'slow.zip'
    package(archive, source='fixture:slow')
    review = manager.inspect(archive)
    began = time.monotonic()
    with pytest.raises(InterruptedError):
        manager.apply(review, rights_approved=True, supported_languages=['uk'],
                      cancelled=lambda: time.monotonic() - began > .4)
    assert time.monotonic() - began < 3
    assert not (manifest.parent / 'models/voices/packages').exists()
    assert manager.store.read()['revision'] == review.settings_revision


def test_owner_legacy_policy_is_applied_before_enumeration(installed):
    library, _, manager, _ = installed
    configure(manager, 'holos')
    registry = VoiceRegistry.for_library(library)
    assert not any(voice.id.endswith(':blocked-fixture') for voice in registry.list_voices())
    status = next(row for row in registry.engine_status() if row['engine'] == 'dordiai-voices')
    assert not any(row['id'].endswith(':blocked-fixture') for row in status['profiles'])



def test_helper_bytes_changed_between_parent_verification_and_child_load_are_rejected(installed, tmp_path, monkeypatch):
    _, selected, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    run = model_voices._run
    def replace_after_capture(command, **kwargs):
        (selected.parent / 'voice_policy.py').write_text('raise RuntimeError("unreviewed replacement")', encoding='utf-8')
        return run(command, **kwargs)
    monkeypatch.setattr(model_voices, '_run', replace_after_capture)
    with pytest.raises(model_voices.EngineUnavailable):
        manager.inspect(archive)


def test_captured_owner_modules_do_not_execute_a_matching_malicious_bytecode_cache(installed, tmp_path, monkeypatch):
    import importlib.util
    import marshal
    import struct
    _, selected, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    marker = tmp_path / 'must-not-run.txt'
    run = model_voices._run
    def plant_cache(command, **kwargs):
        helper = selected.parent / 'voice_policy.py'
        stat = helper.stat()
        cache = Path(importlib.util.cache_from_source(str(helper)))
        cache.parent.mkdir(exist_ok=True)
        payload = compile('from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("executed")',
                          str(helper), 'exec')
        cache.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack('<III', 0, int(stat.st_mtime), stat.st_size)
                          + marshal.dumps(payload))
        return run(command, **kwargs)
    monkeypatch.setattr(model_voices, '_run', plant_cache)
    assert manager.inspect(archive).document['id'] == 'fixture'
    assert not marker.exists()


def test_installation_cannot_silently_reapprove_another_registered_executable(installed, tmp_path, monkeypatch):
    _, selected, manager, _ = installed
    configure(manager)
    other = tmp_path / 'other-local-program'
    other.write_bytes(b'original unrelated program')
    value = manager.store.read()
    value['config']['espeak_ng'] = str(other)
    value['bindings']['espeak_ng'] = sha256(other.read_bytes()).hexdigest()
    write_json(manager.store.path, value)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    query = voice_packages._query
    def external_change(manifest, action, **kwargs):
        result = query(manifest, action, **kwargs)
        if action == 'install':
            other.write_bytes(b'different unrelated program')
        return result
    monkeypatch.setattr(voice_packages, '_query', external_change)
    with pytest.raises(ValueError, match='registered runtime changed'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert manager.store.read() == value
    assert LocalModelManifest.load(selected).manifest_sha256 == review.manifest_sha256


def test_interrupted_after_actual_install_never_publishes_an_unreviewed_registration(installed, tmp_path, monkeypatch):
    _, selected, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    value = manager.store.read()
    query = voice_packages._query
    state = {'cancelled': False}
    def stop_after_install(manifest, action, **kwargs):
        result = query(manifest, action, **kwargs)
        if action == 'install':
            state['cancelled'] = True
        return result
    monkeypatch.setattr(voice_packages, '_query', stop_after_install)
    with pytest.raises(InterruptedError):
        manager.apply(review, rights_approved=True, supported_languages=['uk'],
                      cancelled=lambda: state['cancelled'])
    assert manager.store.read() == value
    assert (selected.parent / 'models/voices/packages/fixture/1.0.0/sample.wav').is_file()
    with pytest.raises(ModelManifestError, match='missing from its reviewed manifest'):
        LocalModelManifest.load(selected).verify(require_complete=True)


def test_installer_cannot_modify_unrelated_reviewed_runtime_assets(installed, tmp_path, monkeypatch):
    _, selected, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    query = voice_packages._query
    def tamper_runtime(manifest, action, **kwargs):
        result = query(manifest, action, **kwargs)
        if action == 'install':
            (selected.parent / 'models/Holos/model.bin').write_bytes(b'unapproved altered model')
        return result
    monkeypatch.setattr(voice_packages, '_query', tamper_runtime)
    with pytest.raises(ModelManifestError, match='existing reviewed runtime asset changed'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert manager.store.read()['revision'] == review.settings_revision


def test_owner_data_lock_must_not_be_a_hard_link_to_another_file(installed, tmp_path):
    import os
    _, selected, manager, _ = installed
    configure(manager)
    root = selected.parent / 'models/voices/packages'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'')
    os.link(outside, root / '.voice-packages.lock')
    raw = json.loads(selected.read_bytes())
    raw['files']['models/voices/packages/.voice-packages.lock'] = sha256(b'').hexdigest()
    write_json(selected, raw)
    value = manager.store.read()
    value['bindings']['dordiai_manifest'] = sha256(selected.read_bytes()).hexdigest()
    write_json(manager.store.path, value)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    with pytest.raises(ModelManifestError, match='linked files'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert outside.read_bytes() == b''



def test_missing_installed_metadata_cannot_be_sealed_as_a_successful_profile(installed, tmp_path, monkeypatch):
    _, selected, manager, _ = installed
    configure(manager)
    archive = tmp_path / 'reference.zip'
    package(archive)
    review = manager.inspect(archive)
    original = manager.store.read()
    query = voice_packages._query
    def remove_metadata(manifest, action, **kwargs):
        result = query(manifest, action, **kwargs)
        if action == 'install':
            (selected.parent / 'models/voices/packages/fixture/1.0.0/voice-package.json').unlink()
        return result
    monkeypatch.setattr(voice_packages, '_query', remove_metadata)
    with pytest.raises(ModelManifestError, match='omitted package data'):
        manager.apply(review, rights_approved=True, supported_languages=['uk'])
    assert manager.store.read() == original
    assert LocalModelManifest.load(selected).manifest_sha256 == review.manifest_sha256


# Lifecycle cases use the same deliberately synthetic owner API above, through
# actual isolated children. They do not imply speaker/model or acoustic quality.
@pytest.fixture
def lifecycle_ready(installed, tmp_path):
    from carr_library.audio.voice_lifecycle import VoiceProfileLifecycle
    from carr_library.operations import ResourceBroker, ResourceLimits
    library, manifest, manager, calls = installed
    configure(manager)
    for identity, versions in [('fixture', ('1.0.0', '2.0.0')), ('other', ('1.0.0',))]:
        for version in versions:
            archive = tmp_path / (identity + '-' + version + '.zip')
            package(archive, identity=identity, version=version)
            manager.apply(manager.inspect(archive), rights_approved=True, supported_languages=['uk'])
    broker = ResourceBroker.for_app(library, limits=ResourceLimits(
        cpu_slots=2, memory_bytes=512 * 1024**2, disk_bytes=64 * 1024**2,
        min_free_disk_bytes=0))
    lifecycle = VoiceProfileLifecycle(library, manager=manager, broker=broker)
    return library, manifest, manager, lifecycle, calls


def test_reference_export_uses_actual_owner_package_and_contains_only_reviewed_version_data(lifecycle_ready, tmp_path):
    library, selected, manager, lifecycle, calls = lifecycle_ready
    before = selected.read_bytes()
    registry = VoiceRegistry.for_library(library).fingerprint()
    review = manager.review_version('fixture', '1.0.0')
    target = tmp_path / 'private-export.dordiai-voice'
    result = lifecycle.export(review, target, export_rights_approved=True)
    assert result == {'path': str(target.resolve()), 'sha256': sha256(target.read_bytes()).hexdigest(),
        'size_bytes': target.stat().st_size, 'profile': 'fixture', 'version': '1.0.0',
        'encrypted': False, 'model_inference': 'NOT_RUN'}
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {'voice-package.json', *review.document['files']}
        assert json.loads(archive.read('voice-package.json')) == review.document
        for name, row in review.document['files'].items():
            assert sha256(archive.read(name)).hexdigest() == row['sha256']
    assert selected.read_bytes() == before and VoiceRegistry.for_library(library).fingerprint() == registry
    assert lifecycle.broker.snapshot()['leases'] == [] and not list(tmp_path.glob('.voice-export-*'))
    assert all('nvda_worker.py' not in row[-1] for row in calls)  # metadata bridge, no neural inference


@pytest.mark.parametrize('permission', [False, None, 1, 'yes'])
def test_export_needs_its_own_explicit_rights_even_with_production_approval(lifecycle_ready, tmp_path, permission):
    _, _, manager, lifecycle, calls = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    before = len(calls)
    with pytest.raises(ValueError, match='permission to export'):
        lifecycle.export(review, tmp_path / 'not-exported.zip', export_rights_approved=permission)
    assert len(calls) == before and not (tmp_path / 'not-exported.zip').exists()


def test_export_never_reads_or_overwrites_an_occupied_destination(lifecycle_ready, tmp_path):
    _, _, manager, lifecycle, calls = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    target = tmp_path / 'occupied.zip'
    target.write_bytes(b'unrelated original')
    before = len(calls)
    with pytest.raises(FileExistsError):
        lifecycle.export(review, target, export_rights_approved=True)
    assert target.read_bytes() == b'unrelated original' and len(calls) == before


@pytest.mark.parametrize('kind', ['registration', 'reference'])
def test_changed_export_authority_never_publishes_an_archive(lifecycle_ready, tmp_path, kind):
    _, selected, manager, lifecycle, calls = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    if kind == 'registration':
        manager.store.revalidate(expected_revision=manager.store.read()['revision'])
    else:
        (selected.parent / 'models/voices/packages/fixture/1.0.0/sample.wav').write_bytes(b'changed fixture')
    before = len(calls)
    with pytest.raises((ValueError, ModelManifestError)):
        lifecycle.export(review, tmp_path / 'unapproved.zip', export_rights_approved=True)
    assert len(calls) == before and not (tmp_path / 'unapproved.zip').exists()
    assert lifecycle.broker.snapshot()['leases'] == []


def test_cancelled_real_owner_export_discards_private_stage_and_releases_admission(lifecycle_ready, tmp_path):
    _, _, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    (tmp_path / 'voice-maintenance-case.txt').write_text('slow-export')
    target = tmp_path / 'cancelled.zip'
    with pytest.raises(InterruptedError):
        lifecycle.export(review, target, export_rights_approved=True,
                         cancelled=lambda: (tmp_path / 'voice-export-started.txt').exists())
    assert (tmp_path / 'voice-export-started.txt').is_file()
    assert not target.exists() and not list(tmp_path.glob('.voice-export-*'))
    assert lifecycle.broker.snapshot()['leases'] == []


def test_revocation_preserves_all_reference_versions_and_requires_new_explicit_activation(lifecycle_ready):
    library, selected, manager, lifecycle, _ = lifecycle_ready
    root = selected.parent / 'models/voices/packages'
    original = {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob('*') if path.is_file()
                and path.name not in ('voice-packages.json', '.voice-packages.lock')}
    review = lifecycle.review_removal('fixture')
    assert review.active and not review.delete_files and review.confirmation == 'REVOKE fixture'
    assert '1.0.0, 2.0.0' in review.summary() and 'permanently' not in review.summary()
    value = lifecycle.remove(review, confirmation=review.confirmation)
    assert manager.store.read() == value
    assert {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()
            and path.name not in ('voice-packages.json', '.voice-packages.lock')} == original
    runtime = LocalModelManifest.load(selected)
    runtime.verify(require_complete=True)
    assert runtime.settings['revoked_profiles'] == ['fixture']
    assert 'fixture' not in runtime.settings['profile_reviews']
    assert 'fixture' not in runtime.settings['profile_languages']
    assert [voice.id for voice in VoiceRegistry.for_library(library).list_voices()] == ['dordiai-voices:other']
    fresh = manager.review_version('fixture', '1.0.0')
    with pytest.raises(ValueError, match='permission'):
        manager.apply(fresh, rights_approved=False, supported_languages=['sk'])
    manager.apply(fresh, rights_approved=True, supported_languages=['sk'])
    runtime = LocalModelManifest.load(selected)
    assert runtime.settings['revoked_profiles'] == []
    assert VoiceRegistry.for_library(library).choose_voice('sk').id == 'dordiai-voices:fixture'


def test_delete_removes_only_selected_versions_and_retains_legacy_fallback_revocation(lifecycle_ready):
    library, selected, manager, lifecycle, _ = lifecycle_ready
    root = selected.parent / 'models/voices/packages'
    foreign = {path: path.read_bytes() for path in (root / 'other').rglob('*') if path.is_file()}
    models = {path: path.read_bytes() for path in (selected.parent / 'models').rglob('model.bin')}
    review = lifecycle.review_removal('fixture', delete_files=True)
    assert review.confirmation == 'DELETE fixture' and 'permanently' in review.summary()
    lifecycle.remove(review, confirmation=review.confirmation)
    assert not (root / 'fixture').exists()
    assert all(path.read_bytes() == data for path, data in {**foreign, **models}.items())
    runtime = LocalModelManifest.load(selected)
    runtime.verify(require_complete=True)
    legacy, profiles, owned = voice_packages.managed_catalog(
        runtime, {'fixture': {'name': 'old same-ID catalog entry'}})
    assert 'fixture' not in legacy and 'fixture' not in profiles and 'fixture' in owned
    assert runtime.settings['revoked_profiles'] == ['fixture']
    assert all('packages/fixture/' not in name for name, _ in runtime.files)
    assert lifecycle.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('confirmation', ['', 'DELETE other', 'REVOKE fixture', 'DELETE fixture '])
def test_deletion_requires_exact_selected_action_confirmation(lifecycle_ready, tmp_path, confirmation):
    _, selected, manager, lifecycle, calls = lifecycle_ready
    review = lifecycle.review_removal('fixture', delete_files=True)
    before = len(calls), selected.read_bytes(), manager.store.read()
    with pytest.raises(ValueError, match='exact reviewed'):
        lifecycle.remove(review, confirmation=confirmation)
    assert (len(calls), selected.read_bytes(), manager.store.read()) == before
    assert (selected.parent / 'models/voices/packages/fixture').is_dir()
    assert not (tmp_path / 'voice-removal-entered.txt').exists()


def test_stale_profile_review_does_not_enter_owner_removal(lifecycle_ready, tmp_path):
    _, selected, manager, lifecycle, _ = lifecycle_ready
    review = lifecycle.review_removal('fixture', delete_files=True)
    manager.apply(manager.review_version('fixture', '1.0.0'),
                  rights_approved=True, supported_languages=['uk'])
    with pytest.raises(ValueError, match='changed after removal review'):
        lifecycle.remove(review, confirmation=review.confirmation)
    assert (selected.parent / 'models/voices/packages/fixture/2.0.0/sample.wav').is_file()
    assert not (tmp_path / 'voice-removal-entered.txt').exists()
    assert lifecycle.broker.snapshot()['leases'] == []


def test_removal_precondition_is_checked_after_the_actual_owner_lock_is_acquired(lifecycle_ready, tmp_path):
    _, selected, manager, lifecycle, _ = lifecycle_ready
    review = lifecycle.review_removal('fixture', delete_files=True)
    original = selected.read_bytes(), manager.store.read()
    (tmp_path / 'voice-maintenance-case.txt').write_text('change-index-before-yield')
    with pytest.raises(model_voices.EngineUnavailable):
        lifecycle.remove(review, confirmation=review.confirmation)
    assert (selected.read_bytes(), manager.store.read()) == original
    assert (selected.parent / 'models/voices/packages/fixture/1.0.0/sample.wav').is_file()
    assert (selected.parent / 'models/voices/packages/fixture/2.0.0/sample.wav').is_file()
    assert not (tmp_path / 'voice-removal-entered.txt').exists()


@pytest.mark.parametrize('case', ['change-shared-model', 'add-unreviewed-file'])
def test_unreviewed_owner_side_effects_never_rebind_registration(lifecycle_ready, tmp_path, case):
    _, selected, manager, lifecycle, _ = lifecycle_ready
    review = lifecycle.review_removal('fixture')
    original = selected.read_bytes(), manager.store.read()
    (tmp_path / 'voice-maintenance-case.txt').write_text(case)
    with pytest.raises(ModelManifestError):
        lifecycle.remove(review, confirmation=review.confirmation)
    assert (selected.read_bytes(), manager.store.read()) == original
    with pytest.raises((ValueError, ModelManifestError)):
        manager.configuration()


def test_interrupted_actual_deletion_stays_inactive_and_is_not_replayed(lifecycle_ready, tmp_path):
    _, selected, manager, lifecycle, _ = lifecycle_ready
    root = selected.parent / 'models/voices/packages'
    review = lifecycle.review_removal('fixture', delete_files=True)
    original = selected.read_bytes(), manager.store.read()
    (tmp_path / 'voice-maintenance-case.txt').write_text('partial-delete')
    with pytest.raises(model_voices.EngineUnavailable):
        lifecycle.remove(review, confirmation=review.confirmation)
    index = json.loads((root / 'voice-packages.json').read_bytes())
    assert 'fixture' not in index['active'] and index['inactive']['fixture'] == '2.0.0'
    assert (root / 'fixture').is_dir() and list((root / 'fixture').rglob('*.wav'))
    assert (selected.read_bytes(), manager.store.read()) == original
    with pytest.raises((ValueError, ModelManifestError)):
        manager.configuration()
    assert lifecycle.broker.snapshot()['leases'] == []


def test_cancelled_removal_precedes_child_and_all_data_mutation(lifecycle_ready, tmp_path):
    _, selected, manager, lifecycle, calls = lifecycle_ready
    review = lifecycle.review_removal('fixture', delete_files=True)
    before = len(calls), selected.read_bytes(), manager.store.read()
    with pytest.raises(InterruptedError):
        lifecycle.remove(review, confirmation=review.confirmation, cancelled=lambda: True)
    assert (len(calls), selected.read_bytes(), manager.store.read()) == before
    assert not (tmp_path / 'voice-removal-entered.txt').exists()


def test_export_pending_native_cleanup_retains_broker_and_stage_until_exact_owner_ack(
        lifecycle_ready, tmp_path, monkeypatch):
    import threading
    from carr_library.audio import cleanup, process, voice_lifecycle
    _, _, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    actual_query, actual_stop = voice_lifecycle._query, process._stop_owned
    active, allowed, owners = [], threading.Event(), []
    def query(manifest, action, **kwargs):
        active.append(action)
        try:
            return actual_query(manifest, action, **kwargs)
        finally:
            active.pop()
    def stop(child):
        assert actual_stop(child) is True
        if active == ['export'] and not allowed.is_set():
            raise TimeoutError('synthetic uncertainty after actual export child exit')
        return True
    def pending(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)
    monkeypatch.setattr(voice_lifecycle, '_query', query)
    monkeypatch.setattr(process, '_stop_owned', stop)
    monkeypatch.setattr(cleanup, 'pending_cleanup', pending)
    try:
        with pytest.raises(cleanup.AudioCleanupPending) as caught:
            lifecycle.export(review, tmp_path / 'pending.zip', export_rights_approved=True)
        stage, = tmp_path.glob('.voice-export-*')
        assert (stage / 'profile.dordiai-voice').is_file() and not (tmp_path / 'pending.zip').exists()
        assert len(lifecycle.broker.snapshot()['leases']) == 1
        assert not caught.value.cleanup_owner.quiescent
        allowed.set()
        assert caught.value.cleanup_owner.close()
        assert not stage.exists() and lifecycle.broker.snapshot()['leases'] == []
    finally:
        allowed.set()
        for owner in owners:
            owner.close()


def test_lifecycle_export_uses_post_fsync_reviewed_bytes(lifecycle_ready, tmp_path, monkeypatch):
    from carr_library.audio import process
    from carr_library.audio.models import AudioError
    _, selected, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    registration = selected.read_bytes(), manager.store.read()
    original, mutations = process.os.fsync, []
    def fsync(descriptor):
        original(descriptor)
        info = os.fstat(descriptor)
        for staged in tmp_path.glob('.voice-export-*/profile.dordiai-voice'):
            before = staged.stat()
            if (before.st_dev, before.st_ino) == (info.st_dev, info.st_ino):
                staged.write_bytes(b'X' * before.st_size)
                os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
                mutations.append(True)
    monkeypatch.setattr(process.os, 'fsync', fsync)
    target = tmp_path / 'must-not-publish.zip'
    with pytest.raises(AudioError):
        lifecycle.export(review, target, export_rights_approved=True)
    assert mutations == [True] and not target.exists()
    assert (selected.read_bytes(), manager.store.read()) == registration
    assert not list(tmp_path.glob('.voice-export-*'))
    assert lifecycle.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('late', ['cancel', 'cleanup'])
def test_lifecycle_keeps_verified_export_receipt_after_late_completion(
        lifecycle_ready, tmp_path, monkeypatch, late):
    import threading
    from contextlib import contextmanager
    from carr_library.audio import process, voice_lifecycle
    _, _, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    stopped = threading.Event()
    if late == 'cancel':
        original = process.fsync_directory
        def finish(path):
            original(path)
            stopped.set()
        monkeypatch.setattr(process, 'fsync_directory', finish)
    else:
        original = voice_lifecycle.retained_temporary_directory
        @contextmanager
        def staging(**kwargs):
            with original(**kwargs) as path:
                yield path
            raise OSError('synthetic cleanup notice after actual stage removal')
        monkeypatch.setattr(voice_lifecycle, 'retained_temporary_directory', staging)
    target = tmp_path / 'committed.zip'
    result = lifecycle.export(review, target, export_rights_approved=True, cancelled=stopped.is_set)
    assert result['path'] == str(target.resolve())
    assert result['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert result['size_bytes'] == target.stat().st_size
    if late == 'cleanup':
        assert 'saved and verified' in result['cleanup_warning']
    else:
        assert stopped.is_set()
    assert lifecycle.broker.snapshot()['leases'] == []


def test_changed_bytes_after_actual_owner_inspection_cannot_be_adopted_as_export(
        lifecycle_ready, tmp_path, monkeypatch):
    from carr_library.audio import voice_lifecycle
    _, selected, manager, lifecycle, _ = lifecycle_ready
    review = manager.review_version('fixture', '1.0.0')
    registration = selected.read_bytes(), manager.store.read()
    original, mutated = voice_lifecycle._query, []
    def query(manifest, action, **kwargs):
        value = original(manifest, action, **kwargs)
        if action == 'inspect' and '.voice-export-' in str(kwargs.get('package', '')):
            path = Path(kwargs['package'])
            before = path.stat()
            path.write_bytes(b'Z' * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            mutated.append(True)
        return value
    monkeypatch.setattr(voice_lifecycle, '_query', query)
    target = tmp_path / 'never-inspected.zip'
    with pytest.raises(ModelManifestError, match='changed during owner inspection'):
        lifecycle.export(review, target, export_rights_approved=True)
    assert mutated == [True] and not target.exists()
    assert (selected.read_bytes(), manager.store.read()) == registration
    assert lifecycle.broker.snapshot()['leases'] == [] and not list(tmp_path.glob('.voice-export-*'))
