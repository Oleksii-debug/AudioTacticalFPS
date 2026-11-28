# Required CI entrypoint and exact-build evidence

The active root workflows are `carr-v3.yml`, `carr-windows.yml`,
`carr-native-launcher.yml` and `carr-bibliography.yml`.
They run reviewed repository code on disposable Linux/Windows runners with
read-only repository permissions and pinned Actions. Component review workflows
provide additional diagnostic evidence; a component PASS does not replace an
integrated required gate.

## Strict suite execution

From `carr-library` in the provisioned test environment:

```text
python scripts/ci_check.py --jobs 3 tests/packaging/test_ci_check.py
```

Pass explicit test files or pytest node IDs. The wrapper rejects missing or
out-of-tree selectors, empty suites, test errors/failures, skips, XFAIL and XPASS.
It disables inherited pytest options and plugin autoload, retains the actual
child return status and JUnit verdict, and requires cleanup of temporary test
data. Each suite owns a bounded subprocess and its descendants; concurrent
execution keeps independent results and never hides another failed suite.
A completed PID/readiness handoff is atomic so timeout cleanup cannot consume
a partially written owner record.

The wrapper's Python socket guards and telemetry opt-outs are an offline test
policy. They do not claim to isolate arbitrary native code. Native installed
acceptance separately installs verified Windows outbound rules for the actual
runtime executables, verifies active profiles/rules/application filters and
restores the previous settings in `finally`.

Tests which intentionally use a synthetic local HTTP peer carry
`requires_loopback`. Their explicit source/installed gate runs before runtime
egress denial; they must not be put inside `ci_check.py`'s socket-denied child.
Actual-provider access, model startup/download and paid calls are not part of
that synthetic loopback test.

## Platform selection

Required portable selectors contain only scenarios supported on that platform.
Real Windows widgets, Credential Manager, SAPI and User32 probes run on native
Windows. Real FIFO/symlink and fork boundaries run on POSIX. Unsupported native
cases are not counted as portable passes.

The focused integration workflow provisions actual FFmpeg/ffprobe before
effects/conversion acceptance and preserves their identities. Its Windows gate
runs each selected native probe even when an independent source suite fails,
provided the exact source, desktop and audio prerequisites installed correctly.
A failure remains a failure; collecting another independent verdict cannot
make the workflow green.

## Installed source and publication

The full Windows gate builds and verifies the source wheel, installs the sealed
bundle in a separate path and compares installed module hashes with the exact
event commit. Source imports cannot substitute for an installed module.
Artifacts retain the source/tree identity, dependency/runtime identities,
native reports, required suite results and the tested public application wheel.
Private models, recordings, credentials and user documents are not uploaded.

The two launcher jobs exercise MSVC and the pinned Zig toolchain on Windows.
They must correspond to the same delivered source commit as the application.
The installed standard-installer gate separately checks repair/uninstall and
retention of user-owned data.

## Citation, translation and local proposals

Citation generation uses the pinned quickjs-ng dependency and the retained
historical citeproc-js/styles. The independently pinned sort-macro adaptation
has a separate `runtime_adapter` receipt identity and attribution; unchanged
historical receipts remain readable. The exact original/adapted comparison and
seven-style tests retain the same memory/time/output budgets.

Advanced translation uses the existing gateway, vault, approved account scope,
cost ledger and derivative receipts. Its native probe deliberately uses
synthetic HTTPS with actual installed controls and a temporary Windows vault
entry. Local proposal tests use synthetic loopback replies followed by the
actual typed plan review. These checks do not qualify paid-provider billing,
a private model's output quality, acoustic behavior or human NVDA operation.

## Reading results

Use the Actions run and artifact bound to the candidate commit. Do not transfer
an older run's PASS to a reconstructed tree, a different dependency pack or a
new installer. `V3_RELEASE_READINESS.md` records implementation boundaries and
the gate inventory. Physical Windows11/NVDA, actual devices/voices/models,
multilingual quality and signed distribution remain separate verdicts.
