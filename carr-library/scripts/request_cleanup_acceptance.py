"""Exact Windows source/installed cleanup and canonical provider/data regressions."""
from argparse import ArgumentParser
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / 'scripts' / 'request_cleanup_oracle.json'
ORACLE = json.loads(ORACLE_PATH.read_text(encoding='utf-8'))
spec = importlib.util.spec_from_file_location(
    '_request_regression_base', ROOT / 'scripts' / 'audio_batch_acceptance.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
base.ORACLE_PATH, base.ORACLE = ORACLE_PATH, ORACLE
base.SUPPORT = (*base.SUPPORT, 'tests/ui/test_operation_progress.py',
                'tests/ui/test_keyboard_manager.py',
                'tests/conversion/test_office_selection.py',
                'tests/conversion/fixtures/ordered_slides.pptx',
                'tests/audio/test_music_adapters.py',
                'tests/ingest/test_worker_publication_boundary.py',
                'tests/storage/backup_compat_fixture.py',
                'tests/ui/test_import_center.py',
                'tests/extract/test_document_formats_xml.py',
                'tests/conversion/test_legacy_adapters.py',
                'tests/conversion/legacy_fixtures.py')
read, write, require, file_hash = base.read, base.write, base.require, base.file_hash
source_identity, installed_identity = base.source_identity, base.installed_identity
runtime_identity, population = base.runtime_identity, base.population
checkout_inventory, source_python_inventory = base.checkout_inventory, base.source_python_inventory


def tests(mode, results, work):
    require(sys.platform == 'win32', 'These regressions require actual native Windows execution.')
    result = base.tests(mode, results, work)
    path = results / (mode + '-gate.json')
    report = read(path)
    report['qualification_adapter_sha256'] = file_hash(__file__)
    write(path, report)
    return result


def finalize(results):
    gate = {'schema': 1, 'status': 'FAIL', 'source_cases': ORACLE['total_cases'],
            'installed_cases': ORACLE['total_cases'], 'oracle_sha256': file_hash(ORACLE_PATH),
            'qualification_adapter_sha256': file_hash(__file__),
            'native_gui': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    try:
        require(sys.platform == 'win32', 'Actual Windows qualification is required.')
        source = source_identity(results)
        installed = installed_identity(results)
        runtime = runtime_identity()
        gate.update(source)
        for mode in ('source', 'installed'):
            phase = read(results / (mode + '-gate.json'))
            require(os.environ.get(mode.upper() + '_OUTCOME') == 'success'
                    and phase['status'] == 'PASS' and phase['pytest_exit'] == 0
                    and phase['actual_cases'] == ORACLE['total_cases'],
                    'A required test phase failed, timed out, skipped, or did not run.')
            require(all(phase[key] == source[key] for key in source)
                    and phase['wheel_sha256'] == installed['wheel_sha256']
                    and phase['oracle_sha256'] == gate['oracle_sha256']
                    and phase['actual_ffmpeg'] == runtime,
                    'A test phase belongs to another source, wheel, oracle, or runtime.')
            require(population(results / (mode + '-junit.xml')) == phase['case_counter'],
                    'The retained actual JUnit population changed.')
            require(file_hash(results / (mode + '-interpreter.json')) == phase['interpreter_sha256'],
                    'The test interpreter identity receipt changed.')
            checkout_path = results / (mode + '-checkout.json')
            require(file_hash(checkout_path) == phase['checkout_manifest_sha256'],
                    'The retained complete source inventory receipt changed.')
            checkout = read(checkout_path)
            require(checkout['source_commit'] == source['source_commit']
                    and checkout['source_tree'] == source['source_tree']
                    and checkout['files'] == checkout_inventory()
                    and len(checkout['files']) == phase['tracked_source_files'],
                    'The complete tracked source inventory differs from the executed checkout.')
            expected_python = source_python_inventory(checkout['files'], installed['installed_python'])
            require(expected_python['source_only'] == phase['source_only_python'],
                    'Unshipped source diagnostics changed after execution.')
            for name, expected in phase['test_source_sha256'].items():
                require(file_hash(ROOT / name) == expected, 'A test or fixture changed after execution.')
            for name, expected in phase['runner_sha256'].items():
                require(file_hash(ROOT / 'scripts' / name) == expected, 'A runner changed after execution.')
        for mode in ('source', 'installed'):
            require(read(results / (mode + '-gate.json'))['qualification_adapter_sha256']
                    == gate['qualification_adapter_sha256'], 'The qualification adapter changed.')
        gate.update(status='PASS', wheel_sha256=installed['wheel_sha256'],
                    actual_ffmpeg=runtime, actual_windows_file_handles='PASS',
                    previously_failing_cases_retained=55, new_boundary_cases=3,
                    canonical_provider_cases=ORACLE.get('canonical_provider_cases', 0),
                    canonical_preserved_cases=ORACLE.get('canonical_preserved_cases', 0),
                    canonical_progress_cases=ORACLE.get('canonical_progress_cases', 0),
                    headless_admission_cases=ORACLE.get('headless_admission_cases', 0),
                    canonical_state_cases=ORACLE.get('canonical_state_cases', 0),
                    ffmpeg_process_boundary_cases=ORACLE.get('ffmpeg_process_boundary_cases', 0),
                    canonical_progress_numeric_cases=ORACLE.get('canonical_progress_numeric_cases', 0),
                    canonical_conversion_cases=ORACLE.get('canonical_conversion_cases', 0),
                    canonical_studio_keyboard_cases=ORACLE.get('canonical_studio_keyboard_cases', 0),
                    office_workspace_cases=ORACLE.get('office_workspace_cases', 0),
                    canonical_ocr_manifest_cases=ORACLE.get('canonical_ocr_manifest_cases', 0),
                    completed_speech_checkpoint_cases=ORACLE.get('completed_speech_checkpoint_cases', 0),
                    canonical_process_signal_cases=ORACLE.get('canonical_process_signal_cases', 0),
                    canonical_completed_import_binding_cases=ORACLE.get('canonical_completed_import_binding_cases', 0),
                    canonical_presentation_fixture_cases=ORACLE.get('canonical_presentation_fixture_cases', 0),
                    chm_publisher_recovery_cases=ORACLE.get('chm_publisher_recovery_cases', 0),
                    canonical_dordiai_cases=ORACLE.get('canonical_dordiai_cases', 0))
    except Exception as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__)),
              file=sys.stderr, flush=True)
        gate['reason'] = type(error).__name__ + ': ' + str(error)
    write(results / 'request-cleanup-gate.json', gate)
    print(json.dumps(gate, ensure_ascii=False), flush=True)
    return 0 if gate['status'] == 'PASS' else 1


def main():
    parser = ArgumentParser()
    parser.add_argument('phase', choices=('source', 'installed', 'finalize'))
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    results = args.results.resolve(strict=True)
    if args.phase == 'finalize':
        return finalize(results)
    require(args.work is not None, 'The exact fresh qualification workspace is required.')
    return tests(args.phase, results, args.work.resolve(strict=True))


if __name__ == '__main__':
    raise SystemExit(main())
