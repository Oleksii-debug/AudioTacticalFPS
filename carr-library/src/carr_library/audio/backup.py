"""Streaming, verified local Audio Studio snapshots and separate-folder restore.

This covers a saved studio project, its recovery draft and known production
assets/checkpoints/results. It is not a library database or installed-model
backup. API profiles, credential references, executable helpers, quarantine
and arbitrary neighboring files are never collected.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

from ..reader.narration_text import NarrationDraft, decode_json
from ..research.export_file import _filename
from .jobs import check_cancelled, project_lock


DEFAULT_BYTE_BUDGET = 1024 ** 4
DEFAULT_FILE_BUDGET = 1000000
MANIFEST_BUDGET = 64 * 1024 ** 2
BLOCK = 1024 * 1024
_HEX = r'[0-9a-f]{64}'
_STUDIO_KEYS = {'kind', 'version', 'mode', 'project_id', 'title', 'voice', 'language',
                'output_dir', 'source_reference', 'draft'}


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _budget(byte_budget, file_budget):
    if type(byte_budget) is not int or byte_budget <= 0 or type(file_budget) is not int or file_budget <= 0:
        raise ValueError('Snapshot byte and file budgets must be positive integers.')


def _local(path):
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or raw.startswith(('\\\\', '//')) or '://' in raw:
        raise ValueError('Choose a local snapshot path.')
    result = Path(raw).absolute()
    for component in result.parts[1:]:
        _filename(component)
    return result


def _safe(path, *, directory=False):
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Snapshot paths cannot contain links or Windows reparse points.')
        if parent != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Snapshot parents must be regular directories.')
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Choose a regular snapshot file or directory.')
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _allowed(name):
    if name in ('studio/project.json', 'studio/project.json.recovery.json'):
        return True
    if name == 'audio/render-journal.json' or re.fullmatch('audio/plan-' + _HEX + r'\.json', name):
        return True
    if re.fullmatch(r'audio/assets/[^/]+\.wav', name):
        return True
    if re.fullmatch('audio/cache/' + _HEX + r'\.(wav|json|mid|music\.json)', name):
        return True
    if re.fullmatch('audio/renders/' + _HEX + r'/[^/]+\.(wav|mp3|flac|ogg|opus|aac|m4a|m4b|aiff|wma|srt|vtt|json|ffmeta|mid)', name):
        return True
    return False


def _member(name):
    if (not isinstance(name, str) or not name or '\\' in name or name.startswith('/')
            or str(PurePosixPath(name)) != name or not _allowed(name)):
        raise ValueError('Unexpected or unsafe snapshot member.')
    for part in name.split('/'):
        _filename(part)
    return name


def _studio(payload):
    if (type(payload) is not dict or set(payload) != _STUDIO_KEYS
            or payload['kind'] != 'research-audio-studio' or type(payload['version']) is not int
            or payload['version'] != 1 or payload['mode'] != 'LOCAL ONLY'
            or not isinstance(payload['project_id'], str)
            or not re.fullmatch('[0-9a-f]{32}', payload['project_id'])):
        raise ValueError('A saved local Audio Studio project is required for this snapshot.')
    for name in ('title', 'voice', 'language'):
        if not isinstance(payload[name], str) or not payload[name].strip():
            raise ValueError('The saved Audio Studio settings are invalid.')
    NarrationDraft.from_dict(payload['draft'])
    reference = payload['source_reference']
    if reference is not None:
        if type(reference) is not dict or reference.get('kind') not in ('local-text', 'library-source-reading-copy'):
            raise ValueError('Unknown source-reference settings are not exported.')
        allowed = ({'kind', 'filename', 'original_path', 'bytes_sha256'} if reference['kind'] == 'local-text'
                   else {'kind', 'workspace', 'edition', 'revision', 'pages', 'separator'})
        if set(reference) - allowed:
            raise ValueError('Unknown source-reference fields are not exported.')
    return payload


def _read_json(path, *, budget=MANIFEST_BUDGET):
    if _safe(path).st_size > budget:
        raise ValueError('Project metadata exceeds the configured metadata budget.')
    with path.open('rb') as stream:
        raw = stream.read(budget + 1)
    if len(raw) > budget:
        raise ValueError('Project metadata exceeds the configured metadata budget.')
    return decode_json(raw.decode('utf-8'))


def _inventory(project_file, project_dir, *, include_cache, include_audio):
    files = {'studio/project.json': project_file}
    recovery = project_file.with_name(project_file.name + '.recovery.json')
    if recovery.exists() or recovery.is_symlink():
        data = _read_json(recovery)
        if (type(data) is not dict or set(data) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                or data['kind'] != 'audio-studio-recovery' or type(data['version']) is not int
                or data['version'] != 1 or data['project_id'] != _read_json(project_file)['project_id']):
            raise ValueError('The recovery draft is invalid or belongs to another project.')
        _studio(data['project'])
        files['studio/project.json.recovery.json'] = recovery
    if project_dir is None:
        return files
    for base, directories, names in os.walk(project_dir, followlinks=False):
        directory = Path(base)
        relative = directory.relative_to(project_dir)
        if relative == Path('.'):
            permitted = {'assets'} | ({'cache'} if include_cache else set()) | ({'renders'} if include_audio else set())
            directories[:] = [name for name in directories if name in permitted]
        elif relative.parts[0] == 'cache':
            directories[:] = []  # Corrupt/quarantined chunks are never resumed.
        elif relative.parts[0] == 'assets':
            directories[:] = []
        elif relative.parts[0] == 'renders' and len(relative.parts) == 1:
            directories[:] = [name for name in directories if re.fullmatch(_HEX, name)]
        else:
            directories[:] = []
        for child in directories:
            _safe(directory / child, directory=True)
        for name in sorted(names):
            path = directory / name
            member = 'audio/' + path.relative_to(project_dir).as_posix()
            if _allowed(member):
                _member(member)
                _safe(path)
                files[member] = path
    return files


@contextmanager
def _snapshot_lock(project_dir):
    if project_dir is None:
        yield
    else:
        with project_lock(project_dir):
            yield


def backup_project(project_file, destination, *, project_dir=None, include_cache=True,
                   include_audio=True, byte_budget=DEFAULT_BYTE_BUDGET,
                   file_budget=DEFAULT_FILE_BUDGET, cancelled=lambda: False,
                   on_status=lambda message: None):
    """Publish a new ZIP64 snapshot of an idle project; never overwrite a file."""
    _budget(byte_budget, file_budget)
    if type(include_cache) is not bool or type(include_audio) is not bool:
        raise ValueError('Choose whether to include cached chunks and rendered outputs.')
    source, target = _local(project_file), _local(destination)
    _safe(source)
    _safe(target.parent, directory=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Choose a new snapshot filename.')
    root = _local(project_dir) if project_dir is not None else None
    if root is not None:
        _safe(root, directory=True)
        if target.is_relative_to(root):
            raise ValueError('Save the snapshot outside its audio source folder.')
    if target == source:
        raise ValueError('The snapshot cannot replace its project source.')
    with _snapshot_lock(root):
        check_cancelled(cancelled)
        project = _studio(_read_json(source))
        paths = _inventory(source, root, include_cache=include_cache, include_audio=include_audio)
        if len(paths) > file_budget:
            raise ValueError('The project exceeds the configured snapshot file budget.')
        before = {name: _identity(_safe(path)) for name, path in paths.items()}
        total = sum(info[2] for info in before.values())
        if total > byte_budget:
            raise ValueError('The project exceeds the configured snapshot byte budget.')
        # ZIP_STORED keeps memory bounded and avoids spending hours compressing
        # already compressed audio. Its space requirement is predictable.
        if shutil.disk_usage(target.parent).free < total + len(paths) * 1024 + 1024 * 1024:
            raise OSError('There is not enough free space for this project snapshot.')
        parent_identity = _identity(_safe(target.parent, directory=True))[:2]
        fd, temporary = tempfile.mkstemp(prefix='.audio-snapshot-', dir=target.parent)
        os.close(fd)
        stage = Path(temporary)
        records = []
        try:
            with zipfile.ZipFile(stage, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for number, (name, path) in enumerate(sorted(paths.items()), 1):
                    check_cancelled(cancelled)
                    checksum, size = hashlib.sha256(), 0
                    with path.open('rb') as original, archive.open(name, 'w', force_zip64=True) as output:
                        if _identity(os.fstat(original.fileno())) != before[name]:
                            raise ValueError('A source file changed before the snapshot was read.')
                        while block := original.read(BLOCK):
                            check_cancelled(cancelled)
                            size += len(block)
                            if size > before[name][2]:
                                raise ValueError('A source file grew while the snapshot was being made.')
                            checksum.update(block)
                            output.write(block)
                    if size != before[name][2] or _identity(_safe(path)) != before[name]:
                        raise ValueError('A source file changed while the snapshot was being made.')
                    records.append({'path': name, 'size': size, 'sha256': checksum.hexdigest()})
                    on_status(f'Snapshot: saved {number} of {len(paths)} files.')
                current = _inventory(source, root, include_cache=include_cache, include_audio=include_audio)
                if set(current) != set(paths) or any(_identity(_safe(current[name])) != before[name] for name in paths):
                    raise ValueError('The project changed during its snapshot. Try again while it is idle.')
                manifest = {'kind': 'audio-studio-snapshot', 'version': 1, 'project_id': project['project_id'],
                            'files': records, 'total_bytes': total, 'include_cache': include_cache,
                            'include_audio': include_audio,
                            'limitations': ['Audio Studio project only; not a full research-library backup.',
                                'API profiles, credentials, executable helpers and installed voice/music models are excluded.',
                                'Only completed/cache files are copied. Interrupted working scratch files are excluded.']}
                raw_manifest = _json(manifest)
                if len(raw_manifest) > MANIFEST_BUDGET:
                    raise ValueError('Snapshot manifest exceeds its metadata budget.')
                archive.writestr('manifest.json', raw_manifest)
            check_cancelled(cancelled)
            if _identity(_safe(target.parent, directory=True))[:2] != parent_identity:
                raise ValueError('The snapshot destination folder changed.')
            with stage.open('rb') as stream:
                os.fsync(stream.fileno())
            os.link(stage, target)
            return {'snapshot': str(target), 'files': len(records), 'bytes': total,
                    'project_id': project['project_id'], 'limitations': manifest['limitations']}
        finally:
            stage.unlink(missing_ok=True)


def _archive_manifest(archive, *, byte_budget, file_budget):
    infos = archive.infolist()
    if len(infos) > file_budget + 1:
        raise ValueError('The snapshot exceeds the configured file budget.')
    names = [info.filename for info in infos]
    if len(names) != len(set(name.casefold() for name in names)) or names.count('manifest.json') != 1:
        raise ValueError('The snapshot contains duplicate or ambiguous paths.')
    for info in infos:
        mode = (info.external_attr >> 16) & 0xFFFF
        if (info.is_dir() or info.flag_bits & 1 or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise ValueError('Linked, encrypted or unsupported snapshot entries are not accepted.')
    manifest_info = archive.getinfo('manifest.json')
    if manifest_info.file_size > MANIFEST_BUDGET:
        raise ValueError('Snapshot manifest exceeds its metadata budget.')
    raw = archive.read(manifest_info)
    value = decode_json(raw.decode('utf-8'))
    keys = {'kind', 'version', 'project_id', 'files', 'total_bytes', 'include_cache', 'include_audio', 'limitations'}
    if (type(value) is not dict or set(value) != keys or value['kind'] != 'audio-studio-snapshot'
            or type(value['version']) is not int or value['version'] != 1 or type(value['files']) is not list
            or type(value['total_bytes']) is not int or value['total_bytes'] < 0
            or type(value['include_cache']) is not bool or type(value['include_audio']) is not bool
            or len(value['files']) > file_budget):
        raise ValueError('Unsupported Audio Studio snapshot manifest.')
    expected, folded, total = {}, set(), 0
    for record in value['files']:
        if (type(record) is not dict or set(record) != {'path', 'size', 'sha256'}
                or type(record['size']) is not int or record['size'] < 0
                or not isinstance(record['sha256'], str) or not re.fullmatch(_HEX, record['sha256'])):
            raise ValueError('Invalid snapshot file record.')
        name = _member(record['path'])
        if name.casefold() in folded:
            raise ValueError('Duplicate snapshot file record.')
        folded.add(name.casefold())
        expected[name] = record
        total += record['size']
    if (set(names) != set(expected) | {'manifest.json'} or 'studio/project.json' not in expected
            or total != value['total_bytes'] or total > byte_budget):
        raise ValueError('Snapshot contents, byte budget or manifest do not agree.')
    for name, record in expected.items():
        if archive.getinfo(name).file_size != record['size']:
            raise ValueError('Snapshot member size does not match its manifest.')
    return value, expected


def restore_project(snapshot, destination, *, byte_budget=DEFAULT_BYTE_BUDGET,
                    file_budget=DEFAULT_FILE_BUDGET, cancelled=lambda: False,
                    on_status=lambda message: None):
    """Verify every byte, then restore into a newly claimed destination folder.

    The destination contains one ``project`` directory published after complete
    verification. The saved output path and compatible recovery baseline are
    relocated; original hashes remain in ``restore-manifest.json``.
    """
    _budget(byte_budget, file_budget)
    source, target = _local(snapshot), _local(destination)
    _safe(source)
    _safe(target.parent, directory=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Restore requires a new destination folder.')
    if source.is_relative_to(target):
        raise ValueError('The restore destination cannot contain its source snapshot.')
    parent_id = _identity(_safe(target.parent, directory=True))[:2]
    check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(prefix='.audio-restore-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'project'
        stage.mkdir(mode=0o700)
        with zipfile.ZipFile(source, 'r') as archive:
            manifest, records = _archive_manifest(archive, byte_budget=byte_budget, file_budget=file_budget)
            if shutil.disk_usage(target.parent).free < manifest['total_bytes'] + 1024 * 1024:
                raise OSError('There is not enough free space to restore the project.')
            for number, (name, record) in enumerate(sorted(records.items()), 1):
                check_cancelled(cancelled)
                path = stage.joinpath(*name.split('/'))
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                checksum, size = hashlib.sha256(), 0
                with archive.open(name) as incoming, path.open('xb') as output:
                    while block := incoming.read(BLOCK):
                        check_cancelled(cancelled)
                        size += len(block)
                        if size > record['size']:
                            raise ValueError('A snapshot member exceeded its declared size.')
                        checksum.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if size != record['size'] or checksum.hexdigest() != record['sha256']:
                    raise ValueError('A snapshot file failed its SHA256 integrity check.')
                on_status(f'Restore: verified {number} of {len(records)} files.')
        project_path = stage / 'studio' / 'project.json'
        project = _studio(_read_json(project_path))
        if project['project_id'] != manifest['project_id']:
            raise ValueError('The snapshot project identity does not match its manifest.')
        output_root = target / 'project' / 'audio'
        (stage / 'audio').mkdir(exist_ok=True)
        old_hash = records['studio/project.json']['sha256']
        project['output_dir'] = str(output_root)
        relocated = _json(project)
        project_path.write_bytes(relocated)
        recovery = stage / 'studio' / 'project.json.recovery.json'
        recovery_relocated = False
        if recovery.exists():
            value = _read_json(recovery)
            if (type(value) is not dict or set(value) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                    or value['kind'] != 'audio-studio-recovery' or type(value['version']) is not int
                    or value['version'] != 1 or value['project_id'] != project['project_id']):
                raise ValueError('The snapshot recovery draft is invalid.')
            _studio(value['project'])
            if value['project']['project_id'] != project['project_id']:
                raise ValueError('The recovery draft belongs to another project.')
            if value['base_sha256'] == old_hash:
                value['base_sha256'] = hashlib.sha256(relocated).hexdigest()
                value['project']['output_dir'] = str(output_root)
                recovery.write_bytes(_json(value))
                recovery_relocated = True
        restored_manifest = {'kind': 'audio-studio-restored-snapshot', 'version': 1,
                             'source_manifest': manifest, 'output_folder_relocated': True,
                             'recovery_relocated': recovery_relocated,
                             'project_sha256': hashlib.sha256(relocated).hexdigest()}
        (stage / 'restore-manifest.json').write_bytes(_json(restored_manifest))
        for changed in (project_path, recovery if recovery.exists() else project_path, stage / 'restore-manifest.json'):
            with changed.open('rb') as stream:
                os.fsync(stream.fileno())
        check_cancelled(cancelled)
        if _identity(_safe(target.parent, directory=True))[:2] != parent_id:
            raise ValueError('The restore destination parent changed.')
        # Claim the new parent without overwriting even an empty pre-existing
        # directory, then atomically move the verified private staging tree.
        target.mkdir(mode=0o700)
        try:
            stage.rename(target / 'project')
        except BaseException:
            target.rmdir()
            raise
    return {'directory': str(target / 'project'), 'project_file': str(target / 'project' / 'studio' / 'project.json'),
            'project_dir': str(output_root), 'files': len(records), 'bytes': manifest['total_bytes'],
            'recovery_relocated': recovery_relocated, 'limitations': manifest['limitations']}
