# OCR languages and verified offline model packs

The Library menu includes **OCR language models: Fast and Best**. It opens a
native, keyboard-operable catalogue of the two reviewed official Tesseract model
families. Search by a language name or model identifier, check one or more rows
with Space, choose the local folder containing those model files, and choose
**Install selected**. Filtering does not discard checked rows; the selection
summary includes all checked models in the current profile. Fast and Best keep
separate selections and installed files.

The catalogue contains **163 Fast entries** and **162 Best entries**. Entries
include language models, script models, orientation/equation auxiliaries and a
materialized compatibility alias. These counts are not a count of human
languages, supported voices or qualified dordiai-voices entries. The separate
135-entry voice qualification requirement remains independent.

## Choose recognition models when importing

Importing PDF documents or supported PNG/JPEG/TIFF raster originals opens an OCR selection dialog. Choose the models
installed with the OCR runtime, Managed Fast, or Managed Best. Select only the
languages or scripts the document needs. English is the initial selection when
available; the application does not load every installed model by default.
Several checked text models enable an explicit mixed-language Tesseract pass.
An installed `osd` model enables the separate optional orientation control.

`script/Latin` and `script/Cyrillic` are distinct from language models such as
`lat`, `eng`, `ukr` and `slk`. The exact model identifier and byte identity remain
in extraction and resume provenance. Script selection and orientation detection
are distinct from automatic language identification. The separate explicit
**Automatic language detection and paragraph review (offline)** option uses a
pinned offline detector after the initial pass and can retain language-specific
paragraph alternatives from at most eight selected text models. Uncertain or
unsupported decisions preserve the initial candidate; mixed paragraphs are not
automatically concatenated. This does not establish per-language accuracy.
OCR remains unverified until the existing independent source-review workflow
is completed. See [automatic OCR and raster originals](AUTOMATIC_OCR_UK.md)
for the finite rules, retained evidence and separate raster limits.

## Prepare a local model folder

This manager performs **no network requests or automatic downloads**. Acquire
the desired official model files separately, with the same filenames and source
revision shown in the model details. A source folder can contain, for example:

```text
eng.traineddata
ukr.traineddata
slk.traineddata
osd.traineddata
script/Latin.traineddata
script/Cyrillic.traineddata
```

The folder is read without modification. The manager accepts selected files
only if their size and Git blob digest match the pinned upstream source tree.
It records SHA256 over the copied bytes and keeps the full upstream Apache-2.0
notice. The Fast `frk` compatibility alias is materialized from the exact
`deu_latf.traineddata` bytes; no filesystem symlink is installed. An archive,
symlink, reparse point, network share or arbitrary executable is not a model
source. A model from another revision or model family is refused rather than
silently substituted.

The reviewed sources are:

- Fast: [tessdata_fast at 87416418657359cb625c412a48b6e1d6d41c29bd](https://github.com/tesseract-ocr/tessdata_fast/tree/87416418657359cb625c412a48b6e1d6d41c29bd).
- Best: [tessdata_best at e12c65a915945e4c28e237a9b52bc4a8f39a0cec](https://github.com/tesseract-ocr/tessdata_best/tree/e12c65a915945e4c28e237a9b52bc4a8f39a0cec).
- [Official model-family documentation](https://tesseract-ocr.github.io/tessdoc/Data-Files.html) describes Fast as the speed-oriented integer models and Best as the larger floating-point family. Actual accuracy and speed depend on the source and hardware. Both use the LSTM engine; orientation uses its separate legacy OSD mode.

The complete Fast selection occupies **706,776,225 model bytes** including its
physical compatibility-alias copy. Best occupies **1,774,547,803 model bytes**.
The UI displays exact selected sizes, including previously installed models.
Installation checks free space and streams each file instead of loading a full
pack into memory. Complete packs are optional; a small selected set is sufficient.

## Integrity, cancellation and recovery

Installation activates the complete selected batch only after every selected
file passes verification. Cancel leaves the prior manifest active. Validated
but unregistered copies left by interruption can be reused by an explicit
retry. Existing altered files are reported, never silently overwritten.
Recognized interrupted link publications are recovered only after verifying
their exact bytes and accounting for their staging link; unrelated links are
rejected. Model and license writes are staged, synchronized and activated
atomically. Concurrent changes to the starting manifest abort activation.

Installed-status labels are a manifest inventory. Before recognition, selected
model bytes and the license are verified again. The engine executable and any
portable runtime manifest are checked independently. The same selected model
identity is checked after recognition and included in resume identity, so a
changed model cannot silently continue an existing extraction job.

These checks establish correspondence to reviewed upstream bytes and local
integrity. They are not publisher signatures, a hostile-OS sandbox or proof of
OCR accuracy. Model packs are optional local resources; retain the original
offline model folder if you want to reinstall without network access. Research
sources, notes and evidence remain separate from model-installation storage.

## Command-line use

The same catalogue and installation boundary are available without starting the
desktop or opening a research database:

```powershell
research-library --list-ocr-models --ocr-profile fast --ocr-model-search Ukrainian
research-library --list-ocr-models --ocr-profile best --ocr-model-search "Latin script"
research-library --install-ocr-models C:\Offline\tessdata_fast --ocr-profile fast --ocr-model eng --ocr-model ukr --ocr-model slk --ocr-model osd
research-library --verify-ocr-models eng+ukr+slk --ocr-profile fast
```

`--ocr-all-models` explicitly selects the entire chosen family for installation
from the supplied local folder. `--ocr-model-store` selects a separate absolute
local store for that command; the native application uses its standard local
model store unless an explicit `CARR_OCR_MODELS_DIR` override is configured.
No CLI operation implicitly acquires missing models.

## Implementation and acceptance boundary

The shared identifier validator accepts a root model identifier or exactly
`script/<identifier>` while rejecting traversal, drive names and arbitrary
paths. Existing pinned English/Ukrainian/Slovak policy remains available.
`Config.model_profile` defaults to `installed`, preserving prior callers;
`fast` and `best` select managed packs. The Fast pinned policy cannot be combined
with the Best family. Package data includes both complete upstream notices and
the versioned catalogue. No additional runtime Python dependency was introduced.

Tests cover exact source identity, real local OCR, aliases, mixed selection,
portable script discovery, tampering, bounded copying, disk exhaustion,
cancellation, interrupted installation, manifest changes, native control routing
and CLI behavior. The native smoke script exercises actual wx controls when run
on Windows. Automated GUI tests do not establish manual NVDA acceptance; that
status must be reported from an actual Windows/NVDA session.
