"""Exercise immutable-runtime launch policy without claiming a Windows install.

The subprocess harness runs the installer file unchanged. Only Windows platform
identification and environment creation/package installation are simulated; real
Python imports, bytecode writes and the production manifest verifier are used.
A separate test runs real venv/ensurepip bootstrap processes on the test host.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
import venv


ROOT = Path(__file__).resolve().parents[2]

INSTALL_HARNESS = r'''
import collections, hashlib, importlib.machinery, importlib.util, json
import logging, os, pathlib, platform, runpy, shlex, shutil, stat
import subprocess, sys, sysconfig, types
root = pathlib.Path(sys.argv[1])
real_run = subprocess.run

def probe(flags, name, environment, arguments=()):
    code = ('import importlib, json, sys; sys.path.insert(0, '
            + repr(str(root / 'runtime' / 'Lib'))
            + '); importlib.import_module(' + repr(name) + '); '
            + 'print(json.dumps({"dont_write": sys.dont_write_bytecode, '
              '"isolated": bool(sys.flags.isolated)}))')
    completed = real_run([sys.executable, *flags, '-c', code],
                         env=environment, capture_output=True, text=True, check=True)
    record = json.loads(completed.stdout)
    record['stage'] = name
    record['flags'] = flags
    record['arguments'] = arguments
    record['variables'] = {key: environment[key] for key in
        ('PYTHONNOUSERSITE', 'PYTHONSAFEPATH', 'PYTHONDONTWRITEBYTECODE', 'PIP_NO_INDEX')
        if key in environment}
    with (root / 'child-processes.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record) + '\n')
    return completed

class ProbeBuilder:
    def __init__(self, *, with_pip, clear, symlinks):
        self.with_pip = with_pip

    def create(self, destination):
        scripts = pathlib.Path(destination) / 'Scripts'
        scripts.mkdir()
        (scripts / 'python.exe').write_bytes(b'synthetic interpreter location')
        if self.with_pip:
            # CPython venv's ensurepip subprocess has no -I/-B; it inherits env.
            probe([], 'bootstrap_runtime_probe', dict(sys.modules['os'].environ))

class VenvLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        super().exec_module(module)
        module.EnvBuilder = ProbeBuilder

class VenvFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'venv':
            source = root / 'runtime' / 'Lib' / 'venv' / '__init__.py'
            return importlib.util.spec_from_file_location(
                fullname, source, loader=VenvLoader(fullname, str(source)),
                submodule_search_locations=[str(source.parent)])

def package_process(command, **kwargs):
    marker = command.index('-m')
    stage = {'ensurepip': 'bootstrap_runtime_probe', 'pip': 'pip_runtime_probe',
             'carr_library': 'application_runtime_probe'}[command[marker + 1]]
    return probe(command[1:marker], stage, kwargs['env'],
                 command[marker:] if command[marker + 1] == 'ensurepip' else ())

# Keep pathlib bound to the host filesystem while the installer's Windows gate
# sees the intended target. No native Windows execution is represented here.
windows_os = types.ModuleType('os')
windows_os.__dict__.update(os.__dict__)
windows_os.name = 'nt'
windows_os.environ = dict(os.environ)
windows_os.environ['LOCALAPPDATA'] = str(root.parent / 'private-data')
sys.modules['os'] = windows_os
platform.architecture = lambda: ('64bit', '')
version = collections.namedtuple('Version', 'major minor micro releaselevel serial')
sys.version_info = version(3, 11, 18, 'final', 0)
sys.meta_path.insert(0, VenvFinder())
subprocess.run = package_process
runpy.run_path(str(root / 'install_windows.py'), run_name='__main__')
'''


def installer_module():
    path = ROOT / 'packaging' / 'install_windows.py'
    module = types.ModuleType('installer_under_test')
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def launcher_flags(name):
    """Use each supplied Python invocation's actual options in a subprocess."""
    commands = []
    for line in (ROOT / 'packaging' / name).read_text().splitlines():
        match = re.match(r'\s*(?:"[^"]*python\.exe"|py\s+-3\.11)\s+(.*)',
                         line, flags=re.IGNORECASE)
        if match:
            flags = []
            for token in shlex.split(match[1], posix=False):
                if token in ('-m', '-c') or not token.startswith('-'):
                    break
                flags.append(token)
            commands.append(flags)
    if not commands:
        raise AssertionError('No Python invocation found in ' + name)
    return commands


def fixture(root):
    root.mkdir()
    for name in ('install_windows.py', 'Install.cmd', 'ResearchLibrary.cmd',
                 'CheckInstallation.cmd'):
        (root / name).write_bytes((ROOT / 'packaging' / name).read_bytes())
    for name in ('install.lock', 'README.txt', 'wheelhouse/fixture.whl', 'runtime/python.exe'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic bundle file')
    source = root / 'runtime' / 'Lib' / 'venv' / '__init__.py'
    source.parent.mkdir(parents=True)
    source.write_bytes(Path(importlib.util.find_spec('venv').origin).read_bytes())
    for name in ('bootstrap_runtime_probe', 'pip_runtime_probe', 'application_runtime_probe'):
        (source.parent.parent / (name + '.py')).write_text('VALUE = 1\n')
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob('*') if p.is_file()}
    (root / 'bundle-manifest.json').write_text(json.dumps(
        {'schema': 1, 'bundled_python': True, 'files': files}), encoding='utf-8')
    return root


class InstallBytecodeTests(unittest.TestCase):
    def run_installer(self, root, flags):
        environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('PYTHON')}
        # Deliberately start with an environment that permits bytecode writing.
        environment['PYTHONDONTWRITEBYTECODE'] = ''
        return subprocess.run([sys.executable, *flags, '-c', INSTALL_HARNESS, str(root)],
                              capture_output=True, text=True, env=environment)

    def check_install_twice(self, flags):
        with tempfile.TemporaryDirectory(prefix='installer-bytecode-') as temp:
            root = fixture(Path(temp) / 'bundle')
            installer = installer_module()
            installer.verify(root)
            first = self.run_installer(root, flags)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            installer.verify(root)
            records = [json.loads(line) for line in
                       (root / 'child-processes.jsonl').read_text().splitlines()]
            self.assertEqual(len(records), 3)
            self.assertTrue(all(row['dont_write'] for row in records), records)
            self.assertFalse(list((root / 'runtime').rglob('*.pyc')))
            second = self.run_installer(root, flags)
            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            self.assertIn('already has an environment', second.stderr)
            installer.verify(root)

    def test_installer_launcher_first_and_repeat_run_preserve_runtime(self):
        for flags in launcher_flags('Install.cmd'):
            with self.subTest(flags=flags):
                self.check_install_twice(flags)

    def test_direct_installer_disables_bytecode_before_importing_venv(self):
        self.check_install_twice(['-I'])

    def test_application_and_diagnostic_launchers_do_not_modify_runtime(self):
        for name in ('ResearchLibrary.cmd', 'CheckInstallation.cmd'):
            for flags in launcher_flags(name):
                with self.subTest(launcher=name, flags=flags), tempfile.TemporaryDirectory() as temp:
                    root = fixture(Path(temp) / 'bundle')
                    code = ('import sys; sys.path.insert(0, '
                            + repr(str(root / 'runtime' / 'Lib'))
                            + '); import application_runtime_probe')
                    environment = {k: v for k, v in os.environ.items()
                                   if not k.upper().startswith('PYTHON')}
                    subprocess.run([sys.executable, *flags, '-c', code], check=True, env=environment)
                    installer_module().verify(root)

    def test_real_venv_ensurepip_and_nested_pip_inherit_bytecode_policy(self):
        original_builder = venv.EnvBuilder
        with tempfile.TemporaryDirectory(prefix='real-bootstrap-') as temp:
            root = Path(temp)
            bundle = fixture(root / 'bundle')
            completed = self.run_installer(bundle, launcher_flags('Install.cmd')[0])
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            operations = [json.loads(line) for line in
                          (bundle / 'child-processes.jsonl').read_text().splitlines()]
            bootstrap, = [row for row in operations if row['stage'] == 'bootstrap_runtime_probe']
            log = root / 'bootstrap-processes.jsonl'

            class RecordingBuilder(original_builder):
                def setup_python(self, context):
                    super().setup_python(context)
                    self.executable = context.env_exec_cmd
                    site_packages = Path(self._venv_path(context.env_dir, 'purelib'))
                    site_packages.mkdir(parents=True, exist_ok=True)
                    (site_packages / 'bootstrap_probe.py').write_text('VALUE = 1\n')
                    (site_packages / 'sitecustomize.py').write_text(
                        'import json, sys, bootstrap_probe\n'
                        'with open(' + repr(str(log)) + ', "a", encoding="utf-8") as stream:\n'
                        '    stream.write(json.dumps({"dont_write": sys.dont_write_bytecode, '
                        '"isolated": bool(sys.flags.isolated), "no_user_site": bool(sys.flags.no_user_site), '
                        '"safe_path": bool(sys.flags.safe_path)}) + "\\n")\n')

            builder = RecordingBuilder(with_pip=False, clear=False, symlinks=False)
            builder.create(root / 'venv')
            environment = {k: v for k, v in os.environ.items()
                           if not k.upper().startswith(('PYTHON', 'PIP_'))}
            environment.update(bootstrap['variables'])
            # These options and controlled variables came from executing the
            # actual installer; now exercise real ensurepip and its nested pip.
            subprocess.run([builder.executable, *bootstrap['flags'], *bootstrap['arguments']],
                           check=True, capture_output=True, text=True, env=environment)
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertGreaterEqual(len(records), 2, records)
            self.assertTrue(all(row['dont_write'] and row['no_user_site'] and row['safe_path']
                                for row in records), records)
            self.assertFalse(list((root / 'venv').rglob('bootstrap_probe.*.pyc')))


if __name__ == '__main__':
    unittest.main()
