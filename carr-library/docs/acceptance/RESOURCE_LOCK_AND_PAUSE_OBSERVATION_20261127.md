# Resource lock recreation and recording pause observation — 2026-11-27

## Evidence before this correction

Executed source: `a7d3da4125c8f38dfe801740a5bf7b12f9d3605a`; tree: `a74ed6ad5e3837b968a1a2c0a58e9ee8e8e4d082`.

The MAIN Windows run [46067594697](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46067594697), job `140736573719`, stopped in its required source stage after 121 suites: 2,808 PASS, 1 FAIL, 0 SKIP. The failing case was `tests/operations/test_resources.py::test_actual_process_contention_has_no_overlapping_admission`. A spawned child failed while constructing ResourceBroker: `_unheld_path` observed an absent `.admission.lock`, but another contender recreated the leaf before the exception handler's presence check. The handler re-raised the stale FileNotFoundError (WinError 2). The parent's later queue timeout was a consequence. The full MAIN bundle/setup stages did not execute.

Failure artifact: `15232082599`; 75,280 bytes; SHA-256 `2c2f5aede73881cd9289861122c4c33663c1bbc9e93db1fdb77dc7a6c7aa6054`.

The Audio run [46067594546](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46067594546), job `140736593206`, reported 1,617 PASS, 1 FAIL, 0 SKIP across 81 required source suites. `test_initial_pause_never_opens_input_and_has_bounded_wait` received the expected pause-allowance error without opening input, but its timer enclosed the entire capture call: shared resource admission, immutable FFmpeg validation and digest, disk checks, staging, pause and cleanup. The observed duration was approximately 1.343 seconds against a one-second pause assertion. Production acknowledges `paused` only after those preparatory operations and starts the pause budget there. This evidence establishes an incorrect test measurement boundary; it does not establish a scheduler-latency or product pause-budget defect. The separate installed receipt stage, broad regression and final self-test did not execute in this failed job.

Failure artifact: `15231574991`; 67,440 bytes; SHA-256 `5cc7e59b9f79e88dd8a89070b75e160e2d9e25332b0f5a902bf7377671135ed3`.

The same a7 MAIN source-native Import Center workflow passed its corrected real Tab, resize, scrolling, footer visibility, import, exact-search, quote and reopen checks. The new failures do not revoke that specific result.

## Product correction: an unowned, recreated native lock leaf

Candidate `cf7c6b412db3049cce2ac415210fde2780617a69` changes `operations/_locks.py` and its existing resource test module.

- Repeat the complete normal local path and regular-file validation when a previously absent, unowned leaf is present again.
- Catch only FileNotFoundError. On that boundary, validate the existing parent as an actual directory. Return an absent leaf only when it is still absent.
- Limit the operation to three complete observations. Continued churn raises ResourceStateError before native acquisition. No sleep or larger deadline is introduced.
- Keep the complete NativeLock class byte-identical: exact filelock backend/pin, nonblocking native acquisition, held descriptor/path identity, close-only handling, release and fork ownership checks are unchanged.
- Keep all 21 prior resource cases byte-identical, including the actual spawned-process no-overlap test.

Fourteen additional deterministic cases cover construction and acquisition after one or two recreations; rejection of recreated hardlinks, directories and reparse points; missing/non-directory parents; access denial; and the bounded repeated-churn rejection. The fixture performs a real missing-leaf observation and recreates the leaf before propagating the same exception. Reparse metadata is represented directly without requiring privileged Windows symlink creation. The complete resource module now has 35 cases.

## Fixture correction: measure the acknowledged pause

Candidate `29a092df3555221a5c1b191a25f0a1dd6b56623d` changes only the failing test in `tests/audio/test_recording_controls.py`.

The callback records the first actual `paused` notification. It does not reset the timestamp on later notifications. The real capture, admission and immutable runtime validation still execute. The test independently requires the expected pause-allowance error, no input opening, an actual pause notification, elapsed pause time below the original one-second limit, and no remaining pending files. The test's `MAX_PAUSED_SECONDS=.02` and the elapsed-time limit are unchanged. Production recording code is unchanged.

## Existing Fast coverage

Candidate `ee656d28ee4190071a899c55215092c2cae11183` adds `tests/operations/test_resources.py` to each of the two existing source and isolated-installed selectors in `.github/workflows/carr-v3-contracts.yml`.

All other workflow bytes, dependencies, time limits, installed-origin proof, network policy and loopback selectors are unchanged. The existing Windows and Linux jobs will each run 23 source suites and 22 installed suites, plus their unchanged two loopback suites. The resource module uses the already pinned filelock dependency and needs no FFmpeg. The Audio module is not added to Fast.

## Review and execution boundary

The root and an independent packaging reviewer checked the lock and Audio changes against the terminal failure logs and current production owners. A separate reviewer checked the two Fast selector additions. The lock's held-owner class, the 21 prior resource cases and the remaining Fast workflow were checked for exact byte preservation.

This file records a source-review checkpoint: the corrected composition has not executed when this document is authored. It is not a runtime PASS. The PR execution matrix must link subsequent actual source identities, outcomes and artifacts. The required resource replay must include both operating systems and isolated installed code; the full MAIN bundle/setup and Audio stages remain required.

## Applicability of earlier scale evidence

The completed 257 MiB/129 MiB and 2 GiB TXT/PDF, 513 MiB PCM and 1k/10k/50k corpus runs retain their actual executed sources (`203bee5a` and `5f184816`). Their fixture harnesses and direct application/backup owners remain unchanged.

This correction changes a lower-level native-lock helper used by resource admission. Therefore the earlier measurements are historical scale baselines, not proof that every current transitive execution path or the current wheel is byte-identical to those runs. Current resource correctness is separately checked by the unchanged process-contention case and the added bounded-recreation cases in source and installed code on both operating systems. Do not relabel the earlier artifacts or performance numbers as a new full-current-wheel execution.

Manual NVDA, physical audio and listening quality, actual private-engine inference, per-language qualification, full external-reader/DAISY acceptance and production publisher/signing/rights remain separate. No private payload, credential or binary release is included.
