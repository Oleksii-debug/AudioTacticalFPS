# Completed-audio inspection: exact e9 focused failure

The focused Windows run **failed**. Its source phase reported **272 passed / 2 failed** in 42.26 seconds; its isolated installed phase reported **272 passed / 2 failed** in 42.25 seconds. The separate native inspection phase passed **29/29 checks**. This is evidence for the exact e9 run, not an aggregate qualification or a result for a repaired successor.

- Source: [e9bba260fd66cfe5217f8081778fcdfc9dfd202c](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/e9bba260fd66cfe5217f8081778fcdfc9dfd202c), tree `c6a3df5fe5e6ed328ee6d1552d7360f1b94e0d9e`.
- [Run 46107488747 / job 140863383870](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46107488747/job/140863383870), attempt 1, completed 2026-11-28 at 04:34:47 UTC.
- Unmodified decoded job log: 242,203 UTF-8 bytes; SHA256 `d4bf2bf747055d49e55a41af521f0d7fdbe3b96c9a0defda2e9e3e5332fc7cad`.
- Artifact 15248467350: 473,546 bytes; API/uploader SHA256 `a76bfdf3c0c272a31b9abf53d2e782953b7c4984bac90335c82e07142d5974bb`. **The artifact ZIP was not downloaded, decoded or rehashed.**

## Actual failure

Both modes failed only the new `test_helper_private_directory_cleanup_waits_for_owned_process_and_reader_settlement` variants `deadline` and `cancel`. At line 571, `first_refusal.is_set()` was false. The preceding expected TimeoutError/InterruptedError and elapsed-time checks completed. The test did not observe its intended native sharing refusal.

The fixture requested only FILE_READ_ATTRIBUTES 0x80. Microsoft's [sharing algorithm](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/8c0e3f4f-0729-49f4-a14d-7f7add593819) requires the relevant data/execute/write/append/delete access for that conflict; [FILE_LIST_DIRECTORY uses bit 0x1](https://learn.microsoft.com/en-us/windows/win32/fileio/file-access-rights-constants). The reviewed test-only successor [fe1d474](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/fe1d4744fb5b758fb006771d1e78280c278f9f7d) adds this access bit while retaining every actual-refusal, process, stream, reader, retry, timeout and cleanup assertion. It had **source review only** when this record was written. These observations do not identify the holder of the earlier c31 transient directory lock.

## Native result and source bindings

The actual installed native receipt matches all 29 labels, 12 metrics and 13 implementation origins against the exact e9 source and oracle. It exercised named criteria controls, invalid-value refusal, Cancel, a completed real synthetic-PCM inspection, clipboard copy, approved worker-thread export, changed-owner cancellation and cleanup. All 32,000 PCM frames and their recorded authored-pause overlap were accounted for; source, project and completed production bytes remained unchanged. The receipt explicitly leaves the native file picker, manual NVDA and physical audio untested and acoustic accuracy unqualified.

The source-bound wheel receipt reports 442 payload files and 373 Python runtime files. Both 56-package runtime inventories match the integrated lock. The actual FFmpeg 9.1 executable identity, selected test/helper/runner hashes and 13 native origins match their current source bindings. Physical wheel/runtime payloads were not downloaded by this review.

## Population and acceptance limits

The committed oracle declares 274 cases / 117 function identities in four complete modules: production 62, Studio 105, CLI 30 and helper 77. It preserves 148 prior cases and adds 126. **Both failed adapters stop before their JUnit Counter-validation step**, so this record keeps the complete declared Counter separate from the observed 272 + 2 pytest summaries. No independently verified full executed Counter or post-test interpreter receipt is claimed.

The native, firewall restoration and ephemeral-work cleanup steps succeeded. Final aggregate validation correctly failed because both test phases failed. The full integrated main run is separate. No result here is transferred to another head or used to claim real-model/language quality, manual Windows 11/NVDA behavior, listening quality or distribution clearance.

The accompanying JSON retains the complete declared Counter, failed phase receipts, exact native label/metric/origin maps, source bindings, job steps and artifact metadata.
