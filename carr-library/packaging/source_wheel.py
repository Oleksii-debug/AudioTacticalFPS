"""Verify a pure-Python application wheel against an exact local Git commit.

This supports the project's explicit setuptools package list, package-dir and
package-data recipe. It reads committed blobs, never imports application code or
executes a build configuration. Generated metadata is checked separately from
runtime payload identity; this is not a claim of reproducible build timestamps.
"""
import base64
import configparser
import csv
from email.parser import BytesParser
import fnmatch
import hashlib
import io
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import subprocess
import tomllib
import unicodedata
from zipfile import BadZipFile, ZipFile

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version


PROJECT = 'carr-library'
MAX_WHEEL = 64 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
STANDARD_METADATA = {'METADATA', 'WHEEL', 'RECORD', 'entry_points.txt', 'top_level.txt'}


def _git(repository, *arguments):
    environment = dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
    try:
        result = subprocess.run(['git', '--no-replace-objects', '-C', str(repository),
                                 *arguments], capture_output=True, check=True,
                                timeout=20, env=environment)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Required source commit or Git blob is not available locally.') from exc
    return result.stdout


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or value.startswith('/') or ':' in value
            or PurePosixPath(value).as_posix() != value
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Noncanonical source or wheel path.')
    return value


def _wheel_path(name):
    _relative(name)
    for part in name.split('/'):
        if (part.rstrip(' .') != part or PureWindowsPath(part).is_reserved()
                or any(char in '<>:"\\|?*' or ord(char) < 32 or 127 <= ord(char) < 160
                       for char in part)):
            raise ValueError('Wheel path has a Windows alias or reserved character.')
    if '__pycache__' in name.split('/') or name.lower().endswith(('.pyc', '.pyo')):
        raise ValueError('Wheel must not contain bytecode.')
    return unicodedata.normalize('NFC', name).casefold()


def _pattern(path, pattern):
    # Use standard fnmatch for each path component. Do not treat '*' as a
    # recursive glob or silently implement setuptools package discovery.
    _relative(pattern)
    if '**' in pattern.split('/'):
        raise ValueError('Recursive package-data patterns need an explicit inventory.')
    parts, patterns = path.split('/'), pattern.split('/')
    return len(parts) == len(patterns) and all(
        fnmatch.fnmatchcase(part, rule) for part, rule in zip(parts, patterns))


def _tree(repository, source_commit):
    entries = {}
    raw = _git(repository, 'ls-tree', '-rz', '--full-tree', source_commit, '--', PROJECT)
    for row in raw.split(b'\0'):
        if not row:
            continue
        header, path = row.split(b'\t', 1)
        mode, kind, identity = header.decode('ascii').split()
        name = path.decode('utf-8')
        entries[name] = (mode, kind, identity)
    return entries


def _blob(repository, entries, name):
    row = entries.get(name)
    if row is None or row[0] not in ('100644', '100755') or row[1] != 'blob':
        raise ValueError('Required regular source file is absent: ' + name)
    return _git(repository, 'cat-file', 'blob', row[2])


def _inventory(project, entries):
    setuptools = project.get('tool', {}).get('setuptools', {})
    packages = setuptools.get('packages')
    if (not isinstance(packages, list) or not packages or len(set(packages)) != len(packages)
            or any(not isinstance(package, str) or not all(
                part.isidentifier() for part in package.split('.')) for package in packages)):
        raise ValueError('An explicit setuptools package list is required.')
    if setuptools.get('py-modules') or setuptools.get('exclude-package-data'):
        raise ValueError('Unsupported source inventory recipe; use explicit packages and package-data.')
    directories = setuptools.get('package-dir', {})
    data = setuptools.get('package-data', {})
    if not isinstance(directories, dict) or not isinstance(data, dict):
        raise ValueError('Invalid setuptools package mapping.')
    admitted, required = {}, set()
    for package in packages:
        parents = [key for key in directories
                   if key == '' or key == package or package.startswith(key + '.')]
        key = max(parents, key=len) if parents else ''
        base = directories.get(key, '')
        if base:
            _relative(base)
        tail = package[len(key):].lstrip('.').replace('.', '/')
        directory = '/'.join(part for part in (base, tail) if part)
        _relative(directory)
        prefix = PROJECT + '/' + directory + '/'
        patterns = data.get('*', []) + data.get(package, [])
        if not isinstance(patterns, list) or any(not isinstance(pattern, str) for pattern in patterns):
            raise ValueError('Invalid setuptools package-data declaration.')
        for source in entries:
            if not source.startswith(prefix):
                continue
            relative = source[len(prefix):]
            runtime = '/' not in relative and relative.endswith(('.py', '.sql'))
            is_data = any(_pattern(relative, pattern) for pattern in patterns)
            if not runtime and not is_data:
                continue
            if relative.endswith(('.pyc', '.pyo')) or '__pycache__' in relative.split('/'):
                continue
            # Python modules are admitted only through their own declared
            # package, never via a broad package-data glob.
            if relative.endswith('.py') and not runtime:
                continue
            destination = package.replace('.', '/') + '/' + relative
            if destination in admitted and admitted[destination] != source:
                raise ValueError('Ambiguous setuptools package destination.')
            admitted[destination] = source
            if runtime or relative.endswith('.sql'):
                required.add(destination)
    # New product modules must not disappear from both sides of verification
    # merely because their package was omitted from the build recipe. Probes
    # and tests outside the application source root remain intentionally optional.
    application_root = PROJECT + '/src/carr_library/'
    mapped_sources = set(admitted.values())
    omitted = sorted(source for source in entries
                     if source.startswith(application_root)
                     and source.endswith(('.py', '.sql'))
                     and source not in mapped_sources)
    if omitted:
        raise ValueError('Committed application runtime files are outside the declared package inventory: '
                         + ', '.join(omitted))
    if not required:
        raise ValueError('Committed packages contain no required runtime source files.')
    return admitted, required


def _record(files, record):
    rows = {}
    try:
        for row in csv.reader(io.StringIO(files[record].decode('utf-8')), strict=True):
            if len(row) != 3 or row[0] in rows:
                raise ValueError('Invalid or duplicate wheel RECORD row.')
            rows[row[0]] = row[1:]
    except (UnicodeError, csv.Error) as exc:
        raise ValueError('Invalid wheel RECORD.') from exc
    if set(rows) != set(files):
        raise ValueError('Wheel RECORD must list every file exactly once.')
    if rows[record] != ['', '']:
        raise ValueError('Wheel RECORD must leave its own hash and size empty.')
    for name, (digest, size) in rows.items():
        if name == record:
            continue
        algorithm, separator, encoded = digest.partition('=')
        if separator != '=' or algorithm not in ('sha256', 'sha384', 'sha512'):
            raise ValueError('Wheel RECORD needs a strong content digest.')
        actual = base64.urlsafe_b64encode(hashlib.new(algorithm, files[name]).digest()).decode().rstrip('=')
        if encoded != actual or size != str(len(files[name])):
            raise ValueError('Wheel RECORD hash or size mismatch: ' + name)


def _entry_points(files, metadata_prefix, project):
    expected = dict(project.get('entry-points', {}))
    for key, group in [('scripts', 'console_scripts'), ('gui-scripts', 'gui_scripts')]:
        if project.get(key):
            expected[group] = project[key]
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        raw = files.get(metadata_prefix + 'entry_points.txt', b'')
        parser.read_string(raw.decode('utf-8'))
    except (UnicodeError, configparser.Error) as exc:
        raise ValueError('Invalid wheel entry points.') from exc
    actual = {section: dict(parser.items(section)) for section in parser.sections()}
    if parser.defaults() or actual != expected:
        raise ValueError('Wheel entry points differ from committed pyproject.toml.')


def verify_application_wheel(wheel, repository, source_commit):
    """Return exact wheel/source identity evidence, or raise ``ValueError``.

    ``repository`` is a local Git checkout containing carr-library. The returned
    wheel_sha256 covers the same bytes parsed here; callers must use that digest
    to verify later copies instead of blessing a newly read replacement wheel.
    """
    if not isinstance(source_commit, str) or not re.fullmatch('[0-9a-f]{40}', source_commit):
        raise ValueError('Source commit must be a full lowercase Git commit SHA.')
    repository, wheel = Path(repository), Path(wheel)
    resolved = _git(repository, 'rev-parse', '--verify', source_commit + '^{commit}').decode('ascii').strip()
    if resolved != source_commit:
        raise ValueError('Source identity must name the exact commit, not a tag object.')
    entries = _tree(repository, source_commit)
    project = tomllib.loads(_blob(repository, entries, PROJECT + '/pyproject.toml').decode('utf-8'))
    metadata = project.get('project', {})
    if not isinstance(metadata.get('name'), str) or not isinstance(metadata.get('version'), str):
        raise ValueError('Committed project name and version must be static strings.')
    name, version, _build, _tags = parse_wheel_filename(wheel.name)
    if name != canonicalize_name(metadata['name']) or version != Version(metadata['version']):
        raise ValueError('Wheel name or version differs from committed pyproject.toml.')
    admitted, required = _inventory(project, entries)

    with wheel.open('rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Application wheel must be a regular file.')
        wheel_bytes = stream.read(MAX_WHEEL + 1)
    if len(wheel_bytes) > MAX_WHEEL:
        raise ValueError('Application wheel exceeds the verification size limit.')
    files, aliases = {}, set()
    try:
        with ZipFile(io.BytesIO(wheel_bytes)) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(info.file_size for info in infos) > MAX_UNPACKED:
                raise ValueError('Application wheel exceeds the unpacked size limit.')
            for info in infos:
                name_in_wheel = info.filename.rstrip('/') if info.is_dir() else info.filename
                if info.orig_filename != info.filename:
                    raise ValueError('Wheel path contains an embedded alias.')
                alias = _wheel_path(name_in_wheel)
                if alias in aliases:
                    raise ValueError('Duplicate or aliased wheel path.')
                aliases.add(alias)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir():
                    if mode not in (0, stat.S_IFDIR) or info.file_size:
                        raise ValueError('Invalid wheel directory entry.')
                    continue
                if mode not in (0, stat.S_IFREG) or info.flag_bits & 1:
                    raise ValueError('Wheel entries must be regular, unencrypted files.')
                files[info.filename] = archive.read(info)
    except (BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError('Invalid application wheel archive.') from exc

    metadata_prefix = f'{str(name).replace("-", "_")}-{version}.dist-info/'
    for needed in ('METADATA', 'WHEEL', 'RECORD'):
        if metadata_prefix + needed not in files:
            raise ValueError('Application wheel is missing required metadata: ' + needed)
    _record(files, metadata_prefix + 'RECORD')
    parsed = BytesParser().parsebytes(files[metadata_prefix + 'METADATA'])
    if (len(parsed.get_all('Name', [])) != 1 or len(parsed.get_all('Version', [])) != 1
            or canonicalize_name(parsed['Name']) != canonicalize_name(metadata['name'])
            or Version(parsed['Version']) != Version(metadata['version'])):
        raise ValueError('Wheel METADATA name or version differs from committed source.')
    _entry_points(files, metadata_prefix, metadata)
    missing = required - files.keys()
    if missing:
        raise ValueError('Wheel is missing committed runtime files: ' + ', '.join(sorted(missing)))
    missing_data = admitted.keys() - files.keys()
    if missing_data:
        raise ValueError('Wheel is missing committed package files: ' + ', '.join(sorted(missing_data)))
    license_files = set()
    for pattern in metadata.get('license-files', []):
        matches = {path[len(PROJECT) + 1:] for path in entries
                   if _pattern(path[len(PROJECT) + 1:], pattern)}
        if not matches:
            raise ValueError('Declared source license pattern has no committed files: ' + pattern)
        license_files.update(matches)
    missing_licenses = {metadata_prefix + 'licenses/' + path for path in license_files} - files.keys()
    if missing_licenses:
        raise ValueError('Wheel is missing declared license files: ' + ', '.join(sorted(missing_licenses)))

    payload_count = 0
    for path, content in files.items():
        if path in admitted:
            source = admitted[path]
            payload_count += 1
        elif path.startswith(metadata_prefix):
            relative = path[len(metadata_prefix):]
            if relative in STANDARD_METADATA:
                continue
            if not relative.startswith('licenses/'):
                raise ValueError('Undeclared wheel metadata payload: ' + path)
            license_file = relative[len('licenses/'):]
            if license_file not in license_files:
                raise ValueError('Undeclared wheel license payload: ' + path)
            source = PROJECT + '/' + license_file
        else:
            raise ValueError('Undeclared application wheel payload: ' + path)
        if content != _blob(repository, entries, source):
            raise ValueError('Wheel payload differs from committed source: ' + path)

    return {'source_commit': source_commit, 'wheel_sha256': hashlib.sha256(wheel_bytes).hexdigest(),
            'project_name': metadata['name'], 'project_version': metadata['version'],
            'payload_files': payload_count, 'required_runtime_files': len(required)}
