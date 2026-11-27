# CI entrypoint and source identity

## Current owner and commands

The active source and installed-package gates are declared in
[the integrated Windows workflow](../../.github/workflows/carr-windows.yml).
The smaller audio, bibliography, trust, sync, managed-update, capacity and
large-source workflows exercise their own explicit contracts. A verdict belongs
to the exact source SHA and test inventory recorded by that run.

From `carr-library` in the already provisioned, trusted test environment:

```text
python scripts/ci_check.py tests/packaging/test_ci_check.py tests/packaging/test_source_wheel.py
python -I -B scripts/ci_check.py --installed tests/packaging/test_source_wheel.py
```

The wrapper uses the existing pytest runtime in an isolated child. Missing
suites, empty collection, skipped cases, XFAIL, XPASS, test errors and failures
are not a passing required gate. The workflows enumerate the required suites;
a smaller ad hoc selection does not replace that inventory. Pytest autoload and
inherited pytest options are disabled. The runner is not a sandbox for arbitrary
Python code: use reviewed public source and synthetic fixtures, without user
data or unrelated credentials.

Installed mode checks the selected package files against the checkout before
pytest starts. Its inventory follows the explicit setuptools package recipe,
rather than admitting every development helper beneath `src`. Development
bootstrap helpers may remain in the source archive without becoming wheel
payloads. The actual source-archive-to-wheel regression independently checks
that separation.

## Application wheel, source commit and tree

`packaging/source_wheel.py::verify_application_wheel` reads the exact committed
pyproject and source blobs with Git replacement objects and lazy fetch disabled.
It checks the explicit package/data/license inventory, RECORD, entry points,
name, version, path aliases and complete wheel payload. Its receipt binds:

- the full source commit;
- the full source tree resolved from that same commit;
- the SHA-256 of the exact wheel bytes that were inspected;
- the application name/version and counted runtime payloads.

The tree field reuses canonical commit
`e3cf74c243d4ef930b526b5648ca9003c0900057`, blob
`478de248451d59a44b35784eb808fb2974f0af67`. It does not weaken the separate
commit check or make equal version strings proof of equal source. Existing
regressions verify a historical commit while checkout HEAD names different
source, and refuse a stale wheel even when both commits have the same version.

The bundle builder retains the verifier's wheel digest through copying. The
managed A-to-B gate also records commit, tree, bundle-manifest and setup hashes.
Those identities remain authoritative for the exact artifacts; timestamps and
the development version alone are not a reproducible-build or release claim.

## Unified development version

The reconciled source uses `0.2.0.dev3` in pyproject, the package version,
and the root associations of both runtime SBOMs. It succeeds the current dev1
and parallel canonical dev2 identifiers without presenting the union as a
released or signed product. CycloneDX document revisions advance from 3 to 4;
the existing serial numbers and original dependency-generation timestamps are
retained. Dependency components, hashes, licenses and graph edges are unchanged
apart from the root-reference spelling. This is not a claim of a new uv export.

The 17- and 53-dependency locks and FULL FFmpeg 9.1 source lock remain byte-exact.
Regression tests compare those complete input files and reconstruct the previous
SBOM bytes after removing only the reviewed root/revision edits. Setup derives
the displayed version from its actual sealed wheel. Existing dev1/dev2 artifacts
keep their original receipts; no old result is relabelled as dev3 qualification.
The exact commit, tree, wheel, bundle and setup hashes still distinguish builds
that share a development version.

## Dependency and citation runtime

The current base Windows profile contains 17 pinned dependencies; the semantic
profile contains 53. Their matching CycloneDX files retain the admitted
dependency names, versions, hashes, licenses and dependency graph. Optional
spelling and trusted-client components have separate reviewed inputs. Adding an
optional component does not silently rewrite the base dependency locks.

Citation processing uses the current local citeproc-js payload through pinned
`quickjs-ng`. It does not require a separately downloaded Node executable.
Installed bibliography, source-integrity and document gates use the wheel built
from the selected source. Historical Node-selection records do not qualify this
current implementation.

## Native launcher reconciliation

The shared launcher build scripts, PE checker, C quoting core, child probe,
manifest, resource file and toolchain lock are byte-identical to the canonical
e3cf source. The following differences are intentional mappings to existing
owners, not omitted replacements:

| Canonical capability | Current owner and evidence gate |
| --- | --- |
| Pinned Zig 0.14.1 build and actual Windows synthetic child | `carr-windows.yml` builds and runs the exact C source before the full bundle; `carr-managed-update.yml` repeats it for both selected setup sources. The archive length and SHA-256 remain pinned. |
| Native installed entrypoint | The standard-installer and managed-update probes invoke the actual `ResearchLibrary.exe` through self-test/native installation checks. The separate `probe_installed_windows.py` remains available for an already installed private bundle. |
| Optional MSVC build | The exact canonical `build_msvc.ps1` is retained. Its receipt records the selected compiler/tool hashes. The current required delivery lane uses the pinned Zig build; no MSVC execution is inferred from it. |
| Canonical `audio/bin` and Node PATH prepending | Current `audio.runtime` admits the sealed FULL FFmpeg 9.1 `audio-runtime` layout, while citations use QuickJS. The launcher preserves PATH and supplies the existing program/OCR bindings. It does not activate the obsolete Node/audio layout. |
| Managed setup exclusion | The current C launcher holds the shared installation lease until its Python child exits. Application/maintenance owners retain their cooperating leases. This protection is absent from the canonical launcher delta and must survive reconciliation. |

The canonical standalone native workflow duplicates the required pinned-Zig
contract above and also offers the optional MSVC lane. It is not transplanted
over the existing full-bundle and two-setup workflows. The existing integration
fragment retains `CARR_CI_LAUNCHER` so the exact native-tested executable and
receipt are sealed by the builder, without copying synthetic children/compiler
caches into the product.

Keep the current FULL runtime lock and package-specific transfer eligibility
gate. A successful private native check does not grant redistribution rights or
authorize publishing a sealed audio payload. Authenticode, physical output,
NVDA, and optional-model quality remain separate evidence.

## Translation and local proposal gates

Advanced v3 uses the current provider gateway, credential store, per-job consent,
budgets and checkpoint validators. Native probes can use synthetic HTTPS
responses with actual installed widgets; that is not an actual Google request
or a provider-quality result. Local proposal validation stays in the current
typed assistant service.

The strict runner permits numeric loopback only for the two declared local HTTP
fixtures: `tests/loopback/test_local_planning.py` and
`tests/loopback/test_translation_local_adapter.py`. Other destinations remain
blocked by that fixture policy. The Windows sealed-installation phase separately
enables and verifies executable-scoped outbound firewall rules for the selected
runtime programs, then restores the prior profiles and removes its own rules in
`finally`. Neither mechanism claims a whole-machine sandbox. Each native/HTTP
probe reports the exact boundary it exercised.

## Historical prototype evidence

The original 2026-11-26 prototype used pytest 9.0.2 and its native JUnit output.
Its seven-case Linux run, subsequent eleven-case XFAIL/XPASS correction, and the
then-unavailable Windows/CI qualification are historical records. The old
root-workflow authorization block and pypdf 5.x/7.x conflict are superseded by
the active workflows and current pypdf 7.1.1 lock; they are not current blockers.

Upstream sources: [pytest exit codes](https://docs.pytest.org/en/9.0.x/reference/exit-codes.html),
[the reviewed pytest source](https://github.com/pytest-dev/pytest/tree/3d10b5148e03eb82b3ee29181dbdc73cf82699e2),
and [the XFAIL/XPASS implementation](https://github.com/pytest-dev/pytest/blob/3d10b5148e03eb82b3ee29181dbdc73cf82699e2/src/_pytest/skipping.py).
No extra test engine or package manager is introduced by this reconciliation.
