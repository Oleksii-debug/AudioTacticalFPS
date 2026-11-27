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

Importing PDF documents and **Choose installed or managed OCR models** in Import
Center share one OCR configuration. First choose models installed with the OCR runtime, Managed
Fast, or Managed Best. For a library import, the ordered language chooser then
shows the selected profile's available models. Filter the inventory, add only
the languages or scripts the document needs, and use **Move up** or **Move down**
to set recognition order. English is the initial selection when available; the
application does not load every installed model by default. Up to eight text
models enable an explicit mixed-language Tesseract pass. An installed `osd`
model enables the separate optional orientation control.

Named presets preserve the ordered selectors, orientation and explicit
`installed`, `fast` or `best` profile in version 2 of
`ocr-language-presets.json`. Existing version 1 presets remain readable as
`installed`; the next successful save upgrades them explicitly. Loading a
preset for a different profile, or a preset requiring missing models, reports
the mismatch before changing the current selection. The library backup and
restore preserve this file and its model order. Optional model packs themselves
remain in the separate local model store and must be present on the machine
that performs recognition. See [the ordered chooser guide](OCR_LANGUAGE_MANAGER.md)
for keyboard controls and preset recovery.

Cancelling either dialog leaves the existing import configuration unchanged.
Accepted settings apply to future import plans; existing plans retain their
captured configuration and resource limits. Inventory labels are not a claim
that every model byte has already been checked: the selected managed files are
verified before actual recognition.

`script/Latin` and `script/Cyrillic` are distinct from language models such as
`lat`, `eng`, `ukr` and `slk`. The exact model identifier and byte identity remain
in extraction and resume provenance. Script selection and orientation detection
are **not automatic language identification**. Automatic region-level language
identification and a per-language accuracy benchmark are not provided by this
change. OCR remains an unverified candidate until the existing independent
source-review workflow is completed.

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

On Windows, Unicode document, model and temporary-folder paths use relative
native arguments or bounded private staging when needed. Staged files are read
back and checked before recognition; original inputs and models are preserved.
A system runtime's relative `TESSDATA_PREFIX` remains bound to the original
model directory when the child working directory changes. Existing recognition
deadlines and size limits continue to apply.

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
