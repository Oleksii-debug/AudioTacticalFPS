"""Explicit LibreOffice conversion inside the existing isolated conversion worker.

No server, listening socket, document-supplied command or persistent user
profile is used. The outer process owner settles the entire native family
before the caller removes this operation's private directory.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryFile
import time

from defusedxml import ElementTree

from ..durable_state import local_path, regular
from ..extract.document_formats import extract_document
from .models import ConversionError, ConversionUnavailable


def checked_directory(value):
    path = Path(value).absolute()
    if any(':' in part for part in path.parts[1:]):
        raise ConversionError('Selected conversion paths cannot contain alternate streams.')
    path = local_path(path, exists=True)
    regular(path, directory=True)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        drive = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        drive.argtypes, drive.restype = (wintypes.LPCWSTR,), wintypes.UINT
        if drive(path.anchor) == 4:
            raise ConversionError('Select a local conversion installation, without mapped network drives.')
    return path


def regular_file(value):
    path = Path(value)
    checked_directory(path.parent)
    if ':' in path.name:
        raise ConversionError('Selected conversion files cannot contain alternate streams.')
    return regular(path)


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _open_payload_file(path):
    """Bind a real regular file; Windows holds readers without writer sharing."""
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
                             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    else:
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create, close = kernel.CreateFileW, kernel.CloseHandle
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL
        # GENERIC_READ; FILE_SHARE_READ only; OPEN_EXISTING; OPEN_REPARSE_POINT.
        handle = create(str(path), 0x80000000, 0x1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle,
                os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            close(handle)
            raise
    try:
        os.set_inheritable(descriptor, False)
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def verified_reader(value, *, expected_identity=None):
    """Use current local-path guards and keep the exact admitted handle alive."""
    path = Path(value)
    parent = checked_directory(path.parent)
    parent_info = regular(parent, directory=True)
    parent_identity = parent_info.st_dev, parent_info.st_ino
    before = file_identity(regular_file(path))
    if expected_identity is not None and before != expected_identity:
        raise ConversionError('A selected conversion file changed after review.')
    with _open_payload_file(path) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or file_identity(opened) != before):
            raise ConversionError('A selected conversion file changed while opening.')
        observed = regular(checked_directory(parent), directory=True)
        if (observed.st_dev, observed.st_ino) != parent_identity:
            raise ConversionError('The selected conversion parent changed while opening.')
        yield stream
        observed = regular(checked_directory(parent), directory=True)
        if (file_identity(os.fstat(stream.fileno())) != before
                or file_identity(regular_file(path)) != before
                or (observed.st_dev, observed.st_ino) != parent_identity):
            raise ConversionError('A selected conversion file changed while being read.')


FILTERS = {
    '.doc': ('MS Word 97', 'writer.xcd', 'odt', 'writer8'),
    '.ppt': ('MS PowerPoint 97', 'impress.xcd', 'odp', 'impress8'),
    '.xls': ('MS Excel 97', 'calc.xcd', 'ods', 'calc8'),
    '.wpd': ('WordPerfect', 'writer.xcd', 'odt', 'writer8'),
    '.pdb': ('PalmDoc', 'writer.xcd', 'odt', 'writer8'),
    '.wri': ('MS_Write', 'writer.xcd', 'odt', 'writer8'),
}
OFFICE_SUFFIXES = frozenset(FILTERS)
_REGISTRY = 'share/registry/'
_FILES = ('program/soffice.bin', *(_REGISTRY + name for name in
          ('main.xcd', 'writer.xcd', 'calc.xcd', 'impress.xcd')))
_MAX_COMPONENT = 256 * 1024**2
_MAX_REGISTRY = 16 * 1024**2
MAX_STAGE_ENTRIES = 4096
PROFILE_BYTES = 64 * 1024**2
_NS = '{http://openoffice.org/2001/registry}'

# These are public LibreOffice configuration properties. The source and native
# acceptance references are recorded in docs/DOCUMENT_CONVERSION.md.
PROFILE = b'''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="DisableMacrosExecution" oor:op="fuse" oor:finalized="true"><value>true</value></prop>
  <prop oor:name="DisableActiveContent" oor:op="fuse" oor:finalized="true"><value>true</value></prop>
  <prop oor:name="DisableOLEAutomation" oor:op="fuse" oor:finalized="true"><value>true</value></prop>
  <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse" oor:finalized="true"><value>true</value></prop>
  <prop oor:name="MacroSecurityLevel" oor:op="fuse" oor:finalized="true"><value>3</value></prop>
  <prop oor:name="SecureURL" oor:op="replace" oor:finalized="true"><value/></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Common/Load">
  <prop oor:name="UserDefinedSettings" oor:op="fuse"><value>false</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Writer/Content/Update">
  <prop oor:name="Field" oor:op="fuse"><value>false</value></prop>
  <prop oor:name="Chart" oor:op="fuse"><value>false</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Calc/Content/Update">
  <prop oor:name="Link" oor:op="fuse" oor:finalized="true"><value>1</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Update/Update">
  <prop oor:name="Enabled" oor:op="fuse"><value>false</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Common/Misc">
  <prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop>
 </item>
</oor:items>
'''


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('Selected document adapter exceeded its time allowance.')
    return value


def _bounded_digest(stream, expected, check):
    digest, consumed = sha256(), 0
    while True:
        check()
        block = stream.read(min(1024 * 1024, expected - consumed + 1))
        if not block:
            break
        consumed += len(block)
        if consumed > expected:
            raise ConversionError('A selected conversion component grew beyond its captured byte allowance.')
        digest.update(block)
    if consumed != expected:
        raise ConversionError('A selected conversion component was truncated during verification.')
    return digest.hexdigest()


def _executable(selection, deadline):
    if (not isinstance(selection, dict) or set(selection) != {'path', 'bytes', 'sha256'}
            or not isinstance(selection['path'], str) or '\x00' in selection['path']
            or type(selection['bytes']) is not int or not 0 < selection['bytes'] <= 256 * 1024 * 1024
            or not isinstance(selection['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', selection['sha256'])):
        raise ConversionError('Invalid selected converter identity.')
    path = Path(selection['path'])
    if not path.is_absolute():
        raise ConversionError('Selected converter path must be absolute.')
    checked_directory(path.parent)
    info = regular_file(path)
    if info.st_nlink != 1 or info.st_size != selection['bytes']:
        raise ConversionError('The selected converter identity changed.')
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        digest = _bounded_digest(stream, info.st_size, lambda: _remaining(deadline))
    if digest != selection['sha256']:
        raise ConversionError('The selected converter bytes changed.')
    return path



def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Selected LibreOffice conversion cancelled.')


def _paths(path):
    path = Path(path)
    if (not path.is_absolute() or path.name not in ('soffice', 'soffice.exe', 'soffice.com')
            or path.parent.name.casefold() != 'program'):
        raise ConversionUnavailable('Select the actual installed LibreOffice program/soffice executable by absolute local path.')
    checked_directory(path.parent)
    root = checked_directory(path.parent.parent)
    names = {'program/' + path.name, *_FILES}
    if path.name == 'soffice.com':
        names.add('program/soffice.exe')
    # Known installed POSIX launch helpers are part of this selection too.
    # This is a component inventory, not a claim to seal every system library.
    for name in ('soffice.libreoffice', 'oosplash', 'ooenv'):
        candidate = root / 'program' / name
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        names.add('program/' + name)
    return root, tuple(sorted(names))


def _component(root, name):
    path = root / name
    checked_directory(path.parent)
    info = regular_file(path)
    maximum = _MAX_REGISTRY if name.startswith(_REGISTRY) else _MAX_COMPONENT
    if info.st_nlink != 1 or not 0 < info.st_size <= maximum:
        raise ConversionUnavailable('Selected LibreOffice component is linked, empty or exceeds its byte budget.')
    return path, info


@dataclass(frozen=True)
class OfficeSelection:
    path: Path
    root_identity: tuple
    files: tuple

    @classmethod
    def capture(cls, value):
        root, names = _paths(value)
        info = root.stat()
        entries = tuple((name, file_identity(_component(root, name)[1])) for name in names)
        result = cls(Path(value), (info.st_dev, info.st_ino), entries)
        result._unchanged()
        return result

    def _unchanged(self):
        root, names = _paths(self.path)
        info = root.stat()
        if ((info.st_dev, info.st_ino) != self.root_identity
                or tuple(name for name, _ in self.files) != names
                or any(file_identity(_component(root, name)[1]) != identity for name, identity in self.files)):
            raise ConversionError('The selected LibreOffice installation changed after selection.')
        return root

    def snapshot(self, *, cancelled=lambda: False):
        root = self._unchanged()
        rows = []
        for name, identity in self.files:
            _cancel(cancelled)
            path, info = _component(root, name)
            if file_identity(info) != identity:
                raise ConversionError('The selected LibreOffice component changed after selection.')
            with verified_reader(path, expected_identity=identity) as stream:
                digest = _bounded_digest(stream, info.st_size, lambda: _cancel(cancelled))
            rows.append(dict(name=name, bytes=info.st_size, sha256=digest))
        _cancel(cancelled)
        self._unchanged()
        return dict(root=str(root), root_identity=list(self.root_identity),
                    executable='program/' + self.path.name, files=rows)


def _admit(selection, suffix, deadline):
    if (type(selection) is not dict or set(selection) != {'root', 'root_identity', 'executable', 'files'}
            or type(selection['root']) is not str or type(selection['executable']) is not str
            or selection['executable'] not in ('program/soffice', 'program/soffice.exe', 'program/soffice.com')
            or type(selection['files']) is not list
            or type(selection['root_identity']) is not list or len(selection['root_identity']) != 2
            or any(type(value) is not int or value < 0 for value in selection['root_identity'])):
        raise ConversionError('Invalid selected LibreOffice inventory.')
    root, names = _paths(Path(selection['root']) / selection['executable'])
    info = root.stat()
    if (str(root) != selection['root'] or [info.st_dev, info.st_ino] != selection['root_identity']
            or len(selection['files']) != len(names)
            or [row.get('name') for row in selection['files'] if isinstance(row, dict)] != list(names)):
        raise ConversionError('The selected LibreOffice inventory has missing or foreign components.')
    registry = {}
    for item in selection['files']:
        if not isinstance(item, dict) or set(item) != {'name', 'bytes', 'sha256'}:
            raise ConversionError('Invalid selected LibreOffice component.')
        name = item['name']
        path, info = _component(root, name)
        _executable(dict(path=str(path), bytes=item['bytes'], sha256=item['sha256']), deadline)
        if name.startswith(_REGISTRY):
            with verified_reader(path, expected_identity=file_identity(info)) as stream:
                raw = stream.read(_MAX_REGISTRY + 1)
            if len(raw) != item['bytes'] or sha256(raw).hexdigest() != item['sha256']:
                raise ConversionError('The selected LibreOffice registry changed.')
            _remaining(deadline)
            registry[name.removeprefix(_REGISTRY)] = ElementTree.fromstring(raw)
    import_name, module, _, export_name = FILTERS[suffix]
    containers = [node for component in registry[module]
                  if component.tag == _NS + 'component-data'
                  and component.get(_NS + 'package') == 'org.openoffice.TypeDetection'
                  and component.get(_NS + 'name') == 'Filter'
                  for node in component
                  if node.tag == 'node' and node.get(_NS + 'name') == 'Filters']
    for label, needed in ((import_name, 'IMPORT'), (export_name, 'EXPORT')):
        nodes = [node for container in containers for node in container
                 if node.tag == 'node' and node.get(_NS + 'name') == label]
        if len(nodes) != 1:
            raise ConversionUnavailable('The selected LibreOffice installation lacks the required fixed filter.')
        flags = [node for node in nodes[0] if node.tag == 'prop' and node.get(_NS + 'name') == 'Flags']
        if len(flags) != 1 or needed not in ''.join(flags[0].itertext()).split():
            raise ConversionUnavailable('The selected LibreOffice filter does not declare the required operation.')
    properties = {node.get(_NS + 'name') for node in registry['main.xcd'].iter('prop')}
    if not {'DisableMacrosExecution', 'DisableActiveContent', 'DisableOLEAutomation',
            'BlockUntrustedRefererLinks', 'SecureURL'} <= properties:
        raise ConversionUnavailable('The selected LibreOffice lacks the required inactive-content controls.')
    after = checked_directory(root).stat()
    if [after.st_dev, after.st_ino] != selection['root_identity']:
        raise ConversionError('The selected LibreOffice installation changed during admission.')
    return root / selection['executable']


@contextmanager
def _runtime_lease(selection):
    """Use the existing immutable reader through native execution and recheck."""
    with ExitStack() as stack:
        for row in selection['files']:
            path, info = _component(Path(selection['root']), row['name'])
            stack.enter_context(verified_reader(path, expected_identity=file_identity(info)))
        yield


def _stage_usage(stage, maximum, deadline):
    """Bound profile, intermediates and transient files without following links."""
    root = checked_directory(stage)
    before = root.stat()
    pending, count, size = [(root, 0)], 0, 0
    while pending:
        directory, depth = pending.pop()
        _remaining(deadline)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    _remaining(deadline)
                    count += 1
                    if count > MAX_STAGE_ENTRIES or depth > 16:
                        raise ConversionError('Selected LibreOffice staging exceeds its entry budget.')
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            if depth >= 16:
                                raise ConversionError('Selected LibreOffice staging exceeds its depth budget.')
                            pending.append((checked_directory(entry.path), depth + 1))
                        else:
                            info = regular_file(entry.path)
                            if info.st_nlink != 1:
                                raise ConversionError('Selected LibreOffice staging contains an aliased file.')
                            size += info.st_size
                            if size > maximum:
                                raise ConversionError('Selected LibreOffice staging exceeds its byte budget.')
                    except FileNotFoundError:
                        # The owned native child can remove its transient files.
                        # Permission, enumeration and unsafe-path failures stay errors.
                        continue
        except FileNotFoundError:
            if directory == root:
                raise
    after = checked_directory(root).stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ConversionError('Private LibreOffice staging was replaced.')


def _run(args, stage, deadline, *, output=None, output_limit=0, stage_limit=PROFILE_BYTES):
    environment = {key: value for key, value in os.environ.items()
                   if key in ('SYSTEMROOT', 'WINDIR', 'LANG')}
    environment['PATH'] = (os.pathsep.join((str(Path(args[0]).parent),
        str(Path(environment.get('SYSTEMROOT', 'C:/Windows')) / 'System32')))
        if os.name == 'nt' else os.defpath)
    environment.update(TMPDIR=str(stage), TEMP=str(stage), TMP=str(stage),
                       SAL_USE_VCLPLUGIN='svp', SAL_DISABLE_OPENCL='1', MAX_CONCURRENCY='1')
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        _remaining(deadline)
        _stage_usage(stage, stage_limit, deadline)
        process = subprocess.Popen([str(arg) for arg in args], cwd=stage, env=environment,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
            start_new_session=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                _remaining(deadline)
                if any(os.fstat(stream.fileno()).st_size > 256 * 1024 for stream in (stdout, stderr)):
                    raise ConversionError('Selected LibreOffice diagnostics exceeded their byte budget.')
                if output is not None and os.path.lexists(output) and regular_file(output).st_size > output_limit:
                    raise ConversionError('Selected LibreOffice intermediate exceeds its byte budget.')
                _stage_usage(stage, stage_limit, deadline)
                if process.poll() is not None:
                    break
                time.sleep(.02)
            if process.returncode:
                raise ConversionUnavailable('The selected LibreOffice fixed conversion failed (exit ' + str(process.returncode) + ').')
            stdout.seek(0)
            return stdout.read(256 * 1024), os.fstat(stderr.fileno()).st_size > 0
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def office_records(source, data, suffix, budget, deadline, selection):
    if suffix == '.wri' and (len(data) < 128 or data[:6] != b'\x31\xbe\x00\x00\x00\xab'):
        raise ConversionError('Only the bounded Microsoft Write BE31 text-document header family is enabled.')
    if suffix in ('.doc', '.ppt', '.xls') and not data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        raise ConversionError('Legacy Office input must be its actual binary compound document; renamed text is not accepted.')
    if suffix == '.wpd' and not data.startswith(b'\xffWPC'):
        raise ConversionError('Only the WordPerfect document-header family is enabled.')
    if suffix == '.pdb' and (len(data) < 78 or data[60:68] != b'TEXtREAd'):
        raise ConversionError('Only PalmDoc TEXt/REAd PDB containers are enabled; other PDB subtypes need explicit adapters.')
    executable = _admit(selection, suffix, deadline)
    profile, output_dir = source.parent / 'libreoffice-profile', source.parent / 'office-output'
    if os.path.lexists(profile) or os.path.lexists(output_dir):
        raise ConversionError('Private LibreOffice staging already exists.')
    (profile / 'user').mkdir(parents=True, mode=0o700)
    output_dir.mkdir(mode=0o700)
    with (profile / 'user/registrymodifications.xcu').open('xb') as stream:
        stream.write(PROFILE)
        stream.flush()
        os.fsync(stream.fileno())
    base = [executable, '-env:UserInstallation=' + profile.as_uri(), '--headless',
            '--nologo', '--nodefault', '--nofirststartwizard', '--norestore']
    with _runtime_lease(selection):
        _admit(selection, suffix, deadline)
        stage_limit = 2 * budget.max_source_bytes + PROFILE_BYTES
        version, _ = _run([*base, '--version'], source.parent, deadline, stage_limit=stage_limit)
        version = version.decode('utf-8', 'strict').strip()
        if not re.fullmatch(r'LibreOffice(?:Dev)? [0-9][^\r\n\x00]{0,240}', version):
            raise ConversionUnavailable('The selected executable did not identify a bounded LibreOffice runtime.')
        _admit(selection, suffix, deadline)
        input_filter, _, output_suffix, export_filter = FILTERS[suffix]
        output = output_dir / ('source.' + output_suffix)
        _, warnings = _run([*base, '--infilter=' + input_filter,
            '--convert-to', output_suffix + ':' + export_filter, '--outdir', output_dir, source],
            source.parent, deadline, output=output, output_limit=budget.max_source_bytes,
            stage_limit=stage_limit)
        _admit(selection, suffix, deadline)
    if {path.name for path in output_dir.iterdir()} != {output.name}:
        raise ConversionError('Selected LibreOffice did not produce its single declared intermediate.')
    info = regular_file(output)
    if info.st_nlink != 1 or not 0 < info.st_size <= budget.max_source_bytes:
        raise ConversionError('Selected LibreOffice intermediate is empty, linked or too large.')
    with verified_reader(output, expected_identity=file_identity(info)) as stream:
        converted = stream.read(budget.max_source_bytes + 1)
    _admit(selection, suffix, deadline)
    result = extract_document(converted, '.' + output_suffix, max_bytes=budget.max_source_bytes,
                              max_chars=min(budget.max_text_bytes, 10_000_000), deadline=deadline)
    identity = sha256(json.dumps(selection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    for record in result['records']:
        record['status'] = 'review_required'
        record['logical_locator'] = dict(record.get('logical_locator') or {},
            converter=version, selected_components_sha256=identity,
            intermediate_sha256=sha256(converted).hexdigest(), fixed_input_filter=input_filter,
            fixed_output_filter=export_filter, private_profile_sha256=sha256(PROFILE).hexdigest())
    notices = [*result['warnings'], 'converted_layout_and_printed_pages_not_retained',
               'macros_active_content_and_untrusted_links_disabled', 'selected_native_tool_is_not_an_os_sandbox']
    if warnings:
        notices.append('converter_reported_warnings_review_source_fidelity')
    return 'selected-libreoffice-' + suffix[1:], '\n\n', result['records'], notices
