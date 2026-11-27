"""Strict installed Windows media ownership, editing and recovery acceptance.

Real FFmpeg/FFprobe and fixed synthetic native children are required. This does
not claim microphone hardware, model quality, EPUB reader or manual NVDA proof.
"""
import argparse
import importlib.util
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_acceptance_helpers", ROOT / "scripts" / "verify_operations_acceptance.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
helpers.ORIGINS = [*helpers.ORIGINS,
    "carr_library.audio.models", "carr_library.audio.cleanup",
    "carr_library.audio.windows_job", "carr_library.audio.recording",
    "carr_library.audio.dsp", "carr_library.audio.music",
    "carr_library.audio.audiobook", "carr_library.studio.daisy",
    "carr_library.backup_producers",
    "carr_library.speech.player", "carr_library.speech._io",
    "carr_library.studio.project", "carr_library.studio.clips",
    "carr_library.studio.backup_inputs", "carr_library.studio.publishing",
    "carr_library.ui.recording_dialog", "carr_library.ui.studio_clips",
    "carr_library.ui.publishing_dialog", "carr_library.ui.studio_dialog",
    "carr_library.reader.reading_state",
]
SUITES = [
    "tests/audio/test_recording.py",
    "tests/audio/test_recording_resources.py",
    "tests/audio/test_owned_process_tree.py",
    "tests/audio/test_cleanup_ownership.py",
    "tests/audio/test_retained_native_staging.py",
    "tests/audio/test_publication_fsync.py",
    "tests/audio/test_music.py",
    "tests/audio/test_audiobook.py",
    "tests/audio/test_dsp.py::test_actual_pan_timing_frames_and_source_unchanged",
    "tests/audio/test_dsp.py::test_unicode_quotes_and_many_scene_join_preserve_exact_frames",
    "tests/audio/test_dsp.py::test_trim_does_not_invent_samples_and_final_stereo_is_preserved",
    "tests/audio/test_dsp.py::test_cancellation_during_actual_render_removes_partial_output",
    "tests/operations/test_studio_media_admission.py",
    "tests/operations/test_canonical_asr_admission.py",
    "tests/ui/test_recording_dialog.py",
    "tests/studio/test_clip_editing.py",
    "tests/studio/test_clip_dialog.py",
    "tests/studio/test_clip_cli.py",
    "tests/studio/test_runtime_identity.py",
    "tests/studio/test_publishing.py",
    "tests/studio/test_publishing_cli.py",
    "tests/studio/test_publishing_handlers.py",
    "tests/studio/test_audiobook_publication.py",
    "tests/studio/test_daisy_publishing.py",
    "tests/studio/test_production_cancellation_outcomes.py",
    "tests/storage/test_studio_input_backup.py",
    "tests/storage/test_backup_studio_media_v6.py",
    "tests/storage/test_library_backup_reading_state.py",
    "tests/storage/test_backup_schema_identity.py",
]
REQUIRED_CASES = {
    "test_actual_process_device_contention_and_wait_stop": 3,
    "test_actual_unconfirmed_capture_keeps_capacity_device_and_capture_lock": 1,
    "test_owned_descendant_cannot_outlive_parent_or_failure": 4,
    "test_actual_descendant_and_native_stage_remain_owned_until_cleanup": 2,
    "test_pending_exception_is_preserved_when_deferred_stage_deletion_fails": 1,
    "test_retained_state_lock_accepts_a_previous_owners_completed_release": 1,
    "test_cancellation_during_flush_does_not_publish": 1,
    "test_cancellation_after_link_does_not_reclassify_committed_output": 1,
    "test_canonical_registry_api_renders_pcm_and_rejects_changed_voice_identity": 2,
    "test_pcm_inspection_preserves_old_call_and_enforces_optional_budget_and_cancel": 1,
    "test_real_pending_child_retains_studio_lease_and_job_lock_despite_failed_diagnostics": 3,
    "test_clip_native_owner_keeps_admission_and_never_publishes_a_project_early": 3,
    "test_queued_clip_rechecks_existing_source_guard_before_native_work": 6,
    "test_pending_actual_asr_child_keeps_capacity_job_lock_and_unverified_files": 2,
    "test_actual_pending_epub_child_retains_stage_job_lock_and_broker": 2,
    "test_actual_m4b_contains_exact_titles_measured_chapters_and_audio": 1,
    "test_actual_studio_m4b_embeds_measured_chapters_and_lrc": 1,
    "test_actual_daisy_ncc_smil_audio_source_and_same_receipt": 1,
    "test_typed_media_backup_retains_each_publication_format_without_external_bytes": 6,
    "test_real_daisy_child_exit_uses_same_receipt_recovery_and_preserves_residual_alias": 2,
    "test_real_render_backup_delete_original_state_restore_and_render_again": 1,
    "test_v6_keeps_exact_reading_and_immutable_import_receipts_with_retained_media": 1,
    "test_stop_during_staged_master_validation_prevents_publication_and_resumes": 1,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-root", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("This installed gate requires actual Windows; POSIX evidence is separate.")
    if Path.cwd().resolve() != ROOT:
        raise RuntimeError("Run this gate from the exact carr-library checkout.")
    if version("filelock") != "3.29.0":
        raise RuntimeError("The native lock backend must match the application lockfile.")
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        os.environ.pop(key, None)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    args.results.mkdir(parents=True, exist_ok=True)
    report = args.results / "installed-media.xml"
    evidence = args.results / "installed-media.json"
    # Keep the bundle's own DLL-directory handle alive through module imports
    # and the complete test run. Source-installed CI legitimately has no handle.
    from carr_library.native_runtime import activate_bundled_runtime
    runtime_handle = activate_bundled_runtime()
    try:
        result = {"status": "RUNNING", "source": helpers.source_identity(),
                  "python": sys.version, "filelock": version("filelock"),
                  "audio": helpers.pinned_audio(args.audio_root),
                  "origins": helpers.installed_origins(), "suites": SUITES,
                  "native_runtime_activated": runtime_handle is not None,
                  "manual_nvda": "NOT_RUN", "microphone_hardware": "NOT_RUN",
                  "acoustic_model_quality": "NOT_RUN", "epub_reader": "NOT_RUN",
                  "daisy_reader": "NOT_RUN", "braille_output": "NOT_RUN",
                  "acoustic_sentence_alignment": "NOT_RUN"}
        evidence.write_text(json.dumps(result, indent=2), encoding="utf-8")
        import pytest

        class RejectExpectedFailures:
            seen = False

            def pytest_runtest_logreport(self, report):
                if hasattr(report, "wasxfail"):
                    self.seen = True

        policy = RejectExpectedFailures()
        status = pytest.main(["-q", "-o", "addopts=", "-o", "pythonpath=",
                             "-o", "xfail_strict=true", "-o", "faulthandler_timeout=120",
                             "--import-mode=importlib", "--junitxml=" + str(report),
                             *SUITES], plugins=[policy])
        cases = list(ET.parse(report).iter("testcase")) if report.exists() else []
        failed = sum(any(case.find(tag) is not None for tag in ("failure", "error")) for case in cases)
        skipped = sum(case.find("skipped") is not None for case in cases)
        passed = len(cases) - failed - skipped
        observed = {case.get("classname", "").rsplit(".", 1)[-1] for case in cases}
        missing_suites = sorted({Path(path.split("::", 1)[0]).stem for path in SUITES} - observed)
        actual_cases = {name: sum(case.get("name", "").split("[", 1)[0] == name for case in cases)
                        for name in REQUIRED_CASES}
        required_exact = actual_cases == REQUIRED_CASES
        after = helpers.installed_origins()
        origins_unchanged = after == result["origins"]
        ok = (status == 0 and passed > 0 and failed == skipped == 0 and not policy.seen
              and not missing_suites and required_exact and origins_unchanged)
        result.update(status="PASS" if ok else "FAIL", passed=passed, failed=failed, skipped=skipped,
                      pytest_exit=int(status), expected_failures=policy.seen,
                      required_actual_cases=actual_cases, required_cases_exact=required_exact,
                      missing_suites=missing_suites, origins_after=after,
                      origins_unchanged=origins_unchanged)
        evidence.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        return 0 if ok else 1
    finally:
        if runtime_handle is not None:
            runtime_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
