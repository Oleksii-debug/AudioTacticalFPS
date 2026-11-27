"""Inert inspection for the verified-file-job/1 worker protocol.

The application reads package metadata and bytes from an explicitly selected
local CPython virtual environment. Selected code is never imported or executed
during inspection. The full applicable dependency closure is pinned; additional
visible site-packages bytes are also pinned for launch revalidation.
"""
import csv
from importlib import metadata
import io
import os
from pathlib import Path, PurePosixPath
import re

from ..durable_state import local_path, regular
from .models import fingerprint
from .runtime import file_identity, stopped
from .verified_protocol import (PROTOCOL, canonical, digest, model_identity,
    ROOT_DISTRIBUTIONS, read_bytes, read_json, validate_implementation, validate_packages)

MAX_DISTRIBUTIONS = 100
MAX_FILES = 50000
MAX_BYTES = 64 * 1024**3


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def descriptor_path(worker):
    return Path(worker).with_suffix('.protocol.json')


def worker_descriptor(worker, python, *, cancel=None):
    path = descriptor_path(worker)
    value, identity = read_json(path, maximum=65536)
    if (type(value) is not dict or set(value) != {'kind', 'version', 'protocol', 'worker', 'helper'}
            or value['kind'] != 'whisper-transcribe-protocol'
            or type(value['version']) is not int or value['version'] != 1
            or value['protocol'] != PROTOCOL):
        raise ValueError('Invalid explicit worker protocol descriptor.')
    implementation = []
    for key, selected in (('worker', Path(worker)), ('helper', Path(worker).with_name('recovery_support.py'))):
        expected = value[key]
        if (type(expected) is not dict or set(expected) != {'name', 'sha256', 'size_bytes'}
                or expected['name'] != selected.name):
            raise ValueError('The protocol descriptor must name the exact worker and sibling helper.')
        actual = file_identity(selected, cancel=cancel, maximum=16 * 1024**2)
        row = {'name': selected.name, 'sha256': actual['sha256'], 'size_bytes': actual['size']}
        if canonical(row) != canonical(expected):
            raise ValueError('Reviewed worker/helper bytes do not match their protocol descriptor.')
        implementation.append(row)
    actual = file_identity(python, cancel=cancel, maximum=256 * 1024**2)
    implementation.append({'name': Path(python).name,
                           'sha256': actual['sha256'], 'size_bytes': actual['size']})
    validate_implementation(implementation)
    return implementation, {'path': str(path), **identity}


def _environment(python):
    from packaging.markers import default_environment
    from packaging.version import Version
    python = Path(python)
    if python.parent.name.casefold() not in ('bin', 'scripts'):
        raise ValueError('This protocol requires an explicit local CPython virtual environment.')
    root = local_path(python.parent.parent, exists=True)
    configuration = root / 'pyvenv.cfg'
    raw = read_bytes(configuration, maximum=65536)
    fields = {}
    for line in raw.decode('utf-8', 'strict').splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition('=')
        if not separator or name.strip().casefold() in fields:
            raise ValueError('Invalid virtual environment configuration.')
        fields[name.strip().casefold()] = value.strip()
    if fields.get('include-system-site-packages', '').casefold() != 'false':
        raise ValueError('The reviewed ASR environment must exclude system site-packages.')
    version = Version(fields.get('version', ''))
    if version.is_prerelease or len(version.release) < 3 or version.release[:2] < (3, 11):
        raise ValueError('A supported stable CPython version is required.')
    base_home = Path(fields.get('home', ''))
    if not base_home.is_absolute():
        raise ValueError('The selected environment requires an absolute operational base home.')
    base_home = local_path(base_home, exists=True)
    for directory in {python.parent, Path(python).resolve(strict=True).parent, base_home}:
        if any(directory.glob('*._pth')):
            raise ValueError('Embedded path override files are not supported by this protocol.')
    major_minor = '.'.join(str(part) for part in version.release[:2])
    selected = root / ('Lib/site-packages' if os.name == 'nt' else 'lib/python' + major_minor + '/site-packages')
    selected = local_path(selected, root=root, exists=True)
    regular(selected, directory=True)
    candidates = [root / 'Lib/site-packages', *sorted((root / 'lib').glob('python*/site-packages'))]
    if {str(local_path(path, root=root, exists=True)) for path in candidates if path.exists()} != {str(selected)}:
        raise ValueError('The selected environment has an ambiguous site-packages layout.')
    markers = default_environment()
    if markers['implementation_name'] != 'cpython':
        raise ValueError('Verified worker inspection requires the CPython marker contract.')
    markers.update(python_version=major_minor, python_full_version=str(version),
                   implementation_version=str(version), extra='')
    return root, selected, markers, file_identity(configuration)


def _installed(site, *, cancel=None):
    selected = {}
    paths = sorted(site.glob('*.dist-info'))
    if len(paths) > 8192:
        raise ValueError('Too many installed distribution records.')
    for path in paths:
        stopped(cancel)
        regular(local_path(path, root=site, exists=True), directory=True)
        meta_path = path / 'METADATA'
        raw = read_bytes(meta_path, maximum=1024**2)
        distribution = metadata.PathDistribution(path)
        name = normalized(distribution.metadata.get('Name') or '')
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or name in selected:
            raise ValueError('Missing or ambiguous installed distribution identity.')
        # PathDistribution reads only inert metadata. Recheck the bytes it used.
        if read_bytes(meta_path, maximum=1024**2) != raw:
            raise ValueError('Installed distribution metadata changed during inspection.')
        selected[name] = (distribution, raw)
    return selected


def _closure(installed, markers):
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
    pending = [(name, frozenset()) for name in sorted(ROOT_DISTRIBUTIONS)]
    reached, expanded = set(), {}
    while pending:
        name, extras = pending.pop()
        if name not in installed:
            raise ValueError('The selected runtime is missing required dependency: ' + name)
        distribution, _ = installed[name]
        version = Version(distribution.version)
        previous = expanded.get(name)
        combined = set(extras) | (previous or set())
        if previous is not None and combined == previous:
            continue
        expanded[name] = combined
        reached.add(name)
        if len(reached) > MAX_DISTRIBUTIONS:
            raise ValueError('Runtime dependency closure exceeds its distribution budget.')
        requires_python = distribution.metadata.get('Requires-Python')
        if requires_python and not SpecifierSet(requires_python).contains(markers['python_full_version'], prereleases=False):
            raise ValueError('An installed dependency does not support the selected Python.')
        for text in distribution.requires or ():
            requirement = Requirement(text)
            if requirement.marker is not None and not any(
                    requirement.marker.evaluate({**markers, 'extra': extra})
                    for extra in ('', *sorted(combined))):
                continue
            child = normalized(requirement.name)
            if requirement.url:
                raise ValueError('Direct URL dependencies require a separately reviewed runtime contract.')
            if child not in installed:
                raise ValueError('The selected runtime is missing transitive dependency: ' + child)
            if requirement.specifier and not requirement.specifier.contains(
                    Version(installed[child][0].version), prereleases=True):
                raise ValueError('An installed runtime dependency violates its declared version requirement.')
            pending.append((child, frozenset(normalized(extra) for extra in requirement.extras)))
    return sorted(reached)


def _distribution_files(distribution, root, *, cancel=None, progress=None, counter):
    record_path = distribution._path / 'RECORD'
    raw = read_bytes(record_path, maximum=16 * 1024**2)
    rows, labels = [], set()
    for record in csv.reader(io.StringIO(raw.decode('utf-8', 'strict'), newline='')):
        stopped(cancel)
        if len(record) != 3:
            raise ValueError('Malformed installed distribution RECORD row.')
        label = record[0]
        path = PurePosixPath(label)
        if (not label or path.is_absolute() or '\\' in label or ':' in label
                or path.as_posix() != label or any(ord(character) < 32 for character in label)):
            raise ValueError('Invalid installed file record path.')
        if label in labels:
            raise ValueError('Duplicate installed file record.')
        labels.add(label)
        if len(labels) > MAX_FILES * 2:
            raise ValueError('Installed file inventory exceeds its row budget.')
        if '__pycache__' in path.parts or label.endswith(('.pyc', '.pyo')):
            continue
        target = local_path(distribution.locate_file(label), root=root, exists=True)
        identity = file_identity(target, cancel=cancel, maximum=MAX_BYTES)
        counter[0] += 1
        counter[1] += identity['size']
        if counter[0] > MAX_FILES or counter[1] > MAX_BYTES:
            raise ValueError('Runtime dependency closure exceeds its file or byte budget.')
        if progress:
            progress({'phase': 'Verifying complete ASR dependency closure', 'status': 'running',
                      'current': distribution.metadata['Name'] + ': ' + label,
                      'done': counter[0], 'total': None, 'indeterminate': True})
        rows.append({'path': label, 'sha256': identity['sha256'], 'size_bytes': identity['size']})
    required = {distribution._path.name + '/METADATA', distribution._path.name + '/RECORD'}
    if not required <= labels:
        raise ValueError('Installed metadata and its RECORD must be included in the byte inventory.')
    if raw != read_bytes(record_path, maximum=16 * 1024**2):
        raise ValueError('Installed file inventory changed during inspection.')
    return sorted(rows, key=lambda row: row['path'])


def _visible_inventory(site, *, cancel=None):
    """Pin visible non-bytecode files, including undeclared namespace additions."""
    pending, rows, visited, total = [site], [], 0, 0
    while pending:
        directory = pending.pop()
        regular(local_path(directory, root=site, exists=True), directory=True)
        with os.scandir(directory) as entries:
            for entry in entries:
                stopped(cancel)
                visited += 1
                if visited > MAX_FILES * 2:
                    raise ValueError('Visible ASR runtime inventory exceeds its entry budget.')
                path = local_path(entry.path, root=site, exists=True)
                if path.is_dir():
                    pending.append(path)
                    continue
                relative = path.relative_to(site).as_posix()
                if path.suffix in ('.pyc', '.pyo'):
                    if '__pycache__' not in path.parts:
                        raise ValueError('Sourceless executable bytecode is not supported.')
                    continue
                actual = file_identity(path, cancel=cancel, maximum=MAX_BYTES)
                total += actual['size']
                if len(rows) >= MAX_FILES or total > MAX_BYTES:
                    raise ValueError('Visible ASR runtime inventory exceeds its file or byte budget.')
                rows.append({'path': relative, 'sha256': actual['sha256'], 'size_bytes': actual['size']})
    return {'sha256': digest(sorted(rows, key=lambda row: row['path'])),
            'files': len(rows), 'size_bytes': total}


def extend_profile(profile, *, cancel=None, progress=None):
    """Opt in through a verified descriptor; retain the existing trust workflow."""
    implementation, descriptor = worker_descriptor(profile['worker_path'],
        profile['python_resolved'], cancel=cancel)
    root, site, markers, configuration = _environment(profile['python_executable'])
    installed = _installed(site, cancel=cancel)
    selected = _closure(installed, markers)
    packages, counter = [], [0, 0]
    for name in selected:
        distribution, metadata_bytes = installed[name]
        rows = _distribution_files(distribution, root, cancel=cancel, progress=progress, counter=counter)
        if read_bytes(distribution._path / 'METADATA', maximum=1024**2) != metadata_bytes:
            raise ValueError('Dependency metadata changed while hashing installed files.')
        packages.append({'name': name, 'version': distribution.version, 'sha256': digest(rows)})
    validate_packages(packages)
    model_identity(profile['model_files'])
    visible = _visible_inventory(site, cancel=cancel)
    if file_identity(root / 'pyvenv.cfg') != configuration:
        raise ValueError('Virtual environment configuration changed during inspection.')
    current_implementation, current_descriptor = worker_descriptor(profile['worker_path'],
        profile['python_resolved'], cancel=cancel)
    if canonical(current_implementation) != canonical(implementation) or canonical(current_descriptor) != canonical(descriptor):
        raise ValueError('Worker protocol files changed during inspection.')
    result = {key: value for key, value in profile.items() if key != 'fingerprint'}
    result.update(protocol=PROTOCOL, protocol_descriptor=descriptor,
                  verified_packages=packages, verified_implementation=implementation,
                  verified_site_packages=[str(site)], verified_visible_files=visible,
                  verified_markers=markers,
                  runtime_capability='complete-installed-files-reviewed; execution-and-quality-unqualified')
    result['fingerprint'] = fingerprint(result)
    return result


def launch_arguments(profile, worker_root, *, nonce):
    """Isolated launch: no cwd/user site, .pth processing or old bytecode."""
    if profile.get('protocol') != PROTOCOL or not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise ValueError('A reviewed verified protocol and fresh launch nonce are required.')
    sites = profile.get('verified_site_packages')
    if type(sites) is not list or len(sites) != 1:
        raise ValueError('The inspected import path is missing.')
    site = local_path(sites[0], exists=True)
    regular(site, directory=True)
    root = local_path(worker_root, exists=True)
    cache = root / ('unwritten-bytecode-' + nonce)
    if os.path.lexists(cache):
        raise ValueError('The isolated bytecode namespace must be fresh.')
    bootstrap = (
        'import json,runpy,sys;'
        'sys.path.extend(json.loads(sys.argv[1]));'
        'worker=sys.argv[2];sys.argv=[worker,*sys.argv[3:]];'
        'runpy.run_path(worker,run_name="__main__")'
    )
    return [profile['python_executable'], '-I', '-S', '-B', '-X', 'utf8', '-X', 'pycache_prefix=' + str(cache),
            '-c', bootstrap, canonical([str(site)]).decode('utf-8'),
            profile['worker_path'], '--job-dir', str(root)]
