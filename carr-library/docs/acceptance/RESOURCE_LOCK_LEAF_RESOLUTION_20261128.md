# Resource lock leaf resolution — 2026-11-28

## Actual failed boundary

On source `325ccde85c7a8111465df4231ea97f573e4ec52d`,
[Fast run 46112983334](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46112983334),
Windows job `140880426412` completed with one source-contract failure:
`test_actual_process_contention_has_no_overlapping_admission`.
The source phase had 492 passing cases and one failure; the installed phase
passed 453 cases and the loopback phase passed 45, without skips.

The real spawned contender failed while constructing its ResourceBroker.
Its unheld admission-lock validation resolved the lock leaf, then the shared
directory guard rejected the resulting ancestor at its first network-prefix
check. The parent's queue timeout was a consequence of that child failure.
The supplied temporary directory was local. The log does not print the
resolved ancestor, so it does not independently prove its exact spelling.

## Source diagnosis and narrow correction

[CPython 3.11.9 ntpath.realpath](https://github.com/python/cpython/blob/v3.11.9/Lib/ntpath.py#L674-L734)
first obtains a Windows extended path, then performs a second native lookup
before removing the prefix. If the first lookup succeeds and the unowned leaf
disappears before the second, the differing native error retains that prefix.
This gives a concrete source mechanism for the observed refusal during
released-lock deletion; no raw path value is invented from the traceback.

Only resource-lock pre-acquisition validation changes. It resolves and checks
the existing parent, validates the exact leaf with the existing portable
filename rules, then checks any present leaf through the same regular-file
and hardlink guards. It retains the three-observation absence/replacement
bound, permission-error propagation, and the native acquired descriptor/path
identity proof. The generic model/audio network, device-path, mapped-drive,
symlink and reparse checks are unchanged. No extended path is newly admitted.

## Regression scope and qualification

All 35 existing resource cases and their behavioral assertions remain.
The real unlink / failed metadata read / recreation fixture now observes the
actual leaf validator. Its denial control raises only after the real release
has occurred; the existing bounded-churn and unsafe-replacement assertions
are retained.

Two new construction/acquisition cases delete the actual leaf immediately
after the parent lookup and require a real native lock with matching held and
named identities. On Windows they intercept the real native lookup return,
covering the first/second lookup boundary without sleeps. The POSIX branch
also requires parent-only resolution and performs the same real deletion.
Three additional absent-leaf cases retain device-name, alternate-stream and
trailing-dot refusals. Eight construction/acquisition controls inject actual
leaf metadata PermissionError and other OSError values before the initial
presence decision or after real deletion/recreation; those exact exceptions
must propagate before native acquisition and preserve the existing file bytes.
Both presence probes catch only FileNotFoundError. Expected resource cases are
now 48; unchanged Fast selectors therefore expect 506 source cases and 466
installed cases, plus the existing 45 loopback cases.

This checkpoint is **NOT_RUN**. The existing cross-process contention case,
all strict no-skip gates and the final native/package qualification still
require actual execution of the composed source. No runtime budget, fixture
deadline, lock ownership assertion or checksum requirement is relaxed.
