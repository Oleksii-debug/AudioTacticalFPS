"""CI helper/source boundary tests; these do not simulate a C API qualification."""
from hashlib import sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location('hunspell_msvc_build_tests',
    Path(__file__).resolve().parents[2] / 'scripts/build_hunspell_msvc.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def entry(name, raw):
    return {'path': name, 'bytes': len(raw),
            'git_blob': sha1(f'blob {len(raw)}\0'.encode('ascii') + raw).hexdigest()}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'upstream source'; root.mkdir()
    content = {'COPYING': b'Original synthetic test notice.\n',
               'msvc/libhunspell.vcxproj': b'<Project />\n',
               'src/hunspell/hunvisapi.h': b'// exact synthetic test header\r\n'}
    for name, raw in content.items():
        path = root / name; path.parent.mkdir(exist_ok=True, parents=True); path.write_bytes(raw)
    manifest = {'files': [entry(name, raw) for name, raw in content.items()], 'license_files': ['COPYING']}
    return root, manifest


@pytest.fixture
def toolchain(tmp_path):
    vs = tmp_path / 'VS2022'
    vc = vs / 'VC/Tools/MSVC/14.44.35207'
    sdk = tmp_path / 'Windows Kits/10'
    paths = [vs / 'MSBuild/Current/Bin/MSBuild.exe',
             vs / 'MSBuild/Microsoft/VC/v170/Microsoft.Cpp.targets',
             vc / 'bin/Hostx64/x64/cl.exe', vc / 'bin/Hostx64/x64/link.exe',
             sdk / 'bin/10.0.26100.0/x64/rc.exe',
             sdk / 'Include/10.0.26100.0/um/Windows.h',
             sdk / 'Lib/10.0.26100.0/um/x64/kernel32.lib']
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'Not a real compiler: ' + path.name.encode())
    return {'msbuild': paths[0], 'vc_tools_dir': vc, 'sdk_dir': sdk, 'sdk_version': '10.0.26100.0\\'}


def pe(*, machine=0x8664, characteristics=0x2000, magic=0x20b):
    """Header-only fixture, deliberately not executable and never C API tested."""
    raw = bytearray(256); raw[:2] = b'MZ'; struct.pack_into('<I', raw, 0x3c, 128)
    raw[128:132] = b'PE\0\0'
    struct.pack_into('<H', raw, 132, machine)
    struct.pack_into('<H', raw, 150, characteristics)
    struct.pack_into('<H', raw, 152, magic)
    return raw


@pytest.fixture
def build_fixture(tmp_path, source, toolchain, monkeypatch):
    root, manifest = source
    output = tmp_path / 'new build'
    monkeypatch.setattr(helper, 'IS_WINDOWS', True)
    monkeypatch.setattr(helper, '_manifest', lambda: manifest)
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    calls = []
    def synthetic_compiler(command, *, cwd, log, env, timeout):
        calls.append((command, env))
        assert cwd == output / 'source/msvc'
        assert timeout == 300
        log.write_bytes(b'Explicit synthetic runner; no native compilation.\n')
        (output / 'bin').mkdir()
        (output / 'bin/libhunspell.dll').write_bytes(pe())
    monkeypatch.setattr(helper, '_run', synthetic_compiler)
    return root, output, toolchain, calls, synthetic_compiler


def test_committed_manifest_binds_exact_official_commit_and_license_set():
    manifest = helper._manifest()
    assert manifest['commit'] == '2969be996acad84b91ab3875b1816636fe61a40e'
    assert manifest['tag'] == 'v1.7.2' and len(manifest['files']) == 38
    assert sum(item['bytes'] for item in manifest['files']) == 3332658
    assert manifest['license_files'] == ['COPYING', 'COPYING.LESSER', 'COPYING.MPL', 'license.hunspell', 'license.myspell']


@pytest.mark.parametrize('mutation', ['same_size', 'newline', 'missing'])
def test_source_must_match_git_blob_bytes_even_with_preserved_timestamps(source, mutation):
    root, manifest = source
    helper._verified_source(root, manifest)
    path = root / 'src/hunspell/hunvisapi.h'; before = path.stat(); raw = path.read_bytes()
    if mutation == 'missing': path.unlink()
    else:
        path.write_bytes(raw.replace(b'exact', b'wrong') if mutation == 'same_size' else raw.replace(b'\r\n', b'\n'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises((ValueError, FileNotFoundError)):
        helper._verified_source(root, manifest)


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/outside', 'src\\outside', './COPYING', 'COPYING//extra'])
def test_manifest_paths_cannot_escape_or_silently_normalize(source, name):
    root, manifest = source
    manifest['files'][0]['path'] = name
    with pytest.raises(ValueError): helper._verified_source(root, manifest)


def test_source_budget_and_notice_are_checked(source, monkeypatch):
    root, manifest = source
    monkeypatch.setattr(helper, 'MAX_SOURCE_BYTES', 1)
    with pytest.raises(ValueError, match='budget'): helper._verified_source(root, manifest)
    monkeypatch.setattr(helper, 'MAX_SOURCE_BYTES', 4096)
    manifest['license_files'].append('absent-notice')
    with pytest.raises(ValueError, match='license'): helper._verified_source(root, manifest)


def test_source_directory_link_is_rejected_without_following_it(source, tmp_path):
    root, manifest = source
    link = tmp_path / 'linked source'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(root), str(link))
    else:
        link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match='Links/reparse'):
        helper._verified_source(link, manifest)


@pytest.mark.parametrize('names', [
    ('ProgramData', 'ALLUSERSPROFILE', 'SystemDrive'),
    ('PROGRAMDATA', 'allusersprofile', 'systemdrive'),
])
def test_sanitized_environment_preserves_common_application_data_roots(toolchain, names):
    # Windows documents these three forms for FOLDERID_ProgramData. MSBuild's
    # FileTracker resolves that known folder before processing tracked outputs.
    inherited = {
        'SystemRoot': r'C:\Windows',
        names[0]: r'C:\ProgramData', names[1]: r'C:\ProgramData', names[2]: 'C:',
        'CL': '/FIunreviewed.h', '_CL_': '/FIanother.h', 'LINK': 'unreviewed.obj',
        'ForceImportBeforeCppTargets': 'unreviewed.targets',
        'MSBuildExtensionsPath': 'unreviewed extensions', 'OtherProperty': 'arbitrary',
    }
    env = helper._environment(helper._tools(**toolchain), inherited)
    assert {name: env.get(name) for name in names} == {
        names[0]: r'C:\ProgramData', names[1]: r'C:\ProgramData', names[2]: 'C:',
    }
    assert env['SystemRoot'] == r'C:\Windows'
    assert env['CL'] == '/utf-8'
    assert not {'_CL_', 'LINK', 'ForceImportBeforeCppTargets',
                'MSBuildExtensionsPath', 'OtherProperty'} & env.keys()
    assert inherited['CL'] == '/FIunreviewed.h'


def test_build_fixture_retains_only_verified_source_and_separates_native_api_claim(build_fixture, monkeypatch):
    source, output, kwargs, calls, _ = build_fixture
    (source / 'Directory.Build.targets').write_text('Unselected arbitrary build input.', encoding='utf-8')
    monkeypatch.setenv('CL', '/FIunreviewed.h')
    monkeypatch.setenv('ForceImportBeforeCppTargets', 'unreviewed.targets')
    report = helper.build(source, output, **kwargs)
    assert report['status'] == 'BUILT' and report['native_api'] == report['dll']['c_api'] == 'NOT_RUN'
    assert report['bundled'] is False and report['distribution_clearance'] == 'NOT_GRANTED'
    assert not (output / 'source/Directory.Build.targets').exists()
    assert (output / 'source/COPYING').read_bytes() == (source / 'COPYING').read_bytes()
    assert json.loads((output / 'build-report.json').read_text(encoding='utf-8')) == report
    command, env = calls[0]
    assert '/p:PreBuildEventUseInBuild=false' in command and '/p:ImportDirectoryBuildProps=false' in command
    assert '/p:ImportDirectoryBuildTargets=false' in command and '/noAutoResponse' in command and '/nr:false' in command
    assert '/p:PlatformToolset=v143' in command and '/p:WindowsTargetPlatformVersion=10.0.26100.0' in command
    assert env['CL'] == '/utf-8' and 'ForceImportBeforeCppTargets' not in env
    assert set(report['build']['tools']) == {'msbuild', 'cl', 'link', 'rc'}
    for selected in report['build']['tools'].values():
        assert selected['sha256'] == sha256(Path(selected['path']).read_bytes()).hexdigest()


def test_existing_destination_is_never_replaced_or_run(build_fixture):
    source, output, kwargs, calls, _ = build_fixture
    output.mkdir(); saved = output / 'retained'; saved.write_bytes(b'keep')
    with pytest.raises(FileExistsError): helper.build(source, output, **kwargs)
    assert saved.read_bytes() == b'keep' and not calls


def test_invalid_source_has_no_output_or_compiler_side_effect(build_fixture):
    source, output, kwargs, calls, _ = build_fixture
    (source / 'COPYING').write_bytes(b'not the pinned notice')
    with pytest.raises(ValueError): helper.build(source, output, **kwargs)
    assert not output.exists() and not calls


@pytest.mark.parametrize('target', ['upstream', 'retained', 'compiler'])
def test_changed_sources_or_tool_cannot_publish_success(build_fixture, monkeypatch, target):
    source, output, kwargs, _, run = build_fixture
    def changed(*args, **kw):
        run(*args, **kw)
        path = {'upstream': source / 'COPYING', 'retained': output / 'source/COPYING',
                'compiler': kwargs['msbuild']}[target]
        raw = path.read_bytes(); path.write_bytes(b'!' + raw[1:])
    monkeypatch.setattr(helper, '_run', changed)
    with pytest.raises(ValueError): helper.build(source, output, **kwargs)
    assert not (output / 'build-report.json').exists()


def test_failed_compiler_keeps_diagnostic_log_without_success_receipt(build_fixture, monkeypatch):
    source, output, kwargs, _, run = build_fixture
    def failed(*args, **kw):
        run(*args, **kw); raise RuntimeError('synthetic compiler failure')
    monkeypatch.setattr(helper, '_run', failed)
    with pytest.raises(RuntimeError, match='compiler failure'): helper.build(source, output, **kwargs)
    assert (output / 'build.log').is_file() and not (output / 'build-report.json').exists()


@pytest.mark.parametrize('changed', [{'machine': 0x14c}, {'characteristics': 0}, {'magic': 0x10b}])
def test_output_must_be_a_64_bit_dll(tmp_path, changed):
    path = tmp_path / 'wrong.dll'; path.write_bytes(pe(**changed))
    with pytest.raises(ValueError, match='AMD64'): helper._dll(path)


@pytest.mark.parametrize('delimiter', [';', '$', '%', ','])
def test_msbuild_property_injection_is_rejected_before_output(build_fixture, delimiter):
    source, output, kwargs, calls, _ = build_fixture
    output = output.with_name('build' + delimiter + 'injection')
    with pytest.raises(ValueError, match='property delimiter'): helper.build(source, output, **kwargs)
    assert not output.exists() and not calls


@pytest.mark.parametrize('mode', ['cancel', 'deadline', 'failure'])
def test_actual_owned_process_is_reaped_and_log_is_retained(tmp_path, monkeypatch, mode):
    created = []
    actual = subprocess.Popen
    def start(*args, **kwargs):
        process = actual(*args, **kwargs); created.append(process); return process
    # Keep stdlib subprocess untouched; Windows lazy imports subclass Popen.
    monkeypatch.setattr(helper, 'subprocess', SimpleNamespace(Popen=start, DEVNULL=subprocess.DEVNULL,
        STDOUT=subprocess.STDOUT, run=subprocess.run, TimeoutExpired=subprocess.TimeoutExpired,
        CREATE_NEW_PROCESS_GROUP=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200)))
    marker, log = tmp_path / 'started', tmp_path / 'build.log'
    code = ("import pathlib,sys,time; print('owned fixture',flush=True); "
            "pathlib.Path(sys.argv[1]).write_text('started',encoding='utf-8'); "
            + ("sys.exit(7)" if mode == 'failure' else "time.sleep(30)"))
    expected = {'cancel': InterruptedError, 'deadline': TimeoutError, 'failure': RuntimeError}[mode]
    with pytest.raises(expected) as failure:
        # This stdlib-only child owns the base interpreter, avoiding the Windows
        # venv redirector while retaining the actual Popen and termination path.
        environment = os.environ.copy()
        environment.pop('__PYVENV_LAUNCHER__', None); environment.pop('PYTHONEXECUTABLE', None)
        executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True))
        helper._run([executable, '-I', '-B', '-c', code, str(marker)], cwd=tmp_path, log=log,
                    env=environment, timeout=.4 if mode == 'deadline' else 10,
                    cancelled=lambda: mode == 'cancel' and marker.exists())
    assert created and created[0].poll() is not None and log.is_file()
    if mode != 'deadline': assert b'owned fixture' in log.read_bytes()
    if mode == 'failure':
        assert 'exit 7' in str(failure.value) and 'owned fixture' in str(failure.value)
