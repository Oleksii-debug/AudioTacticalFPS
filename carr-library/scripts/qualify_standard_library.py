"""Exact installed audio-free library workflows, using the existing strict runner.

This CI helper requires native Windows and the selected generation's interpreter.
It never imports product code from the checkout. Test fixtures are synthetic;
no NVDA, real-corpus or audio-inference qualification is inferred.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from zipfile import ZipFile

SUITES = [
    "tests/e2e/test_semantic_runtime.py",
    "tests/e2e/test_import_worker.py",
    "tests/e2e/test_epub_import.py",
    "tests/e2e/test_academic_workflow.py",
    "tests/e2e/test_notebook_research_workflow.py",
    "tests/citations/test_csl.py::test_all_seven_real_processors_render_same_metadata"
]
EXPECTED = {
    "tests.e2e.test_semantic_runtime": {
        "test_actual_multilingual_ranker_and_complete_token_coverage": 1,
        "test_actual_application_semantic_scope_and_original_quotes": 1,
        "test_long_source_tail_is_not_silently_truncated": 1,
        "test_overlong_query_has_explicit_lexical_fallback": 1,
        "test_manifest_cannot_approve_another_model": 1,
        "test_linked_model_directory_is_rejected": 1,
        "test_actual_persistent_embeddings_reopen_without_reinference": 1,
        "test_actual_semantic_snapshot_survives_concurrent_membership_revocation": 1
    },
    "tests.e2e.test_import_worker": {
        "test_native_parsers_run_in_a_separate_process_and_persist_fingerprint": 1,
        "test_cancelled_native_request_can_resume_after_original_disappears": 1,
        "test_worker_deadline_returns_without_hanging_the_caller": 1,
        "test_lost_worker_reply_after_commit_recovers_idempotently": 2,
        "test_resume_rejects_changed_runtime_configuration": 1,
        "test_parallel_resume_cannot_overwrite_request_completion": 1,
        "test_request_scope_and_malformed_pdf_fail_closed": 1,
        "test_worker_rejects_unbounded_or_unknown_protocol_fields": 1,
        "test_scanned_pdf_requires_explicit_review_for_each_exact_page_before_publication": 1,
        "test_reviewed_import_rejects_changed_approval_without_publishing": 1
    },
    "tests.e2e.test_epub_import": {
        "test_epub_worker_preserves_order_metadata_and_unverified_logical_locations": 1,
        "test_repeated_spine_occurrences_remain_distinct_logical_segments": 1,
        "test_epub_sections_share_exact_locations_with_reading_desk_bookmarks": 1,
        "test_epub_open_callback_rechecks_archived_scope": 2,
        "test_epub_original_survives_reopen_and_backup_restore_exactly": 1,
        "test_epub_same_ids_and_assets_remain_isolated_between_libraries": 1,
        "test_exact_epub_replay_reuses_revision_and_adds_only_requested_memberships": 1,
        "test_changed_epub_requires_distinct_edition_and_preserves_old_selected_source": 1,
        "test_epub_commit_survives_lost_worker_reply_and_missing_original_path": 1,
        "test_cancelled_epub_has_no_partial_catalog_and_can_resume": 1,
        "test_unsupported_primary_epub_content_cannot_publish_partial_catalog": 1,
        "test_interrupted_epub_publication_rolls_back_catalog_then_public_retry_succeeds": 1
    },
    "tests.e2e.test_academic_workflow": {
        "test_academic_reference_roundtrip_preserves_source_evidence": 1,
        "test_academic_import_is_atomic_and_exports_preserve_metadata": 1,
        "test_reading_views_paginate_without_losing_exported_records": 1,
        "test_duplicate_review_and_stale_edit_never_erase_research": 1,
        "test_linked_document_opens_without_search_or_quotation_authority": 1,
        "test_linked_document_checks_library_and_retained_bytes_again": 1
    },
    "tests.e2e.test_notebook_research_workflow": {
        "test_notebook_search_fresh_navigation_and_backup_restore": 1,
        "test_phrase_fields_and_exact_tag_only_search_stay_in_one_library": 1,
        "test_equal_evidence_ids_in_different_topics_remain_distinct": 1,
        "test_archived_parent_hides_descendants_until_explicitly_included": 1,
        "test_fresh_navigation_reloads_note_tags_path_and_archive_authorization": 1,
        "test_current_metadata_edits_preserve_historical_notebook_citations": 1,
        "test_cancellation_and_finite_deadlines_apply_to_search_and_fresh_navigation": 1,
        "test_source_corruption_fails_instead_of_returning_a_partial_notebook": 1,
        "test_revoked_pdf_review_blocks_full_and_fresh_notebook_results": 1,
        "test_saved_span_never_creates_a_phrase_across_source_pages": 1,
        "test_archived_library_is_not_enabled_by_the_archived_topic_option": 1
    },
    "tests.citations.test_csl": {
        "test_all_seven_real_processors_render_same_metadata": 1
    }
}
MODULES = (
    'carr_library', 'carr_library.application', 'carr_library.semantic',
    'carr_library.extract.ocr_runtime', 'carr_library.ingest.worker',
    'carr_library.library_backup', 'carr_library.reader.source_file',
    'carr_library.citations.csl', 'carr_library.search.pagination',
)
TEST_VERSIONS = {'pytest': '9.0.2', 'reportlab': '4.4.9'}
TEST_ADDITIONS = {'pytest', 'reportlab', 'iniconfig', 'pluggy', 'pygments', 'colorama'}
PROOF_FILES = (
    'scripts/qualify_standard_library.py', 'scripts/qualify_standard_library.ps1',
    'scripts/ci_check.py', 'scripts/ci_watchdog.py',
    'packaging/probe_standard_installer_windows.py', 'packaging/create_delivery.py',
    'packaging/source_wheel.py',
)
JSON_LIMIT = 4 * 1024 ** 2
TOTAL_SECONDS = 1200


def _load(path, name):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _sha(path):
    from carr_library.audio.jobs import verified_reader
    with verified_reader(path) as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _read(path):
    from carr_library.audio.jobs import read_json
    return read_json(path, limit=JSON_LIMIT)


def _write(path, value):
    encoded = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(encoded) > JSON_LIMIT:
        raise ValueError('Qualification report exceeds its fixed byte budget.')
    with Path(path).open('xb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _versions():
    result = {}
    for count, distribution in enumerate(importlib.metadata.distributions(), 1):
        if count > 512:
            raise ValueError('Installed distribution inventory exceeds its fixed budget.')
        name = re.sub(r'[-_.]+', '-', distribution.metadata['Name']).lower()
        if not name or name in result:
            raise ValueError('Duplicate installed distribution identity.')
        result[name] = distribution.version
    return result


def _origins(bundle, hashes):
    base = (bundle / '.venv' / 'Lib' / 'site-packages').resolve(strict=True)
    result = {}
    for name in MODULES:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        relative = name.replace('.', '/') + ('/__init__.py' if path.name == '__init__.py' else '.py')
        expected = base.joinpath(*relative.split('/'))
        if path != expected or hashes.get(relative) != _sha(path):
            raise AssertionError('Installed source origin or bytes differ: ' + name)
        result[name] = {'path': str(path), 'sha256': hashes[relative]}
    for name, module in tuple(sys.modules.items()):
        if name == 'carr_library' or name.startswith('carr_library.'):
            filename = getattr(module, '__file__', None)
            if filename is not None and not Path(filename).resolve().is_relative_to(base / 'carr_library'):
                raise AssertionError('Product module escaped the installed package: ' + name)
    return result


def _snapshot(bundle, project, source_commit, manifest_sha256):
    if os.name != 'nt' or not sys.flags.isolated or not sys.flags.dont_write_bytecode:
        raise RuntimeError('Run native Windows installed Python with -I -B.')
    from carr_library.audio import runtime
    from carr_library.audio.jobs import verified_reader
    from carr_library.audio.model_manifest import _local_inventory, local_path
    bundle, project = local_path(bundle), local_path(project)
    executable = bundle / '.venv' / 'Scripts' / 'python.exe'
    if not Path(sys.executable).samefile(executable):
        raise AssertionError('The qualifier is not running in the selected installed generation.')
    for value, size in ((source_commit, 40), (manifest_sha256, 64)):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(size) + '}', value):
            raise ValueError('Exact source and manifest identities are required.')
    probe = _load(project / 'packaging' / 'probe_standard_installer_windows.py', 'qualification_seal')
    raw, identity = probe._verified_seal(bundle, source_commit)
    if identity['manifest_sha256'] != manifest_sha256:
        raise AssertionError('The selected generation does not match the independently pinned manifest.')
    manifest = json.loads(raw)
    if manifest.get('bundled_audio_runtime') is not False:
        raise AssertionError('This qualification requires an explicitly audio-free payload.')
    for flag in ('bundled_python', 'bundled_model', 'bundled_ocr', 'bundled_native_runtime',
                 'bundled_native_launcher'):
        if manifest.get(flag) is not True:
            raise AssertionError('Required installed component is absent: ' + flag)
    wheels = [name for name in manifest['files']
              if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
    if len(wheels) != 1:
        raise AssertionError('Expected exactly one sealed application wheel.')
    wheel = bundle / wheels[0]
    verifier = _load(project / 'packaging' / 'source_wheel.py', 'qualification_source')
    hashes = {}
    base = bundle / '.venv' / 'Lib' / 'site-packages'
    with verified_reader(wheel) as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['files'][wheels[0]]:
            raise AssertionError('The application wheel changed after seal verification.')
        proof = verifier.verify_application_wheel(wheel, project.parent, source_commit)
        if proof['wheel_sha256'] != manifest['files'][wheels[0]]:
            raise AssertionError('The installed wheel differs from the exact Git source proof.')
        stream.seek(0)
        with ZipFile(stream) as archive:
            for item in archive.infolist():
                if not item.filename.startswith('carr_library/') or item.is_dir():
                    continue
                digest = hashlib.sha256()
                with archive.open(item) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block)
                path = local_path(base.joinpath(*item.filename.split('/')))
                runtime.regular_file(path)
                if _sha(path) != digest.hexdigest():
                    raise AssertionError('Installed product bytes differ from their sealed wheel: ' + item.filename)
                hashes[item.filename] = digest.hexdigest()
    actual = {'carr_library/' + path.relative_to(base / 'carr_library').as_posix()
              for path, info in _local_inventory(base / 'carr_library') if not path.is_dir()}
    if actual != set(hashes):
        raise AssertionError('Unexpected or missing files exist in the installed product package.')
    head = verifier._git(project.parent, 'rev-parse', '--verify', 'HEAD').decode('ascii').strip()
    if head != source_commit:
        raise AssertionError('The test/helper checkout is not the explicit current B source commit.')
    source_hashes = {}
    for relative in (*PROOF_FILES, *(value.partition('::')[0] for value in SUITES)):
        expected = verifier._git(project.parent, 'show', source_commit + ':carr-library/' + relative)
        digest = hashlib.sha256(expected).hexdigest()
        if _sha(project / relative) != digest:
            raise AssertionError('Qualification source differs from the exact B commit: ' + relative)
        source_hashes[relative] = digest
    tree = verifier._git(project.parent, 'rev-parse', '--verify', source_commit + '^{tree}').decode('ascii').strip()
    if not re.fullmatch('[0-9a-f]{40}', tree):
        raise AssertionError('The exact source tree could not be resolved.')
    return {
        'schema': 1, 'source_commit': source_commit, 'source_tree': tree,
        'manifest_sha256': manifest_sha256, 'wheel_sha256': proof['wheel_sha256'],
        'package_hashes': hashes, 'origins': _origins(bundle, hashes),
        'source_hashes': source_hashes,
        'versions': _versions(),
    }


def _preserved(before, after):
    for field in ('source_commit', 'source_tree', 'manifest_sha256', 'wheel_sha256',
                  'package_hashes', 'origins', 'source_hashes'):
        if before.get(field) != after.get(field):
            raise AssertionError('Installed qualification changed protected identity: ' + field)
    for name, version in before['versions'].items():
        if after['versions'].get(name) != version:
            raise AssertionError('Test-only installation changed a production distribution: ' + name)
    if set(after['versions']) - set(before['versions']) - TEST_ADDITIONS:
        raise AssertionError('Unexpected test-only distributions were installed.')
    if any(after['versions'].get(name) != version for name, version in TEST_VERSIONS.items()):
        raise AssertionError('Exact offline test dependencies are missing.')


def _cases(path, module, returncode):
    if returncode != 0:
        raise AssertionError('The installed suite returned a nonzero exit code: ' + module)
    cases = list(ET.parse(path).iter('testcase'))
    if not cases or any(any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases):
        raise AssertionError('Installed workflows require every case to pass without skips.')
    if any(case.get('classname') != module for case in cases):
        raise AssertionError('JUnit contains an undeclared installed test module.')
    actual = Counter(case.get('name', '').split('[', 1)[0] for case in cases)
    if actual != Counter(EXPECTED[module]):
        raise AssertionError('Installed workflow case identities/counts differ: ' + module)
    return dict(actual)


def _private_dlls(bundle):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetModuleHandleW.argtypes, kernel.GetModuleHandleW.restype = [wintypes.LPCWSTR], wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    result = {}
    for name in ('msvcp140.dll', 'msvcp140_1.dll'):
        module = kernel.GetModuleHandleW(name)
        if not module:
            continue
        value = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(module, value, len(value))
        expected = bundle / 'native-runtime' / 'bin' / name
        if not 0 < length < len(value) or not Path(value.value).samefile(expected):
            raise AssertionError('A native dependency loaded outside the verified generation: ' + name)
        result[name] = {'path': str(expected), 'sha256': _sha(expected)}
    return result


def _child(bundle, observation, baseline, baseline_sha256, pytest_child):
    from carr_library.native_runtime import activate_bundled_runtime
    bundle = Path(bundle).resolve(strict=True)
    if _sha(baseline) != baseline_sha256:
        raise AssertionError('The parent qualification binding changed.')
    binding = _read(baseline)
    handle = activate_bundled_runtime(bundle_root=bundle)
    if handle is None:
        raise AssertionError('The selected generation has no verified native runtime.')
    try:
        before = _origins(bundle, binding['package_hashes'])
        try:
            exec(pytest_child, {'__name__': '__main__'})
        finally:
            after = _origins(bundle, binding['package_hashes'])
            if after != before:
                raise AssertionError('Installed origins changed while executing tests.')
            _write(observation, {'origins': after, 'private_dlls': _private_dlls(bundle)})
    finally:
        handle.close()


def qualify(bundle, project, source_commit, manifest_sha256, baseline, baseline_sha256, report):
    from carr_library.audio.engines import _start_process, _stop
    bundle, project, report = Path(bundle).resolve(strict=True), Path(project).resolve(strict=True), Path(report)
    if _sha(baseline) != baseline_sha256:
        raise AssertionError('The pre-dependency snapshot changed.')
    before = _read(baseline)
    installed = _snapshot(bundle, project, source_commit, manifest_sha256)
    _preserved(before, installed)
    runner = runpy.run_path(str(project / 'scripts' / 'ci_check.py'))['PYTEST_CHILD']
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS',
                                  'CARR_CI_ALLOW_LOOPBACK', 'CARR_FFMPEG')}
    environment.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1',
        CARR_PROGRAM_DIR=str(bundle), CARR_TEST_MODEL_DIR=str(bundle / 'models' / 'minilm'),
        CARR_OCR_DIR=str(bundle / 'ocr'), TESSDATA_PREFIX=str(bundle / 'ocr' / 'tessdata'),
        ORT_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    environment['PATH'] = str(bundle / 'ocr') + os.pathsep + environment.get('PATH', '')
    observed, native = {}, {}
    deadline = time.monotonic() + TOTAL_SECONDS
    for index, selector in enumerate(SUITES, 1):
        path, _, selected_case = selector.partition('::')
        module = path[:-3].replace('/', '.')
        xml = report.parent / ('library-' + str(index) + '.xml')
        observation = report.parent / ('library-' + str(index) + '-origins.json')
        log = report.parent / ('library-' + str(index) + '.log')
        if any(value.exists() for value in (xml, observation, log)):
            raise FileExistsError('Use a fresh qualification evidence directory.')
        with tempfile.TemporaryDirectory(prefix='carr-installed-library-') as temporary:
            stage = Path(temporary)
            copied = stage / path
            copied.parent.mkdir(parents=True)
            shutil.copyfile(project / path, copied)
            if _sha(copied) != installed['source_hashes'][path] or _sha(copied) != _sha(project / path):
                raise AssertionError('The selected workflow source changed while staging.')
            ini = stage / 'pytest.ini'
            ini.write_text('[pytest]\naddopts=\npythonpath=\n', encoding='utf-8')
            child_code = (
                'import runpy,sys\n'
                'helper=runpy.run_path(' + repr(str(Path(__file__).resolve())) + ')\n'
                'bundle,observation,baseline,binding=sys.argv[1:5]; del sys.argv[1:5]\n'
                "helper['_child'](bundle,observation,baseline,binding," + repr(runner) + ')\n')
            remaining = min(600, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError('Installed library qualification exceeded its aggregate deadline.')
            selection = str(copied) + ('::' + selected_case if selected_case else '')
            with log.open('xb') as output:
                process = _start_process([
                    sys.executable, '-I', '-B', '-c', child_code, str(bundle), str(observation),
                    str(baseline), baseline_sha256, '-c', str(ini), '-o', 'addopts=', '-o', 'pythonpath=',
                    '-o', 'xfail_strict=true', '--import-mode=importlib', '--rootdir', str(stage),
                    '--durations=15', '--junitxml', str(xml), '--basetemp', str(stage / 'pytest'),
                    '--', selection], cwd=stage, env=environment, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, shell=False)
                try:
                    code = process.wait(timeout=remaining)
                finally:
                    _stop(process)
            observed[module] = _cases(xml, module, code)
            metadata = _read(observation)
            if metadata.get('origins') != installed['origins']:
                raise AssertionError('A child did not preserve the exact installed origins.')
            if module == 'tests.e2e.test_semantic_runtime':
                native = metadata['private_dlls']
                if set(native) != {'msvcp140.dll', 'msvcp140_1.dll'}:
                    raise AssertionError('Actual model inference did not exercise both required private native DLLs.')
    final = _snapshot(bundle, project, source_commit, manifest_sha256)
    _preserved(before, final)
    if observed != EXPECTED:
        raise AssertionError('Not every declared installed workflow completed.')
    result = {
        'schema': 'carr-installed-library-qualification/1', 'status': 'PASS',
        'source_commit': source_commit, 'source_tree': final['source_tree'],
        'manifest_sha256': manifest_sha256, 'wheel_sha256': final['wheel_sha256'],
        'passed': sum(sum(functions.values()) for functions in observed.values()),
        'failed': 0, 'skipped': 0, 'expected_failures': False, 'suites': SUITES,
        'case_counts': observed, 'origins': final['origins'], 'private_native_dlls': native,
        'qualification_source_hashes': final['source_hashes'],
        'installed_package': True, 'sealed_files_unchanged': True,
        'production_distributions_unchanged': True, 'audio_payload': False,
        'model_inference': 'PASS', 'scanned_source_review': 'PASS',
        'epub_retention': 'PASS', 'citations_and_research_backup': 'PASS',
        'manual_nvda': 'NOT_RUN', 'real_corpus_quality': 'NOT_RUN',
    }
    _write(report, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'qualify'))
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--baseline-sha256')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime(bundle_root=args.bundle.resolve(strict=True))
    if handle is None:
        raise AssertionError('The installed qualification requires its private native runtime.')
    try:
        if args.action == 'snapshot':
            _write(args.baseline, _snapshot(args.bundle, args.project, args.source_commit, args.manifest_sha256))
        else:
            if args.report is None or not args.baseline_sha256:
                parser.error('Qualification requires the original baseline digest and a fresh report.')
            result = qualify(args.bundle, args.project, args.source_commit, args.manifest_sha256,
                             args.baseline, args.baseline_sha256, args.report)
            print(json.dumps(result, sort_keys=True))
    finally:
        handle.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
