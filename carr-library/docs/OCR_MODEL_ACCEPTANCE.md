# V3 OCR model management: implementation and executed evidence

This change implements a bounded part of V3-01: script-model compatibility,
official Fast/Best catalogues, verified offline installation, explicit managed
profile selection and native import integration. It does not mark the whole V3
project, automatic regional language identification or 135-voice qualification
complete. Existing source-integrity and independent-review requirements remain.

## Upstream implementation checks before integration

The measurements in this section are retained from the canonical PR114
implementation at `329d31937d029352ee36c43c62320c68347cc44d`. They describe that
implementation's recorded runs, not a new run of the whole combined application.

The clean CPython 3.12.14 environment used the project's exact base dependency
pins: pypdf 7.1.1, pypdfium2 5.13.0, python-docx 1.2.0, platformdirs 4.10.0,
filelock 3.29.0, Pillow 13.0.0 and defusedxml 0.7.1. The actual local Tesseract
version was 5.5.0. `pip check` passed.

- Full OCR/extraction selection: **199 passed, zero failed or skipped**. Official
  English, Ukrainian, Slovak and OSD bytes were independently checked against
  existing pinned sizes and SHA256 before staging. Their source files were not
  modified. The existing synthetic OCR fixtures measured CER 0 for English,
  0.012346 for Ukrainian, 0 for Slovak, and 0 for each 90/180/270-degree rotation
  fixture. These measurements apply to those synthetic pages only.
- Combined model management, native-handler routing, import batches, real
  isolated workers and source-wheel regressions: **283 passed**.
- Catalogue, model store, managed runtime, selector, manager and desktop-handler
  suites executed under the existing **kernel network-deny** runner:
  **195 passed, zero failures or skips**. Its native socket self-check passed;
  child processes inherited the network restrictions.
- Two additional managed-profile end-to-end tests passed using actual scanned
  PDF extraction in the isolated worker. Profile/DPI/timeout and exact model
  fingerprints survive quarantine and application reopen. Explicit comparison
  against independent synthetic ground truth allows the original job to finish.
  Same-size model tampering after review stops resume before any revision,
  page, edition or source-review publication. Source bytes and checkpoints remain
  unchanged; printed-page approval is not inferred.
- An actual separately installed wheel passed **36 package/CLI checks**:
  wheel RECORD identities, shipped catalogue and complete license files,
  isolated CLI searches, no model or library creation and Python network denial.

The test groups overlap and must not be added together as a unique test count.

After reconciliation with the canonical catalogue in `01820f1`, the catalogue,
renamed CLI/adapter, store, managed runtime and runtime-identity suites passed
**112 tests, zero failures or skips**. The native handler/selector/Import Center
suites passed **156 tests**. Both actual managed-profile PDF import scenarios
also passed under the kernel network-deny runner (**2 passed, zero skipped**).
These are checks of the reconciled implementation, and overlap earlier groups.

## Independent defects found and fixed

Script identifiers were previously rejected or flattened to their filename.
The strict shared validator now preserves `script/<name>` and records exact
selected-byte identity without accepting arbitrary paths.

Independent store review reproduced stale-manifest selection and a concurrent
manifest overwrite. Selection revalidates its starting manifest, and installation
refuses activation after a manifest change. License and model writes are staged,
read back, synchronized and published without replacing an existing model.
Known interrupted publication twins can be recovered; unaccounted links fail.
Cancellation preserves the prior active manifest and supports an explicit retry.

The native selector initially blocked native-text PDFs on machines without
Tesseract. A distinct missing-engine outcome now offers explicit continuation
with text extraction. Broken runtimes and invalid managed profiles do not fall
back. The real PDF regression retains unverified raster pages.

The empty CLI verification argument previously fell through to desktop startup.
It now fails within OCR validation without opening a research database.

## Windows gates and truthful status

The existing Windows workflow now includes the new selector/manager/import
handler suites, an installed-wheel native wx smoke, and prepared-bundle managed
model plus real import tests. Test-only command discovery binds the prepared OCR
runtime; application behavior still does not mutate PATH. Windows fixtures use
real junctions/hardlinks where symbolic-link privileges are unavailable, and
copied runtime fixtures retain their sibling DLLs.

`scripts/windows_ocr_models_smoke.py` checks native profile/search/checklist
events, readable names, text readback, separate profile selections, focus and
preservation of a parent's unsaved note. It reports its actual platform. The
installed mode verifies wheel RECORD before importing catalogue/UI APIs.

Actual Windows results belong to the exact-commit workflow evidence after this
change is pushed; the local checks above do not substitute for them. Manual
Windows 11 keyboard/NVDA speech and listening acceptance remain **NOT RUN** in
this environment. A native automated GUI pass does not change that status.

## Upstream identity and rights

Fast is pinned to `87416418657359cb625c412a48b6e1d6d41c29bd`; Best is pinned to
`e12c65a915945e4c28e237a9b52bc4a8f39a0cec`. The shipped catalogue SHA256 is
`1e55383fc79da1ce2d72ffca2dcd4b4fb24d69e29cd23425562cc20af7775e0b`.
The native manager uses a typed adapter over the single canonical
`ocr-model-catalog.json`; the pack preparer and manager share the same metadata.
Its 163 Fast /162 Best choices include auxiliary models and one compatibility
alias; those are not human-language or voice-quality counts. Each install checks
the upstream Git blob and records actual SHA256. The complete respective
Apache-2.0 notices are verified against upstream blob identity and packaged.

No paid APIs, private documents or public binary releases were used for this
integration. Explicitly authorized test setup restored the existing pinned
Ukrainian and Slovak artifacts through `prepare_windows_ocr.cached_artifact` and
verified English and OSD against the same lock. The application and model manager
perform no downloads; original library sources remain unchanged.

## PR99 and PR98 integration

The canonical manager is integrated with PR98's ordered language chooser,
resource-preserving configuration and reviewed-pack bindings. Existing v1/v2
presets remain readable without rewriting; v3 adds explicit managed Fast/Best
choices. New regression cases cover profile changes, stale inventory rejection,
cancelled preset activation, mixed-version preset backup/restore and rejection of
ambiguous OCR/document/DOI/audio CLI operations.

On the merged source, eleven strict suites passed **326 tests, zero failures or
skips**: runtime identity (12), reviewed packs (31), actual managed PDF imports
(2), model CLI (15), language/preset validation (72), ordered native-dialog
routing (28), OCR options (9), Import Center (22), desktop handlers (59), shared
manager routing (28) and selection validation (48). The real OCR cases used the
four exact locked model files; the Windows GUI probe itself was not run locally.
All 202 source/script Python modules parsed, the project TOML parsed, and the
merge passed the whitespace/conflict-marker check.

The installed Windows language smoke now invokes the real canonical manager's
background installer using native control events and the bundled official
English/Ukrainian/Slovak model bytes. It verifies the resulting store, chooses an
ordered managed profile, saves that preset and separately exercises legacy
reviewed-pack activation. The compatibility fixture is prepared with the public
pack API; there is one active native installer. The portable runtime is checked
for unchanged identity afterward. This script is an automated native gate, not
evidence of manual keyboard or NVDA use; the new merged native run is pending.
