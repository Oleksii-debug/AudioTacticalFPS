"""Prepare an external updater from the exact installed A distribution.

Only manifest-declared distribution files and eight fixed, independently bound
maintenance helpers are copied. The active virtual environment, user data and
private runtimes are never copied. The existing offline installer creates a new
environment at its final path inside an owned native process family.
"""
from __future__ import annotations

import argparse
import base64
import configparser
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time

from ._files import TrustError, component, directory, file_info, json_bytes, json_value, read_bytes, sync_directory
from .update_handoff import (OwnerUpdateConfiguration, _copy_snapshot_file, _digest,
                             _directory_rows, _same_directories, _hash_file)
from .updates import UpdatePolicy, relative_path

_HELPERS = {'maintenance.py', 'installation_lease.py', 'bundle_verifier.py',
            'managed_installation.py', 'windows_job.py', 'cleanup.py', 'models.py', 'process.py'}
_PRIVATE = {'.venv', 'data', 'state', 'models', 'voices', 'references', 'voice-packages',
            'dordiai-voices', 'node_modules', '.git', '.env'}

# These exact files were observed in the independently pinned CPython runtime.
# They are pip implementation modules, not an admission for a private models
# directory. Every name still requires the complete A bundle/owner SHA and the
# original ordinary-file, no-reparse and byte-hash checks below.
_RUNTIME_PIP_MODEL_FILES = frozenset(
    'runtime/Lib/site-packages/pip/_internal/models/' + suffix
    for module in ('__init__', 'candidate', 'direct_url', 'format_control',
                   'index', 'installation_report', 'link', 'release_control',
                   'scheme', 'search_scope', 'selection_prefs', 'target_python', 'wheel')
    for suffix in (module + '.py', '__pycache__/' + module + '.cpython-311.pyc')
)


def _publish_new(path, raw):
    """Publish without replacing a competing destination, even on a late race."""
    path = Path(path)
    rows = _directory_rows(path.parent)
    component(path.name)
    descriptor, temporary = tempfile.mkstemp(prefix='.prepare-update-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _same_directories(rows)
        os.link(temporary, path)  # atomic no-replace; the source is our own file
        os.unlink(temporary)
        temporary = None
        sync_directory(path.parent)
        if read_bytes(path, max(1, len(raw))) != raw:
            raise TrustError('The newly published update host receipt changed.')
        _same_directories(rows)
    finally:
        if temporary is not None:
            os.unlink(temporary)


class UpdateHostProvisioning:
    """An explicit, independently hashed owner recipe, never a guessed endpoint."""
    def __init__(self, path, expected_sha256):
        self.path = Path(path).absolute()
        self.sha256 = _digest(expected_sha256)
        self.raw = read_bytes(self.path, 65536)
        if sha256(self.raw).hexdigest() != self.sha256:
            raise TrustError('The owner maintenance preparation recipe changed.')
        value = json_value(self.raw, maximum=65536)
        expected = {'schema', 'kind', 'runtime', 'installation', 'library', 'publisher',
                    'repository', 'anchor', 'close_timeout', 'operation_timeout', 'source', 'configuration'}
        if (type(value) is not dict or set(value) != expected
                or type(value['schema']) is not int or value['schema'] != 1
                or value['kind'] != 'carr-managed-update-provision'):
            raise TrustError('Invalid owner maintenance preparation recipe.')
        self.value = value
        runtime, source = value['runtime'], value['source']
        if (type(runtime) is not dict or set(runtime) != {'root', 'manifest'}
                or type(source) is not dict
                or set(source) != {'bundle_sha256', 'owner_sha256', 'setup_sha256'}
                or type(value['installation']) is not dict
                or set(value['installation']) != {'root', 'work'}):
            raise TrustError('Invalid independently selected maintenance paths or A identity.')
        self.root = directory(value['installation']['root'])
        self.work = directory(value['installation']['work'])
        self.library = directory(value['library'])
        self.destination = Path(runtime['root']).absolute()
        component(self.destination.name)
        directory(self.destination.parent)
        self.manifest = Path(runtime['manifest']).absolute()
        self.configuration = Path(value['configuration']).absolute()
        for output in (self.manifest, self.configuration):
            component(output.name)
            directory(output.parent)
        if any(os.path.lexists(path) for path in (self.destination, self.manifest, self.configuration)):
            raise TrustError('Choose new host, manifest and configuration destinations; retain any previous attempt.')
        if self.manifest == self.configuration or self.path in (self.manifest, self.configuration):
            raise TrustError('The independent recipe and generated receipts must remain distinct.')
        groups = (self.root, self.work, self.library, self.destination)
        if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
               for index, a in enumerate(groups) for b in groups[index + 1:]):
            raise TrustError('The updater host, program, research data and recovery directories must be separate.')
        for output in (self.path, self.manifest, self.configuration):
            if any(output.is_relative_to(parent) for parent in groups):
                raise TrustError('The recipe and host receipts must be outside every managed payload directory.')
        if self.work.stat().st_dev != self.root.stat().st_dev:
            raise TrustError('The existing managed recovery requires the installation filesystem.')
        self.identities = tuple(_directory_rows(p) for p in
            (self.root, self.work, self.library, self.destination.parent,
             self.manifest.parent, self.configuration.parent, self.path.parent))
        self.source = {key: _digest(value) for key, value in source.items()}
        publisher = value['publisher']
        keys = {'product', 'channel', 'bootstrap', 'bootstrap_sha256', 'current_build',
                'data_schema', 'max_package_bytes', 'max_unpacked_bytes', 'max_files'}
        if type(publisher) is not dict or set(publisher) != keys:
            raise TrustError('The preparation recipe requires the complete independent TUF policy.')
        try:
            bootstrap = base64.b64decode(publisher['bootstrap'], validate=True)
        except (ValueError, TypeError):
            raise TrustError('Invalid independently provisioned TUF root.') from None
        self.policy = UpdatePolicy(**{**publisher, 'bootstrap': bootstrap})
        repository = value['repository']
        if type(repository) is not dict or set(repository) != {'endpoint', 'target', 'tls_ca_pem', 'timeout'}:
            raise TrustError('Invalid explicit update repository.')
        relative_path(repository['target'])
        if repository['endpoint'] is not None:
            from .update_http_worker import _endpoint
            _endpoint(repository['endpoint'])
        if (type(repository['timeout']) is not int or not 1 <= repository['timeout'] <= 3600
                or repository['tls_ca_pem'] is not None and
                (type(repository['tls_ca_pem']) is not str or len(repository['tls_ca_pem'].encode('utf-8')) > 16384)):
            raise TrustError('Invalid explicit HTTPS transport budget or trust material.')
        from .anchors import WindowsVaultAnchor
        anchor = value['anchor']
        if type(anchor) is not dict or set(anchor) != {'reference', 'binding', 'profile_id'}:
            raise TrustError('The protected update anchor must be independently configured.')
        WindowsVaultAnchor(**anchor, product=self.policy.product)
        for field, lower, upper in (('close_timeout', 1, 300), ('operation_timeout', 60, 7200)):
            if type(value[field]) is not int or not lower <= value[field] <= upper:
                raise TrustError('Invalid explicit update deadline.')
        self.check()

    def check(self):
        for rows in self.identities:
            _same_directories(rows)
        if read_bytes(self.path, 65536) != self.raw:
            raise TrustError('The independent host preparation recipe changed.')

    def document(self, manifest_sha256):
        value = {key: item for key, item in self.value.items() if key not in {'source', 'configuration'}}
        return {**value, 'kind': 'carr-managed-update-host', 'runtime': {
            'root': str(self.destination), 'manifest': str(self.manifest), 'sha256': _digest(manifest_sha256),
            'python': 'app/.venv/Scripts/python.exe', 'maintenance': 'maintenance/maintenance.py'}}


def _source(recipe, *, cancelled):
    recipe.check()
    root = recipe.root
    marker_raw = read_bytes(root / 'managed-install.ini', 8192)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(marker_raw.decode('utf-8'))
    if parser.sections() != ['installation'] or parser.defaults():
        raise TrustError('The source is not the selected standard installation.')
    marker = dict(parser['installation'])
    fields = {'schema', 'product', 'state', 'bundle_sha256', 'setup_sha256', 'owner_sha256'}
    if (set(marker) != fields or marker['schema'] != '1'
            or marker['product'] != 'Resonance.StandardInstall.v1' or marker['state'] != 'complete'
            or any(marker[name] != recipe.source[name] for name in recipe.source)):
        raise TrustError('The exact selected A installation is not complete or changed.')
    owner_raw = read_bytes(root / 'owned-files.json', 32 * 1024**2)
    if sha256(owner_raw).hexdigest() != recipe.source['owner_sha256']:
        raise TrustError('The independently pinned A ownership receipt changed.')
    owner = json_value(owner_raw, maximum=32 * 1024**2)
    if (type(owner) is not dict
            or set(owner) != {'schema', 'product', 'bundle_sha256', 'files', 'directories', 'maintenance'}
            or type(owner['schema']) is not int or owner['schema'] != 1
            or owner['product'] != 'Resonance.StandardInstall.v1'
            or owner['bundle_sha256'] != recipe.source['bundle_sha256']
            or type(owner['files']) is not dict or not owner['files']
            or type(owner['maintenance']) is not dict
            or set(owner['maintenance']) != _HELPERS | {'ResonanceSetup.exe'}
            or owner['maintenance']['ResonanceSetup.exe'] != recipe.source['setup_sha256']):
        raise TrustError('The selected A maintenance inventory is unsupported.')
    raw = read_bytes(root / 'app/bundle-manifest.json', 16 * 1024**2)
    if sha256(raw).hexdigest() != recipe.source['bundle_sha256']:
        raise TrustError('The independently pinned A distribution manifest changed.')
    manifest = json_value(raw, maximum=16 * 1024**2)
    files = manifest.get('files') if type(manifest) is dict else None
    if (type(manifest) is not dict or manifest.get('schema') != 1 or manifest.get('bundled_python') is not True
            or manifest.get('bundled_launcher') is not True
            or manifest.get('bundled_trusted_client') is not True
            or type(files) is not dict or not 1 <= len(files) <= 100000
            or not {'runtime/python.exe', 'install_windows.py', 'install.lock',
                    'trusted-client-dependencies.json'}.issubset(files)):
        raise TrustError('A must contain the verified offline trusted-client dependencies and Python runtime.')
    folded, total = set(), 0
    for name, digest in files.items():
        parts = relative_path(name)
        if (name.casefold() in folded
                or (any(part.casefold() in _PRIVATE for part in parts)
                    and name not in _RUNTIME_PIP_MODEL_FILES)
                or owner['files'].get(name) != _digest(digest)):
            raise TrustError('The host cannot include private, ambiguous or unowned distribution paths.')
        folded.add(name.casefold())
        if cancelled():
            raise InterruptedError('Host preparation cancelled.')
        actual, size = _hash_file(root / 'app' / name, maximum=4 * 1024**3, cancelled=cancelled)
        total += size
        if actual != digest or total > 32 * 1024**3:
            raise TrustError('A declared source distribution file changed or exceeds the host byte allowance.')
    for name in sorted(_HELPERS):
        expected = _digest(owner['maintenance'][name])
        if _hash_file(root / 'maintenance' / name, maximum=4 * 1024**2, cancelled=cancelled)[0] != expected:
            raise TrustError('A fixed source maintenance helper changed.')
    recipe.check()
    if (read_bytes(root / 'managed-install.ini', 8192) != marker_raw
            or read_bytes(root / 'owned-files.json', 32 * 1024**2) != owner_raw):
        raise TrustError('The source installation authority changed during verification.')
    return raw, files, owner['maintenance']


def _seal(root, *, cancelled):
    files, directories, used = {}, [], 0
    pending = [root]
    folded = set()
    while pending:
        parent = pending.pop()
        rows = _directory_rows(parent)
        directories.append(rows)
        if cancelled():
            raise InterruptedError('Host verification cancelled.')
        for path in parent.iterdir():
            name = path.relative_to(root).as_posix()
            relative_path(name)
            if name.casefold() in folded:
                raise TrustError('The generated host contains colliding paths.')
            folded.add(name.casefold())
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400:
                pending.append(path)
            else:
                digest, size = _hash_file(path, maximum=4 * 1024**3, cancelled=cancelled)
                files[name] = digest
                used += size
            if len(folded) > 100000 or used > 32 * 1024**3:
                raise TrustError('The generated host exceeds its fixed inventory allowance.')
        _same_directories(rows)
    for rows in directories:
        _same_directories(rows)
    return json_bytes({'schema': 1, 'files': files})


def prepare_update_host(recipe, *, cancelled=lambda: False):
    if type(recipe) is not UpdateHostProvisioning:
        raise TrustError('Use the independently hashed owner preparation recipe.')
    if os.name != 'nt':
        raise TrustError('The actual offline maintenance runtime is prepared on Windows.')
    from ..installation_lease import installation_lease
    from ..audio.process import run_process
    from ..audio.models import AudioBudget, AudioCleanupPending
    from .installation import _wait_for_cleanup
    deadline = time.monotonic() + recipe.value['operation_timeout']

    def stopped():
        recipe.check()
        return cancelled() or time.monotonic() >= deadline

    result = None
    with installation_lease(recipe.root):
        raw, files, helpers = _source(recipe, cancelled=stopped)
        if stopped():
            raise InterruptedError('Host preparation cancelled before creating a destination.')
        if any(os.path.lexists(path) for path in (recipe.destination, recipe.manifest, recipe.configuration)):
            raise TrustError('A preparation destination is no longer new.')
        recipe.destination.mkdir(mode=0o700)
        target_rows = _directory_rows(recipe.destination)
        app, maintenance = recipe.destination / 'app', recipe.destination / 'maintenance'
        app.mkdir()
        maintenance.mkdir()
        _publish_new(app / 'bundle-manifest.json', raw)
        total = sum(file_info(recipe.root / 'app' / name).st_size for name in files)
        if shutil.disk_usage(recipe.destination).free < 3 * total + 64 * 1024**2:
            raise TrustError('The external host needs space for its own complete offline environment.')
        for name, digest in files.items():
            recipe.check()
            _same_directories(target_rows)
            target = app.joinpath(*relative_path(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            directory(target.parent)
            _copy_snapshot_file(recipe.root / 'app' / name, target, digest,
                                maximum=4 * 1024**3, cancelled=stopped)
        for name in sorted(_HELPERS):
            _copy_snapshot_file(recipe.root / 'maintenance' / name, maintenance / name, helpers[name],
                                maximum=4 * 1024**2, cancelled=stopped)
        _source(recipe, cancelled=stopped)
        spec = importlib.util.spec_from_file_location('_carr_prepared_host_maintenance', maintenance / 'maintenance.py')
        if spec is None or spec.loader is None:
            raise TrustError('The captured maintenance helper could not be loaded.')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if set(module.HELPERS) != _HELPERS or any(Path(p).parent != maintenance for p in module.HELPERS.values()):
            raise TrustError('The fixed maintenance closure escaped its copied directory.')
        module.bundle(app, recipe.source['bundle_sha256'])
        environment = {k: v for k, v in os.environ.items()
                       if not k.upper().startswith(('PYTHON', 'PIP'))
                       and k.upper() != '__PYVENV_LAUNCHER__'}
        environment.update(PIP_NO_INDEX='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise TimeoutError('Host preparation reached its explicit deadline.')
        try:
            process = run_process([str(app / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8',
                                   str(app / 'install_windows.py')],
                cwd=app, env=environment, owned_windows_job=True, cancelled=stopped,
                watch=(recipe.destination,), budget=AudioBudget(timeout=min(1800, remaining),
                    max_output_bytes=32 * 1024**3, max_staging_files=100000, max_log_bytes=4 * 1024**2))
            if process.returncode != 0:
                raise TrustError('The existing offline installer refused host preparation.')
            file_info(app / '.venv/.installation-complete')
            # Actual installed imports prove the captured A wheel supplies this
            # fixed supervisor and its trusted-client dependency closure.
            process = run_process([str(app / '.venv/Scripts/python.exe'), '-I', '-B', '-c',
                'import carr_library.security.update_host; import tuf.ngclient; import cryptography'],
                cwd=app, env=environment, owned_windows_job=True, cancelled=stopped,
                budget=AudioBudget(timeout=60, max_log_bytes=65536))
            if process.returncode != 0:
                raise TrustError('A lacks the installed fixed updater or trusted-client dependencies.')
        except AudioCleanupPending as error:
            _wait_for_cleanup(error)
            raise
        recipe.check()
        _same_directories(target_rows)
        module.bundle(app, recipe.source['bundle_sha256'], allow_installed=True)
        _source(recipe, cancelled=stopped)
        sealed = _seal(recipe.destination, cancelled=stopped)
        checksum = sha256(sealed).hexdigest()
        configuration_raw = json_bytes(recipe.document(checksum))
        configuration_digest = sha256(configuration_raw).hexdigest()
        _publish_new(recipe.manifest, sealed)
        result = {'schema': 1, 'status': 'PREPARED', 'runtime': str(recipe.destination),
                  'manifest_sha256': checksum, 'configuration': str(recipe.configuration),
                  'configuration_sha256': configuration_digest,
                  'source_bundle_sha256': recipe.source['bundle_sha256'],
                  'installed_application_changed': False, 'vendor_rollout': 'NOT_RUN'}
        try:
            _publish_new(recipe.configuration, configuration_raw)
            verified = OwnerUpdateConfiguration(recipe.configuration, configuration_digest)
            verified.verify_runtime(cancelled=stopped)
            return result
        except BaseException as error:
            try:
                published = read_bytes(recipe.configuration, 65536) == configuration_raw
            except Exception:
                published = False
            if published:
                error.update_host_prepared = result
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', required=True, type=Path)
    parser.add_argument('--recipe-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_update_host(UpdateHostProvisioning(args.recipe, args.recipe_sha256))
        print(json_bytes(result).decode('ascii'))
        return 0
    except Exception as error:
        receipt = getattr(error, 'update_host_prepared', None)
        if receipt is not None:
            print(json_bytes(receipt).decode('ascii'))
        print('Host preparation refused: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
