# Reuse-first decisions and evidence (26 November 2026)

## Adopted and executed
- SQLite FTS5 (https://sqlite.org/fts5.html): tested 3.46.1 with unicode61, BM25, external-content index and insert trigger. Parameterized FTS, post-filter against raw page offsets and scoped collection membership. No separate search server or own inverted index.
- pypdf (https://github.com/py-pdf/pypdf; BSD-3-Clause): tested local 5.9.0 on two-page generated PDF. Candidate keeps physical page index; printed label is unknown unless separately supplied/verified. This is a tested version, NOT a production dependency lock; review security and current stable versions before packaging.
- Python stdlib: SQLite transactions and composite foreign keys, SHA-256, JSON, paths and text. Do not introduce extra dependencies for functions already supplied by stdlib.

## Candidates, NOT integrated yet
- pypdfium2 (https://github.com/pypdfium2-team/pypdfium2), Apache-2.0/BSD-3-Clause bindings and PDFium BSD-style plus third-party notices. Render pages to bounded images; use Tesseract (https://github.com/tesseract-ocr/tesseract; Apache-2.0 plus dependencies) for offline recognition. Validate native Windows deployment, engine and language data hashes, timeouts, cancellation, OCR quality and licensing. OCRmyPDF (https://github.com/ocrmypdf/OCRmyPDF; MPL-2.0) optional separate integration, not mandatory due to dependency overhead and PDF rewrite; preserve originals.
- Zotero (https://www.zotero.org/support/notes and https://github.com/zotero/zotero-docs/blob/main/content/collections_and_tags.md): reference for collections, tags, notes, bibliographic interoperability. Consider export/import formats or an adapter; not a mandatory installation.
- calibre (https://github.com/kovidgoyal/calibre; GPL-3.0), Recoll (https://recoll.org/; GPL), Paperless-ngx (https://github.com/paperless-ngx/paperless-ngx): evaluate complete products first, but direct embedding is not selected at this point because of UI/architecture, deployment or licensing constraints. Reassess interoperability; do not copy source without a concrete compatible licensing decision.
- sentence-transformers (https://github.com/huggingface/sentence-transformers; Apache-2.0 library): optional multilingual semantic search after lexical baseline; model weights need independent license and offline benchmarks. Current candidate thematic mode is keyword/BM25, not semantic embeddings.
- python-docx and an EPUB adapter: test structured heading/paragraph extraction, unsafe archive bounds and stable location anchors; no invented printed pages for reflowable files.
- tk-uia (https://github.com/HuzPro/tk-uia; MIT): Windows accessibility candidate for Tk if selected. Upstream documents UIA tree support, not independently validated NVDA speech. Compare against native widgets/wxPython; physical Windows/NVDA acceptance required. PyInstaller or another packaging tool needs pinned versions, license notices and offline installer tests.

## Existing projects
Inventory: 14 connected repositories' tree/metadata inspected; relevant source excerpts inspected in the shortlist. Existing public projects contain OCR adapters, document extraction/resource bounds, chunking, context search and accessibility audit approaches. Private projects contain book import, reading semantics, local library and stable location workflows. No private source or private repository details copied into this public repository. Do not assume public visibility grants an open-source license. Reuse behavior and independently implemented contracts, or use separately authorized/licensed code after exact-version review. Detailed private inventory may be retained in Drive.

## Executed candidate evidence
Files: bootstrap/universal_library.py and bootstrap/test_universal_library.py.
Command in local Linux/Python 3.13.5 environment: python -m pytest -q
Result: initial test exposed silent duplicate-membership rejection bypass (no data leakage demonstrated); fixed explicit workspace ownership check; second run 2 passed in 0.17s.
SHA256 code: 005b2944d8e3a61cc49a7c770a9faef0fbb4cc33399ec544a3b1b782c108acfd
SHA256 tests: 92cd05adca41a0312eaf81a822f2e39d2a6e354577e34a2d2902a31b65ed632d
Drive roundtrip byte hashes matched original tested files; GitHub readback matched uploaded UTF-8 code. Tests exercise persistent independent workspaces, multiple collections, topic tree and cycle rejection, Unicode, PDF and text import, physical/unknown printed page, source exact quote, note updates, export, cross-workspace rejection and reopening database.
Limitations: prototype only. No GUI, scanned PDF OCR, full semantic ranking, verified printed page inference, source binary archival/revision migration, robust untrusted file sandbox, complete CRUD or backup/restore. FTS rank can be influenced by global corpus statistics despite result scope filtering; production isolation review needed. PDF extraction is native text only and trusted local input; not verified against scan images. No Windows/NVDA run, no real Carr corpus, no production release. Review candidates and integrate into owned module contracts instead of maintaining parallel production implementation.
