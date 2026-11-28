"""Inspect an installed dordiai-voices runtime in its own interpreter.

This CARR-owned bridge calls the installed product's metadata helpers. It does
not embed its catalogue, models, source files, recordings or inference engine.
The script is deliberately independent of CARR imports: a private embedded
interpreter may not have CARR or any of its dependencies installed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys


FORMAT = "carr-dordiai-inventory-v1"
MAX_CATALOGUE_BYTES = 8 * 1024 * 1024


def _ordinary_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISREG(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400)


def _managed_reference_inventory(runtime, models, references, backend, reference_model,
                                 voice_assets, voice_policy):
    # The model directory is captured from the current complete manifest by the
    # host. This private metadata child performs no model import or inference.
    if (not isinstance(reference_model, str) or not reference_model
            or "\\" in reference_model or ":" in reference_model or "\x00" in reference_model):
        raise ValueError("Invalid reviewed reference model")
    relative = PurePosixPath(reference_model)
    if (relative.is_absolute() or any(part in ("", ".", "..") for part in reference_model.split("/"))):
        raise ValueError("Invalid reviewed reference model")
    model = runtime.joinpath(*relative.parts).resolve(strict=True)
    if not model.is_relative_to(runtime) or not model.is_dir():
        raise ValueError("Reference model must remain inside the reviewed runtime")
    managed = voice_assets.discover_voices(references, engine_id=backend)
    names = dict(voice_policy.filter_voice_catalog(
        {key: row["name"] for key, row in managed.items()}))
    compatible = {voice: [] for voice in names}
    declared = {}
    roles_required = ({"reference_audio", "reference_text"} if backend == "omnivoice"
                      else {"target_embedding"})
    for voice in names:
        document = managed[voice]
        if document["engine"]["id"] != backend:
            raise ValueError("Managed reference backend changed")
        roles = {row["role"] for row in document["files"].values()}
        if not roles_required <= roles:
            raise ValueError("Managed reference roles are missing")
        for raw in document["languages"]:
            if not isinstance(raw, str):
                raise ValueError("Invalid managed reference language")
            code = raw.lower().replace("_", "-")
            if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", code):
                raise ValueError("Invalid managed reference language")
            # ConvertedVoices explicitly selects only these base-language
            # carriers. Its mixed-text fallback is not another supported pair.
            if backend == "openvoice" and code.split("-")[0] not in {"uk", "en"}:
                continue
            declared.setdefault(code, set()).add(voice)
    if backend == "omnivoice":
        common = (_ordinary_file(runtime / "reference_voices.py")
                  and _ordinary_file(model / "use_onnx.py"))
    else:
        # Import only the owner's declared carrier metadata, never instantiate
        # its converter/model. Its English profile choice also controls whether
        # the second English timing carrier is required.
        from converted_voices import VOICE_FILES, PROFILES
        converter = models / "OpenVoiceV2"
        common = (_ordinary_file(runtime / "converted_voices.py")
            and any(_ordinary_file(converter / name) for name in (
                "tone_converter.deterministic.onnx", "tone_converter.fp32.onnx", "tone_converter_q8.onnx"))
            and any(_ordinary_file(converter / name) for name in (
                "tone_ref_encoder.onnx", "tone_ref_encoder_q8.onnx")))
    languages = []
    for code, profiles in sorted(declared.items()):
        available = []
        for voice in profiles:
            present = common
            if backend == "openvoice":
                carrier_code = code.split("-")[0]
                if carrier_code == "en" and voice in PROFILES:
                    carrier_code = PROFILES[voice][2]
                required = [carrier_code, "en"] if carrier_code == "en_male" else [carrier_code]
                for selected in required:
                    name = VOICE_FILES.get(selected)
                    if not isinstance(name, str) or Path(name).name != name:
                        raise ValueError("Invalid installed OpenVoice carrier metadata")
                    carrier = models / "Piper" / name
                    present = present and _ordinary_file(carrier) and _ordinary_file(carrier.with_suffix(".onnx.json"))
            if present:
                available.append(voice)
        present = bool(available)
        languages.append({"code": code, "name": code, "resources_present": present,
            "status": "resources-present-unverified" if present else "resources-missing",
            "partial_variant": False, "interactive_reading_ready": None, "route": "reference"})
        for voice in available:
            compatible[voice].append(code)
    return names, compatible, languages


def inspect_runtime(runtime: Path, *, reference_engine="holos", reference_model=None) -> dict:
    if reference_engine not in {"holos", "openvoice", "omnivoice"}:
        raise ValueError("No reviewed reference backend")
    if reference_engine == "holos" and reference_model is not None:
        raise ValueError("The legacy Holos probe keeps its fixed configuration")
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
    if reference_engine == "holos":
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

    else:
        names, compatible, languages = _managed_reference_inventory(
            runtime, models, references, reference_engine, reference_model, voice_assets, voice_policy)

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
        **({"reference_engine": reference_engine} if reference_engine != "holos" else {}),
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
        if len(sys.argv) not in {2, 4}:
            raise ValueError("Supply a runtime and, only for a managed backend, its reviewed model")
        options = ({} if len(sys.argv) == 2 else
                   {"reference_engine": sys.argv[2], "reference_model": sys.argv[3]})
        result = inspect_runtime(Path(sys.argv[1]), **options)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
        return 0
    except Exception as error:
        # Engine exceptions and paths can contain private source text. Return
        # only the exception class, never a traceback or its message.
        print(json.dumps({"probe_error": type(error).__name__}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
