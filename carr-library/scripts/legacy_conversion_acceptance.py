"""Require real legacy-tool fixtures and the unchanged conversion boundaries."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('_carr_conversion_acceptance', HERE / 'conversion_acceptance.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=('calibre', 'djvu', 'chm'), required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--source-tree', required=True)
    args = parser.parse_args()
    checkout, output = args.checkout.resolve(strict=True), args.output.resolve(strict=True)
    root = args.runtime_root.resolve(strict=True)
    oracle_path = HERE / 'legacy_conversion_oracles.json'
    plan = json.loads(oracle_path.read_text(encoding='utf-8'))
    native = plan['native'][args.family]
    selectors = tuple(module.replace('.', '/') + '.py::' + name
                      for module, names in native.items() for name in names)
    count = sum(sum(names.values()) for names in native.values())
    report = dict(schema=1, status='RUNNING', family=args.family, source_commit=args.source_commit,
                  source_tree=args.source_tree, source='NOT_RUN', installed='NOT_RUN',
                  native_fixtures='NOT_RUN', manual_nvda='NOT_RUN', real_user_corpus='NOT_RUN',
                  format_support_advertisement='NOT_CHANGED_BY_GATE')
    try:
        if sys.platform != 'win32' or not sys.flags.isolated or sys.version_info[:2] != (3, 11):
            raise ValueError('The legacy fixture gate requires actual isolated Windows CPython3.11.')
        variable = {'calibre': 'CARR_CALIBRE', 'djvu': 'CARR_DJVUTXT', 'chm': 'CARR_7ZIP'}[args.family]
        chosen = Path(os.environ[variable])
        if not chosen.is_absolute() or not chosen.resolve(strict=True).is_relative_to(root):
            raise ValueError('The selected executable is outside the prepared pinned runtime.')
        source_spec = importlib.util.spec_from_file_location('_legacy_source_wheel', checkout / 'packaging/source_wheel.py')
        source_wheel = importlib.util.module_from_spec(source_spec)
        source_spec.loader.exec_module(source_wheel)
        report['wheel_source'] = source_wheel.verify_application_wheel(args.wheel, checkout.parent, args.source_commit)
        tree = subprocess.run(['git', 'rev-parse', args.source_commit + '^{tree}'], cwd=checkout,
                              check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        if tree != args.source_tree:
            raise ValueError('Legacy qualification declared a different source tree.')
        report['installed_identity'] = base.installed_identity(args.wheel)
        report['runtime_files'] = base.inventory(root)
        helpers = [Path(__file__), HERE / 'conversion_acceptance.py', oracle_path,
                   checkout / 'tests/conversion/legacy_fixtures.py',
                   checkout / 'tests/conversion/native_diagnostics.py']
        tests = {checkout / (name.replace('.', '/') + '.py') for name in plan['portable']}
        tests.update(checkout / (name.replace('.', '/') + '.py') for name in native)
        report['source_files'] = {str(path.relative_to(checkout)): base.digest(path)
                                  for path in (*helpers, *sorted(tests))}
        (output / 'before-execution.json').write_text(json.dumps(report, sort_keys=True), encoding='utf-8')
        boundaries, actual = output / 'boundaries', output / 'native'
        boundaries.mkdir()
        actual.mkdir()
        for mode in ('source', 'installed'):
            report[mode] = base.run_population(mode, checkout, boundaries, plan['portable'])
            if report[mode]['cases'] != 162:
                raise ValueError('Legacy conversion boundary population is incomplete.')
        report['native_fixtures'] = {}
        for mode in ('source', 'installed'):
            report['native_fixtures'][mode] = base.run_population(mode, checkout, actual, native, selectors)
            if report['native_fixtures'][mode]['cases'] != count:
                raise ValueError('Actual legacy fixture population is incomplete.')
        if base.installed_identity(args.wheel) != report['installed_identity']:
            raise ValueError('Installed source changed during legacy qualification.')
        if base.inventory(root) != report['runtime_files']:
            raise ValueError('Selected native runtime changed during legacy qualification.')
        if {str(path.relative_to(checkout)): base.digest(path) for path in (*helpers, *sorted(tests))} != report['source_files']:
            raise ValueError('A qualification helper or fixture changed during execution.')
        report.update(status='PASS', portable_source_cases=162, portable_installed_cases=162,
                      native_source_cases=count, native_installed_cases=count,
                      required_functions=native)
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        (output / 'legacy-converter-gate.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                                                         encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
                                                 'native_source_cases', 'native_installed_cases')}, sort_keys=True))


if __name__ == '__main__':
    main()
