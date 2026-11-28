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

- Candidate: `71ca92be691455d0ff51ed163b1d91749e5ae62e`.
- Complete tree: `73c43b7486974a7b301aa8c210c891b4d94ddb0d`, **1,567 files**, recursively verified for every path, type, mode and blob identity before the branch update.
- Canonical input: `e5c64a5245583f8f8b404a0f042f6ca438c24d7f`. All 910 canonical paths remain present.
- Current technical contracts: finalization `db8adbdc483a03c262885928c7ed214ca70bbaa7` and audio/agent `d0b2761dd22b33233b311d4800230fb47f5d3d8b`.
- The temporary component/preflight workflows are excluded. The final tree contains the **seven maintained workflows** below.

**The current PR head has actual main and V3 portable failures and must not merge.** Main stopped at the dependency inventory because the old guard counted the reviewed embedded Slovak source as a pip distribution. V3 portable reported 10,898 passes and five failures: two fixture mismatches, durable waiting-state recording for two OCR cancellation cases, and cancellation observed after an actual TXT commit. Reviewed corrections retain every original assertion and are under separate focused and composed acceptance. Healthy running gates continue to retain their own results; the corrected final candidate will require its own completed gates.

| Required workflow | Required scope | Current verdict |
| --- | --- | --- |
| CARR Windows integrated desktop | Real standard installation and sealed bundle, offline runtime, required native controls and full selected source/installed suites | FAIL at dependency inventory: 52 pip pins plus one embedded source component; full bundle/source phases did not run. [Run 46124998890](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124998890), record `884ff0dabfe218d7d97c7da8d7e8edb843210b7e`. |
| CARR V3 local studio and assistant | Portable source integrity plus installed/native/offline Studio and Assistant acceptance | Portable FAIL: 10,898 passed, five failed, 42 subtests; native job continues. [Run 46124998937](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124998937), portable record `a984e0a5a8d6454a520277d9f4628f224d59ba6c`. |
| Actual library corpus scale | All 12 actual corpus/source tiers below, including required fresh backup/restore on the largest profiles | Six tiers PASS: 1,000 books, 65 MiB and 129 MiB on both OS. The remaining six larger tiers continue. [Run 46124999025](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124999025). |
| CARR native Windows launch entry | Actual native launcher, process ownership and installed entry behavior | PASS: nine actual PE checks in each of MSVC and pinned Zig jobs |
| CARR bibliography and import acceptance | Installed bibliography/import and document protocol boundaries | PASS: 1,162 cases in 27 suites and 69 installed native checks. [Run 46124998884](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124998884), record `cd590c064f6f74d3b73c83ab30943f0cae634bff`. |
| Selected Calibre reading review | Eight actual selected formats, source/installed and real reader routes | PASS on this head: 235 suite cases, eight installed conversions and four negative controls |
| Selected legacy document and archive conversion review | Required Windows/Linux document and archive conversion acceptance | PASS on both OS: 401 boundary cases, nine actual source and 33 installed cases each; four additional Linux registry cases |

The scale matrix uses Windows and Linux for **1,000 / 10,000 / 50,000 books**, with 40 segments per book, and actual **65 MiB + 173 bytes / 129 MiB + 173 bytes / exactly 2 GiB** source files. Generation, admission, publication, committed interruption, same-queue resume, full catalog/search scope, saved resource policies and integrity evidence remain mandatory. The exact 2 GiB profile is not inferred from a smaller source or a sparse file.

The maintained Windows and V3 gates now execute all eight final native scope contracts through one committed helper. The helper binds the expected event head, complete source/installed Python inventories, exact probe/config/oracle files, all **22** selected runtime/notice assets and the selected audio evidence before and after every outcome. Existing tests and assertions remain required; the 19 newly missing test files were added to each of the four maintained source/installed selectors.

The first shared preflight at `9d8da1b3096046b49ff66a8dd0c0331bd6a26b15` ([run 46124240341](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46124240341)) compiled all **1,246 Python files**, ten embedded Python blocks and eight workflows, then the actual PowerShell parser found a forbidden final comma in the main source-suite array. The native job correctly did not run after that dependency failure. Record: `838e2fa4f474f77d77cf4aa0519de94a5db8652b`.

The two delimiter corrections passed the actual parser at `84816b20f32ca8142b9e07591a7ec3fd1b4dce17`: 1,246 Python files, ten embedded Python blocks and all 43 PowerShell blocks passed. That preflight then stopped on the old notice fixture, before the eight native scope probes ran. [Run 46126153255](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46126153255), record `5e6e1b1a34a9a125903f05658850628cb31ec1c2`.

The notice/menu fixture corrections have now passed 61 source and 61 isolated installed cases on each OS: [run 46128142463](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46128142463), record `8a8168ccdb4c1b6e30dd0b473f96f774b68abc2e`. The importer focused correction is running at `5b87b13974d57d1aeb59f92c78c0f64366fd8a8c` ([46130074946](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46130074946)); the strict runtime inventory correction at `eb9b66d67322c3c243d9b8f27bc8e93dd23fe3c9` ([46130223315](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46130223315)). The latter verifies the exact eight embedded source/notice files, retains the pip lock and SBOM dependency edges, exercises both bundle/refresh suites, and executes the actual committed main dependency step on Windows.

A fresh preflight composes all 11 reviewed public corrections at `bdc3c6f1383f39e9c0b8e6f60c694a11a5fbb515`: [run 46130308277](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46130308277). These pending runs do not qualify the current or a future PR head.

The earlier zero-job GitHub definition rejection was separately corrected by moving the same event-head expression into a job environment value. The current main run was admitted as an actual named job; that admission is not a runtime PASS.

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
