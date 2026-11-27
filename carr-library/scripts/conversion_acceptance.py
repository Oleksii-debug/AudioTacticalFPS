"""Actual optional-converter Windows qualification with exact source binding.

Run in the fresh installed Python under -I. Both required populations execute
real host tools; a skip, xfail, missing case, timeout or changed byte fails.
"""
from collections import Counter
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

EXPECTED = {
    'tests.conversion.test_selected_adapters_native': {
        'test_real_selected_pandoc_rtf_keeps_original_and_bound_receipt': 1,
        'test_real_rtf_external_field_cannot_supply_unselected_local_content': 1,
        'test_real_pandoc_output_limit_publishes_no_partial_copy': 1,
        'test_real_existing_ocr_bridge_reads_every_selected_image_frame': 2,
        'test_cancel_settles_existing_ocr_family_before_removing_its_private_files': 1,
    },
    'tests.conversion.test_office_native': {
        'test_real_selected_legacy_filter_retains_original_and_exact_provenance': 5,
        'test_selected_office_cancellation_settles_family_before_private_profile_cleanup': 1,
        'test_selected_office_text_budget_keeps_destination_empty': 1,
    },
}


OFFICE_PORTABLE = {
    "tests.conversion.test_office_selection": {
        "test_selected_inventory_checks_fixed_installed_filter_without_executing": 1,
        "test_queued_selection_refuses_new_authority_before_worker": 3,
        "test_snapshot_rechecks_earlier_component_after_last_hash": 1,
        "test_worker_admission_refuses_changed_or_incomplete_inventory": 5,
        "test_fixed_adapter_rejects_false_format_before_runtime_admission": 5,
        "test_expired_deadline_never_spawns_a_native_process": 1,
        "test_native_launch_keeps_existing_owner_and_scrubs_unselected_configuration": 1,
        "test_staging_budget_and_lookup_failures_cannot_report_complete": 4,
        "test_profile_is_per_operation_and_disables_active_content_and_external_updates": 1,
        "test_private_phase_survives_failure_without_publishing_source_or_native_logs": 3,
        "test_progress_refuses_untrusted_marker_content_and_aliased_marker": 1,
        "test_gui_launcher_is_not_silently_promoted_to_unselected_console": 1,
        "test_selected_console_manifest_binds_both_launchers_before_native_work": 1
    },
    "tests.conversion.test_office_stage_races": {
        "test_native_removed_profile_entry_does_not_abort_the_remaining_stage_census": 2,
        "test_valueerror_for_a_still_existing_stage_entry_is_never_treated_as_disappearance": 2,
        "test_permission_failure_during_missing_entry_confirmation_remains_a_failure": 1,
        "test_missing_native_entry_cannot_hide_lost_or_replaced_private_stage_root": 2,
        "test_disappearance_after_observed_policy_violation_cannot_make_stage_valid": 3,
        "test_reappeared_owned_descendant_does_not_turn_a_missing_snapshot_into_a_live_entry_refusal": 2
    }
}


def digest(path):
    with path.open('rb') as stream:
        return __import__('hashlib').file_digest(stream, 'sha256').hexdigest()


def inventory(root, *, maximum=3 * 1024**3, limit=50000):
    root = root.resolve(strict=True)
    result, total = {}, 0
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Qualification runtime contains a reparse point.')
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if len(result) >= limit or total > maximum:
                    raise ValueError('Qualification runtime exceeds its bounded inventory.')
                result[path.relative_to(root).as_posix()] = dict(bytes=info.st_size, sha256=digest(path))
            else:
                raise ValueError('Qualification runtime contains an unsupported entry.')
    if not result:
        raise ValueError('Empty qualification runtime inventory.')
    return result


def installed_identity(wheel):
    import carr_library
    import bootstrap
    prefix = Path(sys.prefix).resolve(strict=True)
    actual, origins = {}, {}
    for package in (carr_library, bootstrap):
        origin = Path(package.__file__).resolve(strict=True)
        if (not origin.is_relative_to(prefix)
                or 'site-packages' not in {part.casefold() for part in origin.parts}):
            raise ValueError('Application resolved outside the isolated installed environment.')
        origins[package.__name__] = str(origin)
        for path in origin.parent.rglob('*.py'):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(origin.parent):
                raise ValueError('Installed application source escaped its package.')
            actual[package.__name__ + '/' + path.relative_to(origin.parent).as_posix()] = digest(path)
    with ZipFile(wheel) as archive:
        expected = {name: sha256(archive.read(name)).hexdigest()
                    for name in archive.namelist()
                    if name.endswith('.py') and name.split('/')[0] in ('carr_library', 'bootstrap')}
    if not expected or actual != expected:
        raise ValueError('Installed application source differs from the exact wheel.')
    return dict(isolated=bool(sys.flags.isolated), prefix=str(prefix),
                module_origins=origins, files=actual, wheel_sha256=digest(wheel))


def strict_junit(path, expected_functions):
    cases = list(ET.parse(path).iter('testcase'))
    expected = Counter({(module, name): count for module, names in expected_functions.items()
                        for name, count in names.items()})
    observed = Counter((row.get('classname'), row.get('name', '').partition('[')[0]) for row in cases)
    if observed != expected or any(row.find(tag) is not None for row in cases
                                   for tag in ('failure', 'error', 'skipped')):
        raise ValueError('Required optional-converter case population did not pass exactly.')
    return {'cases': len(cases), 'functions': expected_functions}


def run_population(mode, checkout, output, expected_functions, selectors=None):
    if selectors is None:
        selectors = tuple(name.replace('.', '/') + '.py' for name in expected_functions)
    child = runpy.run_path(str(checkout / 'scripts/ci_check.py'))['PYTEST_CHILD']
    origins = output / (mode + '-origins.json')
    binding = r'''
import hashlib, json, pathlib, sys
import carr_library, bootstrap
expected_root = pathlib.Path(__ROOT__).resolve(strict=True)
origins = {}
for package in (carr_library, bootstrap):
    origin = pathlib.Path(package.__file__).resolve(strict=True)
    if not origin.is_relative_to(expected_root):
        raise ValueError('The test process imported a foreign application.')
    if __INSTALLED__ and ('site-packages' not in {p.casefold() for p in origin.parts} or not sys.flags.isolated):
        raise ValueError('The installed test process lost isolation.')
    origins[package.__name__] = {'path': str(origin), 'sha256': hashlib.sha256(origin.read_bytes()).hexdigest()}
pathlib.Path(__OUTPUT__).write_text(json.dumps({'isolated': bool(sys.flags.isolated), 'module_origins': origins}, sort_keys=True), encoding='utf-8')
'''
    binding = binding.replace('__ROOT__', repr(str(sys.prefix if mode == 'installed' else checkout)))
    binding = binding.replace('__INSTALLED__', repr(mode == 'installed')).replace('__OUTPUT__', repr(str(origins)))
    arguments = [sys.executable]
    if mode == 'installed':
        arguments.append('-I')
    arguments += ['-B', '-X', 'utf8', '-c', binding + '\n' + child, 'offline',
                  '-o', 'addopts=', '-o', 'pythonpath=', '-o', 'xfail_strict=true',
                  '--import-mode=importlib', '--durations=13', '--junitxml',
                  str(output / (mode + '-junit.xml')), '--basetemp', str(output / (mode + '-tmp')),
                  '--', *(str(checkout / path) for path in selectors)]
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(key, None)
    if mode == 'source':
        env['PYTHONPATH'] = os.pathsep.join((str(checkout / 'src'), str(checkout)))
    with (output / (mode + '-pytest.txt')).open('wb') as log:
        process = subprocess.run(arguments, cwd=checkout, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, timeout=900, check=False)
    if process.returncode:
        # Preserve the full raw file, and make a bounded actionable failure
        # visible in the job log without a second artifact-reader job.
        path = output / (mode + '-pytest.txt')
        # Surface fixed synthetic diagnostics even if several later tracebacks
        # displace them from the ordinary bounded failure tail.
        with path.open('rb') as stream:
            remaining, emitted = 8 * 1024**2, 0
            while remaining > 0 and emitted < 128 * 1024:
                line = stream.readline(min(32768, remaining))
                if not line:
                    break
                remaining -= len(line)
                if line.startswith(b'NATIVE_DIAGNOSTIC '):
                    print(line.decode('utf-8', 'replace').rstrip(), flush=True)
                    emitted += len(line)
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 24000))
            print(stream.read(24000).decode('utf-8', errors='replace'), flush=True)
        raise ValueError(mode + ' optional-converter pytest failed: ' + str(process.returncode))
    result = strict_junit(output / (mode + '-junit.xml'), expected_functions)
    result['origins'] = json.loads(origins.read_text(encoding='utf-8'))
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--family', choices=('selected', 'office'), required=True)
    parser.add_argument('--pandoc-root', type=Path)
    parser.add_argument('--office-root', type=Path)
    parser.add_argument('--ocr-root', type=Path)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--source-tree', required=True)
    args = parser.parse_args()
    checkout, output = args.checkout.resolve(strict=True), args.output.resolve(strict=True)
    expected_functions = {name: rows for name, rows in EXPECTED.items()
                          if ('selected_adapters' in name) == (args.family == 'selected')}
    selectors = tuple(name.replace('.', '/') + '.py' for name in expected_functions)
    count = sum(sum(rows.values()) for rows in expected_functions.values())
    report = dict(schema=1, status='RUNNING', family=args.family, source_commit=args.source_commit,
                  source_tree=args.source_tree, windows=sys.platform,
                  source='NOT_RUN', installed='NOT_RUN', manual_nvda='NOT_RUN',
                  real_user_corpus='NOT_RUN', printed_page_fidelity='NOT_QUALIFIED')
    try:
        if sys.platform != 'win32' or not sys.flags.isolated or sys.version_info[:2] != (3, 11):
            raise ValueError('This gate requires actual isolated Windows CPython3.11.')
        selections = [('CARR_PANDOC', args.pandoc_root)] if args.family == 'selected' else [('CARR_SOFFICE', args.office_root)]
        for variable, root in selections:
            path = Path(os.environ[variable])
            if not path.is_absolute() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
                raise ValueError('An explicitly selected converter is outside the prepared pinned runtime.')
        if args.family == 'selected' and Path(os.environ['CARR_OCR_DIR']).resolve(strict=True) != args.ocr_root.resolve(strict=True):
            raise ValueError('OCR is not the explicit prepared runtime.')
        spec = importlib.util.spec_from_file_location('_carr_source_wheel', checkout / 'packaging/source_wheel.py')
        source_wheel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source_wheel)
        report['wheel_source'] = source_wheel.verify_application_wheel(
            args.wheel, checkout.parent, args.source_commit)
        tree = subprocess.run(['git', 'rev-parse', args.source_commit + '^{tree}'], cwd=checkout,
                              check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        if tree != args.source_tree:
            raise ValueError('The declared source tree differs from its actual commit.')
        report['installed_identity'] = installed_identity(args.wheel)
        portable_selectors = (tuple(name.replace('.', '/') + '.py' for name in OFFICE_PORTABLE)
                              if args.family == 'office' else ())
        fixture_files = (*selectors, *portable_selectors, 'tests/conversion/native_diagnostics.py')
        report['selected_test_sha256'] = {name: digest(checkout / name) for name in fixture_files}
        report['harness_sha256'] = digest(Path(__file__))
        if args.family == 'selected':
            from carr_library.extract.ocr_runtime import verify_ocr_bundle
            runtime = verify_ocr_bundle(args.ocr_root)
            runtime.require_languages('eng', orientation=True)
            report['ocr_manifest_sha256'] = runtime.manifest_sha256
            roots = {'pandoc': args.pandoc_root, 'ocr': args.ocr_root}
        else:
            roots = {'libreoffice': args.office_root}
        report['runtime_files'] = {name: inventory(root) for name, root in roots.items()}
        # Keep initial byte evidence even if a required native case fails.
        (output / 'before-execution.json').write_text(json.dumps(report, sort_keys=True), encoding='utf-8')
        if args.family == 'office':
            portable_output = output / 'portable-office'
            portable_output.mkdir()
            report['portable_office'] = dict(source='NOT_RUN', installed='NOT_RUN', required_cases=40)
            report['portable_office']['source'] = run_population(
                'source', checkout, portable_output, OFFICE_PORTABLE)
            report['portable_office']['installed'] = run_population(
                'installed', checkout, portable_output, OFFICE_PORTABLE)
        report['source'] = run_population('source', checkout, output, expected_functions)
        report['installed'] = run_population('installed', checkout, output, expected_functions)
        if installed_identity(args.wheel) != report['installed_identity']:
            raise ValueError('Installed source changed during native qualification.')
        runtime_changes = {}
        for name, root in roots.items():
            before, after = report['runtime_files'][name], inventory(root)
            if before != after:
                added = sorted(set(after) - set(before))
                removed = sorted(set(before) - set(after))
                changed = sorted(key for key in set(before) & set(after)
                                 if before[key] != after[key])
                def details(names):
                    return [dict(path=key, before=before.get(key), after=after.get(key))
                            for key in names[:64]]
                runtime_changes[name] = dict(
                    before_files=len(before), after_files=len(after),
                    after_inventory_sha256=sha256(json.dumps(after, sort_keys=True,
                        separators=(',', ':')).encode('utf-8')).hexdigest(),
                    added_count=len(added), removed_count=len(removed), changed_count=len(changed),
                    added=details(added), removed=details(removed), changed=details(changed),
                    diagnostic_rows_per_kind_limit=64)
        report['runtime_inventory_changes'] = runtime_changes
        if runtime_changes:
            print('RUNTIME_INVENTORY_CHANGE ' + json.dumps(runtime_changes, sort_keys=True), flush=True)
            raise ValueError('Selected runtime bytes changed during native qualification.')
        if {name: digest(checkout / name) for name in fixture_files} != report['selected_test_sha256']:
            raise ValueError('Native fixture source changed during qualification.')
        report.update(status='PASS', source_cases=count, installed_cases=count,
                      qualifiers=['synthetic_fixture_reading_copy', 'original_retained',
                                  'explicit_runtime_identity', 'required_cancel_and_budget_boundaries'])
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        (output / 'converter-gate.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                                                  encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree',
                                                 'source_cases', 'installed_cases')}, sort_keys=True))


if __name__ == '__main__':
    main()
