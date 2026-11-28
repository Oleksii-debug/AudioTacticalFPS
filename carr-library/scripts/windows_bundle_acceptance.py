"""Require every installed-bundle check before publishing tested-wheel evidence.

This is CI orchestration, not a product or release-qualification adapter.
A verified build can be inspected by other gates even when native checks fail.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


MAX_REPORT_BYTES = 32768
NATIVE_CHECKS = (
    "windows_transcription_smoke.py",
    "native_trust_status_smoke.py",
    "windows_clip_editor_smoke.py",
    "windows_recording_smoke.py",
    "windows_publishing_smoke.py",
    "windows_local_planning_smoke.py",
    "windows_translation_v3_smoke.py",
    "windows_translation_local_smoke.py",
    "windows_epub_reading_smoke.py",
    "windows_pronunciation_smoke.py",
    "windows_studio_pronunciation_smoke.py",
    "windows_operations_smoke.py",
    "windows_reader_player_smoke.py",
    "windows_audio_player_smoke.py",
    "windows_effect_chain_smoke.py",
    "windows_reading_presentation_smoke.py",
    "windows_reading_tabs_smoke.py",
    "windows_incremental_backup_smoke.py",
    "windows_voice_preferences_smoke.py",
    "windows_desktop_integration_smoke.py",
)
VALIDATION_CHECKS = (
    "epubcheck_acceptance.py",
    "daisy_schema_acceptance.py",
    "installed_pytest",
    "installed_junit",
    "canonical_junit",
    "owned_lifetime_junit",
    "final_bundle_seal",
    "source_wheel_proof",
)
REQUIRED_CHECKS = NATIVE_CHECKS + VALIDATION_CHECKS


def verdict(value):
    if (not isinstance(value, dict) or set(value) != {"schema", "checks"}
            or type(value["schema"]) is not int or value["schema"] != 1):
        raise ValueError("Invalid installed-bundle exit report.")
    rows = value["checks"]
    if not isinstance(rows, list) or len(rows) != len(REQUIRED_CHECKS):
        raise ValueError("Every required installed-bundle check must complete.")
    names = []
    failed = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"name", "exit_code"}
                or not isinstance(row["name"], str)
                or type(row["exit_code"]) is not int
                or not -(2 ** 31) <= row["exit_code"] < 2 ** 31):
            raise ValueError("Invalid required-check exit receipt.")
        names.append(row["name"])
        if row["exit_code"] != 0:
            failed.append(dict(row))
    if tuple(names) != REQUIRED_CHECKS:
        raise ValueError("Required checks are missing, repeated, changed, or reordered.")
    return {
        "schema": 1,
        "status": "FAIL" if failed else "PASS",
        "completed": len(rows),
        "required": len(REQUIRED_CHECKS),
        "native_required": len(NATIVE_CHECKS),
        "validation_required": len(VALIDATION_CHECKS),
        "failed": failed,
    }


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate exit-report field.")
        result[key] = value
    return result


def read_report(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Installed-bundle exit report exceeds its byte limit.")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) != 1:
            raise ValueError("Expected one installed-bundle exit report.")
        result = verdict(read_report(arguments[0]))
    except (OSError, UnicodeError, ValueError) as error:
        result = {"schema": 1, "status": "FAIL", "error": str(error)[:256]}
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
