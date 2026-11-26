# Fresh citation action boundary

Candidate for CARR-08/09 integration; no new database schema, UI, parser or model. The stale Context clipboard reproduction in PR62 motivates this adapter. Use copy_authorized_span from the trusted application action handler, never the stale context. The read factory must start a fresh consistent transaction for each action, authorize current workspace, topic and evidence membership, return the exact selected record, and supply trusted historical and metadata readers. The displayed snapshot checksum binds selection; it is not an authorization token. No external caller may inject the factory. Return text only after successful context-manager exit; host must not write to clipboard on any error. Authorization is at the fresh read transaction, not a guarantee against subsequent revocation. Real production trust service and schema integration remain CARR-01/05/08/13 work. This adapter deliberately reuses the existing clipboard method, including its second resolution; benchmark and optimize only after correctness acceptance.

## Exact dependency provenance and execution

Combined isolated Linux scratch directory, not a merge. Existing code reconstructed from exact fetched source; each Git blob verified against the pinned remote:
- PR26 b03ea0dd033d8368fb75df723dfccf5b95e9adff, citations/anchors.py, 0b3021f8c3c0de85f3ff555d7a066c100e6ef439.
- PR40 1695032aefc59707188020bda751d7c417dda56c, citations/historical.py, 4438576d15534f2f72a361e1ff3ef5d84b49f528.
- PR34 c90a0c6e3316aaa5974c20500f16dba01db88dfb, reader/context.py, 5253f61f57d9fb92b361a8b1ebeae0fc96b734a6.
- PR59 35811de3c15267c160075b81a2f008eeaafa7d23, research/span_snapshot.py, b59b736b4c49fd35c9badb040f59ab9be7d45919.

Python 3.13.5 / native SQLite 3.46.1, real disk SQLite fixture. In combined carr-library root: PYTHONPATH=src python -m pytest -q tests/citations/test_authorized_action.py. Actual result: 12 passed in 0.27s, exit 0. New executed blobs: authorized_action.py 58b6809cabf8f333d8aaba69af7544b3660f2a4b; test_authorized_action.py 0061cd0ae99627197c61a14ae3032127d8f54a02. Synthetic authorization and source approval fixtures only; no actual PDF render or label attestation exercised this run. Coverage: original Unicode exact copy, physical fallback, known label display, scope mismatch, revoked membership, deleted and moved evidence, revoked metadata and second-page source approval, changed checksum, note separation, wrong reader identity, context-manager error and invalid positions/modes. No actual clipboard, production reviewer authentication, Windows/NVDA, CI or ONNX inference claim.

## Public upstream reuse decision

Native SQLite transaction/query facilities and previous candidate APIs suffice here. Inspected citeproc-py https://github.com/citeproc-py/citeproc-py/tree/f6a1f79e41b05a373e09ec387aca41b9b1277441, BSD-2-Clause LICENSE blob b7b097bbc746d5ce1aa3be77b50fb7e35fcfce8b and examples/citeproc_json.py blob 4ac9105aa7e51714bd80fee0ebd7fa24056f81cc. Ready CSL bibliography formatter, potentially useful as a later export adapter, but no source authorization, historical revision or membership proof in that formatting API. Not adopted at integrity boundary; no new dependency, no execution of upstream code, no copied code. Zoteus remains an optional separate research integration rather than authority for our sealed citation. Existing MIT cairnsearch from PR17 is unchanged; not re-executed here.

No merge or gate closure. Independent review of exact assembled source and application action binding required.
