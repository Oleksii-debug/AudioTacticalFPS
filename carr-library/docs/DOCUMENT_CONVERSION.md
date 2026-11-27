# Reading document conversion

The conversion action creates a **new UTF-8 reading text file** and a sibling
source-hash provenance report. The original selected document is never edited.
This is an additive facade over the current document reader registry and the
existing isolated import worker. It does not install another extraction engine.

## Current supported scope

The registry enables TXT, PDF, DOCX, EPUB, HTML/HTM/XHTML, Markdown/MD, EML,
FB2/FB3, ODT/ODP/ODS, PPTX and XLSX. Each existing reader retains its conservative
container, XML, MIME, fidelity and resource checks. A particular malformed,
encrypted, ambiguous or unsupported document can still be rejected.

The capability list checks installed reader dependencies, including the exact
python-docx and defusedxml revisions required by their readers. This is
dependency evidence, not a successful extraction, language qualification or OCR
model test. PDF OCR uses the application's current local model selection and
its existing reader. Missing/failed OCR, unreadable nonblank pages and mixed
native/image pages fail explicitly instead of publishing an apparently complete
partial transcript. Use the reviewed import workflow for those cases.

Legacy AZW/AZW3, CHM, DjVu, DOC/PPT/XLS, LIT, MOBI, PDB/PRC, RTF, TCR and WPD,
and standalone image/TIFF formats remain unavailable in this adapter. No Pandoc,
Calibre, remote service, model download, decryption or DRM bypass is supplied.

Reading separators join ordered extracted records. The report preserves each
record's exact output character interval and text hash, its logical locator,
physical PDF page when available, and reader warnings. Logical locations do not
become printed page numbers. Every derivative remains unverified for quotation.
Strict UTF-8 TXT preserves CR/LF/form-feed and other characters; its optional
UTF-8 BOM is omitted.

## Public API

```python
from pathlib import Path
from carr_library.conversion import ConversionBudget, ConversionService

source = Path("/chosen/source.docx")
destination = Path("/chosen/reading-output")  # Existing local directory.
service = ConversionService(
    destination,
    input_roots=(source,),  # Only explicitly selected files/directories.
    budget=ConversionBudget(timeout=120),
)
result = service.extract(source, "reading.txt", cancelled=lambda: False)
print(result.path, result.report_path)
```

`extract_many(sources, cancelled=..., progress=...)` processes a finite selection
sequentially. The progress callback receives `(completed_count, ConversionResult)`.
Generated batch names have a bounded ordinal and source stem. Completed pairs
remain available if a later source fails or the user cancels. Batches do not have
a durable extraction/resume journal; use the existing import workflow when
checkpoint recovery is required.

## Resource and filesystem boundaries

Defaults and maxima are 64 MiB per source, 32 MiB reading text, 16 MiB provenance,
10,000 records, 1,000 PDF pages, 1,000 selected files and a 120-second per-worker
deadline (configurable from 0.1 to 600 seconds). The supervisor polls cancellation
and terminates the process subtree. Its existing POSIX address/CPU limits remain
in force; Windows has process/time isolation, not a new address-space quota.
This is not an operating-system sandbox.

The facade copies a bounded selected source into a private local stage, hashes
that copy, and checks the original identity/hash again before publication.
Symlinks, reparse points, network paths, alternate streams, invalid portable
filenames and aliased source hardlinks are rejected using the existing shared
filesystem guards. Outputs remain inside the selected local destination and
outside a removable program folder. The desktop also keeps external conversion
outputs outside the research library's registered internal namespaces.

Text and report use the current atomic no-replace publication helper. Existing
files are never overwritten. The provenance report is published last as the
completion marker. Failure before that move rolls back only an unchanged text
created by the current operation. A process crash or directory-sync failure can
leave an unreceipted text, or a matching pair whose durability was not confirmed;
the operation reports the error and does not claim completion. Such files are
left available for inspection rather than silently replaced on retry.

The report schema is `carr-reading-derivative/1`; it records exact source/text
SHA256 and sizes, source filename/format, extraction method, elapsed time,
segments, warnings, `original_modified=false`,
`printed_pages_reconstructed=false` and `verified_for_quotation=false`.

## Desktop integration

Root integration must call
`carr_library.ui.document_conversion.install_conversion_actions(frame)`
after installing the native menu bar. It registers stable commands
`conversion.documents` and `conversion.formats`, so the existing keyboard
settings and command launcher can discover them. It uses the current
`DesktopFrame.run_job`, Cancel event, and close-safe completion callbacks; it
does not own a second executor. Source and output selection use native dialogs.
PDF model choices use the existing OCR selection UI.

## Verification status for this source candidate

**NOT RUN.** The executor was unavailable when this patch was prepared.
Earlier tests and platform evidence do not qualify this new adapter.

Required strict gates:

- `tests/conversion/test_conversion.py`
- `tests/conversion/test_conversion_ui.py`
- Existing import worker, source-snapshot and cancellation selections.
- Existing current document-format, DOCX, EPUB and PDF native-text selections.
- Installed-product `-I` / `--import-mode=importlib` collection with the same
  real dependency set and no source-tree package leakage.
- Native Windows menu/command/cancellation and screen-reader acceptance after
  the root desktop hook is wired.

The new tests exercise actual supervised TXT, HTML, Markdown, email, FB2,
DOCX, EPUB and native PDF conversion, source hashes and locators, real budget
rejections, copy cancellation, original mutation, no-overwrite races, receipt
publication failures, batch retention and isolated dispatch validation. Mocked
UI lifecycle and filesystem-failure cases are narrow boundary tests, not
physical desktop or OCR language acceptance.
