# CARR-03: page OCR reuse candidate, 2026-11-26

Actual adapter to installed upstream libraries and executable, not a new OCR implementation. Upstream release commit references resolved through GitHub:
- pypdf 5.9.0, BSD-3-Clause, https://github.com/py-pdf/pypdf/commit/2a91bd4d0b5bda90f2eae741e383813b6cda9721
- pypdfium2 5.8.0, BSD-3-Clause / Apache-2.0 plus PDFium and bundled dependency notices, https://github.com/pypdfium2-team/pypdfium2/commit/9a41bfc7fb4ace979833edcb6920abf83d2b6fa3
- Tesseract 5.5.0, Apache-2.0 plus native dependency notices, https://github.com/tesseract-ocr/tesseract/commit/64eab6c457b2337dd690746a5fde5c222b40d5f8
- Pillow 12.3.0 (MIT-CMU installed metadata); tests use ReportLab 4.4.9 and pytest 9.0.2. DejaVu Sans font required by fixture generator; no font distributed here.

These are installed versions tested locally; not a reproducibly built source supply-chain attestation. Network download from runtime unavailable (DNS failure); no upstream code vendored or downloaded during this run. Exact release commits resolved; packaging must verify binary/traineddata digests, current advisories, notices, Windows executable discovery and offline installer. PR #17 remains the non-PDF extractor candidate; this independent scope does not duplicate it.

From carr-library root with these packages installed and local Tesseract eng/ukr models on PATH:
    PYTHONPATH=src python -m pytest -q tests/extract/test_page_ocr.py

Linux local result: 5 passed in 2.57s, exit 0. First run failed because PdfPage did not support context manager; fixed using contextlib.closing. Tests exercise native text, scanned page, blank, mixed layout, missing engine, simulated timeout, pixel limit, invalid language/index and Ukrainian OCR. English and Ukrainian synthetic single lines exact after outer whitespace trim (0 character and word errors for those two lines only); general CER/WER NOT_RUN. Source SHA256 58e5c096e9b8d6df736f82a076074fcd942fa49d9569aadcd88be6050d405030; test SHA256 63c94fe1f433b374e4dd6a8e58f0256be9f37a92fea653d47b2617e126c3b227.

Candidate only, trusted inputs. Never mark OCR or inferred labels verified. Raw native and OCR text/TSV retained separately; mixed page requires review. No automatic citation publication. Page count and render pixels bounded, subprocess timeout implemented; parser CPU/memory/time isolation, raw output bounds, malicious PDFs, orientation detection, native geometry, source snapshot across pages, durable checkpoint, integration into ingest/DB, Windows, independent review and real-corpus quality remain open. G2 not passed.


## Source-loss corrections and assembled regression, 2026-11-26

Actual PDFium/Tesseract regressions reproduced two source-loss paths: unquoted OCR TSV was interpreted as quoted CSV, dropping literal quotation marks or merging subsequent word rows; text rendered at RGB250 was incorrectly classified as an empty page. OCR TSV now disables CSV quote processing and automatic blank status requires an entirely white raster. Layout rows are validated atomically against the rendered page; invalid rows retain original text and TSV with `ocr_layout_invalid`, without exposing partial blocks. Each OCR text/TSV read is capped at 16 MiB, and oversized output yields `ocr_output_limit`. The complete serialized checkpoint remains subject to the existing independent ingest limit.

Runtime: Linux Python 3.12.14, pypdf 7.1.1, pypdfium2 5.13.0, Pillow 13.0.0, Tesseract 5.5.0, ReportLab 4.4.9, pytest 9.0.2. Existing eng/ukr/slk/osd models were loaded from the pinned model directory. Three actual-parser/engine regressions failed before the fix. The new import regression reproduced an incorrectly completed two-page document with silently omitted faint content; after the fix, it remains quarantined with both physical-page checkpoints, including after journal reopen and retry, and never changes the original file.

Final independently executed assembled subset: **125 passed, 0 failed, 0 skipped**, exit 0, 41.28 seconds. From the assembled product root, set `TESSDATA_PREFIX` to the verified pinned-model directory and run `PYTHONPATH=src:. python -m pytest -q tests/extract tests/ingest/test_pipeline.py tests/citations/test_pdf_mapping_contradiction.py tests/citations/test_pdf_review_attestation.py`. This includes the current DOCX guard corrections from PR47 and integration dependencies assembled at `7924b6fd46887e626b876ce7040f0d35ce5b3a7e`. The two new test files use real PDF/engine fixtures for ordinary paths; malformed OCR output and resource-limit cases inject subprocess output explicitly. New OCR regression file alone: 9 passed.

This update preserves candidate/review status, separate native/OCR text and physical locators. It adds no new dependency, source download or neural model. Completely white rendered pixels are only the current bounded blank-screening rule, not a general proof about every possible PDF rendering. Native parser isolation, complete deadline enforcement, real corpus acceptance and Windows/NVDA remain separate product gates; no release declaration follows from this subset.

### Portable runtime reconciliation

The source-loss corrections and the two regression files above were imported from
Oleksii-debug/AudioTacticalFPS commit
`4574068581b34bdbc72127f4e9790bc09b38a486` (PR18) and reconciled with the
package-local OCR runtime. The original TSV validation, bounded output reads and
faint-scan handling remain intact. Runtime discovery, manifest integrity checks,
explicit portable tessdata selection, native orientation and extraction
fingerprints are preserved alongside them. This attribution identifies the
upstream correction; the assembled file also contains the portable integration.

Reconciliation verification on Linux, 2026-11-26: **136 passed, 0 failed,
0 skipped**, exit 0, 52.54 seconds. With `TESSDATA_PREFIX` set to the existing
verified eng/ukr/slk/osd directory, run `PYTHONPATH=src:. python -m pytest -q -p
no:cacheprovider tests/extract tests/ingest/test_pipeline.py
tests/citations/test_pdf_mapping_contradiction.py
tests/citations/test_pdf_review_attestation.py`. This includes actual portable
runtime inference without PATH installation, original source-loss regressions,
native extraction fingerprints, pinned OCR models and citation boundaries.
Native Windows/NVDA execution remains NOT_RUN.
