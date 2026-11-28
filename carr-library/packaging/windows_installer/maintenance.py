"""Fixed offline install/repair/uninstall operations called by the Inno wrapper.

No package install logic lives here: the captured, verified bundle's existing
install_windows.py is used at its final absolute path. Every previous tree is
retained on repair; uninstall removes only exactly recorded, unchanged files.
"""
from __future__ import annotations

import argparse
import configparser
from contextlib import nullcontext
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import uuid


HERE = Path(__file__).absolute().parent
PRODUCT = 'Resonance.StandardInstall.v1'
OWNER = 'owned-files.json'
MARKER = 'managed-install.ini'
MAX_MANIFEST = 32 * 1024 * 1024
MAX_FILES = 100000
MAINTENANCE = ('ResonanceSetup.exe', 'maintenance.py', 'installation_lease.py',
               'bundle_verifier.py', 'process.py', 'windows_process.py', 'models.py',
               'cleanup.py', 'managed_installation.py')
LEGACY_MAINTENANCE = tuple(name for name in MAINTENANCE if name != 'managed_installation.py')


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lease_source = HERE / 'installation_lease.py'
if not lease_source.exists():
    lease_source = HERE.parent.parent / 'src/carr_library/installation_lease.py'
lease_module = _module('resonance_installation_lease', lease_source)
plain, installation_lease = lease_module.plain, lease_module.installation_lease
verify_source = HERE / 'bundle_verifier.py'
if not verify_source.exists():
    verify_source = HERE.parent / 'install_windows.py'
verifier = _module('resonance_trusted_bundle_verifier', verify_source)
protocol_source = HERE / 'managed_installation.py'
if not protocol_source.exists():
    protocol_source = HERE.parent.parent / 'src/carr_library/managed_installation.py'
upgrade_protocol = _module('resonance_managed_upgrade_protocol', protocol_source)
process_source = HERE / 'process.py'
if not process_source.exists():
    process_source = HERE.parent.parent / 'src/carr_library/audio/process.py'
PROCESS_HELPERS = ('process.py', 'windows_process.py', 'models.py', 'cleanup.py')
HELPERS = {'maintenance.py': HERE / 'maintenance.py', 'installation_lease.py': lease_source,
           'bundle_verifier.py': verify_source, 'managed_installation.py': protocol_source,
           **{name: process_source.parent / name for name in PROCESS_HELPERS}}


def _process_api():
    """Import the existing native owner and its relative imports as one package.

    The four sources are captured unchanged by the setup's exact helper
    inventory. No installed application or optional dependency is imported
    before the offline environment has been created.
    """
    directory = process_source.parent
    for name in PROCESS_HELPERS:
        plain(directory / name)
    identity = hashlib.sha256(str(directory).encode('utf-8')).hexdigest()[:24]
    name = '_resonance_installer_process_' + identity
    package = sys.modules.get(name)
    if package is None:
        package = types.ModuleType(name)
        package.__package__ = name
        package.__path__ = [str(directory)]
        package.__spec__ = importlib.util.spec_from_loader(name, loader=None, is_package=True)
        sys.modules[name] = package
    elif getattr(package, '__path__', None) != [str(directory)]:
        raise RuntimeError('The captured process helper package has another source.')
    process = importlib.import_module(name + '.process')
    cleanup = importlib.import_module(name + '.cleanup')
    return types.SimpleNamespace(owned_process=process.owned_process,
        pending_cleanup=cleanup.pending_cleanup,
        AudioCleanupPending=cleanup.AudioCleanupPending,
        pending_owners=cleanup._pending_owners)


def _wait_for_cleanup(owner):
    """Keep this synchronous helper and its exclusive lease until quiescence."""
    interrupted = None
    try:
        print('Waiting for the owned installation processes to stop; program files are retained.',
              flush=True)
    except (KeyboardInterrupt, SystemExit) as signal:
        interrupted = signal
    except (OSError, UnicodeError):
        # A closed console must not release ownership of a live process tree.
        pass
    while True:
        try:
            if owner.quiescent_event.is_set():
                break
            owner.close()
            owner.quiescent_event.wait(.25)
        except (KeyboardInterrupt, SystemExit) as error:
            # Returning early would let Inno or install() roll back a still-live
            # program directory. Deliver the interruption only after shutdown.
            if interrupted is None:
                interrupted = error
    if interrupted is not None:
        raise interrupted


def _wait_for_pending(error, process_api):
    """Drain every causal owner before any surrounding stream or lease exits."""
    interrupted = None
    for owner in process_api.pending_owners(error):
        try:
            _wait_for_cleanup(owner)
        except (KeyboardInterrupt, SystemExit) as signal:
            # Each owner is already quiescent when _wait_for_cleanup returns
            # its recorded interruption. Finish every other owner first.
            if interrupted is None:
                interrupted = signal
    if interrupted is not None:
        raise interrupted from error

def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate installation manifest field.')
        result[key] = value
    return result


def digest(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid installation checksum.')
    return value


def relative(value):
    if (type(value) is not str or not value or len(value) > 1024 or '\\' in value
            or any(c in value for c in ':<>"|?*') or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid owned installation path.')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in ('.', '..') or p.endswith((' ', '.')) for p in path.parts):
        raise ValueError('Invalid owned installation path.')
    for part in path.parts:
        base = part.split('.')[0].upper()
        if base in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            raise ValueError('A Windows device name cannot be an owned file.')
    return path


def read(path, limit=MAX_MANIFEST):
    before = plain(path)
    if before.st_size > limit:
        raise ValueError('Installation metadata exceeds its budget.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError('Installation file changed before reading.')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = plain(path)
    if len(raw) != before.st_size or any((i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns, i.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) for i in (after, current)):
        raise ValueError('Installation file changed during reading.')
    return raw


def sha(path):
    before = plain(path)
    with path.open('rb') as stream:
        if (os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('Installation payload changed before hashing.')
        result = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    current = plain(path)
    if any((i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns, i.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) for i in (after, current)):
        raise ValueError('Installation payload changed during hashing.')
    return result


def write(path, raw):
    plain(path.parent, directory=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(path):
            plain(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(root, *, exclude_top=()):
    plain(root, directory=True)
    files, directories, folded = {}, [], set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        if Path(directory) == root:
            for name in exclude_top:
                if name in dirs:
                    plain(root / name, directory=True)
                    dirs.remove(name)
        for name in sorted(dirs):
            child = Path(directory) / name
            plain(child, directory=True)
            text = child.relative_to(root).as_posix()
            relative(text)
            if text.casefold() in folded:
                raise ValueError('Case-colliding installation paths.')
            folded.add(text.casefold())
            directories.append(text)
        for name in sorted(names):
            child = Path(directory) / name
            text = child.relative_to(root).as_posix()
            relative(text)
            if text.casefold() in folded:
                raise ValueError('Case-colliding installation paths.')
            folded.add(text.casefold())
            if len(files) >= MAX_FILES:
                raise ValueError('Installation inventory exceeds its entry budget.')
            files[text] = sha(child)
    return files, sorted(directories)


def bundle(root, expected, *, allow_installed=False):
    raw = read(root / 'bundle-manifest.json')
    if hashlib.sha256(raw).hexdigest() != digest(expected):
        raise ValueError('The bundle differs from this installer identity.')
    document = json.loads(raw, object_pairs_hook=unique)
    # The current bundle builder verifies the x64 launcher against its exact
    # committed source and seals ResearchLibrary.exe in files. It has no
    # bundled_launcher flag; the actual inventory is the launcher authority.
    files = document.get('files')
    if (document.get('target') != 'Windows x64 / CPython 3.11'
            or document.get('bundled_python') is not True
            or not isinstance(files, dict) or 'ResearchLibrary.exe' not in files):
        raise ValueError('Standard installation requires the existing complete Python/native launcher bundle.')
    if not re.fullmatch('[0-9a-f]{40}', str(document.get('source_commit', ''))):
        raise ValueError('Standard installation requires an exact source commit.')
    expected_files = dict(files, **{'bundle-manifest.json': expected})
    for name, checksum in expected_files.items():
        relative(name)
        digest(checksum)
    actual, _ = inventory(root, exclude_top=('.venv',) if allow_installed else ())
    if actual != expected_files:
        raise ValueError('The pristine bundle contains missing, changed, or undeclared files.')
    verifier.verify(root, manifest_bytes=raw)
    return document


def marker(root):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(read(root / MARKER, 8192).decode('utf-8'))
    if parser.sections() != ['installation'] or parser.defaults():
        raise ValueError('Invalid managed installation identity.')
    values = dict(parser['installation'])
    if set(values) != {'schema', 'product', 'state', 'bundle_sha256', 'setup_sha256', 'owner_sha256'} or values['schema'] != '1' or values['product'] != PRODUCT or values['state'] not in ('preparing', 'complete', 'updating', 'uninstalled'):
        raise ValueError('Invalid managed installation identity.')
    for name in ('bundle_sha256', 'setup_sha256', 'owner_sha256'):
        digest(values[name])
    return values


def set_marker(root, state, bundle_hash, setup_hash, owner_hash='0' * 64):
    text = '[installation]\n' + ''.join(f'{key}={value}\n' for key, value in dict(schema='1', product=PRODUCT, state=state, bundle_sha256=digest(bundle_hash), setup_sha256=digest(setup_hash), owner_sha256=digest(owner_hash)).items())
    write(root / MARKER, text.encode('utf-8'))


def owned(root, expected, setup_hash, *, allow_updating=False):
    info = marker(root)
    if info['bundle_sha256'] != digest(expected) or info['setup_sha256'] != digest(setup_hash):
        raise ValueError('Use the matching setup for this installation; this is not an upgrade service.')
    if info['state'] != 'complete' and not (allow_updating and info['state'] == 'updating'):
        raise ValueError('The installation has not completed; retain it and run the matching setup for recovery.')
    raw = read(root / OWNER)
    if hashlib.sha256(raw).hexdigest() != info['owner_sha256']:
        raise ValueError('The owned-file manifest changed. No files were removed.')
    value = json.loads(raw, object_pairs_hook=unique)
    if set(value) != {'schema', 'product', 'bundle_sha256', 'files', 'directories', 'maintenance'} or value['schema'] != 1 or value['product'] != PRODUCT or value['bundle_sha256'] != expected:
        raise ValueError('Invalid owned-file manifest.')
    if type(value['files']) is not dict or not value['files'] or len(value['files']) > MAX_FILES or type(value['directories']) is not list or type(value['maintenance']) is not dict:
        raise ValueError('Invalid owned-file inventory.')
    seen = set()
    for path in (*value['files'], *value['directories']):
        relative(path)
        if path.casefold() in seen:
            raise ValueError('Ambiguous owned-file inventory.')
        seen.add(path.casefold())
    for checksum in value['files'].values():
        digest(checksum)
    # The wrapper has a fixed set of files; supplied JSON cannot own arbitrary
    # files elsewhere in the program folder or in a research-data directory.
    if set(value['maintenance']) not in (set(MAINTENANCE), set(LEGACY_MAINTENANCE)):
        raise ValueError('Invalid installer maintenance inventory.')
    for name, checksum in value['maintenance'].items():
        if sha(root / 'maintenance' / name) != digest(checksum):
            raise ValueError('The maintenance tools changed. Use the original verified setup.')
    if value['maintenance']['ResonanceSetup.exe'] != setup_hash:
        raise ValueError('The cached setup identity changed.')
    return value


def location(root):
    root = Path(root).absolute()
    if '..' in root.parts or root == Path(root.anchor) or root.name in ('.', '..'):
        raise ValueError('Choose a new local program folder.')
    if os.name == 'nt' and (not root.drive or root.drive.startswith('\\')):
        raise ValueError('Use a local per-user Windows drive folder.')
    parent = root.parent
    while not os.path.lexists(parent):
        parent = parent.parent
    plain(parent, directory=True)
    local = os.environ.get('LOCALAPPDATA')
    if local:
        data = (Path(local) / 'Universal Research Library').absolute()
        if root == data or root in data.parents or data in root.parents:
            raise ValueError('The program and research-data directories must be separate.')
    return root


def preflight(root, expected, setup_hash):
    """Reserve only a fresh folder; an existing folder needs our exact receipt."""
    root = location(root)
    if not root.exists():
        root.mkdir(parents=True)
    plain(root, directory=True)
    if not os.path.lexists(root / MARKER):
        if any(root.iterdir()):
            raise ValueError('This folder contains unowned files. Choose a new program folder.')
        # Atomic exclusive creation prevents two first installs claiming a
        # preexisting directory. The persistent file is never replaced/deleted.
        with (root / lease_module.LEASE).open('xb'):
            pass
        set_marker(root, 'preparing', expected, setup_hash)
    else:
        info = marker(root)
        if info['bundle_sha256'] != digest(expected) or info['setup_sha256'] != digest(setup_hash):
            raise ValueError('This program folder belongs to a different installation identity.')
        plain(root / lease_module.LEASE)
    return root


def _bootstrap(program):
    if os.name != 'nt':
        raise RuntimeError('Actual offline installation requires Windows; source tests use an explicit synthetic runner.')
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith(('PYTHON', 'PIP_'))}
    environment.update(PIP_NO_INDEX='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    # Capture and reuse the current production process family owner. No
    # application import, alternate supervisor or dependency resolver is needed.
    process_api = _process_api()
    command = [str(program / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8',
               str(program / 'install_windows.py')]
    # Keep the same regular binary stream alive even if construction or exit
    # reports pending native cleanup. Never roll back a still-executing tree.
    with tempfile.TemporaryFile(mode='w+b') as output:
        completed = False
        operation_error = None
        try:
            with process_api.owned_process(command, cwd=program, env=environment,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output) as child:
                try:
                    result = child.wait(timeout=1800)
                    if result:
                        raise subprocess.CalledProcessError(result, command)
                    completed = True
                except BaseException as error:
                    operation_error = error
                    raise
        except BaseException as error:
            owners = process_api.pending_owners(error)
            _wait_for_pending(error, process_api)
            if owners and isinstance(error, process_api.AudioCleanupPending):
                # Once the complete family is proven quiet, retain the actual
                # bootstrap outcome. A cleanup retry is not a successful
                # constructor, and a masked operation error must not disappear.
                if operation_error is not None and operation_error is not error:
                    raise operation_error from error
                if completed:
                    return
            raise

def check(root, expected, setup_hash, *, owner=None):
    root = location(root)
    with installation_lease(root, exclusive=True, owner=owner):
        info = marker(root)
        if info['bundle_sha256'] != expected or info['setup_sha256'] != setup_hash:
            raise ValueError('This folder belongs to a different installation identity.')
        if info['state'] == 'complete':
            owned(root, expected, setup_hash)
        else:
            allowed = {MARKER, lease_module.LEASE, 'maintenance', 'unins000.exe', 'unins000.dat', 'unins000.msg'}
            if any(p.name not in allowed for p in root.iterdir()):
                raise ValueError('Unowned or incomplete files were retained. Choose a new program folder.')
            maintenance = root / 'maintenance'
            if maintenance.exists():
                for path in maintenance.iterdir():
                    if path.name == 'ResonanceSetup.exe':
                        expected_file = setup_hash
                    elif path.name in HELPERS:
                        expected_file = sha(HELPERS[path.name])
                    else:
                        raise ValueError('The maintenance folder contains an unowned file.')
                    if sha(path) != expected_file:
                        raise ValueError('A retained maintenance file changed; it will not be overwritten.')
    return {'status': 'PASS', 'operation': 'check'}


def install(payload, root, expected, setup_hash, *, owner=None, runner=_bootstrap, setup_source=None):
    bundle(payload, expected)
    root = preflight(root, expected, setup_hash)
    with installation_lease(root, exclusive=True, owner=owner):
        info = marker(root)
        if info['state'] == 'complete':
            owned(root, expected, setup_hash)  # Metadata must be trustworthy; changed app files are retained.
        elif any(p.name not in {MARKER, lease_module.LEASE, 'maintenance', 'unins000.exe', 'unins000.dat', 'unins000.msg'} for p in root.iterdir()):
            raise ValueError('An incomplete installation contains retained files. Preserve it and choose a new program folder.')
        if setup_source is not None:
            sources = dict(HELPERS, **{'ResonanceSetup.exe': Path(setup_source)})
            source_hashes = {name: sha(source) for name, source in sources.items()}
            if source_hashes['ResonanceSetup.exe'] != setup_hash:
                raise ValueError('The setup source changed before caching.')
            tools = root / 'maintenance'
            tools.mkdir(exist_ok=True)
            plain(tools, directory=True)
            if any(path.name not in sources for path in tools.iterdir()):
                raise ValueError('The maintenance folder contains an unowned file.')
            for name, source in sources.items():
                source_hash = source_hashes[name]
                destination = tools / name
                if os.path.lexists(destination):
                    if sha(destination) != source_hash:
                        raise ValueError('An existing maintenance file will not be overwritten.')
                else:
                    with source.open('rb') as incoming, destination.open('xb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if sha(destination) != source_hash:
                        raise ValueError('A cached maintenance file changed during copying.')
        program = root / 'app'
        stage = root / ('staging-' + uuid.uuid4().hex)
        recovery = None
        previous_receipts = {}
        previous_moved = new_published = False
        shutil.copytree(payload, stage, symlinks=False)
        bundle(stage, expected)
        if os.path.lexists(program):
            plain(program, directory=True)
            for name in (OWNER, MARKER):
                if os.path.lexists(root / name):
                    previous_receipts[name] = read(root / name)
        try:
            if os.path.lexists(program):
                recovery = root.parent / (root.name + '-recovery-' + uuid.uuid4().hex)
                recovery.mkdir(exist_ok=False)
                # Allocate recovery receipts while the original application is
                # still in place. A failed copy must not depend on moving it
                # back, or allocating replacement metadata, to stay usable.
                for name in previous_receipts:
                    shutil.copyfile(root / name, recovery / name)
                # All unknown/modified files travel with this directory, never
                # into a delete list. Later failures restore the whole tree.
                program.rename(recovery / 'app')
                previous_moved = True
            stage.rename(program)
            new_published = True
            runner(program)
            if not (program / '.venv/.installation-complete').is_file():
                raise ValueError('The existing offline installer did not publish completion.')
            verifier.verify(program)
            files, directories = inventory(program)
            maintenance = {name: sha(root / 'maintenance' / name) for name in MAINTENANCE}
            if maintenance['ResonanceSetup.exe'] != setup_hash:
                raise ValueError('Cached setup changed before completion.')
            record = dict(schema=1, product=PRODUCT, bundle_sha256=expected, files=files,
                          directories=directories, maintenance=maintenance)
            raw = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > MAX_MANIFEST:
                raise ValueError('Owned-file receipt exceeds its budget.')
            write(root / OWNER, raw)
            set_marker(root, 'complete', expected, setup_hash, hashlib.sha256(raw).hexdigest())
        except BaseException:
            if new_published and program.exists():
                failed = root.parent / (root.name + '-incomplete-' + uuid.uuid4().hex)
                program.rename(failed)
            if previous_moved:
                (recovery / 'app').rename(program)
                for name, original in previous_receipts.items():
                    current = root / name
                    # Pre-bootstrap errors never changed these files. Avoid a
                    # needless new write when the original failure was ENOSPC.
                    if os.path.lexists(current) and read(current) == original:
                        continue
                    retained = recovery / name
                    if os.path.lexists(retained) and read(retained) == original:
                        if os.path.lexists(current):
                            plain(current)
                        os.replace(retained, current)
                    else:
                        write(current, original)
            raise
        return {'status': 'PASS', 'operation': 'repair' if recovery else 'install',
                'bundle_sha256': expected, 'retained_previous': recovery.name if recovery else None,
                'user_data_modified': False}


def uninstall(root, expected, setup_hash, *, owner=None):
    root = location(root)
    with installation_lease(root, exclusive=True, owner=owner):
        record = owned(root, expected, setup_hash)
        program = root / 'app'
        current, directories = inventory(program)
        if current != record['files'] or directories != record['directories']:
            raise ValueError('Program files were changed or unowned files were added. Run Repair to retain them before uninstalling.')
        for name, checksum in record['files'].items():
            path = program.joinpath(*relative(name).parts)
            if sha(path) != checksum:
                raise ValueError('An owned file changed before removal; remaining files were retained.')
            path.unlink()
        for name in sorted(record['directories'], key=lambda s: (s.count('/'), s), reverse=True):
            program.joinpath(*relative(name).parts).rmdir()
        program.rmdir()
        # Only fixed metadata that was verified above is changed. Inno removes
        # its own checked maintenance files, shortcuts and uninstall record.
        (root / OWNER).unlink()
        set_marker(root, 'uninstalled', expected, setup_hash)
        return {'status': 'PASS', 'operation': 'uninstall', 'bundle_sha256': expected,
                'user_data_modified': False, 'coordination_receipt_retained': True}


def _upgrade_proof(root, expected, setup_hash, proof_file, proof_sha256):
    root = location(root)
    raw = read(Path(proof_file), upgrade_protocol.MAX_PROOF)
    proof = upgrade_protocol.decode_proof(raw, proof_sha256)
    upgrade_protocol.require_binding(proof, root, expected, setup_hash)
    previous = upgrade_protocol.previous_path(proof)
    plain(previous, directory=True)
    if Path(proof_file).absolute() != previous / upgrade_protocol.PROOF_FILE:
        raise ValueError('The captured upgrade proof is outside its retained transaction.')
    old = marker(previous)
    if old['state'] != 'complete' or any(old[key] != value for key, value in proof['old'].items()):
        raise ValueError('The retained original installation differs from the upgrade approval.')
    if sha(previous / OWNER) != old['owner_sha256']:
        raise ValueError('The retained original ownership receipt changed.')
    return root, proof, previous


def upgrade_check(payload, root, expected, setup_hash, *, proof_file, proof_sha256,
                  owner, setup_source):
    if owner is None:
        raise ValueError('A managed update requires its actual exclusive maintenance lease.')
    root, proof, previous = _upgrade_proof(root, expected, setup_hash, proof_file, proof_sha256)
    with installation_lease(root, exclusive=True, owner=owner):
        current = marker(root)
        if current['state'] != 'complete' or any(current[key] != value for key, value in proof['old'].items()):
            raise ValueError('The installation changed after the reviewed update selection.')
        old = owned(root, current['bundle_sha256'], current['setup_sha256'])
        if inventory(root / 'app') != (old['files'], old['directories']):
            raise ValueError('Changed or foreign program files need reviewed Repair before updating.')
        document = bundle(payload, expected)
        if document['source_commit'] != proof['new']['source_commit'] or sha(Path(setup_source)) != setup_hash:
            raise ValueError('The proposed setup or source differs from the captured update.')
        if any(os.path.lexists(previous / name) for name in ('app', 'maintenance')):
            raise ValueError('Recover the already started managed update before another attempt.')
    return {'status': 'PASS', 'operation': 'upgrade-check'}


def upgrade(payload, root, expected, setup_hash, *, proof_file, proof_sha256, owner,
            setup_source, runner=_bootstrap):
    """Fixed Setup-B operation; the trusted outer transaction owns recovery.

    Ordinary install/repair still require the same setup identity. This path
    never commits the protected update anchor or clears the pending marker.
    The outer owner retains the exact native lease through Inno registration,
    health verification and commit, and can restore the complete original.
    """
    root, proof, previous = _upgrade_proof(root, expected, setup_hash, proof_file, proof_sha256)
    with installation_lease(root, exclusive=True, owner=owner):
        upgrade_check(payload, root, expected, setup_hash, proof_file=proof_file,
            proof_sha256=proof_sha256, owner=owner, setup_source=setup_source)
        stage = root / ('staging-upgrade-' + proof['transaction'])
        if os.path.lexists(stage):
            raise ValueError('Managed update staging already exists; recover it explicitly.')
        shutil.copytree(payload, stage, symlinks=False)
        bundle(stage, expected)
        # Until the outer protected commit, even a complete B environment is
        # deliberately unavailable to normal application entrypoints.
        set_marker(root, 'updating', expected, setup_hash)
        (root / 'app').rename(previous / 'app')
        (root / 'maintenance').rename(previous / 'maintenance')
        tools = root / 'maintenance'
        tools.mkdir()
        sources = dict(HELPERS, **{'ResonanceSetup.exe': Path(setup_source)})
        hashes = {name: sha(path) for name, path in sources.items()}
        if hashes['ResonanceSetup.exe'] != setup_hash:
            raise ValueError('The authenticated setup changed before caching.')
        for name, source in sources.items():
            with source.open('rb') as incoming, (tools / name).open('xb') as output:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if sha(tools / name) != hashes[name]:
                raise ValueError('A managed update helper changed during caching.')
        stage.rename(root / 'app')
        runner(root / 'app')
        if not (root / 'app/.venv/.installation-complete').is_file():
            raise ValueError('The new offline environment did not publish completion.')
        verifier.verify(root / 'app')
        files, directories = inventory(root / 'app')
        actual_helpers = {name: sha(tools / name) for name in MAINTENANCE}
        if actual_helpers != hashes:
            raise ValueError('Managed update helpers changed during bootstrap.')
        record = dict(schema=1, product=PRODUCT, bundle_sha256=expected,
                      files=files, directories=directories, maintenance=actual_helpers)
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > MAX_MANIFEST:
            raise ValueError('New managed ownership receipt exceeds its byte budget.')
        write(root / OWNER, raw)
        set_marker(root, 'updating', expected, setup_hash, hashlib.sha256(raw).hexdigest())
    return {'status': 'PASS', 'operation': 'upgrade-staged', 'transaction': proof['transaction'],
            'bundle_sha256': expected, 'protected_commit': False, 'user_data_modified': False}


def finish_upgrade(root, *, proof_file, proof_sha256, owner):
    if owner is None:
        raise ValueError('Managed recovery requires its actual exclusive maintenance lease.')
    raw = read(Path(proof_file), upgrade_protocol.MAX_PROOF)
    proof = upgrade_protocol.decode_proof(raw, proof_sha256)
    root, proof, _ = _upgrade_proof(root, proof['new']['bundle_sha256'],
        proof['new']['setup_sha256'], proof_file, proof_sha256)
    with installation_lease(root, exclusive=True, owner=owner):
        current = marker(root)
        record = owned(root, proof['new']['bundle_sha256'], proof['new']['setup_sha256'], allow_updating=True)
        if inventory(root / 'app') != (record['files'], record['directories']):
            raise ValueError('The new installed program changed before update commit.')
        set_marker(root, 'complete', current['bundle_sha256'], current['setup_sha256'], current['owner_sha256'])


def restore_upgrade(root, *, proof_file, proof_sha256, owner, restore_wrapper):
    """Restore A's exact files and wrapper before permitting ordinary launches."""
    if owner is None:
        raise ValueError('Managed recovery requires its actual exclusive maintenance lease.')
    raw = read(Path(proof_file), upgrade_protocol.MAX_PROOF)
    proof = upgrade_protocol.decode_proof(raw, proof_sha256)
    root, proof, previous = _upgrade_proof(root, proof['new']['bundle_sha256'],
        proof['new']['setup_sha256'], proof_file, proof_sha256)
    failed = upgrade_protocol.failed_path(proof)
    with installation_lease(root, exclusive=True, owner=owner):
        # Keep all replaced or partial B files; never recursively delete them.
        for name in ('app', 'maintenance'):
            retained = previous / name
            if os.path.lexists(retained):
                plain(retained, directory=True)
                if os.path.lexists(root / name):
                    plain(root / name, directory=True)
                    failed.mkdir(exist_ok=True)
                    plain(failed, directory=True)
                    if os.path.lexists(failed / name):
                        raise ValueError('Managed update recovery destination already exists.')
                    (root / name).rename(failed / name)
                retained.rename(root / name)
        old_owner = read(previous / OWNER)
        old = json.loads(old_owner, object_pairs_hook=unique)
        if inventory(root / 'app') != (old['files'], old['directories']):
            raise ValueError('The retained original application changed; automatic recovery stopped.')
        for name, checksum in old['maintenance'].items():
            if sha(root / 'maintenance' / name) != checksum:
                raise ValueError('The retained original setup tools changed; automatic recovery stopped.')
        write(root / OWNER, old_owner)
        restore_wrapper()
        # Marker is last: failed wrapper restoration must leave launches blocked.
        write(root / MARKER, read(previous / MARKER, 8192))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('preflight', 'check', 'install', 'uninstall', 'upgrade-check', 'upgrade'))
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--bundle-sha256', required=True)
    parser.add_argument('--setup-sha256', required=True)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--setup-source', type=Path)
    parser.add_argument('--lease-owner', nargs=2, type=int)
    parser.add_argument('--upgrade-proof', type=Path)
    parser.add_argument('--upgrade-proof-sha256')
    args = parser.parse_args(argv)
    if args.operation in ('upgrade-check', 'upgrade'):
        if any(value is None for value in (args.payload, args.setup_source,
                args.upgrade_proof, args.upgrade_proof_sha256, args.lease_owner)):
            parser.error('Managed upgrade requires its complete captured identity and live lease.')
        callback = upgrade_check if args.operation == 'upgrade-check' else upgrade
        result = callback(args.payload, args.root, args.bundle_sha256, args.setup_sha256,
            proof_file=args.upgrade_proof, proof_sha256=args.upgrade_proof_sha256,
            owner=args.lease_owner, setup_source=args.setup_source)
    elif args.operation == 'preflight':
        preflight(args.root, args.bundle_sha256, args.setup_sha256)
        result = {'status': 'PASS', 'operation': 'preflight'}
    elif args.operation == 'install':
        if args.payload is None:
            parser.error('Install requires its captured payload.')
        result = install(args.payload, args.root, args.bundle_sha256, args.setup_sha256,
                         owner=args.lease_owner, setup_source=args.setup_source)
    elif args.operation == 'check':
        result = check(args.root, args.bundle_sha256, args.setup_sha256, owner=args.lease_owner)
    else:
        result = uninstall(args.root, args.bundle_sha256, args.setup_sha256, owner=args.lease_owner)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('Resonance setup: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
