# Historical span approval integration (candidate)

Base a450bd9eb97b11764f0fe2d70d72efbf9788aa5d. This is an executable cross-candidate regression, not a production migration or new trust service. Reuses real pypdf 5.9.0 and PDFium 5.8.0 (via pypdfium2), ReportLab 4.4.9 test fixture and SQLite 3.46.1. All new PDF content is synthetic. The trusted approval rows in the fixture are not real reviewer authentication.

## Reproduction
In a scratch directory place these exact unchanged files from their respective candidate heads under src/carr_library:
- PR26 b03ea0dd033d8368fb75df723dfccf5b95e9adff citations/anchors.py (blob 0b3021f8c3c0de85f3ff555d7a066c100e6ef439).
- PR40 1695032aefc59707188020bda751d7c417dda56c citations/historical.py (4438576d15534f2f72a361e1ff3ef5d84b49f528).
- PR55 fa4efdcdbeab68dd1ca037c8f6a8701be6437f6d citations/pdf_review_attestation.py (fa7126a20d181c59962977a10a19938238036403).
- PR60 73f0a81114378c0ebd64ce40adcf67ec2bab6c73 citations/printed_label_attestation.py (1eb9e06b79a5e0a39354da0d358581a7598512e4).
- PR34 c90a0c6e3316aaa5974c20500f16dba01db88dfb reader/context.py (5253f61f57d9fb92b361a8b1ebeae0fc96b734a6).
- PR59 35811de3c15267c160075b81a2f008eeaafa7d23 research/span_snapshot.py (b59b736b4c49fd35c9badb040f59ab9be7d45919).

Place this PR's test under tests/citations. Run PYTHONPATH=src python -m pytest -q tests/citations/test_span_approval_integration.py on Linux. Python 3.13.5, pytest 9.0.2. Executed 8 PASS, 0 FAIL, 0.61s, exit 0. All six scratch source blobs match connector readback; test blob 3b80df5b31640da66e72ab3272b5fa49cc879e5c. The last test reproduces an API gap, not a passing security gate: stale Context.clipboard rechecks page text and labels but not metadata authorization. A fresh restore_span with the same historical loader rejects the revoked metadata. The other tests confirm exact two-page quotes after reopening SQLite, rejected text/label/metadata revocation, label drift, scope isolation and a forged author even after payload checksum recomputation.

## Assembly boundary
CARR-08/09 must use current authorized topic/evidence membership and restore_span with fresh trusted metadata, text and label lookups at every copy/export and context visit, in one caller-owned read transaction. Do not use an older Context as permission to emit citation data. No production caller integration or trusted approval persistence/authentication is proven by the fixture. CARR-05/01 own schema and transaction contract. CARR-13 owns security review. Existing source storage, metadata and approval authority need independent acceptance. Windows/NVDA/CI/model inference NOT_RUN.

## Upstream reuse
No new dependencies or parser rewrite. Existing PDFium source pin 9a41bfc7fb4ace979833edcb6920abf83d2b6fa3 at https://github.com/pypdfium2-team/pypdfium2 and pypdf source pin 2a91bd4d0b5bda90f2eae741e383813b6cda9721 at https://github.com/py-pdf/pypdf from earlier review. PR17 MIT cairnsearch import at eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 independently rechecked: all 8 files including LICENSE match exact upstream content and blob SHAs. No changes to that import.

External candidate reviewed: https://github.com/Receivingsignal/zotero-annotations/tree/4ea98c1f5fb383845c002875dae9f3fbd07eb12c (MIT LICENSE retrieved; zotero_annotation_tool.py inspected). Potential future user-authorized Zotero annotation import, NOT adopted at citation boundary: build_annotation_objects trims quotation text and guesses page index from sortIndex when position is absent; connect_zotero copies live DB/WAL/SHM separately, and private temp copies have no demonstrated lifetime guarantees. Not a source-exact or atomic snapshot proof. No execution, copying or private Zotero access. W3C Web Annotation Data Model https://www.w3.org/TR/annotation-model/ is useful for future interoperability, but normalized selectors are not identical to immutable raw offsets; no contract changes here.

Status REVIEW_REQUIRED, G3/U3 open.
