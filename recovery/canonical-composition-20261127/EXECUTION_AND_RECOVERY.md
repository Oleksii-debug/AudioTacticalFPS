# Resonance composition — execution and recovery checkpoint, 2026-11-27

This record preserves observed development work. It is **not a release, installed acceptance, or final platform completion claim**.

## Published source and validation anchors

- Latest canonical branch observed at `e69ba2403515f21509686d66f4bc8b3673756d81`; its source tree matches independently qualified historical candidate7425. The final union still needs its own execution.
- Latest PR143 application branch observed at `4fae6fa69daf86fc9566f8847d74bccc8a4dc5a5`, including actual reader PCM geometry attestation.
- This is public source recovery checkpoint9, extending checkpoint8 `6076d38c264f7738bd548d6458e839d084a3765f`. It archives 154 source candidates; earlier150-source compilation evidence remains attributed to its original snapshot.
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
4. Carry the reviewed public provider-cancellation fix into the final composition. Its original negative and all 72 fixed cases passed on Ubuntu; the Windows result remains pending at this checkpoint. Keep the same cancellation/cleanup assertions.
5. Add new regression selectors to the existing workflows, run the exact composed source gate, and record its unchanged source inventory and actual dependency identities.
6. Reconcile the latest public parents, finish the real application merge and update PR143 only with evidence for that exact composition. Native Windows, installed-origin, human NVDA/listening, private engine/model and rights qualification remain separate gates.

No private source, owner engine bytes, model weights, recordings, signing material, business policy or endpoint was published by this checkpoint. The earlier automatic review rejected publication of the whole unfinished local commit; it remains unpublished. Public-derived archive repairs and this factual execution record are narrower independently reviewed work.

## Subsequent remote results and bounded native correction

Run 45982468090 completed 279 citation tests on Python 3.11.18 / QuickJS-NG 0.18.0.2 in 185.03 seconds and parsed all 64 PowerShell blocks without errors. The Windows process suite failed first when the fixture parent read an empty, newly created PID marker; ownership admission had not yet returned to production runner code. The subsequent held-file cleanup error therefore does not establish a production cleanup failure after admission. Checkpoint 7 changes only synthetic marker publication to a fully written sibling followed by `os.replace`, after opening the held lock, and retains all 82 existing assertion calls. Fresh full finite Windows and POSIX tests are required; no timeout, assertion or production cleanup behavior was weakened.

## Native runner confirmation and provider-cancellation correction

The exact production runner `67e42f01e73ef7bfcd3686ff34f646aab26c74f3` and atomic-marker test `bc05942a32e086cc86238802bffc9023cc4123cd` passed 25 tests plus five subtests on Windows (run45984033388/job140454938998,6.60s). The same files passed 25 plus five on POSIX after the workflow used the absolute venv entry (run45984384730/job140456134164,4.90s). The intervening preparer-pin rejection and POSIX path-identity failure remain in CI history; neither was changed into a pass.

The provider-cancellation defect was traced to the facade forwarding caller cancellation but not its own Stop event while the adapter held the cold-probe lock. Public facade `b042c9f31e65aaae4819a1ec40422895cf8be9ff` forwards both checks through the existing operation callback, preserving cleanup and concurrent admission. Root and two independent source reviews accepted it. Branch `carr/dordiai-discovery-recovery-20261127`, commit `5e1f7385c13badf4a53f0a4c8a677afa7daceb42`, executes the unchanged original negative then all four provider/probe suites on Windows and POSIX; its run45985132540 is pending here.

The pending completed-master playlist also needs SHA/size-bound worker-to-GUI and queue-to-open admission; that source-derived issue was not executed after the outage. The [remaining scope audit](REMAINING_SCOPE.md) records substantial missing implementation, including live microphone monitoring and several timeline/player/batch workflows. It must not be reduced to a list of unperformed qualification tests.

## Checkpoint9: public reconstruction and new measured results

The local executor remains unavailable. Exact public source and complete retained generic patches allowed the seven typed-backup changes to be reconstructed without reading or uploading the unfinished localc04 source. All seven candidate blobs were fetched back exactly. Thirty-five existing test functions remain; twenty additional parametrized producer/tamper/scope cases are included. Two intentional fixture assertions now bind the actual artifact path and current extension version. Two canonical924 initial-root cancellation checks were also replayed into the typed sidecar union.

A separate [backup proof branch](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/f8f9d83411bae7471f1d5642d58684e096471026) provides the reviewed 16-overlay dependency closure over shared8c. It requires two specific original adapter failures, then seven complete suites with actual FFmpeg/ffprobe, zero failures/errors/skips and unchanged source. This fresh composition is not asserted identical to the inaccessible local118-test tree; its CI is pending.

The provider proof [run45986810416](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45986810416), exact59cde34, completed its Ubuntu job140464461514: the old implementation produced exactly the expected 1 PASS / 1 FAIL in 60.81 s; the corrected implementation passed all 72 cases / four suites in 7.28 s with unchanged source and zero failures/errors/skips. Windows job140464461808 remains queued here. The preceding45985132540 was cancelled while queued following a workflow-only explicit newline setting. Subsequent inspection confirmed the application's existing attributes already protected its bytes; this was redundant protection, not a demonstrated source defect, and no test failure is attributed to that cancelled run.

The canonical-only [planner correction942e2b9](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/942e2b914223329320946cfc9c5b26d76da78948) passed [run45988474768](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45988474768), job140469996481. The old SYSTEM failed specifically for its missing registered transcription tool; the corrected source passed 64 cases / three suites in 11.44 s, including six new proposal/authority cases. Source identities remained unchanged and no failures/errors/skips were permitted. Canonicale69 already registers and dispatches transcription; shared4fa does not. This prompt is therefore a related canonical-only proposal and is not silently overlaid into the incomplete archive.

The [latest public reconciliation findings](PUBLIC_RECONCILIATION.md) preserve export authority, ASR restore trust, resource/process ownership, import publication and actual WAV geometry. A separate public Studio557 candidate already implements clip operations, master effects and named presets through a different project model. [Remaining scope](REMAINING_SCOPE.md) now records that code as available for reconciliation rather than universally absent. Its presence does not resolve the frozen schema downgrade or SHA-bound master playback admission.

No new component result qualifies the final full application, installed package, actual private model/voice, physical device or human NVDA behavior.
