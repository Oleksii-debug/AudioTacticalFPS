# Canonical Save All convergence — 2026-11-27

## Exact incremental integration

The actual `carr-library` ref advanced from
`a44fac04072389caa40e0d6e799b13797712f797` to
`e5c64a5245583f8f8b404a0f042f6ca438c24d7f` at 16:43:44 UTC through reviewed PR183.
The PR metadata still reported the older base SHA; direct branch/ref reads and
the actual Git graph identify the current canonical source.

Full untruncated tree comparison found exactly two changed paths:
`src/carr_library/ui/save_all.py` and `tests/ui/test_save_all.py`.
Both updated V3 specifications and all other canonical files are unchanged.

The reviewed composition is `8ea8a6ef8fe68bee83bde1f2d64287e4f495cc1e`
(tree `9985c289ea755e9d7d94d4a673a9a609d1f3eba8`). The merge records that composition first and the
new canonical commit second. The actual common ancestor is `a44fac0`.
[Machine-readable resolution](acceptance/canonical-e5c-merge-resolution.json)
binds all two base/canonical/current/selected blob identities and leaves no
unresolved path. The earlier 481-path convergence ledger remains intact.

## Resulting Save All behavior

A cancellation arriving after the final confirmed durable save does not turn
a complete operation into an unsaved result. Clean remaining documents do not
invent failures. Cancellation before any confirmed write and cancellation with
retained edits/failures still remain visible.

Failures belong to the exact registered document, including when a new
document reuses the same key. The final dirty scan retains newer drafts
separately. Focus skips stale registrations and unusable targets, selecting the
first usable live failed draft after completion and pending-close observers.

The current `has_unsaved`, `after_current` and complete `defer_close`
function remain byte-identical. The test file retains its 17 current cases,
all 14 exact canonical cases and 2 added observer/close composition cases:
**33 expected, not yet executed at preparation**.

The three separately reviewed process-observer follow-ups change fixtures
only; their exact observed failures and preserved assertions are recorded in
[the observer evidence](acceptance/PROCESS_OBSERVER_FIXTURES_20261127.md).

## Verified source boundaries

Relative to `5f18481654277425ab8af170575de978e7a2a69c`, the only application
source change is Save All. Build inputs, scripts, workflow files, package
version `0.2.0.dev3`, 17 core / 53 semantic pins, both SBOMs and the FULL
FFmpeg9.1 payload identity are unchanged.

Large-source, pending-import recovery and complete-corpus production owners
remain byte-identical to that source. Their selected runs
[large sources](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46032433858)
and [capacity](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46032433929)
retain their actual `5f184816` identity. They are not relabeled as this merge.
The new integrated/installed/native runs must execute the changed Save All and
observer tests and bind the new bundle to its actual commit/tree.

## Earlier complete audio execution

The exact `c55d24e310e2ed4e8919a9b63b1695cbbcca49fb`
[audio run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46029884588/job/140610712214)
is terminal SUCCESS:

| Stage | Actual result |
| --- | --- |
| Required recording/native contracts | 80 suites; 1,579 passed, 0 failed, 0 skipped |
| Actual numeric-loopback translation | 31 passed, 0 failed, 0 skipped |
| Broader provider/audio recovery | 1,604 passed, 6 explicitly reported skips, 22 passing subtests |
| Installed native scenarios | SAPI/effects/capture/clips/publication/book/schedule/managed and legacy queues/templates/translation succeeded |

The six broader-suite skips cover POSIX-only paths, two unavailable actual
FluidSynth/TimGM6mb cases and an absent Windows audio output device. They do
not establish those device/runtime capabilities. Manual NVDA and listening
quality remain NOT_RUN. Stage counts overlap and must not be summed as unique
test coverage.

Artifact `15220185148`, SHA-256
`7e3e881f61991aa1ac2d0a53c27f142cdbebe405e852e99d3903520b69eb47e3`.
This prior execution proves its own source; new Save All execution remains
pending at preparation. Current verdicts belong in PR99/Actions without
changing source merely to refresh a status line.
