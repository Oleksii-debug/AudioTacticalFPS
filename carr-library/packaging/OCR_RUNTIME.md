# Offline scanned-document import

The complete Windows package includes Tesseract and local English, Ukrainian
and Slovak models. The native import dialog offers each language and a combined
selection. Model downloads and a separate OCR installation are not needed.

Image-only PDF pages wait for explicit independent review. Their original files,
physical page numbers, raw recognition output and extraction records are retained.
Use Library, Review pending imported page to review the exact source page, then
resume the import. Every required page must be approved before the complete book
is published for searching and quotation. The review is bound to its exact source
and rendering. Mixed native/image contradictions and failed recognition remain
quarantined; no partial document is published.

## Reproducible Windows package

The runtime uses Tesseract 5.5.4 from the official MSYS2 UCRT64 package repository.
Its static PE dependency closure is copied without changing executable bytes.
The runtime manifest enumerates every executable, DLL, model, notice and
provenance file. The model revision and hashes match `extract/pinned_tessdata.py`.
The accompanying corresponding-source archive retains upstream source tarballs,
build recipes, patches and source-hash evidence for the selected dependencies.

Build the normal offline application package with these additional arguments:

```console
python packaging/build_windows_bundle.py ... --ocr-dir prepared/ocr --ocr-source-archive prepared/ocr-corresponding-source.tar
```

The builder verifies the entire runtime before copying and again afterwards.
When the manifest declares corresponding sources, their separately pinned
archive is mandatory. Both runtime and source archive are included in the final
bundle's file-hash inventory. A fresh output directory is required.

`ResearchLibrary.cmd` supplies the package-local OCR location. Installation and
`CheckInstallation.cmd` perform actual recognition of a temporary synthetic
image after verifying the delivered files. A recognition failure produces a
readable error and a nonzero result. They do not read the user's research data.

Developer source installations can use a locally installed Tesseract. An explicit
`CARR_OCR_DIR` selects a verified portable runtime; an invalid explicit selection
fails instead of silently using another engine. The application also discovers
packages beside its interpreter environment. It does not search the current
working directory for executable code or change system PATH. Language models
are passed explicitly, and runtime/model changes prevent mixed-version resume.

## Evidence limits

Publisher package sizes and SHA256 values are checked against the official
repository metadata obtained over HTTPS. A local file manifest is an integrity
inventory, not a publisher signature. The acquisition record identifies any
upstream signature entries not independently verified. Component notices and
source grants are preserved; the source inventory is review evidence, not a
legal certification.

Linux tests execute real OCR and verify source retention, multilingual extraction,
restart, explicit per-page approval and rejection of altered reviews. Cross-host
packaging and static Windows dependency inspection do not constitute native
Windows or NVDA acceptance. The installed OCR self-check reports its actual
platform explicitly.
