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
