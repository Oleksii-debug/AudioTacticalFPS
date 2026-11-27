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

RTF is enabled only after the user explicitly selects an installed Pandoc
executable. The worker binds its byte hash and invokes fixed `--sandbox`, RTF
input and plain-text output options. No custom filters, templates, external
resource embedding, shell command or alternate reader is accepted.

PNG, JPEG, BMP, TIFF and WebP use the current local Tesseract runtime and its
existing Unicode path bridge. OCR requires an explicit language/configuration
selection; installed, managed Fast/Best and verified offline-pack authorities
remain the same as PDF OCR. Every frame is checked before runtime admission,
then decoded and recognized sequentially. Image-frame ordinals remain distinct
from physical PDF pages and printed pagination. Optional orientation evidence
is retained for review and never silently replaces the baseline text.

Legacy AZW/AZW3, CHM, DjVu, DOC/PPT/XLS, LIT, MOBI, PDB/PRC, TCR and WPD remain
unavailable. No converter, model, remote service, decryption or DRM bypass is
downloaded or supplied by this action.

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

For RTF pass `pandoc=Path('/explicit/installed/pandoc')` to the service. For
raster input pass `image_ocr=True` together with an explicitly chosen existing
`carr_library.extract.page_ocr.Config`; the default service never silently OCRs
an image or discovers and activates Pandoc from PATH. API callers retain the
same explicit input and destination selection rules.

`extract_many(sources, cancelled=..., progress=...)` processes a finite selection
sequentially. The progress callback receives `(completed_count, ConversionResult)`.
Generated batch names have a bounded ordinal and source stem. Completed pairs
remain available if a later source fails or the user cancels. Batches do not have
a durable extraction/resume journal; use the existing import workflow when
checkpoint recovery is required.

## Resource and filesystem boundaries

Defaults and maxima are 64 MiB per source, 32 MiB reading text, 16 MiB provenance,
10,000 records, 1,000 PDF pages/image frames, 1,000 selected files and a 120-second per-worker
deadline (configurable from 0.1 to 600 seconds). The supervisor polls cancellation
and terminates the process subtree. Its existing POSIX address/CPU limits remain
in force; Windows has process/time isolation, not a new address-space quota.
This is not an operating-system sandbox.

Raster defaults are 20 million pixels per frame and 80 million cumulatively
per input (the latter can be lowered or raised to at most 200 million).
Pandoc's explicit RTS heap allowance defaults to 512 MiB and is bounded from
64 MiB to 1 GiB. Its child inherits the existing worker's native process family;
there is no nested POSIX session that can escape cancellation. Diagnostics and
output files are checked during execution, and the existing outer supervisor
settles all descendants before private files are removed. Source-byte,
text-byte and total-time limits continue to apply to both adapters.

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
PDF/image model choices use the existing OCR selection UI. RTF additionally
opens an explicit local-executable picker; cancelling either choice starts no
worker and creates no derivative.

## Verification status for this source candidate

On Linux/Python 3.12, the current source ran 60 cases with zero skips: 30 existing
conversion cases, five portable UI cases, 19 selected-adapter boundary cases
and six real optional-tool cases. The latter exercised installed Pandoc
3.1.11.1 and local Tesseract: RTF, an external-field control, a small output
limit, Unicode PNG, two-frame TIFF and cancellation after raster staging.
This is source/host evidence. It does not establish native Windows dialogs,
every OCR language, installed Windows runtime identity or screen-reader use.

Required strict gates:

- `tests/conversion/test_conversion.py`
- `tests/conversion/test_conversion_ui.py`
- `tests/conversion/test_selected_adapters.py`
- `tests/conversion/test_selected_adapters_native.py` only in a gate explicitly
  provisioning compatible Pandoc and English Tesseract. Missing tools fail;
  the suite has no skip or simulated-success fallback.
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
