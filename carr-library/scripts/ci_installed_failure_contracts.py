"""Four unchanged whole modules isolated from the incomplete full installed run.

BookBatch30 runs alone first; translation20, ASR admission40 and preferences23
then run in three separate source processes. Each installed whole has its own
strict isolated process. Ordinary failures remain failures and do not hide the
other modules. This diagnostic never replaces the full installed acceptance.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import file_digest, sha1, sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from time import monotonic
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "tests/studio/test_book_batch.py": 30,
    "tests/assistant/test_translation_documents.py": 20,
    "tests/transcription/test_admission_boundaries.py": 40,
    "tests/speech/test_voice_preferences.py": 23
}
FUNCTIONS = {
    "tests/studio/test_book_batch.py": [
        "test_batch_preview_prepare_and_reopen_reuse_existing_jobs_without_speech",
        "test_actual_two_book_render_and_reopened_resume_verify_original_outputs",
        "test_changed_review_cannot_reuse_preparation_approval",
        "test_scope_duplicate_and_missing_plan_bindings_are_rejected",
        "test_source_change_in_later_book_keeps_first_outputs_and_stops_before_second",
        "test_active_control_stops_current_item_and_never_starts_later_books",
        "test_pause_between_books_retains_verified_first_book_and_resume_does_not_duplicate",
        "test_old_invocation_signal_cannot_stop_a_new_explicit_run",
        "test_observer_failure_does_not_relabel_or_repeat_completed_books",
        "test_backup_restore_preserves_order_and_requires_new_explicit_destination_review",
        "test_passive_backup_refuses_unbound_batch_membership",
        "test_plan_save_then_prepare_uses_one_lock_at_a_time",
        "test_stop_between_books_does_not_signal_a_foreign_locked_job",
        "test_failure_in_second_part_preserves_actual_first_part_receipt_and_resume",
        "test_postcommit_control_cleanup_failure_retains_results_and_does_not_repeat_audio",
        "test_primary_failure_is_not_masked_by_control_cleanup_failure",
        "test_selection_postcommit_sync_warning_requires_real_confirmation_before_jobs",
        "test_competing_same_bytes_selection_is_not_adopted_as_our_commit",
        "test_same_path_library_replacement_invalidates_existing_batch_service_before_writes",
        "test_saved_batch_inventory_never_silently_omits_unreadable_directory",
        "test_active_invocation_tamper_after_real_pcm_refuses_publication_and_later_books"
    ],
    "tests/assistant/test_translation_documents.py": [
        "test_paragraph_chunks_preserve_exact_unicode_and_all_source_separators",
        "test_paragraph_limit_is_inclusive_and_rejects_more_before_job_creation",
        "test_batch_review_and_prepare_never_contact_provider_or_mutate_originals",
        "test_old_batch_review_never_authorizes_different_selection_or_profile",
        "test_duplicate_and_modified_reading_copies_are_not_batch_sources",
        "test_actual_job_resume_and_structured_export_keep_original_lineage",
        "test_source_revocation_during_preparation_returns_already_saved_jobs",
        "test_cancel_after_first_document_preserves_completion_and_resumes_remaining_once",
        "test_changed_current_profile_blocks_new_calls_but_completed_work_stays_offline",
        "test_partial_batch_export_returns_real_committed_directories_and_never_deletes_them",
        "test_local_structured_state_uses_passive_complete_backup_and_restores_without_server",
        "test_restored_pending_batch_needs_fresh_source_approval_before_any_provider_call",
        "test_cli_document_capture_enforces_remaining_batch_budget_during_reads",
        "test_direct_local_resume_rereads_current_catalog_before_any_request"
    ],
    "tests/transcription/test_admission_boundaries.py": [
        "test_export_rejects_changed_payload_before_external_publication",
        "test_export_receipt_verification_observes_cancellation",
        "test_export_payload_is_bounded_and_interruptible",
        "test_saved_receipt_decodes_only_verified_bytes",
        "test_saved_receipt_corruption_blocks_external_export",
        "test_verified_json_preserves_strict_state_parsing",
        "test_edited_copy_decodes_only_the_bytes_it_verifies",
        "test_edited_copy_export_uses_the_captured_revision",
        "test_edited_copy_rejects_changed_bytes_and_observes_cancel",
        "test_stop_interrupts_admission_hash_and_reaches_child_cleanup",
        "test_runtime_change_rejects_attempt_without_resurrecting_cache",
        "test_failed_archive_keeps_durable_cache_rejection_until_retry",
        "test_idle_inventory_is_cancellable_without_rehashing_runtime",
        "test_actual_child_import_never_writes_or_replaces_python_bytecode",
        "test_known_runtime_rejection_takes_precedence_over_simultaneous_stop",
        "test_cancelled_post_admission_check_preserves_committed_valid_receipt",
        "test_stop_during_historical_receipt_hash_preserves_durable_results",
        "test_terminal_only_status_policy_keeps_real_child_checkpoint_progress",
        "test_terminal_status_retains_strict_schema_and_exact_budget"
    ],
    "tests/speech/test_voice_preferences.py": [
        "test_legacy_load_does_not_rewrite_bytes_or_probe_optional_engines",
        "test_reload_persists_exact_ids_profiles_and_external_declarations",
        "test_two_windows_refuse_stale_preference_save_without_overwriting",
        "test_engine_window_cannot_overwrite_a_later_voice_preference_change",
        "test_shared_settings_lock_refuses_overlapping_changes",
        "test_explicit_voice_beats_default_but_not_exact_language_guard",
        "test_profile_controls_are_not_applied_to_a_different_voice_or_language",
        "test_no_region_or_script_fallback_to_similarly_named_voice",
        "test_adapter_underscore_tag_and_request_case_use_one_exact_language",
        "test_removed_voice_and_unsupported_controls_do_not_pick_another_voice",
        "test_invalid_preferences_keep_prior_bytes_and_do_not_echo_values",
        "test_owner_guard_is_rechecked_after_latest_settings_read_before_commit"
    ]
}
PINS = {
    "tests/studio/test_book_batch.py": "860b5e112e803527add77983733b0f1833b465e5",
    "tests/assistant/test_translation_documents.py": "fc0d6148287026dbf6cb2640e5e4b0926a2d0ef8",
    "tests/transcription/test_admission_boundaries.py": "5fb6e519bfa27ea296aeee775985fc31ba4afc65",
    "tests/speech/test_voice_preferences.py": "e0993f9ed2f782cad2c8b4e9834a436dba335d45",
    "tests/assistant/test_translation_local.py": "3db04918a25fa59567e5325be789ee7039b84d59",
    "tests/transcription/fixture_runtime.py": "b4168929226bbe224e75a29f9899b3c63f237eda",
    "tests/transcription/fixtures/protocol_worker.py": "db988156c9c16504a8b82f8b65d4349086fec388",
    "tests/studio/test_book_audio.py": "f98140e413d0fb32784493045c0882011f6a081c",
    "tests/reader/test_ebook_reader.py": "39f3837d66bac1dc69ab9b2b05f9e31e6e1be849",
    "bootstrap/__init__.py": "6981b7faa1cca8d29f37fe85751c110c693dae55",
    "src/carr_library/application.py": "82af31743ca5cc6557477f6492c08b912751f4a4",
    "src/carr_library/durable_state.py": "d2b95bde04aa72cdd21ee6b4f0092d3350f84fcb",
    "src/carr_library/library_backup.py": "30ed3b152fc1cd26b338200b270e73f2e193a3f9",
    "src/carr_library/backup_producers.py": "0fb5e91a440ded8b62b806b944bdd4722f288e65",
    "src/carr_library/state_backup.py": "73e2450f8a912d0fcf70b547fe2cfe4d4750c219",
    "src/carr_library/runtime_settings.py": "00df790d393f9d54f87d0dcf891e6506916437a1",
    "src/carr_library/speech/runtime.py": "f5c43ef5f03af626f44096be0a2b10f034c09c80",
    "src/carr_library/speech/reading.py": "8fcebccb61d1e0a3b616928a75a22f3e9306ed77",
    "src/carr_library/speech/source.py": "cef32840ecbb5b07c0313e206d9bf622ee20db87",
    "src/carr_library/speech/marks.py": "5f2459052d21d0c358641738cb595b265ef01a48",
    "src/carr_library/catalog/intake.py": "8518f012491b542a57593af75993907983ddda0d",
    "src/carr_library/operations/broker.py": "f09561333d33ae68480260668ec05b617cd72213",
    "src/carr_library/operations/_locks.py": "ae161b8483f84b2a4aa2f153b4b11b08f76b9693",
    "src/carr_library/audio/models.py": "35694ae7cb9ad00e6a37ee11e18cdf60c6b2d7f6",
    "src/carr_library/audio/process.py": "480272d0224770f6b0ec9c8efc8e70d9aab42b0b",
    "src/carr_library/audio/windows_job.py": "27aeb47c39660ac74335d791e22e58b44058db3b",
    "src/carr_library/audio/cleanup.py": "77d69f8406db93dc0ff6110b9597ad805bd92dc5",
    "src/carr_library/assistant/models.py": "1700d0191b4a27b09d18bb6e231b822cf183bd8a",
    "src/carr_library/assistant/profiles.py": "a3adbe7cc693bc8a3972144007ee15cc85b3b200",
    "src/carr_library/assistant/providers.py": "caf3b41d5ec7f7e7abe28d33b44cf0307d48f1be",
    "src/carr_library/assistant/translation.py": "29324c1891c4e1fc278f410af6221e50ae3f9b04",
    "src/carr_library/assistant/translation_local.py": "6be65ce3dd519777343b4b014722e1ea608ac225",
    "src/carr_library/assistant/translation_documents.py": "c4b72384ddede9f7e9f20c1e175bc8c97b602b78",
    "src/carr_library/assistant/translation_cli.py": "36363f86034ec1d4c144506e78855fc81306d93c",
    "src/carr_library/transcription.py": "641ea1c9c2e983b1cc2c4042db33d42e13bcae1e",
    "src/carr_library/integrations/transcription_protocol.py": "e1f49c8ba5e928234dd80de9157403de31eacb1e",
    "src/carr_library/integrations/transcription_runtime.py": "4d73f5a609e9d1e2760072beca896464a2cb5827",
    "src/carr_library/integrations/transcription_scope.py": "19726ef6b0f239eccdcd76ceaedb7ebfcedf846c",
    "src/carr_library/integrations/transcription_values.py": "e744e7c0c0b7cbaf47d2cb926f9fca81690c54ab",
    "src/carr_library/integrations/transcription_exports.py": "873892f7dfd0a0865835bc988f3f0f719a646724",
    "src/carr_library/integrations/transcription_backup.py": "ba9889e81a8d0cf85dfc567cf0ebf8a0812a21ad",
    "src/carr_library/ingest/restore.py": "7c752b0203eb3d85bf3dfdeb2dea9a8c76013f37",
    "src/carr_library/studio/book_batch.py": "1b414a49cb44894a27a4d17d79560cc1ccddd268",
    "src/carr_library/studio/book_audio.py": "d1e54a397ea5ad22737f34aa1c7160e56759f2bb",
    "src/carr_library/studio/project.py": "63a20c64909db77b9958e9c16627b78f08050d95",
    "src/carr_library/studio/render.py": "3fafc206b592fedd3cf5c836c1b1bc7a0387e5ab",
    "src/carr_library/studio/publishing.py": "e369f0d40cbc818d9374e1d4f84bc3e28e09ad63",
    "src/carr_library/studio/managed_inputs.py": "fdc163215c53240dd6058e8f4235d2e8aa03db10",
    "src/carr_library/audio/dsp.py": "f7833b5f600e3a822a3fa2f2068661c922576230",
    "src/carr_library/reader/ebook.py": "014a056ddaaa2a671d09b169d3b6ea7f6ee59634",
    "src/carr_library/reader/reading_desk.py": "0acd7f25fb17de24b434741ff3b7cf2c634a4c8a",
    "scripts/ci_check.py": "6ad3055cfcd7c6a1b1af8544ae462c5d51adb473",
    "packaging/source_wheel.py": "478de248451d59a44b35784eb808fb2974f0af67",
    "packaging/windows-cp311.lock": "8dad99c0ea80470b7253873aee78e08857affec6",
    "packaging/windows-sync-cp311.lock": "cd9c18a24d7a7ac203d81cf6a8d82d8bcb8f8ab3",
    "packaging/windows-sync-test-cp311.lock": "e3f6eda5976e867abe7267c04e0b867b1fd16f7a",
    "packaging/trusted-client.lock": "6c4b48ac09572dc259d62d0d801057ab47c04583",
    "packaging/linux-sync-core-cp311.lock": "08f945066be748972a10d5b0ce46480fff722959",
    "packaging/prepare_windows_audio.py": "65b172ac432f28e865200aed2f8e610dda1ac7cf",
    "packaging/windows-audio.lock.json": "2bc706ffc6208f5596a10abee61d3da33f20e6c6"
}
COMPANIONS = ["tests/assistant/test_translation_local.py","tests/transcription/fixture_runtime.py","tests/transcription/fixtures/protocol_worker.py","tests/studio/test_book_audio.py","tests/reader/test_ebook_reader.py"]
MODULES = ["bootstrap","carr_library.application","carr_library.durable_state","carr_library.library_backup","carr_library.backup_producers","carr_library.state_backup","carr_library.runtime_settings","carr_library.speech.runtime","carr_library.speech.reading","carr_library.speech.source","carr_library.speech.marks","carr_library.catalog.intake","carr_library.operations.broker","carr_library.operations._locks","carr_library.audio.models","carr_library.audio.process","carr_library.audio.windows_job","carr_library.audio.cleanup","carr_library.assistant.models","carr_library.assistant.profiles","carr_library.assistant.providers","carr_library.assistant.translation","carr_library.assistant.translation_local","carr_library.assistant.translation_documents","carr_library.assistant.translation_cli","carr_library.transcription","carr_library.integrations.transcription_protocol","carr_library.integrations.transcription_runtime","carr_library.integrations.transcription_scope","carr_library.integrations.transcription_values","carr_library.integrations.transcription_exports","carr_library.integrations.transcription_backup","carr_library.ingest.restore","carr_library.studio.book_batch","carr_library.studio.book_audio","carr_library.studio.project","carr_library.studio.render","carr_library.studio.publishing","carr_library.studio.managed_inputs","carr_library.audio.dsp","carr_library.reader.ebook","carr_library.reader.reading_desk"]
BOOK_SUITE = 'tests/studio/test_book_batch.py'
ADMISSION_SUITE = 'tests/transcription/test_admission_boundaries.py'


def require(value, message):
    if not value:
        raise ValueError(message)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def sources():
    result = {}
    for relative, expected in PINS.items():
        path = ROOT / relative
        require(path.is_file() and not path.is_symlink(), 'Missing ordinary source: ' + relative)
        raw = path.read_bytes()
        actual = sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        require(actual == expected, 'Changed pinned source: ' + relative)
        result[relative] = dict(git_blob=actual, sha256=sha256(raw).hexdigest(), bytes=len(raw))
    return result


def git(*args):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(ROOT.parent), *args],
        check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def child(command, output, *, cwd, ci, timeout=600):
    environment = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTEST_PLUGINS', 'PYTEST_ADDOPTS'):
        environment.pop(key, None)
    environment.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
        ORT_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
    isolation = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
    with output.open('xb') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, **isolation)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            ci._terminate_process_tree(process)
            raise
    require(output.stat().st_size <= 8 * 1024**2, 'Required-suite log exceeds its finite bound.')
    print(output.read_text(encoding='utf-8', errors='replace'), flush=True)
    return code


INSTALLED_CHILD = r"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

wheel, proof_path, checkout, receipt, contract_path = map(Path, sys.argv[1:6])
arguments = sys.argv[6:]
contract = json.loads(contract_path.read_text(encoding='utf-8'))
proof = json.loads(proof_path.read_text(encoding='utf-8'))
assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__
assert sha256(wheel.read_bytes()).hexdigest() == proof['wheel_sha256']
import carr_library, bootstrap
package = Path(carr_library.__file__).resolve(strict=True).parent
helper = Path(bootstrap.__file__).resolve(strict=True).parent
assert package.parent == helper.parent
assert not package.is_relative_to(checkout) and not helper.is_relative_to(checkout)
assert all(not Path(p).resolve().is_relative_to(checkout) for p in sys.path if p)
payload = {}
with ZipFile(wheel) as archive:
    for name in archive.namelist():
        if name.startswith(('carr_library/', 'bootstrap/')) and not name.endswith('/'):
            payload[name] = sha256(archive.read(name)).hexdigest()
assert payload
def verify():
    rows = {}
    for relative, digest in payload.items():
        path = package.parent / relative
        assert path.is_file() and not path.is_symlink()
        assert sha256(path.read_bytes()).hexdigest() == digest, relative
    for name in contract['modules']:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        assert path.is_relative_to(package if name.startswith('carr_library') else helper)
        rows[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return rows
before = verify()
sys.argv = ['pytest', *arguments]
try:
    exec(compile(contract['pytest_child'], '<existing-ci-check-policy>', 'exec'),
         {'__name__': '__main__'})
except SystemExit as result:
    code = result.code
else:
    raise AssertionError('Existing strict pytest child did not exit explicitly.')
after = verify()
assert after == before
receipt.write_text(json.dumps({'status': 'PASS', 'origins': before,
    'wheel_sha256': proof['wheel_sha256'], 'wheel_payload_files_verified': len(payload),
    'isolated': True, 'source_injection': False, 'payload_unchanged': True},
    indent=2, sort_keys=True) + '\n', encoding='utf-8')
raise SystemExit(code)
"""

SOURCE_CHILD = r"""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys

root, contract_path, receipt = map(Path, sys.argv[1:4])
arguments = sys.argv[4:]
assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__
root = root.resolve(strict=True)
contract = json.loads(contract_path.read_text(encoding='utf-8'))
sys.path[:0] = [str(root / 'src'), str(root)]
def verify():
    rows = {}
    for name in contract['modules']:
        relative = ('bootstrap/__init__.py' if name == 'bootstrap'
                    else 'src/' + name.replace('.', '/') + '.py')
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve(strict=True)
        expected = (root / relative).resolve(strict=True)
        assert actual == expected
        digest = sha256(actual.read_bytes()).hexdigest()
        assert digest == contract['source_hashes'][relative]
        rows[name] = {'path': str(actual), 'sha256': digest}
    return rows
before = verify()
sys.argv = ['pytest', *arguments]
try:
    exec(compile(contract['pytest_child'], '<existing-ci-check-policy>', 'exec'),
         {'__name__': '__main__'})
except SystemExit as result:
    code = result.code
else:
    raise AssertionError('The actual strict pytest child did not exit explicitly.')
after = verify()
assert before == after
receipt.write_text(json.dumps({'status': 'PASS', 'mode': 'source',
    'origins': before, 'unchanged': True}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
raise SystemExit(code)
"""


def audio_inputs(selected):
    require(os.name == 'nt', 'This diagnostic requires the actual Windows codec closure.')
    pin = json.loads((ROOT / 'packaging/windows-audio.lock.json').read_text(encoding='utf-8'))
    rows = {}
    for name in ('ffmpeg', 'ffprobe'):
        path = (selected / 'bin' / (name + '.exe')).resolve(strict=True)
        found = shutil.which(name)
        require(found and Path(found).resolve(strict=True) == path,
                'Actual codec discovery differs from the explicitly selected runtime: ' + name)
        before = path.stat()
        require(path.is_file() and not path.is_symlink() and before.st_nlink == 1,
                'Selected codec is not an ordinary unlinked file.')
        with path.open('rb') as stream:
            digest = file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                'Selected codec changed while hashing.')
        expected = pin['output_files']['bin/' + name + '.exe']
        require(before.st_size == expected['size'] and digest == expected['sha256'],
                'Selected codec bytes differ from the existing lock.')
        rows[name] = dict(path=str(path), size=before.st_size, sha256=digest)
    return rows


def run_whole(*, mode, number, suite, expected, work, evidence, ci,
              wrapper, contract, staged=None, wheel=None):
    """Retain one real whole result; origin failures are never ordinary failures."""
    prefix = mode + '-' + str(number)
    xml = evidence / (prefix + '.xml')
    receipt = evidence / (prefix + '-origins.json')
    log = evidence / (prefix + '.txt')
    selected = ROOT / suite if mode == 'source' else staged / suite
    root = ROOT if mode == 'source' else staged
    arguments = ['-v', '-c', os.devnull if mode == 'source' else str(staged / 'pytest.ini'),
        '--rootdir', str(root), '--confcutdir', str(root), '--import-mode=importlib',
        '-o', 'pythonpath=', '-o', 'addopts=', '-o', 'xfail_strict=true',
        '-o', 'faulthandler_timeout=120', '-o', 'faulthandler_exit_on_timeout=false',
        '--durations=0', '--durations-min=0', '--junitxml', str(xml),
        '--basetemp', str(work / (prefix + '-pytest')), '--', str(selected)]
    if mode == 'source':
        command = [sys.executable, '-I', '-B', str(wrapper),
                   str(ROOT), str(contract), str(receipt), *arguments]
    else:
        command = [sys.executable, '-I', '-B', str(wrapper), str(wheel),
            str(evidence / 'source-wheel-proof.json'), str(ROOT.parent),
            str(receipt), str(contract), *arguments]
    timeout = (ci._reviewed_suite_timeout(suite, 900)
               if suite == ADMISSION_SUITE else ci.SUITE_TIMEOUT_SECONDS)
    started = monotonic()
    try:
        result = child(command, log, cwd=work if mode == 'source' else staged,
                       ci=ci, timeout=timeout)
    except subprocess.TimeoutExpired:
        # child() rethrows this exact timeout only after the existing owned
        # process-tree termination succeeds. A cleanup exception still aborts.
        require(log.is_file() and log.stat().st_size <= 8 * 1024**2,
                'Timed-out diagnostic log is missing or unbounded.')
        print(log.read_text(encoding='utf-8', errors='replace'), flush=True)
        row = dict(mode=mode, suite=suite, status='FAIL', reason='WHOLE_TIMEOUT',
            expected_cases=expected, expected_functions=FUNCTIONS[suite],
            timeout_seconds=timeout, elapsed_seconds=round(monotonic()-started, 6),
            cases_complete=False, origin_after='NOT_OBSERVED',
            timeout_cleanup='EXISTING_CI_PROCESS_TREE_ROUTINE_RETURNED',
            log_sha256=sha256(log.read_bytes()).hexdigest())
        write(evidence / (prefix + '-summary.json'), row)
        print('FOCUSED_WHOLE ' + json.dumps(row, sort_keys=True), flush=True)
        return row
    # Before/after source or complete installed wheel verification is required
    # even when the unmodified test reports an ordinary assertion failure.
    require(receipt.is_file(), 'Required child origin receipt is absent: ' + prefix)
    origins = json.loads(receipt.read_text(encoding='utf-8'))
    require(origins.get('status') == 'PASS'
            and set(origins.get('origins', {})) == set(MODULES),
            'A required source or installed origin was not verified: ' + prefix)
    if mode == 'source':
        require(origins.get('mode') == 'source' and origins.get('unchanged') is True,
                'Source before/after identity proof is incomplete.')
    else:
        require(origins.get('isolated') is True
                and origins.get('source_injection') is False
                and origins.get('payload_unchanged') is True,
                'Installed child identity proof is incomplete.')
    require(xml.is_file(), 'A completed whole child produced no JUnit: ' + prefix)
    cases = list(ET.parse(xml).iter('testcase'))
    failures = [case.get('name', '') for case in cases
                if any(case.find(tag) is not None for tag in ('failure', 'error'))]
    skips = [case.get('name', '') for case in cases if case.find('skipped') is not None]
    observed_functions = {case.get('name', '').split('[', 1)[0] for case in cases}
    passed = len(cases) - len(failures) - len(skips)
    complete = (len(cases) == expected and observed_functions == set(FUNCTIONS[suite]))
    good = result == 0 and complete and passed == expected and not failures and not skips
    row = dict(mode=mode, suite=suite, status='PASS' if good else 'FAIL',
        expected_cases=expected, expected_functions=FUNCTIONS[suite],
        observed_functions=sorted(observed_functions), cases=len(cases),
        passed=passed, failed=len(failures), skipped=len(skips), exit=result,
        failure_names=failures, skipped_names=skips, cases_complete=complete,
        timeout_seconds=timeout, elapsed_seconds=round(monotonic()-started, 6),
        origin_before_after='PASS', origins=origins,
        junit_sha256=sha256(xml.read_bytes()).hexdigest(),
        log_sha256=sha256(log.read_bytes()).hexdigest())
    write(evidence / (prefix + '-summary.json'), row)
    print('FOCUSED_WHOLE ' + json.dumps({key: row[key] for key in (
        'mode', 'suite', 'status', 'cases', 'passed', 'failed', 'skipped',
        'exit', 'elapsed_seconds', 'failure_names')}, sort_keys=True), flush=True)
    return row


def phase_summary(evidence, name, rows):
    require(len(rows) == len(SUITES) and [row['suite'] for row in rows] == list(SUITES)
            and all(row['mode'] == name for row in rows),
            'Every fixed whole module must have exactly one result in each mode.')
    value = dict(status='PASS' if all(row['status'] == 'PASS' for row in rows) else 'FAIL',
        expected_suites=SUITES, expected_total=sum(SUITES.values()),
        passed=sum(row.get('passed', 0) for row in rows), whole_results=rows)
    write(evidence / (name + '-summary.json'), value)
    return value


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--audio-directory', type=Path, required=True)
    args = parser.parse_args()
    require(os.name == 'nt' and __debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head),
            'Exact Windows commit and assertions are required.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Checkout differs from exact event head.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before = sources()
    own_relative = Path(__file__).resolve().relative_to(ROOT.parent).as_posix()
    require(git('show', args.expected_head + ':' + own_relative)
            == Path(__file__).read_text(encoding='utf-8').strip(),
            'Orchestrator differs from its committed source.')
    require(len(SUITES) == 4 and sum(SUITES.values()) == 113
            and len(MODULES) == len(set(MODULES)) == 42
            and set(FUNCTIONS) == set(SUITES)
            and sum(map(len, FUNCTIONS.values())) == 66,
            'Fixed whole-suite/function/origin inventory changed.')
    selected_audio = args.audio_directory.resolve(strict=True)
    audio_before = audio_inputs(selected_audio)
    identity = dict(source_commit=args.expected_head, source_tree=git('rev-parse', 'HEAD^{tree}'),
        inputs=before, audio=audio_before, python=sys.version, platform=sys.platform,
        interpreter=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix,
        test_process_flags=['-I', '-B'])
    write(evidence / 'identity-before.json', identity)
    ci = load('failure_existing_ci_check', ROOT / 'scripts/ci_check.py')
    proof_module = load('failure_existing_source_wheel', ROOT / 'packaging/source_wheel.py')
    require(ci.SUITE_TIMEOUT_SECONDS == 600
            and ci.REVIEWED_SUITE_TIMEOUT_SECONDS == {ADMISSION_SUITE: 900},
            'Original reviewed600/900 whole-child policy changed.')
    work_parent = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='carr-focused-failures-', dir=work_parent) as tmp:
        work = Path(tmp)
        source_wrapper = work / 'source-child.py'
        source_wrapper.write_text(SOURCE_CHILD, encoding='utf-8')
        source_contract = work / 'source-contract.json'
        write(source_contract, dict(pytest_child=ci.PYTEST_CHILD, modules=MODULES,
            source_hashes={name: row['sha256'] for name, row in before.items()}))
        rows = list(SUITES.items())
        # The measured Book whole runs by itself under its original600 limit.
        source_rows = [run_whole(mode='source', number=1, suite=rows[0][0], expected=rows[0][1],
            work=work, evidence=evidence, ci=ci, wrapper=source_wrapper, contract=source_contract)]
        require(rows[0][0] == BOOK_SUITE, 'Book must be measured alone before other source modules.')
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(run_whole, mode='source', number=number,
                suite=suite, expected=expected, work=work, evidence=evidence, ci=ci,
                wrapper=source_wrapper, contract=source_contract)
                for number, (suite, expected) in enumerate(rows[1:], 2)]
            source_rows.extend(future.result() for future in futures)
        source_summary = phase_summary(evidence, 'source', source_rows)
        require(sources() == before, 'Pinned sources changed during source diagnostics.')

        wheels = work / 'wheel'
        wheels.mkdir()
        build_code = child([sys.executable, '-B', '-m', 'pip', 'wheel', '--no-index',
            '--no-deps', '--no-build-isolation', '--wheel-dir', str(wheels), str(ROOT)],
            evidence / 'wheel-build.txt', cwd=work, ci=ci, timeout=300)
        require(build_code == 0, 'Current source wheel build failed.')
        found = list(wheels.glob('*.whl'))
        require(len(found) == 1, 'Exactly one current application wheel is required.')
        wheel = found[0]
        proof = proof_module.verify_application_wheel(wheel, ROOT.parent, args.expected_head)
        write(evidence / 'source-wheel-proof.json', proof)
        install_code = child([sys.executable, '-I', '-B', '-m', 'pip', 'install', '--no-index',
            '--no-deps', '--force-reinstall', str(wheel)],
            evidence / 'wheel-install.txt', cwd=work, ci=ci, timeout=180)
        require(install_code == 0, 'Verified current wheel was not installed.')
        require(child([sys.executable, '-I', '-B', '-m', 'pip', 'check'],
            evidence / 'installed-pip-check.txt', cwd=work, ci=ci, timeout=60) == 0,
            'Installed dependency closure is inconsistent.')
        staged = work / 'isolated-tests'
        staged.mkdir()
        for relative in (*SUITES, *COMPANIONS):
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
            require(sha256(target.read_bytes()).hexdigest() == before[relative]['sha256'],
                    'Companion changed during staging.')
        (staged / 'pytest.ini').write_text('[pytest]\naddopts =\n', encoding='utf-8')
        installed_wrapper = work / 'installed-child.py'
        installed_wrapper.write_text(INSTALLED_CHILD, encoding='utf-8')
        installed_contract = work / 'installed-contract.json'
        write(installed_contract, dict(pytest_child=ci.PYTEST_CHILD, modules=MODULES))
        installed_rows = [run_whole(mode='installed', number=number, suite=suite, expected=expected,
            work=work, evidence=evidence, ci=ci, wrapper=installed_wrapper,
            contract=installed_contract, staged=staged, wheel=wheel)
            for number, (suite, expected) in enumerate(rows, 1)]
        installed_summary = phase_summary(evidence, 'installed', installed_rows)
        for relative in (*SUITES, *COMPANIONS):
            require(sha256((staged / relative).read_bytes()).hexdigest() == before[relative]['sha256'],
                    'An installed test or companion changed.')
    require(sources() == before, 'Pinned sources changed during actual diagnostics.')
    require(git('rev-parse', 'HEAD') == args.expected_head, 'Source commit changed.')
    require(audio_inputs(selected_audio) == audio_before, 'Selected codec changed during diagnostics.')
    all_passed = source_summary['status'] == installed_summary['status'] == 'PASS'
    report = dict(status='PASS' if all_passed else 'FAIL', **identity,
        source_passed=source_summary['passed'], installed_passed=installed_summary['passed'],
        source=source_summary, installed=installed_summary, inputs_unchanged=True,
        application_wheel_sha256=proof['wheel_sha256'], selected_whole_suites=SUITES,
        origin_modules=MODULES, expected_failures=False,
        qualification='Four unchanged whole source and isolated installed modules only. Full installed cross-suite leakage, native desktop, acoustic quality and manual NVDA are not qualified by this diagnostic.',
        native='NOT_RUN', full_installed_gate='UNCHANGED_AND_SEPARATELY_REQUIRED')
    write(evidence / 'contracts-summary.json', report)
    print(json.dumps({key: report[key] for key in (
        'status', 'source_commit', 'source_tree', 'source_passed', 'installed_passed',
        'inputs_unchanged')}, sort_keys=True), flush=True)
    require(all_passed, 'Every unchanged source and installed whole must pass without skip or expected failure.')


if __name__ == '__main__':
    main()
