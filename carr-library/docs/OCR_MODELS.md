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
also embedded in `models.json`; installation preserves that exact manifest and
writes the full bound license to `LICENSE.txt`. Neither a Git hash nor a locally computed
checksum is a publisher signature.

## Install and activate

Open **Recognition models** in Import Center and select the **Offline packs**
tab in the existing language dialog. Its offline catalogue supports code/name
filtering and fast/best profiles. Choose explicit model codes, a disk budget,
an existing local model source, and a new prepared-pack folder. **Prepare
verified pack** checks the pinned official bytes and reports the checksum.

Review and retain that checksum separately, choose a new installation folder,
then choose **Install reviewed pack**. Existing directories are never overwritten.
Preparation and installation have cancellable background jobs. A partial
installation cannot pass verification. An installation cannot be placed inside
the sealed portable application or OCR tree.

The same operation is available offline through:

```console
research-library-ocr-models install --source prepared-fast --destination installed-fast --manifest-sha256 INDEPENDENT_RECORDED_SHA256
```

The reusable API is:

```python
from carr_library.extract.language_manager import install_offline_pack
install_offline_pack(pack, destination, expected_manifest_sha256=checksum)
```

Choose **Use verified pack** only after installation or when selecting an already
installed pack with its independently retained checksum. The dialog verifies
the complete pack and checks the engine's actual inventory before switching its
model list. Review the recognition language order and choose **Use settings**.
No language is silently substituted when the pack lacks a previous selection.

Each private import job records `Config.model_pack_path` and
`Config.model_pack_sha256`. Tesseract receives that exact model directory; the
runtime fingerprint includes the pack identity and the base engine identity.
The complete portable runtime inventory remains verified, including unused
bundled models and native libraries. Pack contents and the runtime are rechecked
around inference and before checkpoint reuse. Changing or removing the selected
pack blocks that import; it does not silently fall back to system models.

Choose **Use bundled or system models** explicitly to leave the pack for a new
configuration. Keep installed packs available for resumable jobs. Library backups
retain the private configuration and presets, but do not silently copy a separate
model installation; restoring elsewhere may require installing the same verified
pack and selecting its new path for a new import plan.

Choose the actual languages/scripts present on the page;
installing every model is not a reason to run all of them in one OCR pass.

## Implementation and verification boundary

`load_source`, `list_models` and `prepare_offline_pack` form the offline API for
the native manager. The preparer has no network or subprocess dependency and
does not execute model files. Synthetic tests cover filesystem integrity,
publication, cancellation and budgets. Real selected-model OCR smoke results
are recorded separately; they must not be reported as all-language acceptance.
`tests/extract/test_ocr_packs.py` exercises the complete preparation, installation,
activation and actual English/Ukrainian/Slovak recognition flow, plus a durable
reviewed import that rejects changed model bytes before checkpoint reuse and
resumes only after the approved bytes are restored. Native Windows/NVDA results
must be recorded separately from Linux and headless control-handler tests.

Upstream provenance, revisions, license identities and documented compatibility
are recorded in `extract/OCR_MODEL_CATALOG_REUSE.md`.
