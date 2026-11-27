# Native reading probe and current keyboard owner — 2026-11-27

## Actual failed execution

Source `19239b5d3a6eb167f74cc4be639533a9ca32f1fd`, tree `d42e1ac8cce02d5a6c0aa77d4709a271d7583cfa`.

[MAIN run 46071155988](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46071155988), Windows job `140748155928`, completed all 392 required source suites: **9,644 PASS, 0 FAIL, 0 SKIP**. The corrected resource contention checks and native Import Center workflow passed. At step 30, `scripts/windows_v3_reading_smoke.py:88` then raised:

```text
AttributeError: 'DesktopFrame' object has no attribute 'keyboard_actions'
```

The probe had already confirmed installed module origin, real Windows wx, the first File menu and the Options/Reading menus. It failed immediately on its obsolete owner lookup, before its save, custom-keymap, reading-copy or complete-backup assertions. This was not a timeout. Full MAIN bundle/setup steps were not executed.

Failure artifact: `15235031078`; 272,096 bytes; SHA-256 `f44055a59491cff04748bc3f51514d24ff80b033afaec7fbb0f382c6ba945af6`. Its archive name includes the PR merge-ref hash; the execution and workflow artifact metadata identify the actual source above.

## Existing current behavior

The current desktop installs `frame.keyboard_commands`, owned by `ui.keyboard_settings.KeyboardManager`. Its command bindings use stable semantic IDs, edits go through `apply_overrides`, and its current `KeymapStore` lives at the manager's explicit store path. The older KeyboardActions/KeybindingStore code remains for historical import/export compatibility; it is not the installed desktop owner.

The probe must exercise the current owner. Do not add a legacy facade to the product to satisfy the stale probe, bypass native events or weaken conflict, durability, immutable-source or backup assertions.

The current narration editor's Save operation remains synchronous through controller.save and store.save. Its actual autosave timer starts at 1,200 ms. The probe's 1,800 ms observation and 15-second modal watchdog are therefore retained; no asynchronous-owner change or larger deadline is justified by this failure.

## Reviewed correction

Candidate `59a9c38223bc45c5120e2a5efb92972d6328ab3e` adapts the existing probe to the current KeyboardManager and KeymapStore. It resolves the stable Save command ID by the exact native wx menu item and requires a single matching binding. All 24 original literal check labels remain, including the default installed-origin assertion; the narration modal and reading-backup block is byte-identical. The native menu inventory, all eight required default shortcuts, global shortcut uniqueness, File Save dispatch, durable note save, visible/persisted custom shortcut, conflict rejection without menu mutation and full-backup keymap restoration remain explicit assertions.

Its reading-copy checks retain the read-only original, Tab navigation, source provenance, ordinary Save, recoverable autosave distinct from the saved revision, reopening, unchanged authenticated quotation/source bytes, complete backup, reading resume and bookmark restoration.

Workflow candidate `2c6f3e130d7b0847b5c8b410cc037ec378b8aad6` adds eight lines to the existing early Windows native-command job. It invokes the same corrected probe with -I -B, an unchanged three-minute step limit and step-local `CARR_UI_SOURCE_OVERRIDE=1`. That mode rejects all other nonempty values and proves exact checkout paths for five involved modules. The rest of the workflow, both late installed invocations, dependency pins, artifact upload and the 12-minute native-job limit are byte-identical. The default later invocation retains its isolated installed-module origin requirement. The original late step remains required; no independent workflow, manual dispatch, disabled test or relaxed timeout is introduced.

The root and an independent maintenance reviewer checked the probe against its current owners. The workflow was independently checked by the scope reviewer and root; removing its exact eight added lines reproduces the prior workflow. No application module changes are included.

## Bounded related-source review

The root searched 23 other selected late probe files for the same archived keyboard owner/store names. No other occurrence was found. This is a bounded search for the concrete obsolete-API failure class, not a claim of full API review or native execution of those files. The second probe in the same late step, `windows_ocr_languages_smoke.py`, was separately checked against its current dialog/model-manager/discovery APIs without finding an additional concrete mismatch; actual execution remains required.

The current nine other selected result families are actual SUCCESS on source192: MAIN Linux/native-command companions, both-OS Fast, both-OS Trust, Managed core, Publication, Sync, Bibliography and Audio. Their results keep their actual source identities. The corrected reading probe and the complete MAIN bundle/setup still require actual execution after composition.

## Evidence boundary

This document is a source-review checkpoint. Corrected runtime execution is **NOT_RUN** when authored; the PR matrix records later actual outcomes and artifacts. Source execution, installed origin, manual NVDA/physical audio, actual owner-engine inference, per-language quality, full external-reader/DAISY acceptance and distribution clearance remain separate.
