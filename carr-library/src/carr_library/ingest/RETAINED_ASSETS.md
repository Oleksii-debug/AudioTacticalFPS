# Original-byte retention candidate — CARR-10

Base a450bd9eb97b11764f0fe2d70d72efbf9788aa5d; continuation PR #31. Claim #11 comment 7191241836. Existing source snapshot (#20), writer guard and Ingest (#31) are reused by the adapter, not replaced. The content identity is the original SHA256, not extracted text. Retain once before creating a job; run/resume using the retained path after scoped journal lookup. The external source need not stay available. Actual source bytes are private; do not commit them or put their names in public logs.

## Reuse decision
Actual upstream MIT cairnsearch hash_file from PR #17 at 4c2ee5ba43a778a2b160325cfe48678eeab213d3, source https://github.com/smwaqas89/cairnsearch/tree/eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93, src/cairnsearch/indexer/hasher.py blob b872612f8470191b53af83e41af02f637cbaee69. All eight selected upstream files/license in PR17 match exact upstream Git blobs; the actual hasher blob was reconstructed and executed locally. Reuse the native same-filesystem no-clobber hard-link publication pattern of restore.py/bundle.py, not a new filesystem engine. Tested PDF reader/writer pypdf 5.9.0 already in runtime.

Evaluated HashFS MIT at https://github.com/dgilland/hashfs/tree/ee8c523e8edacccf643d68cb4e033fb740941a44 (LICENSE.rst blob 76825ba28620b0f22f07139eea0c78f8405dd103; hashfs/hashfs.py blob beb9b366d4da2865a9c48c4e904b50b4a0d8c1ad). Not adopted: _copy checks existence then uses shutil.move from a default temp directory; cross-device copy or race may expose partial/overwritten target, and duplicate path is not rehashed. realpath accepts arbitrary existing file paths; repair can delete or relocate evidence. Reuse would require overriding core behavior and is inappropriate for immutable original preservation. No upstream HashFS code copied or executed.

## Evidence
Linux Python 3.13.5, pytest 9.0.2, pypdf 5.9.0. Scratch tree with exact retained_assets.py and test_retained_assets.py, plus unchanged upstream hasher at bootstrap/vendor/cairn_extractors/hasher.py. Run from carr-library:
```
PYTHONPATH=src:. python -m pytest -q tests/ingest/test_retained_assets.py -k 'not real_ingest_retained_roundtrip'
```
10 passed, 1 deselected in 0.14s, exit 0. Actual local filesystem bytes, hard links, hash checks and a two-page PDF reopened using pypdf after external source deletion. Publication race fault injection and scope rejection via a journal test double, not actual full pipeline. Tested source blob 7cd2f1f27e186067cae1a80d66ee5464193ae038 and test blob edd45add5f532d08788b4c799695a47556ea7bce match GitHub readback. Full composed test test_real_ingest_retained_roundtrip added but NOT_RUN locally; requires assembled exact PR17/20/18/21/31 and reviewed filelock dependency. Do not treat deselection as pass. Direct GitHub clone unavailable (DNS); no Windows/CI evidence.

## Boundaries

The shared V3 application now supplies full database-and-originals backup through
`workspace_bundle.py`; its manifest covers published data in all workspaces,
including archived history. Pending import journals and unregistered staging
assets remain excluded. The older revision-only bundle discussed below remains
unchanged. See `docs/contracts/APPLICATION_V3.md` for current integration scope.

Internal trusted service only, no authorization via digest; caller must hold shared cooperative writer guard, supply stable private directory and enforce workspace authorization on all UI retrieval. No external hostile writers or shared/network filesystem. TXT/PDF only, max 64 MiB per asset, no source deletion or GC; a failed import can leave an orphan original. Retrying `retain` with the same validated source and MIME can finish an interrupted publication by removing only positively identified private staging hardlinks. Every extra hardlink must be accounted for before cleanup; unknown links, changed bytes, ambiguous entries, symlinks and reparse points fail without deleting staging originals. `resolve` remains read-only and rejects extra hardlinks. Unbound empty staging directories are preserved. No directory fsync/power-loss durability claim. No full asset backup/restore yet; existing revision bundle excludes originals. Next: verified asset manifest derived from sealed revision references (including historical revisions), restore originals alongside revision snapshot, and compose catalog/topics/evidence with owners. CARR-05/02/08/01 review common schema and lifecycle; CARR-12/13/15 independently test. Windows/NVDA, full corpus and G/U gates remain open.
