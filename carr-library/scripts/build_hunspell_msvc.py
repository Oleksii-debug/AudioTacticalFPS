"""Build the pinned official Hunspell source for ephemeral Windows CI acceptance.

This script never downloads source, tools, dictionaries, or runtime libraries.
It does not install or bundle its output. Run the separate installed-application
hunspell_acceptance.py against the reported DLL to qualify the actual C API.
"""
from __future__ import annotations

import argparse
from hashlib import sha1, sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import struct
import subprocess
import sys
from time import monotonic, sleep


COMMIT = '2969be996acad84b91ab3875b1816636fe61a40e'
REPOSITORY = 'https://github.com/hunspell/hunspell'
MANIFEST_SHA256 = 'f5ea8a9afae905cd1d2dd56415b320fd0949d85e28151eb8ddb63d19d128aefc'
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_TOOL_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 16 * 1024 * 1024
REPARSE_POINT = 0x400
IS_WINDOWS = os.name == 'nt'


def _path(value, *, directory=False, new=False):
    raw = os.fspath(value)
    if raw.startswith(('\\\\', '//')) or any(ord(char) < 32 for char in raw):
        raise ValueError('Only explicit local paths are supported.')
    path = Path(os.path.abspath(raw))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if new and part == path:
                return path
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & REPARSE_POINT:
            raise ValueError(f'Links/reparse points are not admitted: {part}')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'Not a local directory: {part}')
    if new:
        raise FileExistsError(f'The build destination must be new: {path}')
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if not predicate(info.st_mode):
        raise ValueError(f'Not an ordinary {"directory" if directory else "file"}: {path}')
    return path


def _identity(info):
    # Windows path stat adds executable suffix bits which fstat cannot infer.
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, limit):
    path = _path(path)
    before = path.stat()
    if before.st_size > limit:
        raise ValueError(f'File exceeds the explicit byte bound: {path}')
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError(f'File changed before reading: {path}')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(data) > limit or _identity(after) != _identity(before) or _identity(path.stat()) != _identity(before):
        raise ValueError(f'File changed while reading: {path}')
    return data


def _manifest():
    raw = _read(Path(__file__).with_name('hunspell-1.7.2-source.json'), 16384)
    if sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise ValueError('The reviewed source manifest bytes changed.')
    value = json.loads(raw)
    if (value['schema'] != 'resonance-hunspell-source/1' or value['commit'] != COMMIT
            or value['repository'] != REPOSITORY):
        raise ValueError('The official source identity changed.')
    return value


def _verified_source(source, manifest):
    """Read only the bounded manifest set; unexpected checkout files are unused."""
    source = _path(source, directory=True)
    entries = manifest['files']
    if not 1 <= len(entries) <= 64:
        raise ValueError('Source file-count bound exceeded.')
    total, seen, result = 0, set(), []
    for entry in entries:
        relative = PurePosixPath(entry['path'])
        name = relative.as_posix()
        if (relative.is_absolute() or name != entry['path'] or '..' in relative.parts
                or '\\' in name or ':' in name or not relative.parts or name.casefold() in seen):
            raise ValueError('Invalid or duplicate source path in the manifest.')
        seen.add(name.casefold())
        size = entry['bytes']
        if type(size) is not int or size < 0 or not re.fullmatch('[0-9a-f]{40}', entry['git_blob']):
            raise ValueError('Invalid pinned source byte identity.')
        total += size
        if total > MAX_SOURCE_BYTES:
            raise ValueError('Source byte budget exceeded.')
        raw = _read(source.joinpath(*relative.parts), size)
        git_blob = sha1(f'blob {len(raw)}\0'.encode('ascii') + raw).hexdigest()
        if len(raw) != size or git_blob != entry['git_blob']:
            raise ValueError(f'Pinned source bytes changed: {name}')
        result.append((dict(entry, sha256=sha256(raw).hexdigest()), raw))
    if any(name.casefold() not in seen for name in manifest['license_files']):
        raise ValueError('Every reviewed license notice must be retained.')
    return result


def _property_path(value):
    # MSBuild parses separators/expansions inside /p values even with shell=False.
    text = str(value)
    if any(char in text for char in '$%;,"') or any(ord(char) < 32 for char in text):
        raise ValueError('This CI build path contains an MSBuild property delimiter.')
    return text


def _tool(path):
    raw = _read(path, MAX_TOOL_BYTES)
    return {'path': str(path), 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


def _tools(msbuild, vc_tools_dir, sdk_dir, sdk_version):
    vc = _path(vc_tools_dir, directory=True)
    sdk = _path(sdk_dir, directory=True)
    version = sdk_version.rstrip('/\\')
    if (not re.fullmatch(r'14\.\d+\.\d+', vc.name) or vc.parent.name != 'MSVC'
            or vc.parent.parent.name != 'Tools' or vc.parent.parent.parent.name != 'VC'
            or not re.fullmatch(r'10\.0\.\d+\.\d+', version)):
        raise ValueError('Select the explicit VS 2022 v143 tool directory and Windows 10 SDK version.')
    vs = vc.parents[3]
    msbuild = _path(msbuild)
    if msbuild != vs / 'MSBuild' / 'Current' / 'Bin' / 'MSBuild.exe':
        raise ValueError('MSBuild and the selected C++ tools must belong to the same VS installation.')
    targets = _path(vs / 'MSBuild' / 'Microsoft' / 'VC' / 'v170', directory=True)
    _path(targets / 'Microsoft.Cpp.targets')
    _path(sdk / 'Include' / version / 'um' / 'Windows.h')
    _path(sdk / 'Lib' / version / 'um' / 'x64' / 'kernel32.lib')
    paths = {
        'msbuild': msbuild,
        'cl': vc / 'bin' / 'Hostx64' / 'x64' / 'cl.exe',
        'link': vc / 'bin' / 'Hostx64' / 'x64' / 'link.exe',
        'rc': sdk / 'bin' / version / 'x64' / 'rc.exe',
    }
    for path in (vc, sdk, targets, *paths.values()):
        _property_path(path)
    return {'vc': vc, 'sdk': sdk, 'sdk_version': version, 'vs': vs, 'targets': targets,
            'identities': {name: _tool(path) for name, path in paths.items()}}


def _command(output, tools):
    # Global properties override the upstream v140_xp/8.1 defaults without
    # changing a single upstream source/project/license byte.
    props = {
        'Configuration': 'Release_dll', 'Platform': 'x64',
        'PlatformToolset': 'v143', 'VisualStudioVersion': '17.0',
        'VCTargetsPath': str(tools['targets']) + os.sep,
        'VCToolsVersion': tools['vc'].name,
        'VCToolsInstallDir': str(tools['vc']) + os.sep,
        'WindowsTargetPlatformVersion': tools['sdk_version'],
        'WindowsSdkDir': str(tools['sdk']) + os.sep,
        'OutDir': str(output / 'bin') + os.sep,
        'IntDir': str(output / 'obj') + os.sep,
        'UserRootDir': str(output / 'empty-user-props') + os.sep,
        'CLToolPath': str(tools['vc'] / 'bin' / 'Hostx64' / 'x64') + os.sep,
        'LinkToolPath': str(tools['vc'] / 'bin' / 'Hostx64' / 'x64') + os.sep,
        'ResourceCompileToolPath': str(tools['sdk'] / 'bin' / tools['sdk_version'] / 'x64') + os.sep,
        'PreBuildEventUseInBuild': 'false', 'PostBuildEventUseInBuild': 'false',
        'PreLinkEventUseInBuild': 'false', 'ImportDirectoryBuildProps': 'false',
        'ImportDirectoryBuildTargets': 'false', 'UseEnv': 'false',
        'BuildProjectReferences': 'false',
    }
    return [tools['identities']['msbuild']['path'],
            str(output / 'source' / 'msvc' / 'libhunspell.vcxproj'),
            '/nologo', '/noAutoResponse', '/toolsVersion:Current', '/t:Build',
            '/m:2', '/nr:false', '/verbosity:minimal',
            *(f'/p:{name}={_property_path(value)}' for name, value in props.items())]


def _environment(tools, inherited):
    # Do not inherit CL/LINK options, forced imports, or arbitrary MSBuild
    # property variables. The installed compiler/SDK locations are explicit.
    # FileTracker resolves CommonApplicationData through Windows known folders.
    # Preserve their OS directory roots without admitting build overrides.
    allowed = {'systemroot', 'windir', 'comspec', 'temp', 'tmp', 'userprofile',
               'systemdrive', 'programdata', 'allusersprofile',
               'localappdata', 'appdata', 'programfiles', 'programfiles(x86)',
               'programw6432', 'commonprogramfiles', 'commonprogramfiles(x86)',
               'processor_architecture', 'number_of_processors', 'pathext'}
    env = {key: value for key, value in inherited.items() if key.casefold() in allowed}
    system = inherited.get('SystemRoot') or inherited.get('SYSTEMROOT')
    if not system:
        raise ValueError('A Windows SystemRoot is required for the explicit build.')
    env.update({
        'PATH': os.pathsep.join([str(tools['vc'] / 'bin' / 'Hostx64' / 'x64'),
                                str(tools['sdk'] / 'bin' / tools['sdk_version'] / 'x64'),
                                str(Path(tools['identities']['msbuild']['path']).parent),
                                str(Path(system) / 'System32'), str(system)]),
        'VSINSTALLDIR': str(tools['vs']) + os.sep,
        'VCINSTALLDIR': str(tools['vs'] / 'VC') + os.sep,
        'VCToolsInstallDir': str(tools['vc']) + os.sep,
        'VCToolsVersion': tools['vc'].name,
        'WindowsSdkDir': str(tools['sdk']) + os.sep,
        'WindowsSDKVersion': tools['sdk_version'] + os.sep,
        'WindowsSDKLibVersion': tools['sdk_version'] + os.sep,
        'UniversalCRTSdkDir': str(tools['sdk']) + os.sep,
        'UCRTVersion': tools['sdk_version'],
        'VSCMD_ARG_HOST_ARCH': 'x64', 'VSCMD_ARG_TGT_ARCH': 'x64',
        'CL': '/utf-8', 'MSBUILDDISABLENODEREUSE': '1',
    })
    return env


def _stop(process):
    if process.poll() is not None:
        return
    cleanup_error = None
    try:
        if IS_WINDOWS:
            system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
            result = subprocess.run(
                [str(system / 'System32' / 'taskkill.exe'), '/PID', str(process.pid), '/T', '/F'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False)
            if result.returncode:
                cleanup_error = OSError('Windows could not terminate the complete compiler process tree.')
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.TimeoutExpired) as exc:
        cleanup_error = exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    if cleanup_error is not None:
        raise OSError('Compiler process-tree cleanup failed; inspect the retained log.') from cleanup_error


def _run(command, *, cwd, log, env, timeout, cancelled=lambda: False):
    started = monotonic()
    with log.open('xb') as stream:
        isolation = ({'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200)}
                     if IS_WINDOWS else {'start_new_session': True})
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT,
                                   **isolation)
        try:
            while process.poll() is None:
                if cancelled():
                    raise InterruptedError('The owned Hunspell build was cancelled.')
                if monotonic() - started > timeout:
                    raise TimeoutError('The owned Hunspell build exceeded its deadline.')
                if os.fstat(stream.fileno()).st_size > MAX_LOG_BYTES:
                    raise ValueError('The build log exceeded its byte bound.')
                sleep(.05)
            if process.wait() != 0:
                stream.flush()
                # Keep the full bounded log as evidence and surface its final
                # diagnostics in the job console. Otherwise a failed build is
                # opaque until GitHub finishes uploading the complete job.
                with log.open('rb') as diagnostic:
                    diagnostic.seek(max(0, os.fstat(diagnostic.fileno()).st_size - 16384))
                    tail = diagnostic.read(16384).decode('utf-8', errors='replace')
                raise RuntimeError(f'MSBuild failed with exit {process.returncode}; inspect {log}\n'
                                   f'Final compiler diagnostics (at most 16 KiB):\n{tail}')
            if os.fstat(stream.fileno()).st_size > MAX_LOG_BYTES:
                raise ValueError('The build log exceeded its byte bound.')
        finally:
            _stop(process)


def _dll(path):
    raw = _read(path, MAX_TOOL_BYTES)
    if len(raw) < 64 or raw[:2] != b'MZ':
        raise ValueError('MSBuild did not produce a PE DLL.')
    offset = struct.unpack_from('<I', raw, 0x3c)[0]
    if offset > len(raw) - 26 or raw[offset:offset + 4] != b'PE\0\0':
        raise ValueError('Invalid output PE header.')
    machine, = struct.unpack_from('<H', raw, offset + 4)
    characteristics, = struct.unpack_from('<H', raw, offset + 22)
    optional_magic, = struct.unpack_from('<H', raw, offset + 24)
    if machine != 0x8664 or not characteristics & 0x2000 or optional_magic != 0x20b:
        raise ValueError('The output must be an AMD64 PE32+ DLL.')
    return {'path': str(path), 'bytes': len(raw), 'sha256': sha256(raw).hexdigest(),
            'machine': 'AMD64', 'c_api': 'NOT_RUN'}


def build(source, output, *, msbuild, vc_tools_dir, sdk_dir, sdk_version, timeout=300):
    if not IS_WINDOWS:
        raise ValueError('Actual MSVC acceptance requires Windows; no simulated build is reported.')
    if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
        raise ValueError('The build deadline must be between 1 and 600 seconds.')
    source = _path(source, directory=True)
    output = _path(output, new=True)
    _property_path(output)
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Source and new build directories must be separate.')
    manifest = _manifest()
    verified = _verified_source(source, manifest)
    tools = _tools(msbuild, vc_tools_dir, sdk_dir, sdk_version)
    command = _command(output, tools)
    environment = _environment(tools, os.environ)
    output.mkdir()
    retained = output / 'source'
    for metadata, raw in verified:
        path = retained.joinpath(*PurePosixPath(metadata['path']).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(raw)
    (output / 'empty-user-props').mkdir()
    _run(command, cwd=retained / 'msvc', log=output / 'build.log', env=environment, timeout=timeout)
    # Do not claim pinned provenance after a build rewrites source or tools.
    after = _verified_source(retained, manifest)
    _verified_source(source, manifest)
    if [item for item, _ in after] != [item for item, _ in verified]:
        raise ValueError('The build changed the retained source set.')
    for identity in tools['identities'].values():
        if _tool(Path(identity['path'])) != identity:
            raise ValueError('A selected build tool changed while building.')
    dll = _dll(output / 'bin' / 'libhunspell.dll')
    log = _read(output / 'build.log', MAX_LOG_BYTES)
    report = {
        'schema': 'resonance-hunspell-msvc-build/1', 'status': 'BUILT',
        'source_repository': REPOSITORY, 'source_commit': COMMIT,
        'source_manifest_sha256': MANIFEST_SHA256,
        'source_files': [item for item, _ in verified],
        'license_files': manifest['license_files'], 'source_bytes_modified': False,
        'build': {'configuration': 'Release_dll', 'platform': 'x64', 'platform_toolset': 'v143',
                  'vc_tools_version': tools['vc'].name, 'windows_sdk': tools['sdk_version'],
                  'tools': tools['identities'], 'command': command, 'exit_code': 0,
                  'log_bytes': len(log), 'log_sha256': sha256(log).hexdigest()},
        'dll': dll, 'native_api': 'NOT_RUN', 'bundled': False,
        'distribution_clearance': 'NOT_GRANTED',
    }
    with (output / 'build-report.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Explicit local checkout of the pinned official source')
    parser.add_argument('--output', required=True, help='New ephemeral directory, outside the application bundle')
    parser.add_argument('--msbuild', required=True, help='Explicit installed VS 2022 MSBuild.exe')
    parser.add_argument('--vc-tools-dir', required=True, help='Explicit VC/Tools/MSVC/version directory')
    parser.add_argument('--sdk-dir', required=True, help='Explicit Windows Kits/10 directory')
    parser.add_argument('--sdk-version', required=True, help='Explicit installed 10.0.x.y SDK version')
    parser.add_argument('--timeout', type=float, default=300, help='Total compiler deadline, 1..600 seconds')
    args = parser.parse_args(argv)
    try:
        report = build(**vars(args))
    except KeyboardInterrupt:
        print('Hunspell build interrupted; no successful build receipt was published.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Hunspell build failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
