# Official OCR model catalogue and offline packs

The packaged catalogue lists pinned official Tesseract `tessdata_fast` and
`tessdata_best` snapshots. Each profile contains 123 language models, 37 script
models, an orientation/script-detection model (`osd`) and an equation model
(`equ`). These are model entries, not a promise of 162 independently recognized
languages. The deprecated fast `frk` entry is a symlink alias for `deu_latf`, not
a 20-byte language model. Selection resolves it to the real model.

`fast` generally prioritizes speed and smaller model files; `best` prioritizes
recognition quality. Actual recognition depends on source material, layout and
language. Neither the catalogue nor file-integrity checks establish recognition
quality for every listed model. Language and script models use Tesseract's LSTM
engine; `osd` and `equ` have separate purposes. Orientation/script detection
does not identify a page's exact language.

## Find models

With the installed application environment:

```console
research-library-ocr-models list --profile fast --query Ukrainian
research-library-ocr-models list --profile best --kind script
```

Equivalent source or embedded-runtime invocation:

```console
python -m carr_library.extract.ocr_model_catalog list --profile fast --query ukr
```

Each result includes its exact code, name, model size, pinned upstream Git blob
identity and immutable official download URL. Search works offline. The command
does not contact a server or download any model. Profiles use separate immutable
upstream revisions recorded in `extract/ocr-model-catalog.json`.

## Prepare already obtained model files

Obtain the selected files from the exact official URLs in the catalogue, with
an explicit download action. Keep their original names and the `script/`
subdirectory when applicable. Select a source directory containing these
files and a **new**, nonexistent destination within an existing local directory.

```console
research-library-ocr-models prepare --profile fast --source local-models --destination prepared-fast --models eng ukr slk osd
```

The operation streams each selected file, checks the exact pinned size and Git
blob hash, and computes SHA-256 from those verified bytes. A file from a different
profile or revision is rejected. Links, unexpected model identifiers, changes
during copying, insufficient disk space and an existing output directory are
rejected. The default selected-payload budget is 4 GiB; `--max-bytes` accepts a
positive explicit budget. Cancellation removes unpublished staging data.

The result reports `manifest_sha256`. Keep this value separately from the pack;
it binds the exact `models.json` reviewed by the offline installer. The complete
manifest is published only after all selected files and notices. Its schema is
the existing offline-installation schema:

```json
{
  "schema": 1,
  "upstream": "immutable official repository URL for this profile",
  "license": "Apache-2.0 followed by the full upstream license text",
  "models": {
    "eng": {"size": 4113088, "sha256": "SHA-256 computed from verified file bytes"}
  }
}
```

The pack includes the upstream license and path-free provenance. The license is
also embedded in `models.json` so the existing installer retains it even when it
copies only the models and manifest. Neither a Git hash nor a locally computed
checksum is a publisher signature.

## Install and activate

Preparation does not activate or modify the application's sealed OCR runtime.
Use **Library > OCR language models: Fast and Best** to install selected model
files from a local source folder into the separate managed model store. The
manager verifies each file against its pinned upstream family and records its
SHA256 identity before activating it. See [OCR_MODEL_MANAGER.md](OCR_MODEL_MANAGER.md)
for the native and command-line installation and profile-selection workflow.
Managed packs preserve the complete portable-runtime integrity inventory;
adding a model directly to that sealed directory is not supported.
A standalone Tesseract installation can use a prepared directory through its explicit
`--tessdata-dir` option. Choose the actual languages/scripts present on the page;
installing every model is not a reason to run all of them in one OCR pass.

## Implementation and verification boundary

`load_source`, `list_models` and `prepare_offline_pack` form the offline API for
the native manager. The preparer has no network or subprocess dependency and
does not execute model files. Synthetic tests cover filesystem integrity,
publication, cancellation and budgets. Real selected-model OCR smoke results
are recorded separately; they must not be reported as all-language acceptance.

Upstream provenance, revisions, license identities and documented compatibility
are recorded in `extract/OCR_MODEL_CATALOG_REUSE.md`.
