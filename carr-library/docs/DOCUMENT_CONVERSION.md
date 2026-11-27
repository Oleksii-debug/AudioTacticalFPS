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

Binary DOC/PPT/XLS, the WordPerfect header family, and PalmDoc `TEXt/REAd` PDB
now have explicitly selected LibreOffice adapters. The user selects the actual
installed `program/soffice`, `soffice.exe` or `soffice.com`; document metadata
and PATH never select a converter. The worker verifies fixed IMPORT/EXPORT
filter declarations, creates ODT/ODP/ODS in a private stage, then passes the
result through the current guarded ODF reader. Other PDB families are refused.
This implemented path does not qualify every selected LibreOffice build: the
per-filter host evidence, including the outstanding XLS failure, is below.

AZW/AZW3, CHM, DjVu, LIT, MOBI, PRC and TCR remain unintegrated. The inspected
host did not provide `ebook-convert`, `djvutxt`, `ddjvu` or a CHM extraction
tool. Pandoc is not a substitute for unsupported input filters. No converter,
model, remote service, decryption or DRM bypass is downloaded or supplied by
this action.

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

For the selected legacy formats, pass
`office=Path('/explicit/LibreOffice/program/soffice')`. Selection captures the
local installation directory and component identities without executing code.
The queued job rechecks that selection, hashes its bounded component inventory,
and the worker independently verifies those bytes and required fixed filters.
The inventory includes the selected launcher, `soffice.bin`, Windows
`soffice.exe` when selecting `soffice.com`, present known POSIX launch helpers,
and the main/Writer/Calc/Impress registries. It does not claim to seal all native
libraries, system fonts or the entire LibreOffice installation.

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

LibreOffice uses a new `UserInstallation` directory for every operation. Its
fixed profile disables macro execution, active OLE/DDE content, OLE automation,
untrusted document links, and Calc link updates. It has no trusted document
locations and does not use a persistent personal profile. Fixed headless
arguments accept no document-supplied options, shell commands, macros, plugins
or remote server mode. The child inherits the existing import-worker process
family; its outer owner still settles descendants before removing the stage.
The same immutable-reader leases protect selected components during native
execution, with final identity/hash checks before accepting the intermediate.

The temporary tree has a 4,096-entry/16-directory-depth bound and a polled byte
bound of twice the selected source limit plus 64 MiB for the private profile.
Diagnostics are separately bounded to 256 KiB per stream; the ODF intermediate
cannot exceed the selected source-byte budget. Enumeration and permission
errors are failures. A disappearing native temporary file is allowed; links,
reparse points and aliased files are not. The deadline is checked before native
launch as well as during execution. `MAX_CONCURRENCY=1` and disabled OpenCL
avoid unrequested computation fan-out; these are not OS CPU or memory quotas.
The existing 3 GiB POSIX address cap stays unchanged. The desktop passes its
current shared resource coordinator, and that lease remains held through native
cleanup and both output publications. Standalone API callers can pass the same
coordinator with `extract(..., broker=...)` or `extract_many(..., broker=...)`.

These controls configure the selected application; they are not an OS network
sandbox or proof against defects in an arbitrary native converter. Primary
contracts used here are LibreOffice's [command-line parameters](https://help.libreoffice.org/latest/en-GB/text/shared/guide/start_parameters.html),
[Common security configuration](https://github.com/LibreOffice/core/blob/master/officecfg/registry/schema/org/openoffice/Office/Common.xcs),
[Calc link-update configuration](https://github.com/LibreOffice/core/blob/master/officecfg/registry/schema/org/openoffice/Office/Calc.xcs),
and [thread-pool concurrency preference](https://github.com/LibreOffice/core/blob/master/comphelper/source/misc/threadpool.cxx).

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
opens an explicit local-executable picker; the five selected legacy suffixes
use a separate LibreOffice picker. Cancelling a required selection starts no
worker and creates no derivative.

## Upstream historical evidence and R7 verification status

This R7 composition ports the selected adapters into the frozen R6 PDF and
import stack. Its own execution status is **NOT_RUN** until the exact composed
tree completes the portable, installed and explicit native gates below. The
following historical observations belong to upstream commit
`078d254bd4ab4dbe9de4ea80e1e26731cb8ac9e8`; their test counts and host results do
not establish acceptance of this composition.

On Linux/Python 3.12, that upstream source ran 60 cases with zero skips: 30 existing
conversion cases, five portable UI cases, 19 selected-adapter boundary cases
and six real optional-tool cases. The latter exercised installed Pandoc
3.1.11.1 and local Tesseract: RTF, an external-field control, a small output
limit, Unicode PNG, two-frame TIFF and cancellation after raster staging.
This is source/host evidence. It does not establish native Windows dialogs,
every OCR language, installed Windows runtime identity or screen-reader use.

The selected-office checkpoint added and ran five strict source suites on
Linux/Python 3.12: **176 passed, zero failed, zero skipped**. This includes the
existing conversion/UI/selected-adapter and ODF suites, plus 22 new admission,
identity, staging, deadline and inactive-content-profile cases. The ordinary
ODF reader accepts only empty converter form containers and inert vector
geometry; populated/unknown controls or hidden text still fail.

Actual native evidence uses the explicitly selected host LibreOfficeDev
26.8.0.0.alpha0, with original synthetic documents. The final finite native
selection ran **six passes**: four format cases plus cancellation and text-byte
budget enforcement. The XLS case was deliberately excluded from this passing
selection after reproducing its failure; it is not counted as qualified.

| Fixed input filter | Intermediate | Actual host result |
| --- | --- | --- |
| `MS Word 97` (`.doc`) | `writer8` ODT | Passed: exact source hash, readable text and provenance |
| `MS PowerPoint 97` (`.ppt`) | `impress8` ODP | Passed: ordered text and unverified logical locators |
| `WordPerfect` (`.wpd`) | `writer8` ODT | Passed: synthetic WordPerfect 5 document-header family |
| `PalmDoc` (`.pdb`) | `writer8` ODT | Passed: synthetic uncompressed `TEXt/REAd` container |
| `MS Excel 97` (`.xls`) | `calc8` ODS | Implemented and installed filters verified; native host conversion failed on memory, not qualified |

The XLS failure also occurred with an independent one-cell XLS produced with
the test-only xlwt 1.3.0 writer, so it is not solely the LibreOffice fixture
exporter. Observed outcomes were native exit 137 or `std::bad_alloc` under the
existing 3 GiB address cap. One bounded diagnostic recorded 2,168,999,936 bytes
RSS and 2,790,580,224 bytes virtual memory before failure. Limits were not
raised and no derivative was published. A stable selected Windows LibreOffice
run is still required, including XLS; this one development build does not prove
that XLS is unsupported by other compatible runtimes. xlwt is not a product
dependency and neither the 17- nor 53-package Windows lock was changed.

Required strict gates:

- `tests/conversion/test_conversion.py`
- `tests/conversion/test_conversion_ui.py`
- `tests/conversion/test_selected_adapters.py`
- `tests/conversion/test_office_selection.py`
- `tests/conversion/test_office_native.py` only when a compatible actual
  LibreOffice is explicitly selected with `CARR_SOFFICE`. Missing tools fail;
  the full seven-case gate must pass before claiming all five native formats.
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
