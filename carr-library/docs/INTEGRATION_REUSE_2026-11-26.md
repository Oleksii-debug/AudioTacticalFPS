# Reviewed foundation reuse for the integrated candidate

This records code provenance and integration decisions. Native Windows, actual
audio inference, NVDA and release acceptance remain separate gates.

| Existing proposal | Exact source | Reused behavior |
| --- | --- | --- |
| PR97 foundation | `9ff155db24e81e5b4e155c39fa32a91865f16b7b`; reading-state snapshot helpers | Exact bounded snapshot bytes and distinct saved locations, retaining legacy reading-state behavior. |
| PR98 reading foundation | `448ed7910bf01ea89750daa6d97115d0f2716cc6` | Installed OCR catalog, ordered native selection, presets, script-model paths, import configuration capture, keyboard preferences and main-window save commands. |
| PR98 maintenance | Same source | Compact Unicode source-offset mapping and inverse presentation mapping; terminal TXT/PDF replay requires a sealed source and consistent active/superseded state before atomic membership registration. |
| PR97/98 backup validation | Their exact source-binding query adapted to integrated sidecars | Reading positions, bookmarks and Studio source links must resolve to the copied sealed database, including historical revisions and archived workspaces. |
| PR98 native probes | Same source | Installed OCR chooser probe unchanged; save/keyboard/reading-state portion of the native reading probe reused as `windows_keyboard_smoke.py`. |
| PR100 acquisition | `c6a7d4c51e519cf2a74a21a5740cea4d37e91691`; exact reviewed host patch | Four LibraryApp facades, cancellation-safe metadata revision publication and the Academic DOI dialog hook. Existing provider/service/transport/CLI and native dialog modules are reused without a second gateway. |
| PR104 document formats | `4d53fb857fd2019162d61ea6eeab0a64bb22f82b`; exact reviewed host patch | One isolated format registry, extended text/XML adapters, import picker/help/warnings and the existing import/recovery pipeline. The already-integrated NFC/whitespace scope-label normalization is retained. |
| PR106 semantic cache | `e988545afa21f5b25449e5c7a5b8bc4d45d5ecc3`; exact reviewed host patch | A separate cache for each LibraryApp is forwarded to the existing semantic snapshot search. Model/version/scope validation and fresh source authentication remain in the existing retrieval path. |
| PR112 native path repair | `9f537c970a92360651c09d57e3b74d67133a0b57`; selected path-identity patch and regression | The director stores and compares the same canonical output parent during render and resume. Existing completed-job reuse and changed-destination rejection remain tested. |
| PR114 OCR manager via canonical merge | `0bca47fb98508ef72ed709814927607864d2ff7c` | The typed manager adapter uses the existing packaged catalog. Managed Fast/Best installation and runtime binding join the existing ordered chooser, presets and immutable import configuration. |

## One implementation for each workflow

Advanced search and federated search retain the integrated APIs. The compact
Unicode mapping is inserted into the existing literal implementation; it does
not remove Boolean/NEAR retrieval. The terminal replay patch changes only the
registration boundary and preserves source bytes, metadata conflict checks,
historical access and the selected active revision.

Additional document formats retain exact original bytes and unverified logical
segments. No extraction result becomes a verified quotation or a printed-page
claim. The installed source registry drives the pending-original backup suffix
allowlist too; canceled extended imports can resume from restored retained
bytes without reopening a deleted external source. Unsupported legacy formats,
encrypted inputs, executable content and DRM restrictions remain unchanged.

Semantic vectors are optional, derived cache data. Backup excludes them so a
restored library computes them again from its verified sources and configured
model. The cache does not confer quotation authority or replace fresh catalog
and source checks.

Keyboard preferences enumerate the complete installed menu after all feature
installers have run. The existing Audio Studio and source-linked narration
editor own reading derivatives and drafts. The separate `reader/narration.py`
store and editor from PR98 are not installed as a second product workflow.
File Save commits the current research note through its existing transaction;
modal editors keep their explicit contextual controls.

The OCR manager and ordered import chooser share the canonical model catalog and
managed store. Installed/Fast/Best selection is explicit, and managed bytes are
verified before and after recognition. Version-1 ordered presets remain valid;
version-2 presets record managed profile identity and survive full-state backup.
Recognition remains limited to eight explicit text models per pass; OSD and
equation models are auxiliary inventory entries and cannot be selected as text.

Full-state backup uses the integrated `backup_sidecars.py` allowlist, staged
verification, pending-original preservation and restore guards. Keyboard,
ordered OCR presets and local music registrations use their strict validators.
Provider credentials remain in the OS vault; their references clear on restore.
The integrated version-3 sidecar manifest is the nested, versioned `sidecars`
inventory documented in `FULL_STATE_BACKUP.md`. Legacy version-1/version-2
backups remain supported. Unmerged proposals' distinct reading-copy store and
flat version-3 branch formats are not a published migration contract.

## Verification

The first combined portable verification after integration passed 379 tests:
OCR catalog/chooser/import controls, keyboard behavior, terminal replay, bounded
Unicode mapping, advanced search, cancellation, full-state and legacy backups,
reading state, and desktop handlers. Subsequent targeted checks cover the
installed-runtime fingerprint, source wheel and real-source presentation.
On the Linux host, initial multilingual tests exposed absent Ukrainian/Slovak
models in the system installation. With an explicit local model directory whose
four files matched the existing pinned size/SHA-256 records, all 14 actual
page-OCR/pinned-model checks passed using the existing Linux Tesseract runtime.
This is real selected-model inference, not Windows or all-language acceptance.

Acquisition, metadata and existing desktop checks passed 306 tests, with one
native Windows acquisition test explicitly not run on Linux. After installing
the already-pinned local defusedxml wheel into the isolated-parser environment,
98 extended-format/recovery, sidecar and semantic-cache boundary checks passed.
That run includes actual restored pending HTML/Markdown imports after their
external originals were deleted. The missing dependency was repaired in the
test environment; parser isolation and rejection limits were not relaxed.

The Windows workflow includes the reused suites and runs the native keyboard
and OCR chooser probes from the installed package. The final bundle repeats
them with egress blocked and requires its bundled OCR engine and actual English
recognition. These declarations do not claim an unexecuted Windows/NVDA result.
