"""Inspect an installed dordiai-voices runtime in its own interpreter.

This CARR-owned bridge calls the installed product's metadata helpers. It does
not embed its catalogue, models, source files, recordings or inference engine.
The script is deliberately independent of CARR imports: a private embedded
interpreter may not have CARR or any of its dependencies installed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


FORMAT = "carr-dordiai-inventory-v1"
MAX_CATALOGUE_BYTES = 8 * 1024 * 1024


def inspect_runtime(runtime: Path) -> dict:
    runtime = runtime.resolve(strict=True)
    sys.path.insert(0, str(runtime))
    # Use the installed worker's own DLL/bootstrap handling, before importing
    # any of its helpers. CARR's host process and import paths stay untouched.
    import windows_runtime

    windows_runtime.reset_inherited_dll_directory()
    windows_runtime.configure_worker_import_paths()
    import language_catalog
    import nvda_worker
    import voice_assets
    import voice_policy
    import wide_languages

    models = runtime / "models"
    references = models / "voices"
    catalogue = references / "catalog.json"
    if catalogue.stat().st_size > MAX_CATALOGUE_BYTES:
        raise ValueError("Oversized catalogue")
    names = json.loads(catalogue.read_text(encoding="utf-8"))
    if not isinstance(names, dict) or any(not isinstance(v, str) for v in names.values()):
        raise ValueError("Invalid catalogue")
    names = dict(voice_policy.filter_voice_catalog(names))
    managed = voice_assets.discover_voices(references, engine_id="holos")
    names.update({key: value["name"] for key, value in managed.items()})
    names = dict(voice_policy.filter_voice_catalog(names))
    compatible = {voice: [] for voice in names}
    languages = []

    for row in language_catalog.coverage(models, selected_only=False):
        code = row["runtime_code"]
        route = (
            "wide" if wide_languages.registered(code)
            else "native_profile" if code in {"en", "ru"}
            else "native_locale" if code in nvda_worker.CLASSMATE_LOCALES
            else "ukrainian" if code == "uk"
            else None
        )
        present = bool(row["resources_present"]) and route is not None
        languages.append({
            "code": code,
            "name": row["settings_label_uk"],
            "resources_present": present,
            "status": row["status"],
            "partial_variant": bool(row.get("partial_variant")),
            "interactive_reading_ready": row.get("interactive_reading_ready"),
            "route": route,
        })
        if not present:
            continue
        built_in = set(language_catalog.compatible_voice_ids(models, code, tuple(names)))
        # Managed voices override built-ins. Their declared language and actual
        # adapter asset role must both match, as in the installed driver.
        role = {"uk": "holos_profile", "en": "pocket_english_state",
                "ru": "pocket_russian_prompt"}.get(code, "target_embedding")
        for voice in names:
            if voice in managed:
                document = managed[voice]
                codes = {language_catalog.canonical_code(c) for c in document["languages"]}
                roles = {asset["role"] for asset in document["files"].values()}
                available = code in codes and role in roles
            else:
                available = voice in built_in
            if available and code not in compatible[voice]:
                compatible[voice].append(code)

    version = None
    manifest = runtime.parent / "manifest.ini"
    if manifest.is_file() and manifest.stat().st_size <= 65536:
        for line in manifest.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "version":
                version = value.strip().strip('"')
                break
    return {
        "format": FORMAT,
        "engine_id": "dordiai-voices",
        "engine_version": version,
        "worker_sha256": hashlib.sha256((runtime / "nvda_worker.py").read_bytes()).hexdigest(),
        "sample_rate": nvda_worker.OUTPUT_RATE,
        "voices": [{"id": voice, "name": name, "languages": compatible[voice],
                    "styles": ["neutral"]} for voice, name in names.items()],
        "languages": languages,
        "speech_rendering_verified": False,
        "pronunciation_verified": False,
    }


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("Exactly one runtime directory is required")
        result = inspect_runtime(Path(sys.argv[1]))
        print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
        return 0
    except Exception as error:
        # Engine exceptions and paths can contain private source text. Return
        # only the exception class, never a traceback or its message.
        print(json.dumps({"probe_error": type(error).__name__}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
