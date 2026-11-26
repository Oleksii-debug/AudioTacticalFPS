# CARR-15 independent release acceptance — 2026-11-26
Base: a450bd9eb97b11764f0fe2d70d72efbf9788aa5d. Candidate: PR #17 head 4c2ee5ba43a778a2b160325cfe48678eeab213d3. Verdict: REVISION_REQUIRED, not a release. Separate actual Carr collection acceptance from general release.

## Actual reuse verified independently
Source https://github.com/smwaqas89/cairnsearch at eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93. MIT LICENSE and seven Python source blobs fetched from both upstream and the candidate: all 8/8 identical to the vendor manifest. Local code/test blobs matched candidate for universal_library.py (803f463fd4ae92f3bf97af50e1b892c4b187cd47), external_reuse.py (e8db699528710cd4930507ead147ae75da63cfb5), test_external_reuse.py (265bbf8875321bb831eec1b1029b5d07d69947e4), test_universal_library.py (dceacef0f7b8330b6a6895af21199532e11cfe1c) and all seven imported Python source blobs. Use existing extractors, not a replacement.
Linux Python 3.13.5 / SQLite 3.46.1. From candidate carr-library root: python -m pytest -q bootstrap -> 4 passed in 0.45s, exit 0. Separate probe: PYTHONPATH=. python /absolute/path/to/pr17_acceptance.py -> 3 tests, 1 PASS, 2 FAIL, exit 1 (0.036s); script blob d921ddb8f2c0e60740679045095a4ed1be81838c. Use a separate audit checkout for the script; no changes to candidate. Direct git network fetch unavailable in execution environment; verified local bytes against GitHub connector and upstream instead.

## Blocking defects / acceptance mapping
- CARR-REL-001 (U3/G3): known gap in PR17 comment, now independently reproduced as a failing regression. Import Markdown text railway bridge; query railway, save evidence, export. Actual physical=1 and exported number=1, printed label null. Expected no physical page for unpaginated formats; separate logical locator kind/id, never show a fabricated physical citation. Owners CARR-04/08/10. Do not replace the upstream extractors.
- CARR-REL-002 (U4/G3): global FTS5 BM25 corpus statistics influence another workspace's thematic ranking. Workspace A contains two one-word pages, railway and river. Query both: Rail,River. Add 12 railway pages in workspace B; same query in A returns River,Rail. No direct foreign result is returned, but ranking is not isolated; a limit=1 query can expose the influence. Use workspace-specific FTS index/statistics or genuinely scoped rank; do not merely filter global results. Owner CARR-05/07 with CARR-06. Regression must pass after fix.
- Positive check U1/U3: identical title/passage in unrelated workspaces; correct author, only one hit, persisted evidence and no export from other workspace after restart. PASS on synthetic candidate, not complete U1/U3.

## Gate matrix at observed refs
| Gate | Evidence / verdict |
|---|---|
| G0 | Scope V2 and lane issues exist; contracts and final review not complete, CANDIDATE |
| G1 | PR17 4 existing synthetic tests PASS; no complete GUI More/copy walkthrough, CANDIDATE |
| G2 | PR18 c118f8e7ba848f8f3c2286d1d0df7f87be788e70 reports 5 local PASS; not independently executed here, integration/security/resume unverified |
| G3 | PR17 two independent regression failures; semantic benchmark NOT_RUN, REVISION_REQUIRED |
| G4 | Windows/NVDA walkthrough NOT_RUN |
| G5 | PR19 5c4853d8dce3bf397e4f2c240727ac03ee7fc48b reports 5 local PASS; actual clean Windows install/uninstall/upgrade/offline restore NOT_RUN |
| G6 | Carr collection BLOCKED_MISSING_CORPUS per control record; separate non-Carr lawful real fixtures NOT_RUN |
| G7 | NOT_READY; no approved integrated release SHA |
| U1 | Partial persistence/workspace isolation PASS; CRUD/archive/membership lifecycle incomplete |
| U2 | Partial topic tests; notes/tags/saved queries incomplete |
| U3 | CARR-REL-001 FAIL; drift/immutable provenance not fully accepted |
| U4 | CARR-REL-002 FAIL; multilingual and relevance benchmark incomplete |
| U5 | Partial JSON evidence export; backup/restore/Windows/NVDA NOT_RUN |

PR17/18/19 observed open, unmerged; workflow run lookup returned zero runs for each exact head; review lookup returned zero submitted reviews. Base combined status list empty. Absence of checks is not green CI. CARR-12/13/14 evidence not yet available on corresponding issues at inspection. No gate marked DONE, no merge, no release. Next: rerun regressions on integrated exact head, test PR18/19 independently when their bytes are available, verify Windows portable install, offline launch, import/OCR, literal/thematic results, More, copy, restart, cancel, upgrade and restore via NVDA and keyboard. Require independent logs and a lawful non-Carr fixture before general release.
