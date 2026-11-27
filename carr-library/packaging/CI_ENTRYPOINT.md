# Provider-neutral CI entrypoint (candidate, not an active workflow)

This is a small wrapper around the existing pytest dependency in PR #17. No new test engine or package manager. Run only on reviewed code and synthetic fixtures, in a disposable checkout with no private data, tokens, or production credentials. The runner is not a security sandbox for arbitrary Python tests.

From carr-library in the already provisioned, trusted test environment:

    python scripts/ci_check.py tests/packaging/test_ci_check.py

After the coordinator integrates the prototype and other suites, explicitly enumerate the required suite paths (e.g. bootstrap/test_universal_library.py, bootstrap/test_external_reuse.py, tests/packaging). Do not substitute a smaller suite for a required gate. A missing suite, zero tests, skips/xfail, test error or failure returns nonzero. Optional platform-only tests belong in separately declared jobs; report NOT_RUN for the missing platform rather than treating this command as Windows/NVDA evidence. Pytest autoload and inherited pytest options are disabled; any additional test plugin must be deliberately configured and reviewed. Do not run on an untrusted PR checkout with credentials.

Upstream reuse: pytest 9.0.2, MIT, official tag resolved to https://github.com/pytest-dev/pytest/tree/3d10b5148e03eb82b3ee29181dbdc73cf82699e2 ; LICENSE read at exact commit. Reuses the locally installed pytest CLI and its JUnit XML output (https://docs.pytest.org/en/9.0.x/reference/exit-codes.html). No third-party source copied; no claim of wheel-byte provenance or complete lock. PR #17 at 4c2ee5ba43a778a2b160325cfe48678eeab213d3 already declares pytest and imports MIT cairnsearch; all eight manifest files incl LICENSE were rechecked at upstream eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 and match exactly. Leave those sources unchanged.

Linux Python 3.13.5, pytest 9.0.2, pluggy 1.6.0: the command above returned exit 0, 7 passed, 0 failed/skipped, 12.78s. Regression tests actually invoke pytest on passing, failing, empty, skipped and missing suites, path escape, and inherited collect-only option; Unicode/space temporary paths. Windows and external CI NOT_RUN. This is not a claim that any production PR's complete test matrix passes.

CI activation blocker: GitHub Actions requires a root .github/workflows directory (https://docs.github.com/en/actions/tutorials/create-an-example-workflow). Root modifications are outside the authorized scope. No workflow, repository settings, or statuses changed. Minimal exception, if separately approved: one root .github/workflows/carr-library-ci.yml scoped to pull_request base carr-library, read-only contents permission, pinned reviewed actions, ephemeral Linux/Windows runners, no secrets, no publishing, explicit required tests and hash-locked wheels. Alternative: an explicitly authorized external CI runner reporting checks at exact PR head. Neither route is currently active.

Security dependency blocker: PR #17 requirements-external.txt currently excludes pypdf 7.x (pypdf>=5,<7), while CARR-SEC-003 proposes 7.1.1. This mismatch must be resolved by the prototype/extraction owner before a common lock is generated. PyPI publisher commit f0174823a10baf2a6bf387470c2ecb29f8637ab4, BSD-3-Clause, wheel SHA256 04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1 (https://pypi.org/project/pypdf/7.1.1/#files). Download attempt failed locally; upgrade install and compatibility tests NOT_RUN. Do not ship the old 5.9.0 synthetic-only pin. Complete dependency lock, SBOM, clean installer and external CI remain open.

## 2026-11-26 regression correction (CARR-01 independent finding on PR #33)

Explicit `xfail(strict=False)` can yield XPASS with pytest exit 0, and JUnit records it as a passing testcase. The wrapper now invokes the existing native pytest API in the isolated subprocess with a `pytest_runtest_logreport` hook observing `wasxfail`; any actual XFAIL/XPASS yields nonzero (existing failure codes preserved). The XML counter is explicitly labeled junit_passed; it is not the acceptance decision. An inactive conditional xfail marker remains a genuine pass. No new dependency. Upstream MIT implementation inspected at https://github.com/pytest-dev/pytest/blob/3d10b5148e03eb82b3ee29181dbdc73cf82699e2/src/_pytest/skipping.py .

Reproduced against exact old executable blob b0897720de641a636ca799746905b7a1d5c6689e: expanded suite 9 passed, 2 failed (explicit non-strict XPASS and mixed pass/XPASS), exit 1, 20.93s. After correction: `python -m pytest -q tests/packaging/test_ci_check.py` 11 passed, exit 0, 19.84s; `python scripts/ci_check.py tests/packaging/test_ci_check.py` 11 passed, exit 0, 20.34s. Linux Python 3.13.5 / pytest 9.0.2 / pluggy 1.6.0. Tested blobs: scripts/ci_check.py a9743a4f90bbd1f5ec9b26d3c69007274bdde770 and tests/packaging/test_ci_check.py c8b6b6dc0a9507315e2a1b9b657120bb77dd8a71. Windows/actual CI NOT_RUN; independent exact-head review pending. No changes outside carr-library or release status.

## 2026-11-27 exact citation runtime selection

The active V3 Windows workflow prepares Node from the existing private citation
lock, checking the archive size/SHA-256 and all selected file hashes before any
execution. Each consumer explicitly prepends that verified directory and checks
both PowerShell command discovery and the actual Python child's `shutil.which`
result against the pinned executable path and hash. Selection evidence is saved
before the strict guard; native smoke applies outbound firewall coverage to that
same executable. A preparation PASS alone does not establish native execution.

The later diagnostic for source `912b705` established that the explicit
PowerShell Application lookup returned both the pinned and system Node paths.
Each consumer now selects the first effective command result, then still
verifies that exact path and hash against both the lock and Python discovery.
The successor must pass both complete Windows workflows at its own exact source
commit. Prior test counts and uploaded artifacts are not transferred as PASS.

## Installed translation and local proposal gates

Advanced v3 uses the existing gateway, vault, consent and checkpoint contracts;
its native probe uses synthetic HTTPS responses with actual installed widgets
and credentials. Local proposal validation is covered by strict offline suites.
The separate native/HTTP gate permits loopback for a synthetic local model peer
while blocking other IPv4/IPv6 destinations for the selected Python and
PowerShell executables. It verifies each active firewall rule and address filter,
then restores the prior profiles in `finally`. This is executable-scoped evidence,
not a whole-machine sandbox or actual-model quality qualification.
