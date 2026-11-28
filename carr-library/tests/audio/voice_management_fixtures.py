"""Synthetic owner metadata protocol with actual isolated child and OS locks.

This companion contains purpose-written fixtures, never copied owner code,
voice models, real recordings or a claim about acoustic inference.
"""
from hashlib import sha256
import json
from pathlib import Path
import sys
import zipfile

import pytest

from carr_library.audio import voice_transport
from carr_library.audio.voice_packages import VoicePackageManager
from carr_library.audio.voice_settings import VoiceSettingsStore
from carr_library.runtime_settings import load_settings, save_settings
from carr_library.operations import ResourceBroker, ResourceLimits

POLICY = r"""def is_voice_allowed(identity): return identity != 'blocked-fixture'
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
    import os
    root=Path(root)
    with (root/'.voice-packages.lock').open('a+b') as stream:
        if stream.seek(0,2)==0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            if _case(root)=='change-index-before-yield':
                path=root/'voice-packages.json'
                value=load_manifest(path)
                value['active']['fixture']='9.0.0'
                path.write_bytes(json.dumps(value).encode('utf-8'))
            yield
        finally:
            stream.seek(0)
            if os.name=='nt':
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream,fcntl.LOCK_UN)

def _load_index(root):
    path=Path(root)/'voice-packages.json'
    return load_manifest(path) if path.exists() else {'format_version':1,'active':{}}

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
    path=root/'voice-packages.json'
    index=packages.load_manifest(path) if path.exists() else {'format_version':1,'active':{}}
    index['active'][document['id']]=document['version']
    index.get('inactive',{}).pop(document['id'],None)
    path.write_bytes(json.dumps(index,ensure_ascii=False).encode('utf-8'))
def install_voice_package(package,references,engine_id='holos'):
    import zipfile
    document=inspect_package(package)
    assert document['engine']['id']==engine_id
    if document['provenance']['source']=='fixture:slow':
        (Path(references).parents[2]/'voice-install-entered.txt').write_text('actual owner entered')
        time.sleep(20)
    if document['provenance']['source']=='fixture:socket':
        import socket
        socket.create_connection(('127.0.0.1',9))
    if document['provenance']['source']=='fixture:child':
        import subprocess,sys
        subprocess.run([sys.executable,'-c','raise SystemExit(0)'],check=True)
    root=package_root(references)
    root.mkdir(parents=True,exist_ok=True)
    with packages._installation_lock(root):
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
    with packages._installation_lock(root):
        document=packages.load_manifest(root/voice/version/'voice-package.json')
        packages.verify_assets(root/voice/version,document)
        assert document['engine']['id']==engine_id
        _activate(root,document)
        return root/voice/version
"""

def write_json(path, value):
    path.write_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / 'reviewed-runtime'
    root.mkdir()
    assets = {'python/python.exe': b'INERT interpreter marker; only the fixture selects sys.executable',
              'nvda_worker.py': b'# Synthetic metadata-only fixture; never performs speech.\n',
              'voice_policy.py': POLICY.encode(),
              'voice_packages.py': PACKAGES.encode(), 'voice_assets.py': ASSETS.encode(),
              'models/Holos/model.bin': b'SYNTHETIC model, never loaded',
              'models/OmniVoice/model.bin': b'SYNTHETIC model, never loaded',
              'models/OpenVoice/model.bin': b'SYNTHETIC model, never loaded',
              'models/voices/catalog.json': json.dumps({
                  'legacy': 'Synthetic legacy', 'blocked-fixture': 'Excluded fixture'}).encode()}
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
    real_run = voice_transport.run_metadata
    invocations = []
    def synthetic_interpreter(command, **kwargs):
        assert Path(command[0]) == root / 'python/python.exe'
        assert command[1:4] == ['-I', '-B', '-u']
        invocations.append(tuple(command))
        return real_run([sys.executable, *command[1:]], **kwargs)
    monkeypatch.setattr(voice_transport, 'run_metadata', synthetic_interpreter)
    library = tmp_path / 'library'
    (library / 'state').mkdir(parents=True)
    settings = load_settings(library / 'state')
    settings['dordiai'] = {'enabled': True, 'runtime_directory': str(root),
        'python_executable': str(root / 'python/python.exe'),
        'expected_worker_sha256': sha256(assets['nvda_worker.py']).hexdigest()}
    save_settings(library / 'state', settings)
    broker = ResourceBroker.for_app(library, limits=ResourceLimits(cpu_slots=2,
        memory_bytes=1024 * 1024**2, disk_bytes=512 * 1024**2, min_free_disk_bytes=0))
    manager = VoicePackageManager(library, broker=broker)
    saved = manager.register_manifest(selected, expected_revision=0)
    assert saved['revision'] == 1
    return library, selected, manager, invocations


@pytest.fixture
def lifecycle_ready(installed, tmp_path):
    from carr_library.audio.voice_lifecycle import VoiceProfileLifecycle
    library, manifest, manager, calls = installed
    configure(manager)
    for version in ('1.0.0', '2.0.0'):
        path = tmp_path / (version + '.dordiai-voice')
        package(path, version=version)
        manager.apply(manager.inspect(path), rights_approved=True, supported_languages=['uk'])
    return library, manifest, manager, VoiceProfileLifecycle(library, manager=manager, broker=manager.broker), calls


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

