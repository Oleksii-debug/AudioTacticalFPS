# Ordered OCR languages, scripts and profiles

The normal book-import action and Import Center share one native model chooser.
Open **Choose installed or managed OCR models** in Import Center, or select a
PDF for direct import.
First choose **Models installed with the current OCR runtime**, **Managed Fast
models** or **Managed Best models**. The library's ordered chooser then lists
that profile's available models. Installed-runtime discovery runs in a
background worker. Cancelling either dialog leaves the previous import choice
unchanged.

## Keyboard workflow

1. Type a model code or language name in **Filter installed languages and scripts**.
2. Tab to **Installed recognition models** and select a result with the arrow keys.
3. Activate **Add selected**. Repeat for a mixed-language document.
4. Use **Selected models in recognition order**, **Move up**, **Move down** and
   **Remove selected** to set the order. Tesseract language order can affect results.
5. Select the verification policy and, if wanted, orientation checking. **Use settings**
   returns the complete OCR configuration to the import workflow.

All installed safe language codes and `script/Name` selectors are shown, including
models beyond English, Ukrainian and Slovak. Recognizable names are displayed
where known; other installed models retain their exact code. OSD and the legacy
equation detector are auxiliary models and cannot be selected as languages.
Filter changes never remove already selected models. At most eight recognition
models run together in one pass; the inventory can contain up to 1,024 models.
This bounds per-pass resource use without pretending that every installed model
should be loaded for every page.

## Profiles and provenance

- **Installed models** uses the existing runtime identity and checkpoint rules.
  A portable bundle is checked against its complete manifest, including nested
  script-model files and their SHA256 checksums. An invalid explicitly selected
  bundle cannot fall back to another executable or model directory.
- **Managed Fast models** and **Managed Best models** use the separate local
  stores populated by the [offline OCR model manager](OCR_MODEL_MANAGER.md).
  Their inventories identify registered models; the actual import verifies
  each selected model's bytes and the license before recognition and checks
  those identities again around extraction and resumable checkpoints.
- **Pinned tessdata_fast** is an additional policy using the existing exact
  upstream hashes for English, Ukrainian and Slovak. Other models are rejected
  for this policy, and it cannot be combined with Managed Best. The actual
  import verifies the model bytes again; selecting a profile is not a successful
  inference or publisher-authenticity claim.
- A system-installed Tesseract is clearly identified as a system installation.
  The ingest pipeline fingerprints its executable and each selected model and
  checks those identities again around extraction and resumable checkpoints.

The chooser does not download or install models. It does not claim an arbitrary
system model is an official upstream model, identify a document's language,
automatically choose languages, or guarantee accuracy for every listed pack.
OSD checks orientation and script; it does not identify the language. Automatic
and per-region language selection remain separate V3 work. Existing OCR size,
runtime and pixel budgets remain in force. Raw OCR still requires independent
review before it can be used as approved source evidence.

If no OCR engine is installed, the dialog offers **Continue with available text**.
Textual content can still use the existing native extraction path; scanned pages
remain for review. A broken or changed selected runtime produces a readable
error and keeps acceptance disabled until a successful rescan.

## Presets and saved plans

**Save preset** stores a named ordered model selection, explicit `installed`,
`fast` or `best` model profile, verification policy and orientation preference
in version 2 of the current library's `ocr-language-presets.json`.
It contains no source passages, machine paths, passwords or credentials. Writes
use the shared library writer lock, a flushed temporary file and atomic replace.
Replacing a named preset is explicit. The versioned file is bounded and validates
duplicate keys, names and model selectors; linked or malformed files are never
silently overwritten. The complete library backup includes this optional sidecar.

Version 1 presets remain readable as installed-runtime choices without changing
the original file. The next successful preset save writes version 2 and records
`installed` explicitly for each legacy row. A preset from another profile is
reported before changing the current selection; reopen the chooser in the
matching profile to use it. Backup and restore preserve profile identity and
model order. Optional model packs are separate machine resources and are not
included in the library backup.

Existing import plans retain their captured configuration. Changing a dialog
choice or deleting a preset affects new plans only. A saved preset with missing
models produces a readable validation error instead of silently substituting
English. A malformed preset file is retained and reported; manually selecting
installed models remains possible.

## Verification

Required source tests: `tests/extract/test_ocr_languages.py`,
`tests/ui/test_ocr_language_dialog.py`, `tests/ui/test_ocr_selection.py`,
`tests/ui/test_ocr_options.py`, `tests/ui/test_import_center.py` and
`tests/storage/test_backup_ocr_profiles.py`. Existing desktop import handler tests verify
the canonical configuration and cancellation boundary. Portable runtime tests
also cover nested script inventory integrity.

`scripts/windows_ocr_languages_smoke.py` exercises the installed native Windows
dialog, model discovery, filter/list focus, ordered mixed selection, preset
reopening and cancellation. `--require-bundled` additionally requires the sealed
English/Ukrainian/Slovak/OSD inventory and actual English OCR on a synthetic image.
These tests do not establish NVDA speech behavior or multilingual OCR quality.

The CLI adapter uses the upstream documented interfaces:
[Tesseract command-line usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
and [language and script data documentation](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html).
