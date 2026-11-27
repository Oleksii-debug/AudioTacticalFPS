"""Explicit installed Calibre readers inside the existing owned ingest worker.

Only the eight listed, unprotected document families are admitted. This adapter
does not install a runtime or load the user's Calibre library/configuration.
The parent service holds its selected core readers through _invoke's complete
native-family cleanup. Native dependencies remain part of the selected local
installation, not a claim of a separately sealed or sandboxed Calibre bundle.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryFile
import time

from ..audio.jobs import checked_directory, file_identity, regular_file, verified_reader
from .models import ConversionError, ConversionUnavailable

CALIBRE_SUFFIXES = frozenset(('.azw', '.azw3', '.chm', '.djvu', '.lit', '.mobi', '.prc', '.tcr'))
MAX_COMPONENT_BYTES = 256 * 1024**2
MAX_CORE_BYTES = 768 * 1024**2
MAX_CORE_FILES = 12
MAX_LOG_BYTES = 256 * 1024
PRIVATE_BYTES = 64 * 1024**2
SCHEMA = 'carr-selected-calibre/1'
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Selected book conversion cancelled.')


def _layout(path):
    path = Path(path)
    if not path.is_absolute() or path.name not in ('ebook-convert', 'ebook-convert.exe'):
        raise ConversionUnavailable('Choose the installed ebook-convert executable by absolute local path.')
    root = checked_directory(path.parent)
    if path.name.endswith('.exe'):
        names = {path.name, 'app/bin/calibre-launcher.dll', 'app/bin/python-lib.bypy.frozen'}
        binaries = checked_directory(root / 'app/bin')
        pattern = re.compile(r'python3[0-9]+\.dll\Z')
        prefix = 'app/bin/'
    else:
        names = {path.name, 'bin/ebook-convert', 'lib/libcalibre-launcher.so',
                 'lib/calibre-extensions/python-lib.bypy.frozen'}
        binaries = checked_directory(root / 'lib')
        pattern = re.compile(r'libpython3\.[0-9]+\.so(?:\.[0-9]+)*\Z')
        prefix = 'lib/'
    found = []
    with os.scandir(binaries) as entries:
        for index, entry in enumerate(entries, 1):
            if index > 4096:
                raise ConversionUnavailable('The selected Calibre core directory exceeds its entry budget.')
            if pattern.fullmatch(entry.name):
                found.append(prefix + entry.name)
    if not found or len(found) > 4:
        raise ConversionUnavailable('Select a complete self-contained Calibre installation with its Python runtime.')
    names.update(found)
    if len(names) > MAX_CORE_FILES:
        raise ConversionUnavailable('The selected Calibre core exceeds its component budget.')
    for name in names:
        _component(root, name)
    return root, tuple(sorted(names))


def _component(root, name):
    path = root / name
    checked_directory(path.parent)
    info = regular_file(path)
    if info.st_nlink != 1 or not 0 < info.st_size <= MAX_COMPONENT_BYTES:
        raise ConversionUnavailable('A selected Calibre core file is linked, empty or too large.')
    return path, info


def _digest(stream, expected_size, cancelled):
    digest, count = sha256(), 0
    while block := stream.read(min(1024 * 1024, expected_size - count + 1)):
        _cancel(cancelled)
        count += len(block)
        if count > expected_size:
            raise ConversionError('A selected Calibre core file grew during verification.')
        digest.update(block)
    if count != expected_size:
        raise ConversionError('A selected Calibre core file was truncated.')
    _cancel(cancelled)
    return digest.hexdigest()


@dataclass(frozen=True)
class CalibreSelection:
    """A UI selection captures identity; hashes and execution stay off the UI."""
    path: Path
    root_identity: tuple
    files: tuple

    @classmethod
    def capture(cls, value):
        root, names = _layout(value)
        info = root.stat()
        files = tuple((name, file_identity(_component(root, name)[1])) for name in names)
        if sum(identity[2] for _, identity in files) > MAX_CORE_BYTES:
            raise ConversionUnavailable('The selected Calibre core exceeds its total byte budget.')
        result = cls(root / Path(value).name, (info.st_dev, info.st_ino), files)
        result._unchanged()
        return result

    def _unchanged(self):
        root, names = _layout(self.path)
        info = root.stat()
        if ((info.st_dev, info.st_ino) != self.root_identity
                or names != tuple(name for name, _ in self.files)
                or any(file_identity(_component(root, name)[1]) != identity for name, identity in self.files)):
            raise ConversionError('The selected Calibre installation changed after selection.')
        return root

    def snapshot(self, *, cancelled=lambda: False):
        root = self._unchanged()
        rows = []
        for name, identity in self.files:
            _cancel(cancelled)
            path, info = _component(root, name)
            with verified_reader(path, expected_identity=identity) as stream:
                checksum = _digest(stream, info.st_size, cancelled)
            rows.append(dict(name=name, identity=list(identity), bytes=info.st_size, sha256=checksum))
        self._unchanged()
        _cancel(cancelled)
        return dict(schema=SCHEMA, root=str(root), root_identity=list(self.root_identity),
                    executable=self.path.name, files=rows)

    @contextmanager
    def lease(self, snapshot, *, cancelled=lambda: False):
        """The caller exits this only after the existing outer owner is quiet."""
        _validate_selection(snapshot)
        root = self._unchanged()
        if (snapshot['root'] != str(root) or snapshot['root_identity'] != list(self.root_identity)
                or snapshot['executable'] != self.path.name):
            raise ConversionError('The Calibre snapshot belongs to another selection.')
        with ExitStack() as held:
            for row, (name, identity) in zip(snapshot['files'], self.files, strict=True):
                _cancel(cancelled)
                if row['name'] != name or row['identity'] != list(identity):
                    raise ConversionError('The Calibre snapshot changed after review.')
                stream = held.enter_context(verified_reader(root / name, expected_identity=identity))
                if _digest(stream, row['bytes'], cancelled) != row['sha256']:
                    raise ConversionError('The selected Calibre core checksum changed.')
            self._unchanged()
            _cancel(cancelled)
            yield
            self._unchanged()
            _cancel(cancelled)


def _validate_selection(value):
    fields = {'schema', 'root', 'root_identity', 'executable', 'files'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA
            or type(value['root']) is not str or '\x00' in value['root']
            or type(value['executable']) is not str
            or value['executable'] not in ('ebook-convert', 'ebook-convert.exe')
            or type(value['root_identity']) is not list or len(value['root_identity']) != 2
            or any(type(item) is not int or item < 0 for item in value['root_identity'])
            or type(value['files']) is not list or not 1 <= len(value['files']) <= MAX_CORE_FILES):
        raise ConversionError('Invalid selected Calibre core inventory.')
    root, names = _layout(Path(value['root']) / value['executable'])
    info = root.stat()
    if (str(root) != value['root'] or [info.st_dev, info.st_ino] != value['root_identity']
            or [row.get('name') for row in value['files'] if type(row) is dict] != list(names)):
        raise ConversionError('The Calibre inventory has missing, foreign or displaced core files.')
    total = 0
    for row in value['files']:
        if (type(row) is not dict or set(row) != {'name', 'identity', 'bytes', 'sha256'}
                or type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_COMPONENT_BYTES
                or type(row['sha256']) is not str or _HASH.fullmatch(row['sha256']) is None
                or type(row['identity']) is not list or len(row['identity']) != 5
                or any(type(item) is not int for item in row['identity'])
                or row['identity'][2] != row['bytes']):
            raise ConversionError('Invalid selected Calibre core component.')
        _, current = _component(root, row['name'])
        if list(file_identity(current)) != row['identity']:
            raise ConversionError('A selected Calibre core component changed.')
        total += row['bytes']
    if total > MAX_CORE_BYTES:
        raise ConversionError('Selected Calibre core bytes exceed their budget.')
    return value


def _admit(selection, deadline):
    from .adapters import _remaining
    value = _validate_selection(selection)
    for row in value['files']:
        _remaining(deadline)
        with verified_reader(Path(value['root']) / row['name'],
                             expected_identity=tuple(row['identity'])) as stream:
            def cancelled():
                _remaining(deadline)
                return False
            if _digest(stream, row['bytes'], cancelled) != row['sha256']:
                raise ConversionError('A selected Calibre core checksum changed.')
    _remaining(deadline)
    _validate_selection(value)
    return Path(value['root']) / value['executable']


def _input_header(data, suffix):
    if suffix in ('.mobi', '.azw', '.azw3', '.prc'):
        valid = len(data) >= 78 and data[60:68] in (
            (b'BOOKMOBI', b'TEXtREAd') if suffix == '.prc' else (b'BOOKMOBI',))
    elif suffix == '.chm':
        valid = data.startswith(b'ITSF')
    elif suffix == '.lit':
        valid = data.startswith(b'ITOLITLS')
    elif suffix == '.tcr':
        valid = data.startswith(b'!!8-Bit!!')
    elif suffix == '.djvu':
        valid = data.startswith(b'AT&TFORM') and data[12:16] in (b'DJVU', b'DJVM')
    else:
        valid = False
    if not valid:
        raise ConversionError('The selected book does not have the declared document-family header.')


def _environment(stage, root):
    # Do not inherit user plugins, Python hooks, proxies, key logs, personal
    # Calibre configuration, library paths or converter search-path overrides.
    env = {key: value for key, value in os.environ.items() if key in ('SYSTEMROOT', 'WINDIR')}
    paths = [str(root)]
    if os.name == 'nt':
        paths += [str(root / 'app/bin'), str(Path(env.get('SYSTEMROOT', 'C:/Windows')) / 'System32')]
    else:
        paths += [str(root / 'bin'), os.defpath]
    env.update(PATH=os.pathsep.join(paths), LANG='C.UTF-8', LC_ALL='C.UTF-8',
        HOME=str(stage / 'home'), USERPROFILE=str(stage / 'home'),
        APPDATA=str(stage / 'config'), LOCALAPPDATA=str(stage / 'cache'),
        XDG_CONFIG_HOME=str(stage / 'config'), XDG_CACHE_HOME=str(stage / 'cache'),
        TMPDIR=str(stage / 'temporary'), TEMP=str(stage / 'temporary'), TMP=str(stage / 'temporary'),
        CALIBRE_CONFIG_DIRECTORY=str(stage / 'config'), CALIBRE_CACHE_DIRECTORY=str(stage / 'cache'),
        CALIBRE_TEMP_DIR=str(stage / 'temporary'), CALIBRE_NO_NATIVE_FILEDIALOGS='1',
        CALIBRE_IGNORE_CUSTOM_PLUGINS='1', QT_QPA_PLATFORM='offscreen')
    return env


def _stage_usage(stage, maximum, deadline):
    # Reuse the complete bounded no-follow traversal already used by Office.
    from .office import _stage_usage as check
    try:
        check(stage, maximum, deadline)
    except ConversionError as error:
        raise ConversionError(str(error).replace('LibreOffice', 'Calibre')) from error


def _run(args, stage, deadline, *, environment, output=None, output_limit=0, stage_limit=PRIVATE_BYTES):
    """Private adapter child; inherit the existing ingest session / native Job.

    The parent ConversionService holds the selected core until _invoke stops
    every descendant, including a leader that exits while a helper remains.
    This function deliberately never creates a new POSIX session.
    """
    from .adapters import _remaining
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        _remaining(deadline)
        _stage_usage(stage, stage_limit, deadline)
        process = subprocess.Popen([str(arg) for arg in args], cwd=stage, env=environment,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
            start_new_session=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                _remaining(deadline)
                if any(os.fstat(stream.fileno()).st_size > MAX_LOG_BYTES for stream in (stdout, stderr)):
                    raise ConversionError('Selected Calibre diagnostics exceeded their byte budget.')
                if output is not None and os.path.lexists(output) and regular_file(output).st_size > output_limit:
                    raise ConversionError('Selected Calibre output exceeded its text budget.')
                _stage_usage(stage, stage_limit, deadline)
                if process.poll() is not None:
                    break
                time.sleep(.02)
            if process.returncode:
                raise ConversionUnavailable('Selected Calibre could not convert this unprotected document (exit '
                                            + str(process.returncode) + ').')
            stdout.seek(0)
            return stdout.read(MAX_LOG_BYTES), os.fstat(stderr.fileno()).st_size > 0
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def calibre_records(source, data, suffix, budget, deadline, selection):
    from .adapters import _remaining
    _input_header(data, suffix)
    executable = _admit(selection, deadline)
    private = source.parent / 'calibre-private'
    if os.path.lexists(private):
        raise ConversionError('Private Calibre staging already exists.')
    private.mkdir(mode=0o700)
    for name in ('home', 'config', 'cache', 'temporary'):
        (private / name).mkdir(mode=0o700)
    environment = _environment(private, Path(selection['root']))
    stage_limit = budget.max_source_bytes + budget.max_text_bytes + PRIVATE_BYTES
    version, _ = _run([executable, '--version'], source.parent, deadline,
                     environment=environment, stage_limit=stage_limit)
    version = version.decode('utf-8', 'strict').strip()
    if not re.fullmatch(r'ebook-convert(?:\.exe)? \(calibre [0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?\)', version):
        raise ConversionUnavailable('The selected executable did not identify a compatible bounded Calibre CLI.')
    _admit(selection, deadline)
    output = source.parent / 'calibre-reading.txt'
    if os.path.lexists(output):
        raise ConversionError('Private Calibre output already exists.')
    _, had_warnings = _run([executable, source, output, '--txt-output-encoding=utf-8',
        '--txt-output-formatting=plain', '--newline=unix', '--max-line-length=0'],
        source.parent, deadline, environment=environment, output=output,
        output_limit=budget.max_text_bytes, stage_limit=stage_limit)
    _admit(selection, deadline)
    info = regular_file(output)
    if info.st_nlink != 1 or not 0 < info.st_size <= budget.max_text_bytes:
        raise ConversionError('Selected Calibre produced empty, linked or oversized reading text.')
    with verified_reader(output, expected_identity=file_identity(info)) as stream:
        raw = stream.read(budget.max_text_bytes + 1)
    if len(raw) != info.st_size:
        raise ConversionError('Selected Calibre text changed before verification.')
    text = raw.decode('utf-8', 'strict')
    if not text.strip() or '\x00' in text:
        raise ConversionError('Selected Calibre produced no valid reading text. Scanned DjVu needs separate reviewed OCR.')
    _remaining(deadline)
    identity = sha256(json.dumps(selection, sort_keys=True, ensure_ascii=False,
                                separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    record = dict(raw_text=text, physical_index=None, status='review_required',
        logical_locator=dict(section=1, converter=version, selected_core_sha256=identity,
                             output_sha256=sha256(raw).hexdigest(), output_format='plain_utf8',
                             pagination='not_reconstructed'))
    notices = ['converted_text_requires_source_fidelity_review',
        'converted_layout_and_printed_pages_not_retained', 'private_configuration_without_user_plugins',
        'selected_native_tool_is_not_an_os_sandbox', 'selected_core_is_not_a_complete_system_dependency_seal']
    if suffix == '.djvu':
        notices.append('embedded_djvu_text_only_no_new_ocr')
    if suffix == '.prc':
        notices.append('prc_only_mobipocket_or_palmdoc_header_family')
    if had_warnings:
        notices.append('converter_reported_warnings_review_source_fidelity')
    return 'selected-calibre-' + suffix[1:], '\n\n', [record], notices
