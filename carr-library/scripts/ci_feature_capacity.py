"""Strict new-feature acceptance alongside unchanged complete Windows gates.

Only the explicit added feature/capacity modules run here. The pre-existing
source and complete-bundle selectors keep their original steps and deadlines.
This helper never selects a model, voice, external provider or user library.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import csv
from hashlib import sha256
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import sysconfig
import tempfile
import xml.etree.ElementTree as ET

SUITES = ["tests/audio/test_director_effects.py","tests/storage/test_director_effects_backup.py","tests/studio/test_interchange.py","tests/storage/test_cue_interchange_backup.py","tests/assistant/test_translation_partition.py","tests/assistant/test_translation_layout.py","tests/assistant/test_translation_epub_layout.py","tests/ui/test_translation_layout.py","tests/storage/test_translation_layout_backup.py","tests/e2e/test_backup_resource_cli.py","tests/storage/test_backup_budget.py","tests/storage/test_backup_capacity.py","tests/storage/test_backup_resource_roundtrip.py","tests/ui/test_backup_resource_actions.py","tests/e2e/test_import_batch_cli.py","tests/e2e/test_streaming_import_pages.py","tests/ingest/test_import_budget.py","tests/ingest/test_import_budget_worker.py","tests/ingest/test_import_scan_pages.py","tests/ingest/test_import_session.py","tests/storage/test_import_capacity_backup.py","tests/ui/test_import_resources.py","tests/speech/test_reading_pauses.py"]
NAMES = ["carr_library.__main__","carr_library.application","carr_library.assistant.translation","carr_library.assistant.translation_epub_layout_worker","carr_library.assistant.translation_layout","carr_library.assistant.translation_layout_worker","carr_library.assistant.translation_partition","carr_library.audio.cleanup","carr_library.audio.director","carr_library.audio.dsp","carr_library.audio.effect_controls","carr_library.audio.effects","carr_library.audio.projects","carr_library.backup_budget","carr_library.backup_compat","carr_library.backup_incremental","carr_library.backup_incremental_cli","carr_library.backup_options","carr_library.backup_producers","carr_library.backup_report","carr_library.backup_sidecars","carr_library.durable_state","carr_library.ingest.batch_cli","carr_library.ingest.batch_pages","carr_library.ingest.batch_scan","carr_library.ingest.batches","carr_library.ingest.catalog_asset_copy","carr_library.ingest.resource_budget","carr_library.ingest.session","carr_library.ingest.streamed_ingest","carr_library.ingest.streamed_text","carr_library.ingest.worker","carr_library.installation_lease","carr_library.library_backup","carr_library.pending_backup","carr_library.state_recovery","carr_library.storage.revisions","carr_library.streamed_import_cli","carr_library.studio.backup_inputs","carr_library.studio.interchange","carr_library.studio.project","carr_library.studio.publishing","carr_library.studio.render","carr_library.translation_cli","carr_library.ui.audio_effects","carr_library.ui.audio_studio","carr_library.ui.backup_actions","carr_library.ui.backup_incremental_dialog","carr_library.ui.backup_resources","carr_library.ui.desktop_app","carr_library.ui.effects_dialog","carr_library.ui.import_center","carr_library.ui.import_resources","carr_library.ui.translation_dialog","carr_library.speech.focus","carr_library.speech.navigation","carr_library.speech.runtime","carr_library.speech.session","carr_library.ui.reader_synthesis","carr_library.ui.reader_tabs","carr_library.ui.speech_dialog"]
TRANSLATION_NAMES = {
    'carr_library.assistant.profiles', 'carr_library.assistant.translation',
    'carr_library.assistant.providers', 'carr_library.ui.translation_dialog',
    'carr_library.assistant.translation_layout', 'carr_library.assistant.translation_partition',
    'carr_library.assistant.translation_layout_worker',
    'carr_library.assistant.translation_epub_layout_worker',
}
CAPACITY_NAMES = {
    'carr_library.' + name for name in (
        'application', 'durable_state', 'library_backup', 'pending_backup',
        'backup_incremental', 'backup_budget', 'backup_options', 'backup_sidecars',
        'audio.cleanup', 'audio.process', 'catalog.intake', 'ingest.batches',
        'ingest.batch_pages', 'ingest.batch_scan', 'ingest.resource_budget',
        'ingest.streamed_text', 'ingest.streamed_ingest', 'ingest.pipeline',
        'ingest.worker', 'ingest.session', 'ingest.retained_assets',
        'storage.revisions', 'ui.desktop_app', 'ui.import_center',
        'ui.import_resources', 'ui.backup_resources', 'ui.backup_actions',
        'ui.backup_incremental_dialog')
}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def inventory(directory):
    assert directory.is_dir() and not directory.is_symlink(), str(directory)
    result = {}
    for path in sorted(directory.rglob('*.py')):
        assert path.is_file() and not path.is_symlink(), str(path)
        assert len(result) < 4096, 'Unexpected Python namespace growth.'
        result[path.relative_to(directory).as_posix()] = digest(path)
    assert '__init__.py' in result
    return result


def checkout_head(root, expected):
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root.parent,
        check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    assert re.fullmatch(r'[0-9a-f]{40}', expected) and result == expected, (result, expected)
    return result


def installed_record(installed, expected):
    candidates = []
    for distribution in importlib.metadata.distributions(path=[str(installed.parent)]):
        files = distribution.files or ()
        if any(str(path).startswith('carr_library/') for path in files):
            candidates.append(distribution)
    assert len(candidates) == 1, 'Expected one installed distribution owning carr_library.'
    distribution, = candidates
    assert distribution.metadata['Name'] == 'universal-research-library'
    assert Path(distribution.locate_file('carr_library')).resolve() == installed
    text = distribution.read_text('RECORD')
    assert text is not None and len(text) <= 8 * 1024**2
    rows = {}
    for name, checksum, size in csv.reader(text.splitlines()):
        assert name not in rows, name
        rows[name] = (checksum, size)
    for relative, checksum in expected.items():
        value, size = rows['carr_library/' + relative]
        encoded = base64.urlsafe_b64encode(bytes.fromhex(checksum)).decode('ascii').rstrip('=')
        assert value == 'sha256=' + encoded and size == str((installed / relative).stat().st_size), relative
    return sha256(text.encode('utf-8')).hexdigest()


def origins(selected, expected):
    package = importlib.import_module('carr_library')
    assert Path(package.__file__).resolve() == selected / '__init__.py'
    result = {}
    for name in NAMES:
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin, name
        relative = Path(*name.split('.')[1:]).with_suffix('.py')
        path = Path(spec.origin).resolve()
        assert path == selected / relative, (name, str(path), str(selected / relative))
        checksum = digest(path)
        assert checksum == expected[relative.as_posix()], name
        result[name] = {'path': str(path), 'sha256': checksum}
    return result


def bundle_verifier(bundle, head):
    manifest_path = bundle / 'bundle-manifest.json'
    before = manifest_path.read_bytes()
    manifest = json.loads(before)
    assert manifest['source_commit'] == head
    assert all(manifest[key] is True for key in
        ('bundled_model', 'bundled_ocr', 'bundled_native_runtime', 'bundled_audio'))
    spec = importlib.util.spec_from_file_location('feature_bundle_verifier', bundle / 'install_windows.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.verify(bundle)
    return verifier, before


VOICE_PATHS = {
    'runtime_settings.py', 'speech/runtime.py', 'ui/voice_preferences.py',
    'language_qualification/catalog.py', 'speech/session.py', 'ui/reader_synthesis.py',
    'ui/speech_dialog.py', 'ui/reader_tabs.py', 'state_recovery.py', 'backup_producers.py',
    'speech/navigation.py', 'speech/focus.py',
}


def reader_pause_receipt(root, path, selected, expected, *, installed):
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    assert value['status'] == 'PASS' and value['phase'] == 'completed'
    assert value['installed'] is installed and value['native'] is True
    assert type(value['checks']) is list and value['checks']
    assert all(value[key] == 'NOT_RUN' for key in ('acoustic_quality', 'human_listening', 'NVDA'))
    assert value['reading_pauses'] == {
        'status': 'PASS', 'checks': 52, 'sentence_ms': 125, 'paragraph_ms': 250,
        'section_ms': 500, 'pcm_frames': 8600, 'pcm_rate': 8000,
        'native_keyboard': True, 'playback': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    assert set(value['modules']) == VOICE_PATHS
    for relative, row in value['modules'].items():
        assert Path(row['path']).resolve() == selected / relative
        assert row['sha256'] == expected[relative]
        assert digest(selected / relative) == row['sha256']
    return {'report_sha256': digest(path), 'reading_pause_checks': 52,
            'total_checks': len(value['checks']), 'modules': value['modules'],
            'native_keyboard': 'PASS', 'physical_audio': 'NOT_RUN', 'NVDA': 'NOT_RUN'}


def native_receipts(root, report, installed, expected, reference):
    translation_path = report / 'bundle-formatted-translation.json'
    translation = json.loads(translation_path.read_text(encoding='utf-8-sig'))
    assert translation['status'] == 'PASS' and translation['phase'] == 'complete'
    assert len(translation['checks']) == 130 and translation['synthetic_rest_calls'] == 9
    assert translation['native_keyboard'] == 'PASS' and translation['source_override'] is False
    assert all(translation[name] == 'NOT_RUN' for name in
        ('live_google_api', 'billing_and_quality', 'native_approval_and_file_pickers', 'manual_nvda'))
    assert set(translation['origins']) == set(translation['module_sha256']) == TRANSLATION_NAMES
    for name in TRANSLATION_NAMES:
        relative = Path(*name.split('.')[1:]).with_suffix('.py')
        assert Path(translation['origins'][name]).resolve() == installed / relative
        assert translation['module_sha256'][name] == expected[relative.as_posix()]
    exports = translation['structured_exports']
    assert len(exports) == 2 and {entry['format'] for entry in exports} == {'docx', 'epub'}
    for entry in exports:
        assert entry['provider_calls_during_export'] == 0 and entry['quotation_authority'] is False
        assert entry['output_bytes'] > 0
        assert entry['source_partition']['profile'] in ('carr-docx-body-layout/1', 'carr-epub-xhtml-layout/1')
        assert all(re.fullmatch(r'[0-9a-f]{64}', entry[name]) for name in ('source_sha256', 'output_sha256'))

    capacity_path = report / 'bundle-native-backup-import.txt'
    capacity = json.loads(capacity_path.read_text(encoding='utf-8-sig').strip().splitlines()[-1])
    assert capacity['result'] == 'PASS' and type(capacity['checks']) is int and capacity['checks'] > 0
    for name in ('native_keyboard', 'actual_scan_page_continuation', 'actual_snapshot_then_native_cancel',
                 'full_incremental_verify_restore', 'independent_checksum_and_required_base_folders',
                 'same_desktop_executor', 'restored_original_bytes_and_verified_copy'):
        assert capacity[name] == 'PASS', name
    assert capacity['native_full_backup_format'] == 5 and capacity['reviewed_import_original_mib'] == 128
    assert all(capacity[name] == 'NOT_RUN' for name in ('native_folder_picker', 'physical_output', 'NVDA'))
    assert capacity['flat4_compatibility'] == 'SEPARATE_PORTABLE_GATE'
    assert capacity['large_original_capacity'] == 'SEPARATE_API_GATE'
    probe = root / 'scripts/windows_backup_import_access_smoke.py'
    assert capacity['probe_identity'] == {str(probe): digest(probe)}
    identity = capacity['source_identity']
    detailed = {row['path'].removeprefix('carr_library/'):
                {'sha256': row['sha256'], 'bytes': row['bytes']} for row in reference['runtime_rows']}
    assert identity['python_inventory'] == detailed and identity['python_files'] == len(expected)
    assert Path(identity['installed_root']).resolve() == installed
    assert Path(identity['source_root']).resolve() == root / 'src/carr_library'
    assert set(identity['modules']) == CAPACITY_NAMES
    for name, row in identity['modules'].items():
        relative = Path(*name.split('.')[1:]).with_suffix('.py')
        assert Path(row['path']).resolve() == installed / relative
        assert {key: row[key] for key in ('sha256', 'bytes')} == detailed[relative.as_posix()]
    reading = reader_pause_receipt(root, report / 'bundle-voice-preferences.json',
        installed, expected, installed=True)
    return {'reading_pauses': reading, 'translation_report_sha256': digest(translation_path),
            'capacity_report_sha256': digest(capacity_path),
            'translation_checks': 130, 'capacity_checks': capacity['checks'],
            'structured_exports': exports, 'native_keyboard': 'PASS',
            'NVDA': 'NOT_RUN', 'live_google_api': 'NOT_RUN', 'physical_audio': 'NOT_RUN'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('source', 'installed', 'native'), required=True)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--head', required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    assert bool(sys.flags.isolated) == (args.mode != 'source')
    assert sys.dont_write_bytecode
    assert not os.environ.get('PYTHONPATH') and not os.environ.get('PYTEST_ADDOPTS') and not os.environ.get('PYTEST_PLUGINS')
    assert (args.bundle is not None) == (args.mode != 'source')
    root = Path(__file__).resolve().parents[1]
    assert Path.cwd().resolve() == root
    source = (root / 'src/carr_library').resolve()
    installed = (Path(sysconfig.get_path('purelib')) / 'carr_library').resolve()
    assert installed != source and not installed.is_relative_to(source)
    selected = source if args.mode == 'source' else installed
    reference_path = root / 'review/capacity-python-reference-2026-11-28.json'
    reference_before = digest(reference_path)
    reference = json.loads(reference_path.read_text(encoding='utf-8'))
    expected = reference['compact_mapping']
    assert inventory(source) == inventory(installed) == expected
    record_before = installed_record(installed, expected)
    head = checkout_head(root, args.head)
    helper_before = digest(Path(__file__))
    policy_path = root / 'scripts/ci_check.py'
    policy_before = digest(policy_path)
    # Same interpreter-level offline and telemetry policy as the unchanged source gate.
    os.environ.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_TELEMETRY='1',
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    tests_before = {suite: digest(root / suite) for suite in SUITES}
    probes_before = {name: digest(root / 'scripts' / name) for name in
        ('windows_translation_v3_smoke.py', 'windows_backup_import_access_smoke.py',
         'windows_voice_preferences_smoke.py', 'windows_clipboard_reader_smoke.py')}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    result = {'status': 'FAIL', 'mode': args.mode, 'commit': head,
              'helper_sha256': helper_before, 'reference_sha256': reference_before,
              'offline_policy_source_sha256': policy_before,
              'package_python_files': len(expected), 'test_files': tests_before,
              'probe_files': probes_before, 'RECORD_sha256': record_before,
              'manual_NVDA': 'NOT_RUN'}
    with ExitStack() as lifetime:
        verifier, manifest_before = None, None
        if args.bundle is not None:
            bundle = args.bundle.resolve(strict=True)
            assert installed == bundle / '.venv/Lib/site-packages/carr_library'
            verifier, manifest_before = bundle_verifier(bundle, head)
            from carr_library.native_runtime import activate_bundled_runtime
            from carr_library.audio.runtime import activate_bundled_audio_runtime
            native = activate_bundled_runtime(bundle_root=bundle)
            assert native is not None
            lifetime.enter_context(native)
            audio = activate_bundled_audio_runtime(bundle_root=bundle)
            assert audio is not None
            lifetime.enter_context(audio)
            os.environ['PATH'] = str(bundle / 'ocr') + os.pathsep + os.environ.get('PATH', '')
            os.environ['TESSDATA_PREFIX'] = str(bundle / 'ocr/tessdata')
        if args.mode == 'native':
            result['origins'] = origins(selected, expected)
            result['native'] = native_receipts(root, args.report_dir, installed, expected, reference)
        else:
            import pytest
            class Proof:
                before = None
                collected = None
                expected_failure_reports = None
                durations = None

                def pytest_sessionstart(self, session):
                    self.before = origins(selected, expected)
                    self.expected_failure_reports, self.durations = [], {suite: 0.0 for suite in SUITES}

                def pytest_collectreport(self, report):
                    if report.failed:
                        print('FEATURE_CAPACITY_COLLECTION_FAILURE ' + str(report.longrepr)[:12000], flush=True)

                def pytest_collection_finish(self, session):
                    self.collected = {suite: 0 for suite in SUITES}
                    for item in session.items:
                        suite = item.nodeid.split('::', 1)[0].replace('\\', '/')
                        assert suite in self.collected, suite
                        self.collected[suite] += 1
                    assert all(self.collected.values()), self.collected

                def pytest_runtest_logreport(self, report):
                    if report.failed:
                        print('FEATURE_CAPACITY_CASE_FAILURE ' + report.nodeid + '\n' + str(report.longrepr)[:12000], flush=True)
                    if hasattr(report, 'wasxfail'):
                        self.expected_failure_reports.append(report.nodeid)
                    suite = report.nodeid.split('::', 1)[0].replace('\\', '/')
                    assert suite in self.durations
                    self.durations[suite] += report.duration

            proof = Proof()
            report_path = args.report_dir / ('additional-' + args.mode + '.xml')
            options = ['-vv', '-s', '--durations=20', '--import-mode=importlib',
                '-o', 'addopts=', '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=60']
            if args.mode == 'installed':
                options += ['-o', 'pythonpath=']
            with tempfile.TemporaryDirectory(prefix='carr-additional-') as temporary:
                try:
                    policy_source = runpy.run_path(str(policy_path))['PYTEST_CHILD']
                    call = 'status = pytest.main(sys.argv[1:], plugins=[policy])'
                    assert policy_source.count(call) == 1, 'Existing child policy shape changed; review the adapter.'
                    # Preserve every original policy statement and verdict; append only
                    # the exact-origin/collection/timing observer to its pytest call.
                    execution = policy_source.replace(call,
                        'status = pytest.main(sys.argv[1:], plugins=[policy, _additional_proof])')
                    namespace = {'__name__': '__main__', '_additional_proof': proof}
                    argv_before = sys.argv
                    sys.argv = ['-c', *options, '--junitxml', str(report_path),
                        '--basetemp', str(Path(temporary) / 'pytest'), *SUITES]
                    try:
                        try:
                            exec(compile(execution, '<existing-offline-pytest-policy>', 'exec'), namespace)
                        except SystemExit as error:
                            status = error.code
                        else:
                            raise AssertionError('Existing offline policy did not return its verdict.')
                    finally:
                        sys.argv = argv_before
                    assert not namespace['policy'].seen, 'Existing policy rejected XFAIL/XPASS.'
                finally:
                    from carr_library.audio.cleanup import drain_pending_cleanup
                    interruption = drain_pending_cleanup()
                    if interruption is not None:
                        raise interruption
                cases = list(ET.parse(report_path).iter('testcase'))
                result.update(pytest_status=int(status), cases=len(cases),
                    selected_suites=proof.collected, module_seconds=proof.durations, origins=proof.before,
                    expected_failure_reports=proof.expected_failure_reports, junit_sha256=digest(report_path))
                # Preserve exact negative observations before the strict assertions.
                (args.report_dir / ('additional-' + args.mode + '-observed.json')).write_text(
                    json.dumps(result, sort_keys=True, indent=2) + '\n', encoding='utf-8')
                assert status == 0 and cases and proof.before is not None
                assert proof.collected and all(proof.collected.values())
                assert not proof.expected_failure_reports
                assert all(all(case.find(tag) is None for tag in ('failure', 'error', 'skipped')) for case in cases)
                assert origins(selected, expected) == proof.before
        assert inventory(source) == inventory(installed) == expected
        assert installed_record(installed, expected) == record_before
        assert checkout_head(root, args.head) == head
        assert digest(Path(__file__)) == helper_before and digest(reference_path) == reference_before
        assert digest(policy_path) == policy_before
        assert {suite: digest(root / suite) for suite in SUITES} == tests_before
        assert {name: digest(root / 'scripts' / name) for name in probes_before} == probes_before
        if verifier is not None:
            assert (bundle / 'bundle-manifest.json').read_bytes() == manifest_before
            verifier.verify(bundle)
            result['bundle_manifest_sha256'] = sha256(manifest_before).hexdigest()
        result['status'] = 'PASS'
    output = args.report_dir / ('additional-' + args.mode + '-qualification.json')
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'PASS', 'mode': args.mode, 'commit': head,
                      'python_files': len(expected), 'cases': result.get('cases'),
                      'native': result.get('native'), 'manual_NVDA': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
