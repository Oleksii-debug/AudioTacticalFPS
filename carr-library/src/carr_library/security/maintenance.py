"""Explicit owner-reviewed update selection shared by CLI and native review.

Configuration is selected with an independently supplied digest. It supplies no
issuer credentials and never provisions protected state. Downloads, activation
and recovery are separate explicit operations. Existing entitlement state is
neither read nor changed by this controller.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

from ._files import TrustError, json_value, directory
from .anchors import WindowsVaultAnchor
from .handoff import PortableUpdateHandoff, _digest
from .standard_handoff import StandardUpdateHandoff
from .updates import LocalRepositoryClient, UpdatePolicy, Release, relative_path
from .download_state import PausedDownload
from .https import HTTPSRepository, HTTPSRepositoryClient
from ..portable_entry import _read


def _absolute(value, label):
    if (not isinstance(value, str) or not value or '\x00' in value
            or value.startswith(('//', '\\\\')) or not Path(value).is_absolute()
            or '..' in Path(value).parts):
        raise TrustError(label + ' must be an explicitly selected absolute local path.')
    return Path(value).absolute()


@dataclass(frozen=True)
class UpdateConfiguration:
    path: Path
    digest: str
    raw: bytes

    def __post_init__(self):
        _digest(self.digest)
        if not isinstance(self.raw, bytes) or len(self.raw) > 32768 or sha256(self.raw).hexdigest() != self.digest:
            raise TrustError('Owner configuration bytes do not match their reviewed identity.')
        object.__setattr__(self, 'path', _absolute(os.fspath(self.path), 'Owner configuration'))

    @classmethod
    def read(cls, path, digest):
        path = _absolute(os.fspath(path), 'Owner configuration')
        _digest(digest)
        raw = _read(path, 32768)
        if sha256(raw).hexdigest() != digest:
            raise TrustError('The owner configuration differs from its independently supplied SHA256.')
        value = cls(path, digest, raw)
        value.checked()
        return value

    def checked(self):
        value = json_value(self.raw, maximum=32768)
        keys = {'schema', 'product', 'channel', 'current_build', 'bootstrap', 'repository', 'target',
                'cache', 'installation', 'anchor', 'verifier', 'helper_python'}
        if type(value) is not dict or set(value) != keys or value['schema'] != 'carr-update-owner/1':
            raise TrustError('Unsupported owner update configuration.')
        if (not isinstance(value['product'], str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value['product'])
                or not isinstance(value['channel'], str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value['channel'])
                or type(value['current_build']) is not int or not 0 <= value['current_build'] <= 2**53 - 1):
            raise TrustError('Invalid product/channel/current-build selection.')
        relative_path(value['target'])
        for name in ('bootstrap', 'verifier'):
            item = value[name]
            if type(item) is not dict or set(item) != {'path', 'sha256'}:
                raise TrustError('Select the exact reviewed ' + name + ' path and digest.')
            _absolute(item['path'], name); _digest(item['sha256'])
        if Path(value['verifier']['path']).name != 'install_windows.py':
            raise TrustError('Use the existing reviewed install_windows.py bundle verifier.')
        _absolute(value['cache'], 'Update cache')
        repo = value['repository']
        if type(repo) is not dict or repo.get('kind') not in ('local', 'https'):
            raise TrustError('Select an explicit local or HTTPS repository.')
        if repo['kind'] == 'local':
            if set(repo) != {'kind', 'path'}:
                raise TrustError('Unsupported local repository fields.')
            _absolute(repo['path'], 'Offline repository')
        else:
            if set(repo) != {'kind', 'url', 'ca_file', 'ca_sha256'}:
                raise TrustError('Unsupported HTTPS repository fields.')
            if (repo['ca_file'] is None) != (repo['ca_sha256'] is None):
                raise TrustError('A private TLS CA requires its separate digest.')
            if repo['ca_file'] is not None:
                _absolute(repo['ca_file'], 'TLS CA'); _digest(repo['ca_sha256'])
            HTTPSRepository(repo['url'])
        installed = value['installation']
        if (type(installed) is not dict or set(installed) !=
                {'kind', 'program', 'work', 'manifest_sha256', 'setup_sha256', 'version'}
                or installed['kind'] not in ('portable', 'standard')):
            raise TrustError('Select one supported current installation layout.')
        for name in ('program', 'work'):
            _absolute(installed[name], 'Installation ' + name)
        _digest(installed['manifest_sha256'])
        if (not isinstance(installed['version'], str)
                or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,95}', installed['version'])):
            raise TrustError('The original application version is required.')
        if installed['kind'] == 'standard':
            _digest(installed['setup_sha256'])
        elif installed['setup_sha256'] is not None:
            raise TrustError('Portable updates do not accept a standard-setup identity.')
        if value['helper_python'] is not None:
            _absolute(value['helper_python'], 'Maintenance interpreter')
        if installed['kind'] == 'portable' and value['helper_python'] is None:
            raise TrustError('Portable maintenance requires a reviewed external helper interpreter.')
        anchor = value['anchor']
        if (type(anchor) is not dict or set(anchor) != {'reference', 'profile_id', 'binding'}
                or not all(isinstance(anchor[key], str) for key in anchor)):
            raise TrustError('A previously provisioned protected anchor must be explicitly selected.')
        from ..assistant.credentials import validate_profile_id, validate_reference
        validate_profile_id(anchor['profile_id']); validate_reference(anchor['reference'], anchor['profile_id'])
        _digest(anchor['binding'])
        return value

    def unchanged(self):
        if _read(self.path, 32768) != self.raw:
            raise TrustError('Owner configuration changed after review. Select and verify it again.')

    def description(self):
        value = self.checked()
        repo = value['repository']
        return '\n'.join((
            'Explicit owner configuration; no source is selected by default.',
            'Product: ' + value['product'], 'Channel: ' + value['channel'],
            'Source: ' + (repo['url'] if repo['kind'] == 'https' else repo['path']),
            'Layout: ' + value['installation']['kind'],
            'Current program: ' + value['installation']['program'],
            'Maintenance workspace: ' + value['installation']['work'],
            'Current build: ' + str(value['current_build']),
            'Original application version: ' + value['installation']['version'],
            'The owner must have provisioned the selected protected anchor already.',
            'Reading, saving, export and recovery stay available without update authority.'))


class MaintenanceSession:
    def __init__(self, app, configuration, *, anchor=None, verifier=None):
        if type(configuration) is not UpdateConfiguration:
            raise TrustError('Review a pinned owner configuration first.')
        self.app, self.configuration = app, configuration
        self._anchor, self._verifier = anchor, verifier
        self.release = self.package = None
        self.download_state = None
        self.download_notice = ''
        self.last_result = None
        value = configuration.checked()
        self.library_root = directory(app.root)
        program = _absolute(value['installation']['program'], 'Program')
        work, cache = (_absolute(value['installation']['work'], 'Workspace'),
                       _absolute(value['cache'], 'Cache'))
        program_root = program.parent.parent if value['installation']['kind'] == 'standard' else program
        for first, second in ((program_root, self.library_root), (work, self.library_root),
                              (work, program_root), (cache, self.library_root), (cache, program_root)):
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise TrustError('Maintenance roots must not overlap the program or research library.')
        if value['installation']['kind'] == 'portable':
            helper = _absolute(value['helper_python'], 'Maintenance interpreter')
            verifier_path = _absolute(value['verifier']['path'], 'Bundle verifier')
            for path in (helper, verifier_path, configuration.path):
                if any(path.is_relative_to(root) for root in (program_root, work, self.library_root)):
                    raise TrustError('Portable helper, verifier and configuration must remain outside all replaced or data roots.')

    def _context(self):
        self.configuration.unchanged()
        if directory(self.app.root) != self.library_root:
            raise TrustError('The selected research library changed after review.')
        value = self.configuration.checked()
        bootstrap = _read(Path(value['bootstrap']['path']), 512000)
        policy = UpdatePolicy(value['product'], value['channel'], bootstrap,
                              value['bootstrap']['sha256'], value['current_build'])
        repo = value['repository']
        if repo['kind'] == 'https':
            ca = None if repo['ca_file'] is None else _read(Path(repo['ca_file']), 128 * 1024)
            source = HTTPSRepository(repo['url'], ca_pem=ca, ca_sha256=repo['ca_sha256'])
            client = HTTPSRepositoryClient(Path(value['cache']), policy, source)
        else:
            source = directory(repo['path'])
            client = LocalRepositoryClient(Path(value['cache']), policy)
        return value, policy, client, source

    def check(self, *, cancelled=lambda: False):
        self.release = self.package = self.download_state = None
        self.download_notice = ''
        value, _policy, client, source = self._context()
        release = client.check(source, value['target'], cancelled=cancelled)
        self.configuration.unchanged()
        retained = client.paused_download(source, release, cancelled=cancelled)
        self.configuration.unchanged()
        self.release, self.package, self.download_state = release, None, retained
        return release

    def download(self, release, *, cancelled=lambda: False, paused=None, resume=False, progress=None):
        if type(release) is not Release or release != self.release:
            raise TrustError('Choose the exact signed release reviewed in this session.')
        self.package = None
        self.download_notice = ''
        value, _policy, client, source = self._context()
        package = client.download(source, value['target'], reviewed_sha256=release.sha256,
                                  cancelled=cancelled, paused=paused, resume=resume, progress=progress)
        self.download_notice = client.download_notice
        self.configuration.unchanged()
        if package.release != release:
            raise TrustError('Signed release metadata changed after review; review it again.')
        if isinstance(package, PausedDownload):
            self.download_state, self.package = package, None
        else:
            self.package, self.download_state = package, None
        return package

    def resume_download(self, release, *, cancelled=lambda: False, paused=lambda: False, progress=None):
        return self.download(release, cancelled=cancelled, paused=paused, resume=True, progress=progress)

    def cancel_download(self, release):
        if type(release) is not Release or release != self.release:
            raise TrustError('Choose the signed release whose retained download is being cancelled.')
        _value, _policy, client, source = self._context()
        discarded = client.cancel_download(source, release)
        self.package = self.download_state = None
        return {'status': 'cancelled', 'discarded_partial': discarded, 'activated': False}

    def _trusted_verifier(self, value):
        raw = _read(Path(value['verifier']['path']), 4 * 1024**2)
        if sha256(raw).hexdigest() != value['verifier']['sha256']:
            raise TrustError('The trusted local bundle verifier changed.')
        if self._verifier is not None:
            return self._verifier
        path = Path(value['verifier']['path'])
        spec = importlib.util.spec_from_file_location('carr_reviewed_maintenance_installer', path)
        if spec is None:
            raise TrustError('The reviewed bundle verifier could not be loaded.')
        module = importlib.util.module_from_spec(spec)
        # Execute only the independently pinned local verifier snapshot, never
        # a verifier extracted from the downloaded update package.
        exec(compile(raw, str(path), 'exec'), module.__dict__)
        if not callable(getattr(module, 'verify', None)):
            raise TrustError('The reviewed installer has no compatible verify API.')
        return module.verify

    def handoff(self):
        value, policy, _client, _source = self._context()
        selected = value['anchor']
        anchor = self._anchor if self._anchor is not None else WindowsVaultAnchor(
            selected['reference'], selected['binding'], product=value['product'],
            profile_id=selected['profile_id'])
        anchor.load()  # Missing protected state is never created or reset here.
        verify = self._trusted_verifier(value)
        installed = value['installation']
        options = dict(installed_manifest_sha256=installed['manifest_sha256'])
        cls = PortableUpdateHandoff
        if installed['kind'] == 'standard':
            cls = StandardUpdateHandoff
            options.update(installed_setup_sha256=installed['setup_sha256'],
                           installed_version=installed['version'])
        return cls(self.app, Path(installed['program']), Path(installed['work']),
                   policy, anchor, verify, **options)

    def apply(self, *, accepted=False, cancelled=lambda: False):
        if accepted is not True or self.package is None:
            raise TrustError('Download the exact reviewed package and explicitly accept activation.')
        package = self.download(self.release, cancelled=cancelled)
        helper = self.handoff()
        self.configuration.unchanged()
        result = helper.apply(package, cancelled=cancelled)
        self.last_result = result
        return result

    def recover(self, *, accepted=False, cancelled=lambda: False):
        if accepted is not True or self.package is None:
            raise TrustError('Recovery requires a freshly authenticated matching package and explicit approval.')
        package = self.download(self.release, cancelled=cancelled)
        helper = self.handoff()
        self.configuration.unchanged()
        result = helper.recover(package, cancelled=cancelled)
        self.last_result = result
        return result

    def command(self, *, recovery=False):
        if self.release is None:
            raise TrustError('Review a signed release before preparing its maintenance command.')
        value = self.configuration.checked()
        executable = value['helper_python'] or sys.executable
        args = [executable, '-I', '-B', '-X', 'utf8', '-m', 'carr_library.security',
                'recover-update' if recovery else 'apply-update',
                '--configuration', str(self.configuration.path), '--configuration-sha256', self.configuration.digest,
                '--data-dir', str(self.library_root), '--reviewed-sha256', self.release.sha256, '--accept']
        return subprocess.list2cmdline(args) if os.name == 'nt' else __import__('shlex').join(args)
