"""Bounded bridge to an explicitly installed owner's data-only voice package API.

This module contains Carr's protocol and approval boundary only. The selected
installation supplies voice_policy.py, voice_packages.py and voice_assets.py;
none of that private source, its model weights or references is redistributed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

from .voice_manifest import (LocalModelManifest, ModelManifestError, _relative,
                             digest_file, local_path, offline_environment, read_json)

BACKENDS = ('holos', 'openvoice', 'omnivoice')
RUNTIME_FIELDS = {'reference_engine', 'models', 'reference_model', 'references',
                  'managed_profiles', 'profile_reviews', 'revoked_profiles'}
ROLES = {'holos': {'holos_profile'}, 'openvoice': {'target_embedding'},
         'omnivoice': {'reference_audio', 'reference_text'}}
ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z')
LANGUAGE = re.compile(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})*\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
MAX_PACKAGE_BYTES = 8 * 1024**3
MAX_PROFILES = 1024
FORBIDDEN = {'.py', '.pyc', '.pyo', '.pyd', '.pth', '.exe', '.dll', '.bat',
             '.cmd', '.ps1', '.vbs', '.js', '.so', '.dylib', '.lnk', '.url',
             '.pickle', '.pkl'}
REVIEW_FIELDS = {'version', 'document_sha256', 'references', 'rights_approved'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Local voice package operation cancelled.')


def _language(value):
    if not isinstance(value, str):
        raise ModelManifestError('Voice package languages must be explicit language codes.')
    code = value.replace('_', '-').lower()
    if not LANGUAGE.fullmatch(code):
        raise ModelManifestError('Invalid voice package language.')
    return code


def runtime_settings(settings):
    backend = settings.get('reference_engine', 'holos')
    if backend not in BACKENDS:
        raise ModelManifestError('Select the reviewed holos, openvoice or omnivoice backend.')
    if 'managed_profiles' in settings and type(settings['managed_profiles']) is not bool:
        raise ModelManifestError('managed_profiles must be true or false.')
    result = {'reference_engine': backend, 'models': settings.get('models', 'models'),
              'reference_model': settings.get('reference_model', 'models/Holos'),
              'references': settings.get('references', 'models/voices')}
    for key in ('models', 'reference_model', 'references'):
        _relative(result[key])
    revoked = settings.get('revoked_profiles', [])
    if (type(revoked) is not list or len(revoked) > MAX_PROFILES
            or any(not isinstance(identity, str) or not ID.fullmatch(identity) for identity in revoked)
            or len(set(revoked)) != len(revoked)):
        raise ModelManifestError('Invalid explicit managed-profile revocations.')
    reviews = settings.get('profile_reviews', {})
    if type(reviews) is not dict or len(reviews) > MAX_PROFILES:
        raise ModelManifestError('Invalid managed profile reviews.')
    for identity, row in reviews.items():
        if (not isinstance(identity, str) or not ID.fullmatch(identity)
                or type(row) is not dict or set(row) != REVIEW_FIELDS
                or not isinstance(row['version'], str) or not VERSION.fullmatch(row['version'])
                or not isinstance(row['document_sha256'], str) or not DIGEST.fullmatch(row['document_sha256'])
                or row['rights_approved'] is not True or type(row['references']) is not dict
                or not row['references'] or any(not isinstance(role, str) or len(role) > 80
                    or not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                    for role, digest in row['references'].items())):
            raise ModelManifestError('A managed voice needs a version-bound explicit reference-rights review.')
    return result


def runtime_directory(manifest, value):
    relative = _relative(value).as_posix()
    root = manifest.root
    selected = local_path(root.joinpath(*PurePosixPath(relative).parts))
    if (not selected.is_dir() or not selected.is_relative_to(root)
            or not any(name.startswith(relative + '/') for name, _ in manifest.files)):
        raise ModelManifestError('Select a directory containing manifest-listed local assets.')
    return selected


def _document(value, *, backend=None):
    """Validate the small returned contract; the owner API verifies its assets."""
    required = {'format', 'format_version', 'id', 'version', 'name', 'engine',
                'languages', 'provenance', 'files'}
    if (type(value) is not dict or not required <= set(value)
            or set(value) - required - {'styles', 'requires'}
            or value['format'] != 'dordiai-voice-package'
            or type(value['format_version']) is not int or value['format_version'] != 1
            or not isinstance(value['id'], str) or not ID.fullmatch(value['id'])
            or not isinstance(value['version'], str) or not VERSION.fullmatch(value['version'])
            or not isinstance(value['name'], str) or not value['name'].strip() or len(value['name']) > 100
            or type(value['engine']) is not dict
            or set(value['engine']) != {'id', 'compatibility_version'}
            or value['engine']['id'] not in BACKENDS
            or type(value['engine']['compatibility_version']) is not int
            or value['engine']['compatibility_version'] != 1):
        raise ModelManifestError('Unsupported owner voice package metadata.')
    engine = value['engine']['id']
    if backend is not None and engine != backend:
        raise ModelManifestError('The voice package belongs to another configured backend.')
    languages = value['languages']
    if (type(languages) is not list or not 1 <= len(languages) <= 256
            or len({_language(code) for code in languages}) != len(languages)):
        raise ModelManifestError('Invalid per-profile package languages.')
    if (type(value['provenance']) is not dict or not {'source', 'license'} <= set(value['provenance'])
            or any(not isinstance(value['provenance'][key], str)
                   or not value['provenance'][key].strip() or len(value['provenance'][key]) > 4096
                   for key in ('source', 'license'))):
        raise ModelManifestError('A voice package needs its source and license.')
    files, roles, total = value['files'], set(), 0
    if type(files) is not dict or not 1 <= len(files) <= 8190:
        raise ModelManifestError('Invalid voice package asset inventory.')
    folded = set()
    for name, row in files.items():
        path = _relative(name)
        if (path.suffix.lower() in FORBIDDEN or name.casefold() in folded
                or any(part.endswith((' ', '.')) for part in path.parts)
                or any(c in name for c in '<>|?*"')
                or type(row) is not dict or set(row) != {'sha256', 'size_bytes', 'role'}
                or not isinstance(row['sha256'], str) or not DIGEST.fullmatch(row['sha256'])
                or type(row['size_bytes']) is not int or row['size_bytes'] < 0
                or not isinstance(row['role'], str) or not row['role'] or len(row['role']) > 80
                or row['role'] in roles):
            raise ModelManifestError('Only distinct reviewed data assets are accepted in a voice package.')
        folded.add(name.casefold())
        roles.add(row['role'])
        total += row['size_bytes']
    if total > MAX_PACKAGE_BYTES or not ROLES[engine] <= roles:
        raise ModelManifestError('Voice data exceeds the budget or lacks required reference roles.')
    if value.get('styles', ['neutral']) != ['neutral']:
        raise ModelManifestError('This owner adapter exposes neutral speech only.')
    return json.loads(_json(value))


# Only this small bridge is shipped. Private modules are captured and checked in
# the child, then compiled from those exact buffers; a stale .pyc is never loaded.
_PACKAGE_BOOTSTRAP = r"""
import contextlib, hashlib, json, os, pathlib, re, stat, sys, types
def stable(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):
    return hashlib.sha256(stable(value).encode('utf-8')).hexdigest()
def offline(event,args):
    if event in {'socket.connect','socket.connect_ex','socket.bind','socket.getaddrinfo',
                 'socket.sendto','urllib.Request','subprocess.Popen','os.system','os.posix_spawn'}:
        raise PermissionError('Local voice maintenance denies network and child processes')
sys.addaudithook(offline)
request=json.loads(sys.stdin.buffer.read(2*1024*1024+1))
with contextlib.redirect_stdout(sys.stderr):
    captured=[]
    for name in ('voice_policy','voice_packages','voice_assets'):
        entry=request['modules'][name]
        path=pathlib.Path(entry['path'])
        with path.open('rb') as source:
            raw=source.read(2*1024*1024+1)
        if len(raw)>2*1024*1024 or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
            raise ValueError('The reviewed owner helper changed')
        captured.append((name,path,raw))
    for name,path,raw in captured:
        module=types.ModuleType(name)
        module.__file__=str(path)
        sys.modules[name]=module
        exec(compile(raw,str(path),'exec'),module.__dict__)
    policy=sys.modules['voice_policy']
    packages=sys.modules['voice_packages']
    assets=sys.modules['voice_assets']
    references=pathlib.Path(request['references'])
    engine=request['backend']
    action=request['action']
    if action=='catalog':
        installed=packages.list_installed_packages(assets.package_root(references))
        owned={row['id'] for row in installed}
        profiles={}
        for identity,row in assets.discover_voices(references,engine_id=engine).items():
            document={key:value for key,value in row.items() if key!='directory'}
            assets.validate_adapter(document)
            policy.require_voice_allowed(identity)
            profiles[identity]=document
        allowed=[]
        for identity in request.get('legacy_ids',[]):
            if identity not in owned and policy.is_voice_allowed(identity):
                allowed.append(identity)
        result={'profiles':profiles,'allowed_legacy':allowed,'owned':sorted(owned)}
    elif action=='create_reference':
        if engine!='omnivoice':
            raise ValueError('Reviewed recording references require the configured OmniVoice adapter')
        document=assets.validate_adapter(request['document'])
        if document['engine']['id']!=engine or {row['role'] for row in document['files'].values()}!={'reference_audio','reference_text'}:
            raise ValueError('Only the existing OmniVoice audio/text reference recipe is accepted')
        if digest(document)!=request['expected_document_sha256']:
            raise ValueError('The reviewed reference recipe changed')
        source=pathlib.Path(request['source'])
        packages.verify_assets(source,document)
        packages.create_package(source,document,request['output'])
        result=assets.validate_adapter(assets.inspect_package(request['output']))
        if result!=document:
            raise ValueError('The packaged reference differs from its reviewed data')
    elif action=='export':
        root=assets.package_root(references)
        with packages._installation_lock(root):
            rows=assets.voice_versions(references,request['profile'])
            document=next(row for row in rows if row['version']==request['version'])
            assets.validate_adapter(document)
            policy.require_voice_allowed(document['id'])
            if document['engine']['id']!=engine or digest(document)!=request['expected_document_sha256']:
                raise ValueError('The exact installed profile changed after export review')
            directory=root/document['id']/document['version']
            packages.verify_assets(directory,document)
            packages.create_package(directory,document,request['output'])
            result=assets.validate_adapter(assets.inspect_package(request['output']))
            if result!=document:
                raise ValueError('The exported profile differs from its reviewed data')
    elif action=='remove':
        profile=request['profile']
        if not isinstance(profile,str) or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',profile) is None or type(request['delete_files']) is not bool:
            raise ValueError('Explicit profile identity and removal mode are required')
        root=assets.package_root(references)
        original_lock=packages._installation_lock
        @contextlib.contextmanager
        def reviewed_lock(selected_root):
            if pathlib.Path(selected_root).resolve()!=root.resolve():
                raise ValueError('The owner selected a different installation')
            with original_lock(selected_root):
                index=root/'voice-packages.json'
                with index.open('rb') as stream:
                    raw=stream.read(1024*1024+1)
                if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=request['expected_index_sha256']:
                    raise ValueError('Profile activation changed after removal review')
                directory=root/profile
                expected=request['expected_files']
                found={}
                if directory.is_symlink():
                    raise ValueError('Linked reference data cannot be removed')
                if directory.exists():
                    if not directory.is_dir():
                        raise ValueError('The selected profile is not a directory')
                    for count,path in enumerate(directory.rglob('*'),1):
                        if count>32768:
                            raise ValueError('The selected profile exceeds its entry budget')
                        info=path.lstat()
                        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                            raise ValueError('Linked reference data cannot be removed')
                        if stat.S_ISDIR(info.st_mode):
                            continue
                        name=path.relative_to(directory).as_posix()
                        row=expected.get(name)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or row is None or info.st_size!=row['size_bytes']:
                            raise ValueError('The selected profile files changed after review')
                        def stamp(value):
                            return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns,value.st_nlink)
                        checksum=hashlib.sha256()
                        with path.open('rb') as stream:
                            if stamp(os.fstat(stream.fileno()))!=stamp(info):
                                raise ValueError('A selected reference file changed before verification')
                            remaining=info.st_size
                            while remaining:
                                block=stream.read(min(1024*1024,remaining))
                                if not block:
                                    raise ValueError('A selected reference became truncated')
                                checksum.update(block)
                                remaining-=len(block)
                            if stream.read(1) or stamp(os.fstat(stream.fileno()))!=stamp(info):
                                raise ValueError('A selected reference changed during verification')
                        if stamp(path.lstat())!=stamp(info) or checksum.hexdigest()!=row['sha256']:
                            raise ValueError('A selected reference changed after removal review')
                        found[name]=row
                if found!=expected:
                    raise ValueError('The selected profile inventory changed after review')
                yield
        # Keep the owner's existing OS lock and deletion/tombstone implementation.
        # Inject only a precondition immediately after that exact lock is held.
        packages._installation_lock=reviewed_lock
        try:
            packages.uninstall_package(root,profile,keep_files=not request['delete_files'])
        finally:
            packages._installation_lock=original_lock
            assets.clear_cache()
        result={'profile':profile,'deleted':request['delete_files']}
    elif action=='versions':
        result=assets.voice_versions(references,request['profile'])
    else:
        if action in {'inspect','install'}:
            document=assets.validate_adapter(assets.inspect_package(request['package']))
        elif action in {'review_version','activate'}:
            rows=assets.voice_versions(references,request['profile'])
            document=next(row for row in rows if row['version']==request['version'])
            assets.validate_adapter(document)
            directory=assets.package_root(references)/document['id']/document['version']
            packages.verify_assets(directory,document)
        else:
            raise ValueError('Unknown reviewed voice maintenance action')
        if document['engine']['id']!=engine:
            raise ValueError('The package belongs to another configured backend')
        expected=request.get('expected_document_sha256')
        if expected is not None and digest(document)!=expected:
            raise ValueError('The selected voice metadata changed after review')
        if action=='install':
            assets.install_voice_package(request['package'],references,engine_id=engine)
        elif action=='activate':
            assets.activate_voice_version(references,request['profile'],request['version'],engine_id=engine)
        result=document
print(stable(result),flush=True)
"""


def bridge_identity():
    return hashlib.sha256(_PACKAGE_BOOTSTRAP.encode('utf-8')).hexdigest()


def _query(manifest, action, *, cancelled=lambda: False, timeout=120, verify=True, **fields):
    from .model_voices import _run
    _cancel(cancelled)
    if verify:
        manifest.verify(cancelled=cancelled, require_complete=True)
    settings = runtime_settings(manifest.settings)
    references = runtime_directory(manifest, settings['references'])
    worker = manifest.asset(manifest.settings.get('worker', 'nvda_worker.py'))
    modules = {}
    for name in ('voice_policy', 'voice_packages', 'voice_assets'):
        relative = worker.with_name(name + '.py').relative_to(manifest.root).as_posix()
        path = manifest.asset(relative)
        modules[name] = {'path': str(path), 'sha256': dict(manifest.files)[relative]}
    request = {'action': action, 'backend': settings['reference_engine'],
               'references': str(references), 'modules': modules, **fields}
    command = [manifest.asset(manifest.settings.get('python', 'python/python.exe')),
               '-I', '-B', '-u', '-c', _PACKAGE_BOOTSTRAP]
    output = _run(command, data=_json(request).encode('utf-8'), cancelled=cancelled,
                  timeout=timeout, env=offline_environment(), cwd=manifest.root)
    try:
        result = json.loads(output.decode('utf-8'))
        if len(output) > 1024 * 1024:
            raise ValueError()
    except (ValueError, UnicodeError) as error:
        raise ModelManifestError('The installed voice package API returned invalid bounded metadata.') from error
    _cancel(cancelled)
    return result


def managed_catalog(manifest, legacy, *, cancelled=lambda: False):
    result = _query(manifest, 'catalog', legacy_ids=list(legacy), cancelled=cancelled)
    if (type(result) is not dict or set(result) != {'profiles', 'allowed_legacy', 'owned'}
            or type(result['profiles']) is not dict or len(result['profiles']) > MAX_PROFILES
            or type(result['allowed_legacy']) is not list
            or set(result['allowed_legacy']) - set(legacy)
            or type(result['owned']) is not list or len(result['owned']) > MAX_PROFILES
            or any(not isinstance(identity, str) or not ID.fullmatch(identity) for identity in result['owned'])):
        raise ModelManifestError('The installed voice discovery contract is invalid.')
    backend = runtime_settings(manifest.settings)['reference_engine']
    profiles = {}
    for identity, raw in result['profiles'].items():
        document = _document(raw, backend=backend)
        if identity != document['id'] or identity not in result['owned']:
            raise ModelManifestError('The installed profile identity differs from its package.')
        profiles[identity] = document
    if set(result['allowed_legacy']) & set(result['owned']):
        raise ModelManifestError('An owned voice cannot fall back to a legacy profile.')
    revoked = set(manifest.settings.get('revoked_profiles', []))
    return ({key: value for key, value in legacy.items()
             if key in result['allowed_legacy'] and key not in revoked},
            {key: value for key, value in profiles.items() if key not in revoked},
            tuple(sorted(set(result['owned']) | revoked)))


def approved_profile(manifest, document):
    if document['id'] in manifest.settings.get('revoked_profiles', []):
        return False
    record = manifest.settings.get('profile_reviews', {}).get(document['id'])
    references = {row['role']: row['sha256'] for row in document['files'].values()}
    return record == {'version': document['version'], 'document_sha256': _hash(document),
                      'references': references, 'rights_approved': True}


@dataclass(frozen=True)
class VoicePackageReview:
    operation: str
    manifest_sha256: str
    settings_revision: int
    source: str
    source_sha256: str
    document_json: str

    @property
    def document(self):
        return json.loads(self.document_json)

    def summary(self):
        row = self.document
        assets = ', '.join(sorted(value['role'] for value in row['files'].values()))
        return (f"{row['name']}; {row['id']}; version {row['version']}; backend {row['engine']['id']}\n"
                f"Declared languages: {', '.join(row['languages'])}\n"
                f"Reference/data roles: {assets}\nSource: {row['provenance']['source']}\n"
                f"License: {row['provenance']['license']}\n"
                "This review verifies local bytes and metadata. Speech, speaker similarity and language quality "
                "have not been tested. Approve reference rights and select reviewed production languages explicitly.")


class VoicePackageManager:
    """Explicit maintenance of one registered installation using its owner API."""

    def __init__(self, root):
        from .voice_settings import VoiceSettingsStore
        self.store = VoiceSettingsStore(root)

    def _current(self, *, cancelled=lambda: False):
        from .voice_settings import verify_voice_settings_bindings
        value = verify_voice_settings_bindings(self.store.read(), cancelled=cancelled)
        path = value['config'].get('dordiai_manifest')
        if not path:
            raise ModelManifestError('First register the reviewed local dordiai manifest in Local voice engines.')
        manifest = LocalModelManifest.load(path, engine='dordiai-voices',
            expected_sha256=value['bindings']['dordiai_manifest'])
        manifest.verify(cancelled=cancelled, require_complete=True)
        return value, manifest

    def configuration(self, *, cancelled=lambda: False):
        value, manifest = self._current(cancelled=cancelled)
        return {'revision': value['revision'], **runtime_settings(manifest.settings),
                'managed_profiles': manifest.settings.get('managed_profiles', False)}

    def configure(self, backend, models, reference_model, references, *,
                  expected_revision, cancelled=lambda: False):
        value, manifest = self._current(cancelled=cancelled)
        if value['revision'] != expected_revision:
            raise ValueError('Voice settings changed; reopen the reference voice controls.')
        settings = dict(manifest.settings)
        old = runtime_settings(settings)
        settings.update(reference_engine=backend, models=models, reference_model=reference_model,
                        references=references, managed_profiles=True)
        runtime = runtime_settings(settings)
        for key in ('models', 'reference_model', 'references'):
            runtime_directory(manifest, runtime[key])
        if runtime != old or not manifest.settings.get('managed_profiles', False):
            settings['profile_languages'] = {}
            settings['profile_reviews'] = {}
        from dataclasses import replace
        proposed = replace(manifest, settings=settings)
        catalog = read_json(manifest.asset(settings.get('catalog', 'models/voices/catalog.json')))
        managed_catalog(proposed, catalog.get('voices', {}), cancelled=cancelled)
        return self._publish(value, manifest, settings, dict(manifest.files), cancelled)

    def inspect(self, package, *, cancelled=lambda: False):
        value, manifest = self._current(cancelled=cancelled)
        package = local_path(package)
        if not package.is_file() or package.stat().st_size > MAX_PACKAGE_BYTES:
            raise ModelManifestError('Choose a bounded local data-only voice package.')
        checksum = digest_file(package, cancelled=cancelled)
        document = _document(_query(manifest, 'inspect', package=str(package), cancelled=cancelled),
                             backend=runtime_settings(manifest.settings)['reference_engine'])
        if digest_file(package, cancelled=cancelled) != checksum:
            raise ValueError('The selected voice package changed during review.')
        return VoicePackageReview('install', manifest.manifest_sha256, value['revision'],
                                  str(package), checksum, _json(document))

    def versions(self, profile, *, cancelled=lambda: False):
        if not isinstance(profile, str) or not ID.fullmatch(profile):
            raise ValueError('Choose a valid installed profile identity.')
        _, manifest = self._current(cancelled=cancelled)
        rows = _query(manifest, 'versions', profile=profile, cancelled=cancelled)
        if type(rows) is not list or len(rows) > MAX_PROFILES:
            raise ModelManifestError('Invalid installed profile versions.')
        return tuple(_document(row) for row in rows)

    def review_version(self, profile, version, *, cancelled=lambda: False):
        if (not isinstance(profile, str) or not ID.fullmatch(profile)
                or not isinstance(version, str) or not VERSION.fullmatch(version)):
            raise ValueError('Choose an installed profile and version.')
        value, manifest = self._current(cancelled=cancelled)
        document = _document(_query(manifest, 'review_version', profile=profile, version=version,
                                   cancelled=cancelled),
                             backend=runtime_settings(manifest.settings)['reference_engine'])
        if (document['id'], document['version']) != (profile, version):
            raise ModelManifestError('The installed voice identity differs.')
        return VoicePackageReview('activate', manifest.manifest_sha256, value['revision'],
                                  profile, _hash(document), _json(document))

    def apply(self, review, *, rights_approved, supported_languages, cancelled=lambda: False):
        if type(review) is not VoicePackageReview or review.operation not in {'install', 'activate'}:
            raise ValueError('Inspect the exact package or installed version before applying it.')
        if rights_approved is not True:
            raise ValueError('Explicitly confirm your permission to use the selected voice references.')
        document = _document(review.document)
        if type(supported_languages) not in (tuple, list):
            raise ValueError('Select reviewed production languages explicitly.')
        supported = [_language(code) for code in supported_languages]
        declared = [_language(code) for code in document['languages']]
        if len(set(supported)) != len(supported) or set(supported) - set(declared):
            raise ValueError('Production languages must be a distinct subset declared by this profile.')
        value, manifest = self._current(cancelled=cancelled)
        if (value['revision'] != review.settings_revision
                or manifest.manifest_sha256 != review.manifest_sha256):
            raise ValueError('Voice registration changed after review; inspect the package again.')
        backend = runtime_settings(manifest.settings)['reference_engine']
        _document(document, backend=backend)
        prefix = runtime_settings(manifest.settings)['references'] + '/packages'
        index_path = manifest.root / prefix / 'voice-packages.json'
        old_index = read_json(index_path) if index_path.exists() else {'format_version': 1, 'active': {}}
        old_active, old_inactive = _index(old_index)
        self._writable_package_tree(manifest.root / prefix, cancelled)
        fields = {'expected_document_sha256': _hash(document)}
        if review.operation == 'install':
            package = local_path(review.source)
            if digest_file(package, cancelled=cancelled) != review.source_sha256:
                raise ValueError('The voice package changed after review.')
            fields['package'] = str(package)
        else:
            fields.update(profile=document['id'], version=document['version'])
        _cancel(cancelled)
        result = _document(_query(manifest, review.operation, cancelled=cancelled,
                                 timeout=600, **fields), backend=backend)
        if result != document:
            raise ModelManifestError('The owner package result differs from the reviewed data.')
        raw_index = index_path.read_bytes()
        active, inactive = _index(json.loads(raw_index))
        index_sha256 = hashlib.sha256(raw_index).hexdigest()
        expected_active = {**old_active, document['id']: document['version']}
        expected_inactive = {key: version for key, version in old_inactive.items()
                             if key != document['id']}
        if active != expected_active or inactive != expected_inactive:
            raise ModelManifestError('The owner operation changed another profile activation.')
        files = self._result_inventory(manifest, prefix, document, index_sha256, cancelled)
        settings = dict(manifest.settings)
        settings['managed_profiles'] = True
        settings['revoked_profiles'] = [identity for identity in settings.get('revoked_profiles', [])
                                        if identity != document['id']]
        settings['profile_languages'] = dict(settings.get('profile_languages', {}))
        settings['profile_languages'][document['id']] = {
            'supported': supported, 'candidates': [code for code in declared if code not in supported]}
        settings['profile_reviews'] = dict(settings.get('profile_reviews', {}))
        settings['profile_reviews'][document['id']] = {
            'version': document['version'], 'document_sha256': _hash(document),
            'references': {row['role']: row['sha256'] for row in document['files'].values()},
            'rights_approved': True}
        return self._publish(value, manifest, settings, files, cancelled)

    @staticmethod
    def _writable_package_tree(root, cancelled):
        local_path(root, must_exist=False)
        if root.exists():
            for count, path in enumerate(root.rglob('*'), 1):
                _cancel(cancelled)
                if count > 32768:
                    raise ModelManifestError('The voice data directory exceeds its entry budget.')
                path = local_path(path)
                if path.is_file() and path.stat().st_nlink != 1:
                    raise ModelManifestError('Writable voice data cannot contain linked files.')

    def _result_inventory(self, manifest, prefix, document, index_sha256, cancelled):
        selected = prefix + '/' + document['id'] + '/' + document['version']
        expected_data = {selected + '/' + name: row for name, row in document['files'].items()}
        mutable = {prefix + '/voice-packages.json', prefix + '/.voice-packages.lock',
                   selected + '/voice-package.json'}
        old = dict(manifest.files)
        result = {}
        for count, path in enumerate(manifest.root.rglob('*'), 1):
            _cancel(cancelled)
            if count > 32768:
                raise ModelManifestError('The local installation exceeds the directory entry budget.')
            path = local_path(path)
            if not path.is_file() or path == manifest.path:
                continue
            relative = path.relative_to(manifest.root).as_posix()
            digest = digest_file(path, cancelled=cancelled)
            if relative in old and relative not in mutable:
                if digest != old[relative]:
                    raise ModelManifestError('An existing reviewed runtime asset changed during voice installation.')
            elif relative in expected_data:
                row = expected_data[relative]
                if digest != row['sha256'] or path.stat().st_size != row['size_bytes']:
                    raise ModelManifestError('Installed voice data differs from the reviewed package.')
            elif relative not in mutable:
                raise ModelManifestError('The voice operation added an unreviewed runtime file.')
            if relative == selected + '/voice-package.json' and _document(read_json(path)) != document:
                raise ModelManifestError('Installed voice metadata differs from the reviewed package.')
            if relative == prefix + '/voice-packages.json' and digest != index_sha256:
                raise ModelManifestError('Voice activation changed during inventory verification.')
            if relative == prefix + '/.voice-packages.lock' and path.read_bytes() not in (b'', b'0'):
                raise ModelManifestError('Unexpected owner voice lock contents.')
            result[relative] = digest
        required = {*expected_data, selected + '/voice-package.json', prefix + '/voice-packages.json'}
        if set(old) - set(result) or required - set(result):
            raise ModelManifestError('The voice operation removed reviewed assets or omitted package data.')
        return result

    def _publish(self, previous, manifest, settings, files, cancelled):
        from .projects import atomic_json
        from .voice_settings import _bindings
        with self.store.projects.transaction():
            if self.store.read() != previous:
                raise ValueError('Voice settings changed during maintenance; revalidate the installed files.')
            if _bindings(previous['config'], cancelled=cancelled) != previous['bindings']:
                raise ValueError('A registered runtime changed during maintenance; review it again.')
            _cancel(cancelled)
            if digest_file(manifest.path, cancelled=cancelled) != manifest.manifest_sha256:
                raise ValueError('The reviewed runtime manifest changed during this operation.')
            raw = read_json(manifest.path)
            raw['settings'], raw['files'] = settings, files
            runtime_settings(settings)
            if not 1 <= len(files) <= 8192 or len(_json(raw).encode('utf-8')) > 900_000:
                raise ModelManifestError('The reviewed runtime inventory exceeds its manifest budget.')
            _cancel(cancelled)
            atomic_json(manifest.path, raw)
            # Cross-directory publication is deliberately fail closed: a failure
            # after this write leaves the old library binding invalid. The user can
            # explicitly revalidate, without re-installing or inventing a rollback.
            current = LocalModelManifest.load(manifest.path, engine='dordiai-voices')
            current.verify(cancelled=cancelled, require_complete=True)
            bindings = _bindings(previous['config'], cancelled=cancelled)
            if any(digest != previous['bindings'][key] for key, digest in bindings.items()
                   if key != 'dordiai_manifest'):
                raise ValueError('Another registered runtime changed; it was not approved by this voice operation.')
            if self.store.read()['revision'] != previous['revision']:
                raise ValueError('Voice registration changed during maintenance; revalidate the installed files.')
            _cancel(cancelled)
            value = {**previous, 'revision': previous['revision'] + 1, 'restored': False, 'bindings': bindings}
            atomic_json(self.store.path, value)
            return value
    

def _index(value):
    if (type(value) is not dict or set(value) - {'format_version', 'active', 'inactive'}
            or type(value.get('format_version')) is not int or value['format_version'] not in (1, 2)
            or type(value.get('active')) is not dict or type(value.get('inactive', {})) is not dict):
        raise ModelManifestError('Invalid owner voice activation index.')
    active, inactive = value['active'], value.get('inactive', {})
    if set(active) & set(inactive):
        raise ModelManifestError('A voice cannot be both active and inactive.')
    for identity, version in [*active.items(), *inactive.items()]:
        if (not isinstance(identity, str) or not ID.fullmatch(identity)
                or not isinstance(version, str) or not VERSION.fullmatch(version)):
            raise ModelManifestError('Invalid owner voice activation identity.')
    return dict(active), dict(inactive)

