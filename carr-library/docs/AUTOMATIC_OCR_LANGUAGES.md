# Automatic OCR language candidates

Automatic mode is an explicit option in the existing OCR model-selection dialog and in
`python -m carr_library import-batches ... plan --auto-ocr-languages`. Manual selection remains
the default. The selected one-to-eight reading models are a reviewed bootstrap pool;
the option never downloads, installs or selects an unreviewed model.

## One local pipeline

The existing PDFium or Pillow page renderer feeds the existing bounded Tesseract
process. Its first-pass text is classified by the published **py3langid 0.4.0**
model, across the model's full language inventory. Available Tesseract models do
not restrict the detector's distribution or manufacture confidence.

The source, model and wheel identities are recorded in
`extract/ocr-language-detector.json`. The wheel, source files, model and license
were independently checked by the data-only CI run
[46009967990](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46009967990):
the downloaded wheel matched its published SHA256, and its source/model/license
Git blob IDs matched immutable upstream
[3b99caf0](https://github.com/adbar/py3langid/tree/3b99caf00d0dcbc9416a9d06f8e5b690e919a74e).
This proves package identity; it does not measure language or OCR accuracy.
The model uses NPZ/LZMA with `allow_pickle=False`. Its BSD license is included.

A candidate requires at least 48 letters, normalized model confidence at least
0.85 and a 0.20 margin over the next language. These are conservative review
policy thresholds, not calibrated OCR accuracy. Each sample is limited to
8192 UTF-8 bytes with an exact sample digest and truncation flag. Insufficient
text, weak OCR, an unselected language, an ambiguous script-specific model pair,
missing detector or exhausted budget leaves an explicit baseline fallback.
Orientation/script detection is a different option and is never called language
identification.

Up to twelve first-pass paragraph regions can receive a language-specific second
pass through the **same verified Tesseract runtime**, selected model bytes and
remaining deadline. Each crop records its original pixel rectangle, its RGB
digest, local coordinate frame, raw text, raw TSV and validated word rectangles.
Regional work has a shared 20-million-pixel ceiling, a one-MiB evidence limit and
at most twelve extra seconds within the existing page timeout; two seconds are
reserved for the final existing runtime check. Large/partial inventories are
explicitly marked, not silently called complete.

Mixed regions remain independent alternatives. They are not concatenated into a
new claimed transcription. Only a complete consistent paragraph inventory whose
language agrees with the whole-page detector may select a coherent second
whole-page pass; the exact first-pass text and TSV remain in provenance.
Every OCR result still requires visual source review. No printed label,
quotation, original source or native text is silently approved or rewritten.

## Settings, restore and limitations

The existing Config gains one boolean, `automatic_languages`. Existing seven-field
profiles and pending backup records remain valid and byte-preserved. Named presets
use their existing file and passive typed backup validator. Restoring presets
loads no detector, process or external model. Active resumptions still compare
the actual parser/model/runtime identity as before.

The bootstrap pool must contain models able to recognize the relevant scripts.
A Latin-only first pass cannot reconstruct unreadable Cyrillic/Arabic pixels for
a text language detector. Explicit model selection and manual override remain
available. Region proposals do not imply complete mixed-layout analysis, script
disambiguation, support for a particular count of languages, or quotation
accuracy. Those require real document fixtures and human qualification.

## Required executable acceptance

Portable/source suites:

```text
tests/extract/test_language_detection.py
tests/extract/test_ocr_language_regions.py
tests/extract/test_automatic_ocr_native.py
tests/storage/test_ocr_language_presets.py
tests/ui/test_ocr_selection.py
tests/e2e/test_raster_ocr_native.py
```

The first suite uses the actual pinned language model for English, Ukrainian and
Slovak. Region tests label their synthetic OCR callback explicitly. The actual
OCR suite recognizes English PDF and PNG originals through the existing engine,
tests manual bypass and unavailable-detector fallback, and never skips missing
required English runtime. The raster suite preserves the three-format actual
engine gate.

Native Windows selection: `python scripts/windows_automatic_ocr_smoke.py`.
For an installed wheel or sealed bundle, add `--installed`; exact source hashes
and import origins are checked. The native probe operates the actual checkbox,
Use button and Escape through keyboard events. These automated checks do not
claim manual NVDA or OCR/language-quality qualification.

New integration execution is NOT_RUN until its exact component or composed CI
head reports the corresponding cases; the earlier data-only pin run is separate.
