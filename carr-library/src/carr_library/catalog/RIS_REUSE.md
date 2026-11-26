# RIS bibliography intake — CARR-02 candidate

Reuses actual MIT-licensed MrTango/rispy parser at commit b7aae3b2069ced3fb75287711300f2edf0bcac21:
https://github.com/MrTango/rispy/tree/b7aae3b2069ced3fb75287711300f2edf0bcac21

- Original parser blob: 8417de2a00c9afdf114455c1b61808a919b94d80.
- Imported parser blob: f89d29b915c590ff530cc6f3cc65f6a97245a445. Only change: relative import points to existing CARR-08 shared config (PR #29); no parser logic changes.
- Original MIT license retained: 61a240b3b02b5ff26c10deaed955fa7f32940dde.
- Required existing shared config: PR #29 head 2b85bd2d92da831d7e607dc6f1611f8799a0c561, path carr-library/src/carr_library/research/_vendor_rispy/config.py, blob a66e088afbd88134cd49a4fe173ad080f95a5c42 (identical to upstream). Do not duplicate it or merge this PR before the dependency is present.

The wrapper accepts a bounded UTF-8 RIS batch (4 MiB, 5000 records, 100000 lines, 16384 characters/line). It rejects malformed framing, unfinished records and ambiguous multiline tags rather than accepting partial output. All metadata is unverified; repeated authors and conflicting title candidates retain their order. A batch hash plus ordinal locates the metadata input, not the book, edition, page, passage or verified citation. No attachment resolution, URLs, filesystem reads, external calls, metadata enrichment, deduplication, or new edition identities. No schema changes. Unsupported encodings and multiline input fail closed; future support requires explicit tests. A real Zotero export, roundtrip, persistent import and authorization are not yet accepted. UI must treat these fields as plain text.

Reproduction: compose this candidate with the exact shared config from #29 in an isolated checkout; from carr-library run PYTHONPATH=src python -m pytest -q tests/catalog/test_ris_preview.py. Linux, Python 3.13.5: 15 passed in 0.09s, exit 0. Direct clone DNS unavailable; source read through GitHub connector, locally staged and git blob hashes verified against upstream before the run. Actual parser logic executed, no mocks of parsing. Tested adapter blob 0da26a89199d9f8a1d81f9a502a2ffc852fe5f1a; tests blob 9e29df1bdcb84634320ca5600e4da54e113aab6b. All new executable blobs match GitHub readback. No installed package or new runtime dependency. Windows/NVDA, full CI, persistence and independent acceptance NOT_RUN. Synthetic fixtures only. PR17 cairnsearch eight original source/license blobs independently verified against its pinned upstream again; unchanged. No release gate closed.
