# Resonance integration recovery checkpoint — 27 November 2026

## Status

Development was interrupted by loss of the execution environment after its temporary filesystem filled. This document preserves a recovery plan and evidence. It does **not** publish the locally composed application, approve a final release, or establish exact-candidate Windows/NVDA acceptance.

Canonical source used throughout the completed local integration: `33dcc42edd6781b9aeb02dcaaf016b386bcf37b6`, branch `carr-library`. The updated specification is `docs/UNIVERSAL_SCOPE_V3_FINALIZATION.md`.

The complete candidate was not pushed. Automatic approval review rejected the broad Git push because the source-publication scope and absence of non-public material had not been established. A subsequent read-only audit found 213 changed UTF-8 text files, all under `carr-library/` or `.github/workflows/`; no binary/model/media/database/key artifact extensions or recognizable credential patterns; 179 exact blobs already present in inspected public donor heads. This audit is evidence about the inspected candidate, not permission to publish additional private material.

This checkpoint-only branch contains this report on the existing public canonical source. It leaves all implementation branches and release artifacts unchanged.

## Completed local integration, awaiting publication

Local branch: `carr/resonance-finalize-20261127-eea0`.

Last confirmed local commit: `8fc5db6e4d3582b340f98d7651556e311fb1e385`.
Last confirmed local tree: `0568ea24fe4df8955eca8862cb0f8819c5f7ddfe`.

These local objects are **not available on GitHub** unless subsequently recovered and published. Do not treat their hashes as remote links.

| Adopted component | Reviewed public source | Composition decisions |
| --- | --- | --- |
| dordiai shared speech/Studio bridge | PR117, `97ac6746857f3857774c4b5ff117d1fa4f8920db` | Retain canonical focus behavior and current voice registry; no private engine copied. |
| Advanced search and reading backup | PR85, `2bc7f306744a62d58695bb0852a986850679d75e` | Preserve Boolean/NEAR search, pagination, federation, historical backup readers and canonical pending inputs. |
| Source authorization during Save As | PR137, `cb966fced292e6e1e3d5f8c148eca577b6bf59fa` | Reauthorize before and after the native picker. |
| BibTeX and citation-document recovery | PR111, `6ff0c4dc655603dacd926c72de60c3e50f802b6f` | Retain shared destination approval and create-only export publication; BibTeX2.1/pylatexenc2.11. |
| Academic workbench | PR84, `b9c3af79acba216fccf25ec8412a1968c0eb87d5` | Preserve metadata atomicity tests under a distinct filename; retain selected-fields/cancellation handling. |
| DOI/ISBN acquisition | PR100, `634933ff9244c07c76a81336200475417979e14c` | Keep exact consent and provider boundaries, canonical export helper, search modes and CLI validation. |
| Native launcher and semantic cache | PR112, `6b26a782872da6b1b9b3dec432d8f3973801d82a`, then `53b9e563a376b8d545f9925af5a5afd0ecd88e10` | Reuse source-bound launch receipts; preserve sealed installed runtime with explicit child `-B`; no checksum relaxation. |
| OCR equivalent-path binding | PR138, `69f1eb359bbbdd0192c44a06b9e51c1822b6b0d5` | Validate links/reparse components before normalization; retain selected model hashes. |
| Additional source formats | PR104, `7180db0e8622ce45a321199f1c341ebd7e5ccc4e` | Narrow parser/registry transplant, canonical pending-original suffix validation and assistant import filters. Do not transplant donor runtime baseline. |
| Import transaction completion | Local reviewed `5de2e96282d4070cd5fafccd61d95229cda51ee2` and `cf909eefdba03d0ace3433e8bac4530802184307` | Reconcile a stopped child only against exact durable source/catalog/membership proof. Preserve canonical PR111 always-on sealed-revision validation and replay-only registration. |

Additional formats retain original bytes and explicitly unverified logical locations: HTML, Markdown, EML, FB2/FB3, ODT/ODS/ODP, PPTX and XLSX. They do not grant quotation approval or invent printed page numbers.

Important local checkpoints:

- `ed3c18ccc224abf88608dc033b68b3aa7ec01adb`: formats composed with current acquisition CLI and import-completion bindings.
- `7c76da85acdb74ad464447a37664496bb4cf960f`: separate root-sidecar backup component, not yet composed with the final keyboard change.
- Root workflow inspection before outage: 34 valid step structures; 163 required source-suite paths, no duplicates or missing paths; 49 installed additional paths, no duplicates. Final combined keyboard/progress/transcription gates remain to be added.

## Actual local evidence

Selections below cover different revisions and overlap. **Do not add these counts into a single final-candidate result.**

| Selection and inspected source | Actual result |
| --- | --- |
| PR117/PR85 composed search/recovery selection | 13 strict suites, 486 passed, zero failed/skipped |
| PR111 independent source review | 19 suites, 544 passed, zero failed/skipped |
| Resolved composed focus fixture | 43 passed |
| Native-launcher packaging after its files were committed | 18 passed |
| Root import-boundary, immutable-child-runtime, initial-publication and replay selection | 4 strict suites passed; import boundary had23 cases before HTML expansion |
| Root formats/completion/backup/assistant composition | 7 executed strict suites, 296 passed, zero failed/skipped |
| Corrected acquisition CLI invocation on the same composed source | 44 passed, zero failed/skipped |
| Formats donor transplant before root composition | 26 strict suites, 680 passed, zero failed/skipped |
| Independent OCR plus import-boundary composition | 4 strict suites,117 passed, zero failed/skipped; actual local pinned English/OSD OCR |
| Optional root-sidecar backup on exact donor model dependencies | 6 strict suites,113 passed, zero failed/skipped, including12 new combined recovery cases |
| Initial shared keyboard/draft composition | 9 strict suites,308 passed, zero failed/skipped |
| Initial full PR139 progress/import/production composition | 310 passed, zero failed/skipped; separate OCR/source-wheel selection101 passed |

One root formats command referenced a nonexistent acquisition test filename. The real `tests/acquisition/test_acquisition_cli.py` was then run successfully with44 cases. That invocation error is not concealed as a green original run.

Native document-format probing on Linux returned NOT_RUN, exit77. Native acquisition had two explicitly Windows-only skips locally. No manual NVDA result is established. An interrupted final progress run failed to write its JUnit report because of ENOSPC and has **no usable final result**.

## Uncommitted work requiring recovery and review

### Shared keyboard registry and note drafts — PR105

Donor: `e40ae916adeaae66eb0bfe7b5315436945f0bb33`. Local branch: `carr/keyboard-notes-eea0`, based on `7d57b81df500e685db6c9914447581421f6b5aac`. The final changes were written but not committed.

Retain these completed reconciliation decisions:

- One stable command registry and native installer, with bounded compatibility reading of the existing menu-path shortcut file. Keep the earlier file; an explicit Save creates the new profile.
- Preserve existing screen-reader and editing reservations, including Ctrl+Alt+N and F6.
- Retain canonical export destination approval and library/source guards.
- Replace the unscoped singleton autosave timer with source-bound per-evidence recovery; legacy notes remain read-only and require fresh current-evidence authorization.
- Reuse native keyboard probe blob `8b1745886aa48ab0b17a855ef162957b60371f89` from `befdc9f4c85576bc0a699511fff8f8f97f03bd81`.
- Final six-suite rerun result was unavailable at disconnection. Inspect it or rerun; do not infer success.
- Compose the root-sidecar backup commit and change its fixture from Ctrl+Alt+N to Ctrl+Alt+Shift+N before rerunning the12 actual combined backup cases.

### Accessible progress and worker lifetime — PR139

Pinned reviewed donor: `7012bc7c92dbfe502f87ac0ef74cc42cb34bcc78`; stop chasing unrelated moving heads while stabilizing the composition.

The local canonical merge was committed at `8935ae1`, with the prior donor follow-up at `c2e7454`. A six-file lifecycle correction remains uncommitted:

- Studio progress pause/close/timer/report observer faults must not prevent real closure.
- Selected progress voices own a shared registry lease. Release the persistent dordiai worker only after the last selection and any active synthesis finish. Borrowed registries remain caller-owned; cleanup does not block the UI.

Nine focused lifecycle regressions passed before the final broad rerun exhausted disk. The final uncommitted source still needs a complete review, commit and affected tests. The7-line production follow-up at7012, its3 regressions and readiness row were reviewed remotely but not applied after environment loss.

Predecessor7bf705f Windows evidence was useful but is not acceptance of this local composition: the installed bundle and native progress steps passed; the overall desktop run was later cancelled. Do not transfer those results to a different source tree.

### Supplied transcription bridge — PR140

Reviewed donor: `88502883a631a890b32423a5c24033d3c7ee83ba`, stacked on PR85. Clean local baseline composition: `26a2f00`.

Baseline bridge/progress/audio/backup selection passed128 cases, but independent actual-child negative tests exposed three defects:

1. Resolving the selected virtual-environment Python symlink loses that environment's dependencies.
2. The Python audit hook ignores directory-descriptor arguments; a real Python `os.unlink(..., dir_fd=...)` deleted a synthetic protected file outside the job.
3. Stop after visible1/2 sends0/2 to the shared monotonic tracker and produces a false operation failure.

The production correction in `transcription.py` was fully written but not verified or committed. The permanent `test_supplied_worker_bridge.py` patch was **partly written** when disk filled. Restore that test file from the clean local HEAD before applying complete regressions. Preserve the complete independent reproduction file from the workspace if available.

Do not integrate this unfinished patch as approved. Verify selected-venv execution, filesystem descriptor handling, monotonic Stop/resume, permitted worker outputs and truthful completion. Python audit checks are not a native operating-system sandbox. Private worker/model inference remains separate qualification.

## Resumption order

1. Restore the execution environment and inspect existing worktrees. Preserve all local commits and uncommitted files before freeing only known task-owned temporary outputs.
2. Finish and commit keyboard, progress lifetime and transcription corrections; reconstruct the partial transcription test file first.
3. Compose optional root-sidecar backup with the keyboard models and rerun actual combined recovery.
4. Retain canonical focus, source authorization, export publication, metadata/replay and pending-input contracts when reconciling conflicts. Historical contributor merges can silently revert newer features: apply the reviewed delta against its actual baseline, not an unrelated whole-tree replacement.
5. Validate every Windows workflow step, embedded Python block, required source/installed path, distinct test names and retained gate.
6. Run the assembled portable selection and actual source-wheel identity verification. Publish only a reviewed source-only candidate within established authorization; preserve expected-head checks and other contributors' work.
7. Run fresh exact-candidate installed Windows/native gates. Complete manual Windows11/NVDA, owner engine and authorized-audio qualification, signing/updater/entitlement and distribution-rights requirements separately.

No final installer, signed update, full-language acoustic qualification, paid API request, private model/source publication or complete-platform DONE is claimed by this checkpoint.
