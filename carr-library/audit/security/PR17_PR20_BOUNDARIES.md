# CARR-13 — independent security and rights review, 2026-11-26

Verdict: REVISION_REQUIRED for unrestricted imports and release G2/G5/G7. Candidate-only trusted local inputs remain explicitly out of release acceptance. No production code edited and no private corpus inspected.

Base a450bd9eb97b11764f0fe2d70d72efbf9788aa5d; PR17 head 4c2ee5ba43a778a2b160325cfe48678eeab213d3; PR20 head 4694b0d31cce3635b80d22ae7c13eb67b76ed7a1; PR18 head c118f8e7ba848f8f3c2286d1d0df7f87be788e70; PR19 head 5c4853d8dce3bf397e4f2c240727ac03ee7fc48b.

## Executed evidence
Linux Python 3.13.5, SQLite 3.46.1, pypdf 5.9.0. Put PR17 carr-library directory and PR20 carr-library/src directory on PYTHONPATH, then run:
`python -m pytest -q carr-library/tests/security/test_pr17_pr20_boundaries.py`
11 cases: 8 PASS, 3 FAIL, 2 warnings, 0.42 seconds, exit 1. The test file is an intentionally failing release regression suite against the exact candidates, not evidence of a failed base CI run.
Local Git blob SHA matched GitHub for PR17 universal_library.py (803f463fd4ae92f3bf97af50e1b892c4b187cd47), external_reuse.py (e8db699528710cd4930507ead147ae75da63cfb5), and PR20 intake.py (87d3bb0af3051058961b4674057f2724c0bf5e06). Test blob 4722b9c0bea879b0d544d8950d2e95f886d4ceb0. Test inputs all synthetic.

PASS: scoped search/foreign workspace save and export rejection with query punctuation; four archive path checks; HTML script contents not indexed; PR20 symlink rejection and immutable snapshot bytes. These narrow checks are not general XSS, FTS, or filesystem safety certification.

## Findings (deduplication keys)
- CARR-SEC-001, high release risk, PR17: TXT and HTML imports accept links to synthetic files outside selected staging folder (two failing cases). Trusted-local-only prototype, not an exploit of an existing production sandbox. Require all routes to use the existing PR20 bounded intake or equivalent reviewed root confinement (extend supported types, reject reparse points and link races). No reopen of original source. Windows junctions and hostile concurrent replacements must be independently tested.
- CARR-SEC-002, medium/high release risk, PR17: stale-size simulation on native TXT route accepts a 10-byte fixture under a reduced 5-byte adapter budget; delegated read bypasses shared bounded snapshot. One failing case. Require one bounded byte snapshot for all formats, parser in resource-limited worker and no partial publication. No oversized allocation or dangerous PDF used in this audit.
- CARR-SEC-003, high release blocker, PR17/18/19: PR17 requirements specify pypdf>=5,<7; PR19 pins 5.9.0 explicitly for synthetic smoke only; local runtime 5.9.0. Upstream GHSA-5jq2-8x83-x246 (CVE-2026-102994, high, fixed 6.18.0) and GHSA-fhff-j4m8-439h (moderate, fixed 7.0.1) establish affected dependency ranges. No exploit executed. PR17 range excludes the latter fix. Upgrade candidate: pypdf 7.1.1, published 2026-11-18, PyPI attested publisher SHA f0174823a10baf2a6bf387470c2ecb29f8637ab4, wheel SHA256 04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1, BSD-3-Clause LICENSE fetched at exact commit (blob ab327d0312875458ba74983867f1b2622b6de39d). Reuse upstream patches, not custom PDF parsing. Fetch and install hash-verified wheel, rescan current advisories, test parser compatibility, then independently verify process/resource isolation. Download blocked by DNS in local execution environment; upgrade NOT_RUN, not claimed secure.
  Sources: https://github.com/advisories/GHSA-5jq2-8x83-x246 ; https://github.com/py-pdf/pypdf/security/advisories/GHSA-fhff-j4m8-439h ; https://pypi.org/project/pypdf/7.1.1/

## Upstream rights and release boundary
Verified 8/8 GitHub blobs in PR17 (including MIT LICENSE) match declared source at https://github.com/smwaqas89/cairnsearch/tree/eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93, including UPSTREAM.json recorded blob IDs. Preserve these files and notices. PR20 manifest identifies MIT filetype.py subset; its adapter passed the two targeted checks. Broader attribution and transitive license review pending. No blanket legal guarantees.
PR17/18 parsing is not yet sandboxed; full SBOM, XML/archive resource limits, CSV/export injection, no-network enforcement, Windows install/ACL and NVDA, real source rights, backup/restore and independent green CI remain open. No gate closed by this audit.
