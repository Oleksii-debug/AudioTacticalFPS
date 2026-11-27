"""Bounded native diagnosis of the observed standard shortcut-helper timeout.

Disposable CI only, not installer/update qualification. Commands reuse the
current admitted Windows helper and run_owned. Reports keep fixed phase tokens,
error classes and hashes, never raw environment values or private target paths.
"""
from __future__ import annotations
import argparse
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from carr_library.security import standard_windows as standard

PREFIX = 'CARR_HELPER_PHASE:'
PHASES = frozenset(('START', 'UTF8', 'COM_BEFORE', 'COM_AFTER', 'SHORTCUT_BEFORE',
                   'SHORTCUT_AFTER', 'TARGET_SET', 'SAVED', 'TARGET_MATCHED', 'RELEASED'))
CONTROL = (
    "[Console]::WriteLine('CARR_HELPER_PHASE:START');[Console]::Out.Flush();"
    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
    "[Console]::WriteLine('CARR_HELPER_PHASE:UTF8');[Console]::Out.Flush();exit 0"
)


def command(activator=False):
    creation = ("[Activator]::CreateInstance([Type]::GetTypeFromProgID('WScript.Shell'))"
                if activator else "New-Object -ComObject WScript.Shell")
    return (
        "$ErrorActionPreference='Stop';"
        "function phase([string]$value){[Console]::WriteLine('CARR_HELPER_PHASE:'+$value);[Console]::Out.Flush()};"
        "phase 'START';[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();phase 'UTF8';"
        + ("$PSModuleAutoLoadingPreference='None';" if activator else "") +
        "try{phase 'COM_BEFORE';$shell=" + creation + ";phase 'COM_AFTER';"
        "phase 'SHORTCUT_BEFORE';$link=$shell.CreateShortcut($env:CARR_STANDARD_SHORTCUT);"
        "phase 'SHORTCUT_AFTER';$link.TargetPath=$env:CARR_STANDARD_TARGET;phase 'TARGET_SET';"
        "$link.Save();phase 'SAVED';if($link.TargetPath -ne $env:CARR_STANDARD_TARGET){exit 7};"
        "phase 'TARGET_MATCHED';"
        "[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link);"
        "[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell);"
        "phase 'RELEASED';exit 0}catch{"
        "[Console]::WriteLine('CARR_HELPER_ERROR:'+$_.Exception.GetType().Name+':'+$_.Exception.HResult);"
        "[Console]::Out.Flush();exit 9}"
    )


def observe(label, action):
    streams, children = [], []
    launch = standard._start_process

    def captured(arguments, **options):
        # run_owned still owns the actual process and its original output spool.
        streams.append(os.fdopen(os.dup(options['stdout'].fileno()), 'rb'))
        process = launch(arguments, **options)
        children.append(process)
        return process

    started = time.monotonic()
    record = {'label': label, 'status': 'ERROR', 'timeout_seconds': 30}
    standard._start_process = captured
    try:
        value = action()
        if isinstance(value, tuple):
            code, _ = value
            record.update(status='RETURNED' if code == 0 else 'NONZERO', exit_code=code)
        else:
            record.update(status='RETURNED', target_matches=value is True)
    except Exception as error:
        record['error_type'] = type(error).__name__
        code = getattr(error, 'winerror', None)
        if type(code) is int:
            record['winerror'] = code
    finally:
        standard._start_process = launch
        record['elapsed_seconds'] = round(time.monotonic() - started, 6)
        raw = b''
        for stream in streams:
            try:
                stream.seek(0)
                raw += stream.read(2 * 1024**2 + 1)
            finally:
                stream.close()
        text = raw.decode('utf-8-sig', errors='replace')
        record['diagnostic_bytes'] = len(raw)
        record['phases'] = [line[len(PREFIX):] for line in text.splitlines()
                            if line.startswith(PREFIX) and line[len(PREFIX):] in PHASES]
        record['native_errors'] = [line for line in text.splitlines()
            if re.fullmatch(r'CARR_HELPER_ERROR:[A-Za-z0-9_]{1,96}:-?[0-9]{1,20}', line)]
        record['actual_children'] = len(children)
        record['children_settled'] = all(child.poll() is not None
            and getattr(getattr(child, '_carr_job', None), 'handle', None) is None
            for child in children)
    print(json.dumps(record, sort_keys=True), flush=True)
    if not record['children_settled']:
        raise RuntimeError('The existing native process owner has not settled.')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32' or not sys.flags.isolated or os.environ.get('CI', '').lower() != 'true':
        raise RuntimeError('Use an isolated interpreter in disposable actual Windows CI.')
    project = Path(__file__).resolve().parents[1]
    source, tree = os.environ['CARR_DIAGNOSTIC_SOURCE'], os.environ['CARR_DIAGNOSTIC_TREE']
    if not all(re.fullmatch(r'[0-9a-f]{40}', value) for value in (source, tree)):
        raise ValueError('Exact source and tree identities are required.')
    origins = {}
    for name in ('carr_library.security.standard_windows', 'carr_library.audio.engines',
                 'carr_library.audio.windows_process'):
        module = importlib.import_module(name)
        installed = Path(module.__file__).resolve()
        expected = project / 'src' / Path(*name.split('.')).with_suffix('.py')
        if (not installed.is_relative_to(Path(sys.prefix).resolve())
                or installed.is_relative_to(project / 'src')
                or sha256(installed.read_bytes()).digest() != sha256(expected.read_bytes()).digest()):
            raise AssertionError('A diagnostic owner differs from this installed source.')
        origins[name] = sha256(installed.read_bytes()).hexdigest()
    host = standard.WindowsStandardHost(lambda *a, **k: None, lambda *a, **k: None)
    helper = host._powershell()
    info = helper.lstat()
    if not args.work.is_absolute() or '..' in args.work.parts:
        raise ValueError('Use a new absolute disposable work directory.')
    standard.directory(args.work.parent)
    args.work.mkdir()
    report = {'schema': 1, 'kind': 'standard-helper-diagnostic', 'status': 'INCOMPLETE',
              'actual_windows': True, 'isolated': True, 'source_commit': source,
              'source_tree': tree, 'module_sha256': origins,
              'helper_sha256': sha256(helper.read_bytes()).hexdigest(),
              'helper_links': info.st_nlink, 'helper_bytes': info.st_size,
              'qualification': 'NOT_AN_INSTALL_OR_UPDATE_QUALIFICATION', 'observations': []}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def retain(record):
        report['observations'].append(record)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    try:
        with tempfile.TemporaryDirectory(prefix='owned-shortcut-', dir=args.work) as folder:
            root, baseline, target = Path(folder), host.environment(), Path(sys.executable).resolve()
            def run(script, environment):
                return standard.run_owned([helper, '-NoProfile', '-NonInteractive', '-Command', script],
                                          cwd=root, environment=environment, timeout=30)
            retain(observe('filtered-literal-control', lambda: run(CONTROL, baseline)))
            variants = [('filtered-new-object', {}, False),
                        ('filtered-activator-no-autoload', {}, True),
                        ('fixed-system-modules-new-object',
                         {'PSModulePath': str(helper.parent / 'Modules')}, False),
                        ('fixed-system-path-new-object',
                         {'PATH': str(helper.parents[2]) + ';' + str(helper.parent)}, False)]
            fixtures = []
            for index, (label, extra, activator) in enumerate(variants):
                shortcut = root / ('fixture-' + str(index) + '.lnk')
                environment = dict(baseline, CARR_STANDARD_SHORTCUT=str(shortcut),
                                   CARR_STANDARD_TARGET=str(target), **extra)
                retain(observe(label, lambda: run(command(activator), environment)))
                if shortcut.is_file() and 0 < shortcut.stat().st_size <= 256 * 1024:
                    fixtures.append(shortcut)
            if fixtures:
                selected = fixtures[0]
                retain(observe('unchanged-current-shortcut-target',
                               lambda: host._shortcut_target(selected, root) == target))
            else:
                retain({'label': 'unchanged-current-shortcut-target', 'status': 'NOT_RUN',
                        'reason': 'No completed owned synthetic shortcut fixture.'})
        report['status'] = 'DIAGNOSTIC_COMPLETE'
        report['temporary_shortcuts_removed'] = True
    finally:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
