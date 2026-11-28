"""Read-only installed component and upstream-notice inventory.

This reads existing distribution metadata and the existing bundle inventory.
There is no licence database, network lookup, entitlement decision or claim of
redistribution clearance. Only an explicitly selected notice is read in full.
"""
from dataclasses import dataclass, field
from email.parser import Parser
from email import policy
import base64
import csv
import hashlib
from importlib import metadata
from io import StringIO
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sysconfig

MAX_COMPONENTS = 1024
MAX_DIRECTORY_ENTRIES = 8192
MAX_METADATA = 256 * 1024
MAX_METADATA_TOTAL = 8 * 1024 * 1024
MAX_RECORD = 4 * 1024 * 1024
MAX_RECORD_ROWS = 50_000
MAX_NOTICES = 256
MAX_NOTICE = 2 * 1024 * 1024
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_TEXT_SUFFIXES = {'.txt', '.md', '.rst', '.json', ''}
_BUNDLE_GROUPS = (
    ('Bundled Python', 'notices/python/'),
    ('Bundled dependency notices', 'notices/wheels/'),
    ('Portable OCR runtime and models', 'ocr/'),
    ('FFmpeg audio runtime', 'audio-runtime/'),
    ('Native C++ runtime', 'native-runtime/'),
    ('Bundled semantic model', 'models/'),
)


@dataclass(frozen=True)
class Stamp:
    relative: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Component:
    identity: str
    name: str
    version: str
    origin: str
    declared_license: str = ''
    root: Path = field(default=Path('.'), repr=False)
    metadata_stamp: Stamp | None = field(default=None, repr=False)
    metadata_directory: str = field(default='', repr=False)
    license_files: tuple[str, ...] = field(default=(), repr=False)
    bundle_stamp: Stamp | None = field(default=None, repr=False)
    bundle_notices: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def label(self):
        return self.name + (' ' + self.version if self.version else '') + ' — ' + self.origin


@dataclass(frozen=True)
class Notice:
    component: str
    title: str
    root: Path = field(repr=False)
    relative: str
    guards: tuple[Stamp, ...] = field(default=(), repr=False)
    algorithm: str | None = field(default=None, repr=False)
    digest: str | None = field(default=None, repr=False)
    expected_size: int | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Catalogue:
    components: tuple[Component, ...]
    warnings: tuple[str, ...]
    version: str
    status: str = 'complete'

    @property
    def about(self):
        return ('Resonance\nSceneria Innovative Technologies\n\n'
            'Application version: ' + self.version + '\n'
            f'{len(self.components)} installed or delivered component entries.\n\n'
            'Choose a component to read its actual upstream notices. Licences and copyrights '
            'remain specific to each component; this viewer does not assess redistribution clearance. '
            'Application entitlement status is a separate command.\n\n'
            'Installed metadata and local bundle hashes describe this installation. '
            'This read-only view does not verify every engine/model or authenticate a publisher.\n'
            + ('\nAvailability notes:\n' + '\n'.join(self.warnings) if self.warnings else ''))


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Component notice reading cancelled.')


def _phase(message):
    try:
        from .progress import current_progress
        observer = current_progress()
        if observer is not None:
            observer.update(phase=message)
    except Exception:
        pass


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or PurePosixPath(value).as_posix() != value or value.startswith('/')
            or len(value.split('/')) > 32 or any(part in ('', '.', '..') for part in value.split('/'))
            or '\\' in value or ':' in value):
        raise ValueError('Unsafe notice path in component metadata.')
    for part in value.split('/'):
        if (part.rstrip(' .') != part or PureWindowsPath(part).is_reserved()
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>"|?*' for char in part)):
            raise ValueError('A component notice path contains a device or filename alias.')
    return value


def _label(value, limit):
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid installed component label.')
    return value


def _local_directory(root):
    root = Path(root).absolute()
    raw = str(root)
    if raw.startswith(('\\\\', '//', '\\??\\')):
        raise ValueError('Component notices require a local installation directory.')
    for part in (*reversed(root.parents), root):
        info = part.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('Component notice directories cannot contain links or reparse points.')
    return root


def _capture(root, relative, limit, cancelled):
    """Capture one regular file, bind descriptor/path identity, and enforce actual bytes."""
    _check(cancelled)
    root, relative = _local_directory(root), _relative(relative)
    path = root.joinpath(*relative.split('/'))
    _local_directory(path.parent)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or getattr(before, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
            or before.st_size > limit):
        raise ValueError('A component notice is not a bounded regular local file.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if not stat.S_ISREG(opened.st_mode) or identity(before) != identity(opened):
            raise ValueError('The selected component notice changed before reading.')
        chunks, total = [], 0
        while True:
            _check(cancelled)
            block = stream.read(min(64 * 1024, limit + 1 - total))
            if not block:
                break
            total += len(block)
            if total > limit:
                raise ValueError('The component notice exceeds its byte budget.')
            chunks.append(block)
        if identity(os.fstat(stream.fileno())) != identity(opened) or identity(path.lstat()) != identity(opened):
            raise ValueError('The selected component notice changed while being read.')
    _check(cancelled)
    _local_directory(path.parent)
    value = b''.join(chunks)
    if len(value) != opened.st_size:
        raise ValueError('The selected component notice changed its byte length.')
    return value, Stamp(relative, len(value), hashlib.sha256(value).hexdigest())


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate component inventory field.')
        result[key] = value
    return result


def _json(data):
    return json.loads(data.decode('utf-8'), object_pairs_hook=_unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite component inventory.')))


def _guards(root, stamps, cancelled):
    for stamp in stamps:
        _, current = _capture(root, stamp.relative, max(stamp.size, 1), cancelled)
        if current != stamp:
            raise ValueError('The component metadata or bundle inventory changed; reopen About.')


def _is_notice(name):
    path = PurePosixPath(name)
    lower = path.name.lower()
    return (path.suffix.lower() in _TEXT_SUFFIXES
            and any(token in lower for token in ('license', 'licence', 'copying', 'copyright', 'notice', 'readme')))


def _installed(roots, cancelled):
    results, warnings, consumed = [], [], 0
    for root in roots:
        _check(cancelled)
        try:
            root = _local_directory(root)
            names = []
            with os.scandir(root) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > MAX_DIRECTORY_ENTRIES:
                        raise ValueError('Installed component discovery exceeds its directory budget.')
                    if entry.name.endswith(('.dist-info', '.egg-info')):
                        names.append(entry.name)
            for name in sorted(names, key=str.casefold):
                _check(cancelled)
                if consumed >= MAX_METADATA_TOTAL:
                    warnings.append('Installed component metadata exceeds its aggregate budget.')
                    return results, warnings
                if len(results) >= MAX_COMPONENTS:
                    raise ValueError('Installed component discovery exceeds its component budget.')
                try:
                    _relative(name)
                    directory = _local_directory(root / name)
                    distribution = metadata.PathDistribution(directory)
                    metadata_name = 'METADATA' if name.endswith('.dist-info') else 'PKG-INFO'
                    relative = name + '/' + metadata_name
                    located = Path(distribution.locate_file(relative))
                    if located != root / relative:
                        raise ValueError('The distribution redirected its metadata path.')
                    data, captured = _capture(root, relative, min(MAX_METADATA, MAX_METADATA_TOTAL - consumed), cancelled)
                    consumed += len(data)
                    if consumed > MAX_METADATA_TOTAL:
                        raise ValueError('Installed component metadata exceeds its aggregate budget.')
                    headers = Parser(policy=policy.compat32).parsestr(data.decode('utf-8'), headersonly=True)
                    if any(len(headers.get_all(key, [])) != 1 for key in ('Name', 'Version')):
                        raise ValueError('Installed component identity metadata is missing or ambiguous.')
                    component_name = _label(str(headers.get('Name', '')), 128)
                    if not _NAME.fullmatch(component_name):
                        raise ValueError('Invalid installed distribution name.')
                    version = _label(str(headers.get('Version', '')), 128)
                    declared = str(headers.get('License-Expression') or headers.get('License') or '')
                    # Older wheels may carry the full mixed licence here.
                    # METADATA's byte cap already bounds it; retain the text.
                    files = tuple(str(value) for value in headers.get_all('License-File', []))
                    if len(files) > MAX_NOTICES:
                        raise ValueError('Too many declared component licence files.')
                    identity = 'installed:' + hashlib.sha256(str(directory).encode('utf-8')).hexdigest()
                    results.append(Component(identity, component_name, version, 'installed distribution',
                        declared, root, captured, name, files))
                except InterruptedError:
                    raise
                except (OSError, UnicodeError, ValueError) as exc:
                    warnings.append('A distribution notice entry is unavailable: ' + type(exc).__name__ + '.')
                    if consumed > MAX_METADATA_TOTAL:
                        raise
        except InterruptedError:
            raise
        except (OSError, ValueError) as exc:
            warnings.append('Installed metadata in one interpreter directory is unavailable: ' + type(exc).__name__ + '.')
    return results, warnings


def _bundle(root, cancelled):
    from .native_checks import _manifest
    root = _local_directory(root)
    _check(cancelled)
    validated = _manifest(root)  # Reuse the current complete manifest schema/path checks.
    raw, stamp = _capture(root, 'bundle-manifest.json', 8 * 1024 * 1024, cancelled)
    value = _json(raw)
    if value != validated:
        raise ValueError('The installation inventory changed during discovery.')
    results = []
    for label, prefix in _BUNDLE_GROUPS:
        entries = tuple((name, digest) for name, digest in value['files'].items()
                        if name.startswith(prefix) and _is_notice(name))
        for name, digest in entries:
            _relative(name)
            if not _HASH.fullmatch(digest):
                raise ValueError('Invalid notice hash in installation inventory.')
        if len(entries) > MAX_NOTICES:
            raise ValueError('The delivered notice inventory exceeds its component budget.')
        if entries:
            results.append(Component('bundle:' + prefix, label, '', 'delivered local inventory',
                root=root, bundle_stamp=stamp, bundle_notices=tuple(sorted(entries))))
    launcher = 'native-launcher-build.json'
    if launcher in value['files']:
        results.append(Component('bundle:launcher', 'Native application launcher', '',
            'delivered build provenance; no separate licence inferred', root=root, bundle_stamp=stamp,
            bundle_notices=((launcher, value['files'][launcher]),)))
    return results


_AUTO = object()


def catalogue(*, distribution_roots=None, bundle_root=_AUTO, cancelled=lambda: False):
    _check(cancelled)
    _phase('Reading installed component metadata')
    if distribution_roots is None:
        locations = sysconfig.get_paths()
        distribution_roots = tuple(dict.fromkeys(locations[key] for key in ('purelib', 'platlib')))
    if not isinstance(distribution_roots, (tuple, list)) or len(distribution_roots) > 8:
        raise ValueError('Use a bounded set of interpreter distribution directories.')
    components, warnings = _installed(distribution_roots, cancelled)
    if bundle_root is _AUTO:
        from .native_runtime import _discover
        try:
            bundle_root = _discover()
        except (OSError, ValueError):
            bundle_root = None
            warnings.append('The enclosing delivered runtime inventory is unavailable or ambiguous.')
    if bundle_root is not None:
        try:
            components.extend(_bundle(bundle_root, cancelled))
        except InterruptedError:
            raise
        except (OSError, UnicodeError, ValueError):
            warnings.append('Delivered runtime notices are unavailable: the bundle inventory could not be validated.')
    else:
        warnings.append('No enclosing delivered bundle was found; development/standalone runtime notices may be unavailable.')
    _check(cancelled)
    versions = {item.version for item in components
                if item.name.lower().replace('_', '-') == 'universal-research-library'}
    version = next(iter(versions)) if len(versions) == 1 else 'unavailable in installed metadata (development or ambiguous installation)'
    return Catalogue(tuple(sorted(components, key=lambda value: (value.name.casefold(), value.version, value.origin))),
                     tuple(dict.fromkeys(warnings)), version)


def _record_digest(value):
    if not value:
        return None, None
    if '=' not in value:
        raise ValueError('Invalid installed notice RECORD hash.')
    algorithm, encoded = value.split('=', 1)
    if algorithm not in ('sha256', 'sha384', 'sha512'):
        raise ValueError('The installed notice uses an unsupported RECORD hash.')
    raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
    if len(raw) != hashlib.new(algorithm).digest_size:
        raise ValueError('The installed notice RECORD digest length is invalid.')
    return algorithm, raw.hex()


def notices(component, *, cancelled=lambda: False):
    if type(component) is not Component:
        raise ValueError('Choose a component from the current About catalogue.')
    _check(cancelled)
    _phase('Reading the selected component notice inventory')
    if component.bundle_stamp is not None:
        _guards(component.root, (component.bundle_stamp,), cancelled)
        return tuple(Notice(component.name, name, component.root, name, (component.bundle_stamp,),
                            'sha256', digest) for name, digest in component.bundle_notices)
    if component.metadata_stamp is None:
        return ()
    _guards(component.root, (component.metadata_stamp,), cancelled)
    root, directory = component.root, component.metadata_directory
    declared = []
    for value in component.license_files:
        _relative(value)  # Do not allow a License-File header to leave its metadata directory.
        declared.extend((directory + '/licenses/' + value, directory + '/' + value))
    entries, guards = {}, [component.metadata_stamp]
    try:
        data, record_stamp = _capture(root, directory + '/RECORD', MAX_RECORD, cancelled)
    except FileNotFoundError:
        data = None
    if data is not None:
        guards.append(record_stamp)
        reader = csv.reader(StringIO(data.decode('utf-8')), strict=True)
        for count, row in enumerate(reader, 1):
            _check(cancelled)
            if count > MAX_RECORD_ROWS or len(row) != 3:
                raise ValueError('The selected distribution RECORD exceeds its inventory contract.')
            name, digest, size = row
            if name not in declared and not _is_notice(name):
                continue  # Unrelated script rows may legitimately use ../; never resolve them.
            _relative(name)
            if name in entries:
                raise ValueError('Duplicate installed notice RECORD entry.')
            algorithm, checksum = _record_digest(digest)
            if size and (not size.isascii() or not size.isdecimal()):
                raise ValueError('Invalid installed notice RECORD size.')
            expected_size = int(size) if size else None
            entries[name] = Notice(component.name, name, root, name, tuple(guards),
                                   algorithm, checksum, expected_size)
            if len(entries) > MAX_NOTICES:
                raise ValueError('The selected component has too many notice files.')
    # Older metadata can declare a notice without RECORD. Probe only its two
    # bounded local locations; never recursively search an installation.
    for name in declared:
        if name in entries:
            continue
        path = root.joinpath(*_relative(name).split('/'))
        try:
            _local_directory(path.parent)
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('A declared notice is not a regular file.')
        if data is not None:
            raise ValueError('A declared component notice is missing from its installed RECORD.')
        entries[name] = Notice(component.name, name, root, name, tuple(guards))
    _guards(root, tuple(guards), cancelled)
    return tuple(entries[name] for name in sorted(entries, key=str.casefold))


def read_notice(notice, *, cancelled=lambda: False):
    if type(notice) is not Notice:
        raise ValueError('Choose an actual notice from the selected component inventory.')
    _check(cancelled)
    _phase('Reading the selected upstream notice')
    _guards(notice.root, notice.guards, cancelled)
    data, captured = _capture(notice.root, notice.relative, MAX_NOTICE, cancelled)
    if notice.expected_size is not None and notice.expected_size != len(data):
        raise ValueError('The installed notice differs from its recorded size.')
    if notice.algorithm is not None and hashlib.new(notice.algorithm, data).hexdigest() != notice.digest:
        raise ValueError('The upstream notice differs from its recorded hash.')
    _guards(notice.root, notice.guards, cancelled)
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16')
    else:
        text = data.decode('utf-8-sig')
    if any(ord(char) < 32 and char not in '\t\r\n' for char in text):
        raise ValueError('This retained notice is not a supported plain-text document.')
    _check(cancelled)
    evidence = ('Recorded file hash matched.' if notice.algorithm is not None else
                'This installed notice has no recorded file hash; the bounded local bytes are shown.')
    return (notice.component + '\n' + notice.relative + '\n' + evidence
            + '\nSHA-256: ' + captured.sha256 + '\n\n' + text)
