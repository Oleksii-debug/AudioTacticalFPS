# Resonance composition — execution and recovery checkpoint, 2026-11-27

This record preserves observed development work. It is **not a release, installed acceptance, or final platform completion claim**.

## Published source and validation anchors

- Canonical branch observed at `924ae83316a3cf9482aaf2ec51a6e6a5adb614c3`.
- PR143 application branch observed at `8c981c7e947f8d144c8706b5ea022990517c7dd9`.
- Reviewed public source archive checkpoint 6: `f30136619c028a1a95dd6ca17b63e020f1b9d407`.
- Current independent validation source: `4292bb0974009a5082f53cd36a921139aaf6bd8b`, [Actions run 45982468090](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45982468090).
- Main application work remained in a separate, locally resolved composition when the executor transport disconnected. That worktree has **not** been represented as a published final application commit.

Checkpoint 5 validation failed before runtime execution because one archived workflow contained a truncated tool-output string; the active application worktree still contained the complete valid YAML. Checkpoint 6 replaced the archive with the full 128,830-byte Windows workflow (Git blob `dd71a09369ec77e6d00181111c534fe25857c5aa`) and added archive-local `* -text` attributes to preserve committed bytes on Windows. Its actual local source check passed all 150 candidate identities, 137 Python compilations, YAML/embedded-Python parsing and dependency lock/SBOM closure. The fresh native PowerShell job `140449598522` completed successfully. Citation runtime and native process ownership jobs must be read independently from the run; neither is implied by the parser result.

## Actual local execution, before the infrastructure outage

The following are **separate component runs**. They overlap and must not be summed into a unique test total.

| Scope | Observed result | Qualification |
| --- | --- | --- |
| Five citation/regeneration suites, bounded QuickJS | 279 passed, 0 failures/errors/skips; 269.46 seconds | Python 3.12.14 / QuickJS-NG 0.18.0.2; exact source hashes unchanged |
| Public composition's exact 209-selector portable gate | 4,892 passed, 20 failed; 14 subtests passed; 615.22 seconds | 1,123 source/test/script/packaging files unchanged during execution; stopped at configured 20-failure boundary |
| Remaining portable selectors plus newly added trust/conversion/schema tests | 834 passed, 1 failed; 321.87 seconds | Provider-initiated cold-discovery cancellation returned timeout instead of InterruptedError; investigation remains open |
| Acquisition provenance and publication | 189 passed, 0 skips | Four intended negative cases first failed on the old implementation |
| Conversion and retained cleanup | 55 passed, 0 skips | Four intended negative cases first failed on the old implementation |
| Existing audio-conversion numeric controls plus resource ownership | 44 passed, 0 skips | Uses actual existing queued handler and FFmpeg, not a replacement conversion helper |
| EPUB, DAISY and audiobook publication / literal LRC text | 149 passed, 0 skips; 117.51 seconds | Two retained-stage/admission/render-lock regressions first failed; original actual voice test path preserved |
| Trust polling, native action ownership and installer-layout guard | 102 passed, 0 skips | Independent source review accepted; independent boundary selection 38 passed |
| Native-launcher source identity fixtures / source wheel / bundle | 78 passed plus 5 subtests, 0 skips | Three original KeyError cases reproduced; fixture schema corrected and four stronger seal assertions added; production verifier unchanged |
| Existing runtime-provider helper fixture | 21 passed, 0 skips | Retains actual child requests and persisted script/provider identity checks |
| Legacy citation-runtime compatibility fixture | 16 passed, 0 skips | Explicit legacy executable is rejected and canonical QuickJS failures cannot fall back to another engine |
| Sync, UI sync and authentic legacy compatibility | 320 passed, 0 skips; 41.55 seconds | Independent source review accepted; original three retained-path/foreign-HEAD failures reproduced |
| Typed backup adapters and related restore suites | 118 passed, 0 skips; 10.78 seconds | Resolves all 14 backup/pronunciation failures owned by that lane; adds 20 editor roundtrip/tamper/scope cases |

The code-only spelling component was reproduced through the existing pinned repacker and installed with the hash-locked optional dependency. Its upstream example dictionaries were removed by that existing workflow. No user dictionary, model, private engine or recording was imported.

## Reviewed changes incorporated in the local composition

- Canonical command registry, keyboard draft/import/save/undo/export semantics, dialog lifetime, progress and retained preview cleanup.
- Current canonical and shared branch changes reconciled with the retained research/citation resources and single bounded QuickJS executor.
- Immutable acquisition provenance bound to the actual imported asset and sealed revision; forged/replaced source identity does not receive another source's receipt.
- Document-conversion staging and resource ownership held until the exact native cleanup owner acknowledges; completed output/receipt survives later durability or cleanup failure.
- EPUB/DAISY staging, shared admission and render lock retained while the encoder still owns files; completed publication receipt and warning remain recoverable.
- Literal bracket-prefixed LRC text cannot create additional timestamp events.
- Optional signed-metadata polling is off by default, uses the existing client and performs no automatic payload download or installation. Rejected thread admission remains retryable.
- Native trust callbacks are bound to the captured library/controller lifetime; standard installer layout is refused before update-state creation.
- Windows reader/progress/keymap probes and the bounded DAISY resource resolver were corrected in the local source. Their native outcomes require fresh installed execution.

## Frozen adjacent work that still needs final integration

The sync proof has an independently reviewed 11-file package with base/new hashes and patch. It binds retained clip/pending input paths to the final destination through the existing canonical restore writer, retains exclusive final publication and complete re-attestation, and requires an owned successful HEAD replacement before classifying a durability interruption. Current public crypto implementation is unchanged.

The subsequent backup proof changes only two production modules: `backup_producers.py` adds exact passive adapters for the real pronunciation dictionary and Studio autosave; `backup_extensions.py` runs the existing reserved-name check before generic namespace dispatch. The adapters preserve unfinished editor text as data, enforce bounded typed fields, and never execute a regex or unfinished script during backup. Unknown namespaces, full-tree attestation, cancellation and authentic legacy restore remain strict. Final copying of its seven-file checkpoint was interrupted; its manifest write and final SHA readback are unconfirmed even though the 118-test result completed.

The single-PCM/effects work has completed several focused portable runs and an independent ownership review. It keeps the same worker/backend/buffers across failed device close and supports an explicit bounded retry. Final broad composition is **pending**. A later source review identified a required schema-preservation fix: existing `studio/clips.py` save/move paths must preserve a schema-3 effects project instead of forcing schema 2. That correction and real clip edit/move regression have not been executed. Do not label the entire effects handoff complete.

## Immediate recovery order

1. Recover the existing executor/workspace before reading or mutating its local candidates. Do not infer successful writes from interrupted sessions.
2. Preserve the original local `c04e9f56bbafbf341cbfd61cdeb23995506d8dd3` work and unresolved merge/index separately. The completed composition was built in an independent worktree; do not abort or overwrite the original recovery evidence.
3. Verify the frozen local proof manifests and exact file hashes before copying. Restore the sync and typed-backup candidates, then finish the effects/clip schema preservation case.
4. Diagnose the one actual provider-cancellation failure. Keep the cancellation assertion; a timeout is not an acceptable substitute.
5. Add new regression selectors to the existing workflows, run the exact composed source gate, and record its unchanged source inventory and actual dependency identities.
6. Reconcile the latest public parents, finish the real application merge and update PR143 only with evidence for that exact composition. Native Windows, installed-origin, human NVDA/listening, private engine/model and rights qualification remain separate gates.

No private source, owner engine bytes, model weights, recordings, signing material, business policy or endpoint was published by this checkpoint. The earlier automatic review rejected publication of the whole unfinished local commit; it remains unpublished. Public-derived archive repairs and this factual execution record are narrower independently reviewed work.

## Subsequent remote results and bounded native correction

Run 45982468090 completed 279 citation tests on Python 3.11.18 / QuickJS-NG 0.18.0.2 in 185.03 seconds and parsed all 64 PowerShell blocks without errors. The Windows process suite failed first when the fixture parent read an empty, newly created PID marker; ownership admission had not yet returned to production runner code. The subsequent held-file cleanup error therefore does not establish a production cleanup failure after admission. Checkpoint 7 changes only synthetic marker publication to a fully written sibling followed by `os.replace`, after opening the held lock, and retains all 82 existing assertion calls. Fresh full finite Windows and POSIX tests are required; no timeout, assertion or production cleanup behavior was weakened.
