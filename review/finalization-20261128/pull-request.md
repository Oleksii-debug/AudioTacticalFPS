## Why this change is needed

The current Resonance technical project requires the library, research, Reader, Studio, recovery and delivery workflows to share the same source and ownership contracts. This PR reconciles the canonical implementation with the reviewed work and completes the remaining public implementation scope from the updated specification.

## What changes

- Preserves the existing canonical Save All, immutable originals, current-source authorization, exact export/publication receipts, catalog V5 and both explicit saved-ASR codecs. Import, backup, incremental recovery, cancellation and owner withdrawal continue through the existing services.
- Retains the completed independent listening controls, verified-download continuation, typed Assistant operations, automatic OCR, local translation, playback queues, complete-scope search and streaming large-source import.
- Adds reviewed **English, Ukrainian and Slovak speech preparation** to the existing Reader/Studio routes: exact original/derived maps, explicit proposal acceptance, one compound Undo/Redo action, stale-source refusal and ordinary save/recovery.
- Adds **source-text A–B repeat and a sleep interval** to live reading, using the current reader, player, budget and cancellation owner.
- Adds **native Word citation controls** and an optional local Word connection. They use the current CSL/document schema, source-bound references, a fresh working copy and explicit Save/Undo. Uncertain external outcomes require inspection before replay.
- Adds **research-content merge** from two immutable, verified backups into a fresh destination. Whole-workspace conflict choices preserve stable research identities and sources; the separate selective profile requires acknowledgement of omitted operational/audio/settings state. Full backup/restore remains available.
- Adds **read-only long-silence inspection** for a current completed Studio production and **standard chapter CUE sheets** beside the measured WAV master. Inspection hashes the same retained PCM descriptor used for parsing/sampling; interchange and its backup consumers retain source/timeline bindings and exact private-file rules.
- Adds accessible **Help/About component notices** from actual installed metadata and upstream text, plus bounded **redacted diagnostics** with local preview and explicit create-only export.
- Records the **English-first Ukrainian/Slovak UI localization plan**. This is a plan for catalogues, fallback and acceptance; it does not claim translated UI catalogues have shipped.

The implementation guides and requirement/evidence records are in [release readiness](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/71ca92be691455d0ff51ed163b1d91749e5ae62e/carr-library/docs/V3_RELEASE_READINESS.md), [the README](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/71ca92be691455d0ff51ed163b1d91749e5ae62e/carr-library/README.md) and [the measured scale guide](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/71ca92be691455d0ff51ed163b1d91749e5ae62e/carr-library/docs/LARGE_LIBRARY_SCALE.md).

## Exact candidate and required acceptance

The current PR head is `71ca92be691455d0ff51ed163b1d91749e5ae62e`, tree `73c43b7486974a7b301aa8c210c891b4d94ddb0d`. **It has actual main, V3 and Linux 2 GiB failures and must not merge.**

The corrected source is prepared at **`57bd81ebabc0df2ffc4106c44b94805ee2da7588`**, tree **`b58326e5724c64eea195814ac198c4237720292c`**. It has **1,567 files**, 19 changed paths from the current PR head and all **910 canonical paths** retained. Two independent complete composition reviews bind every path, type, mode and blob: `ef655edb386aec113d8f4d1ac1fd3d0619645eb4` and `3500259691cab4cac762b9e70006c4f577bb0b2f`. Exactly seven maintained workflows are present; temporary review workflows are excluded.

The canonical input remains `e5c64a5245583f8f8b404a0f042f6ca438c24d7f`. The technical contracts are finalization `db8adbdc483a03c262885928c7ed214ca70bbaa7` and audio/agent `d0b2761dd22b33233b311d4800230fb47f5d3d8b`.

The corrected source is undergoing its complete native preflight at `6c7e02188dc0c4895cff17f69d75bcd61ee932a9`, whose tree adds only the temporary preflight workflow to the exact prepared source. [Run 46135090394](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46135090394) was queued at **2026-11-28 10:36 UTC**. The prepared source has **not yet replaced the PR head**. It requires its own seven completed maintained workflows and all 12 actual scale tiers before merge; prior results do not transfer.

### Current PR-head results

These are the actual outcomes on **71ca**, retained to explain the corrections. They are not acceptance of prepared **57bd**.

| Required workflow | Required scope | Actual outcome on 71ca |
| --- | --- | --- |
| CARR Windows integrated desktop | Standard installation and sealed bundle, offline runtime, required native controls and selected source/installed suites | **FAIL** at dependency inventory: 52 pip pins plus one embedded source component. Full bundle/source phases did not run. [46124998890](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124998890), record `884ff0dabfe218d7d97c7da8d7e8edb843210b7e`. |
| CARR V3 local studio and assistant | Portable source integrity and installed/native/offline Studio and Assistant acceptance | **FAIL, both jobs completed.** Portable: 10,898 passed, five failed, 42 subtests. Windows installed: 9,851 passed, three failed; shared source: 5,228 passed, three failed. The five unique failures are the corrected notice/menu fixtures and OCR/TXT cancellation cases. Required native steps 9–11 were not reached. Independent broad Windows suite: 5,101 passed, eight skipped; no zero-skip claim. [46124998937](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124998937), records `a984e0a5a8d6454a520277d9f4628f224d59ba6c` and `5594f90582e6b0f2ba5b26e7c83e3f78870878ae`. |
| Actual library corpus scale | All 12 actual corpus/source tiers and fresh backup/restore on all four largest profiles | **Eight PASS, one FAIL, three still running** at the last 10:22 UTC capacity observation. Both OS passed 1,000 books, 10,000 books, 65 MiB and 129 MiB. Linux 2 GiB failed unchanged-index reuse after successful import/resume/catalog. Windows 2 GiB and both 50,000-book tiers continue. No completed largest-tier backup acceptance. [46124999025](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124999025). |
| CARR native Windows launch entry | Actual launcher, process ownership and installed entry behavior | **PASS:** nine actual PE checks in each MSVC and pinned Zig job. [46124998938](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124998938), record `3245c4e650564ed52e853ba3a9329827d9aefb4e`. |
| CARR bibliography and import acceptance | Installed bibliography/import and document protocol boundaries | **PASS:** 1,162 cases in 27 suites plus 69 installed native checks. [46124998884](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124998884), record `cd590c064f6f74d3b73c83ab30943f0cae634bff`. |
| Selected Calibre reading review | Eight selected actual formats, source/installed and real reader routes | **PASS:** 235 suite cases, eight installed conversions and four negative controls. [46124995558](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124995558), record `8f4dc8524191d74228f882ad5fbe3cd08d3d5cf7`. |
| Selected legacy document and archive conversion review | Windows/Linux document and archive conversion acceptance | **PASS on both OS:** 401 boundary cases, nine actual source and 33 installed cases each; four additional Linux registry cases. [46124995570](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124995570), record `4f5d8286802fbea77e5355146638478d7bc64d8f`. |

### Completed focused corrections

| Correction | Actual completed acceptance |
| --- | --- |
| OCR waiting-state persistence and TXT post-commit cancellation | **141 source + 141 isolated installed cases on each OS**, zero failures/skips/expected failures; all original cases and eight added controls pass. Fresh OCR waiting state is recorded only for the current matching invocation. A TXT cancellation latch applies only after this invocation's real publication returns; source descriptors and integrity checks remain enforced. [46130074946](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46130074946), record `59db4383dde5426f77650b3996bc2da3d984b005`. |
| Strict runtime inventory and embedded Slovak component | **42 source + 42 isolated installed cases on each OS, plus 18 subtests per context**, zero failures/skips/expected failures. The real Windows dependency step passed all 52 pinned distributions and exact eight vendored files. Pip pins, SBOM identities/edges, private-refresh immutability and actual notices remain required. [46130223315](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46130223315), record `bfab85fc24f6ef6810b49c0c1a4a868bb7788def`. |
| Notice distribution name and existing Desktop installer fixtures | **61 source + 61 isolated installed cases on each OS**, zero failures/skips/expected failures. The fixtures match the actual distribution and existing menu installers while retaining their original assertions. [46128142463](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46128142463), record `8a8168ccdb4c1b6e30dd0b473f96f774b68abc2e`. |
| Four additional maintained regression-selector lines | Actual syntax passed **1,246 Python files, 11 embedded Python blocks, 43 PowerShell blocks, eight workflows and six entrypoints** on the syntax-only child of prepared 11589. It claims no native acceptance and predates the four final amendments in 57bd. [46132103113](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46132103113), record `5757752b46758985f25299493b7704622501698c`. |

### Native preflight correction

The prior composed preflight at `bdc3c6f1383f39e9c0b8e6f60c694a11a5fbb515` passed syntax, About and diagnostics, then failed at the speech-preparation assertion for direct window destruction. Later scopes were not reached. A bounded read-only capture verified the original artifact's exact size and SHA-256 and decoded the saved stderr; evidence `03aa9e9e6a335263af60dd78a2cbf3f2de7638e6`, [46132889609](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46132889609).

The assertion combined native-peer deletion and unchanged document state, so that failure alone does not identify which operand failed. The independently reviewed probe correction retains both assertions, additionally checks withdrawal and unchanged content before releasing the genuine worker result, and waits within the **same existing deadline** for the exact peer's recorded destruction as well as worker delivery. Product behavior, native launch ownership and scenario bounds are unchanged. The helper now emits bounded failure diagnostics while retaining complete saved stdout/stderr and final integrity checks. Review `4b1fd670cb77b00047981d10402981a7c155ed40`; actual repeated native acceptance remains pending in [46135090394](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46135090394).

The shared helper retains all eight scope contracts: seven required native controls and an additional optional actual-Office probe. It binds the expected head, complete source/installed Python inventories, exact probe/config/oracle files and all **22** selected runtime/notice assets before and after every outcome. Office absence is accepted only as explicit NOT_RUN with zero checks and a reason. Full final main/V3 acceptance also requires their existing offline controls.

### Actual scale evidence and corrected caller policies

The matrix requires Windows and Linux for **1,000 / 10,000 / 50,000 books**, with 40 segments per book, and actual **65 MiB + 173 bytes / 129 MiB + 173 bytes / exactly 2 GiB** source files. Generation, admission, publication, committed interruption, same-queue resume, full catalog/search scope, saved resource policies and integrity evidence remain mandatory. All four largest profiles require fresh backup/verify/restore.

The first six tiers have decoded API timing, memory and disk evidence in `e9c9059cc506f40aa1a08d86776e779d61235938`. Both actual 10,000-book tiers are also measured in `114e9d7422a00e1426b18e9295b9f050b6775abc`: 400,000 segments and 102,790,003 original bytes each; Linux/Windows import 1,723.717/2,874.139 seconds and complete search 553.037/780.657 seconds. Both reused the unchanged index with zero updated rows. Own-process memory peaks are separate measurements; concurrent family peaks and Windows physical allocation are not claimed.

Linux 2 GiB failed the retained zero-rewrite assertion on its second complete-scope query; it did not report timeout or out-of-memory. Evidence `0a45062daebc3b76ed1c097a9cc2be427228839c`. Source review shows the prior caller allowance could admit a private index larger than its optional cache share. The failed index size/cache branch is not reported, so this is a source-supported explanation, not a measured branch observation.

Prepared 57bd changes only the explicit matrix disk allowances: **8 GiB for 50,000 books and 16 GiB for 2 GiB sources**. Product cache partitions, source sizes, memory/import policies, timeouts, warm-index zero-rewrite assertion and complete traversal remain unchanged. Actual free-space admission remains mandatory; projected initial requirements are 37,225,549,000 and 71,072,502,272 bytes respectively. The updated profiles require real final execution. Independent review: `12747fa1dc3baa765faf07cbb29d1335e3b29983`.

Earlier actual failures, raw logs and corrected component records remain in the [durable evidence branch](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/carr/finalization-evidence-20261128/review/finalization-20261128). Queued or unreached work is never recorded as a pass.

## Completed component evidence

Counts below are **per context**, not summed into a release total; these selections overlap. Source and installed results are distinct executions on their recorded heads, with no skipped or expected-failure cases.

| Component | Completed actual result | Evidence/run |
| --- | --- | --- |
| About and component notices | 41 source + 41 installed on each OS; 19 native checks in each of two Windows environments | [46117569608](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46117569608), record `4395289883c6de3d4f4b77a82bd6dce684e0ba84` |
| Redacted diagnostics | Windows 77 + 77, Linux 78 + 78; 135 native assertions | [46118029628](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46118029628), record `4b82c8fe492ec5373c300f8b397074ea5f5f4659` |
| Speech preparation | 755 source + 755 installed on each OS; 321 actual native assertions | [46118114870](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46118114870), [46120170430](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46120170430) |
| Live A–B and sleep | 987 source + 987 installed on each OS; 126 native assertions and five actual selected SAPI renders with controlled PCM output | [46120317097](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46120317097), [46121452393](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121452393) |
| CUE and silence | 342 source + 342 installed on each OS; six upstream libcue self-tests plus actual two-track and 100-chapter boundary parsing; 98 native Studio assertions with all nine silence scenarios passed | [46121179440](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121179440), [46123004216](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46123004216), native record `f651a223e990d12de0f200b17656560b1616ddc6` |
| Word common and native controls | 539 source + 539 installed on each OS; 128 actual required native checks. Actual Office was absent: the optional Office probe reported NOT_RUN with zero checks. All 20 CSL/runtime/notice assets remained bound before/after. | [46120130479](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46120130479), [46126241176](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46126241176), native record `8264ac4a6aa227d0e86b7d7041fe16476de1125d` |
| Research merge | 237 source + 237 installed on each OS; all 12 fixed native criteria and 310 native assertions | [46120328460](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46120328460), [46122906464](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46122906464) |
| Indexed large-source publication | 306 source + 306 installed on each OS, with three additional subtests per context and real rollback/SQLite-plan checks | [46112053954](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46112053954), record `e0ff493a9ebac6bdfb6817e33693023efc3044e1` |
| Actual Windows 512 MiB + 173 bytes | Full 250-segment import, committed interruption/resume, catalog and complete search with the original scratch quota and saved policy unchanged | [46112104315](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46112104315), record `18a9c2c04c248166a5e7b323d0a3c33dc39ad074` |

The Windows 512 MiB record measured import at 71.033 seconds and complete retrieval at 1,791.572 seconds. Its worker and coordinator own-process peaks are separate measurements, not a concurrent family peak or an operating-system RAM cap. This record does not qualify the final 2 GiB or 50,000-book profiles.

The completed silence and Word native records are tracked separately from the final candidate. Earlier failed probes, wrapper/fixture defects and their qualified corrections remain recorded; a queued job is not an executed verdict.

## Qualification boundaries

Required CARR native Word controls use a disclosed controlled external-Word protocol boundary. Actual Office automation is an additional optional probe: absence must report NOT_RUN with zero checks and a reason. It cannot silently turn required CARR controls green.

Hosted native evidence uses controlled audio output while retaining actual selected SAPI/FFmpeg processing, source checks, publication, key events and ownership cleanup. Physical output/microphone, listening quality, human NVDA and the actual Windows11/device profile remain separate qualifications.

Owner-supplied private models/cloning, quality across the canonical 135 language entries, package-specific distribution clearance, production entitlement/update authority and publisher signing require their own actual evidence. This source PR does not publish a distributable installer or activate a paid service.
