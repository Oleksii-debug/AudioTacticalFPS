# Resonance integration evidence

This is a development integration record for [PR98](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/98).
The current [technical specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md) and
[acceptance contract](acceptance.md) retain the full platform scope.
No final installer, distribution clearance or full-platform DONE is asserted.

Source review, adapter presence, integration, actual execution, quality,
Windows/NVDA and distribution rights are independent verdicts. A synthetic worker
or generated fixture is identified as such. Counts below belong to individual
source trees and must not be added together.

## Executed integrated baseline

Baseline source: bbd7f662162c139bf4d4cf06f32257c141b9884a.
Baseline tree: f6618211e7be19ffddba3798fae0d2b349b8dbee.

| Actual run | Result and boundary |
| --- | --- |
| [Linux integrated 45938532434](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45938532434) | 86 required suites, 2347 tests passed, zero failures/skips. |
| [Windows launcher 45938532427](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45938532427), MSVC job | Nine native launcher checks passed. This does not establish the actual Python installation, Authenticode or NVDA. The separate cross-compiler job has its own result. |
| [Windows desktop 45938532422](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45938532422) | Failed the native advanced-search focus assertion before the 215-source-suite and installed-package gates. The failure remains part of the record. |
| [Windows audio 45938532601](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45938532601) | 571 tests and 27 subtests passed; two tests skipped because this FFmpeg build lacks libflite. Actual SAPI/FFmpeg stereo rendering and 14 native controls/project/vault checks passed. The later effects probe failed by reading its committed dialog string before OK. |

The actual baseline render was 534384 stereo frames at 48000 Hz (11.133 seconds).
Measured peak was 0.730438; integrated loudness -20.63 LUFS and true peak -2.73 dBTP.
This is a short synthetic SAPI production proof, not a long-form neural music,
speaker-similarity, native-language pronunciation or physical-device benchmark.

## Independently executed corrections

| Source / run | Actual result |
| --- | --- |
| 96778659f5f51e5bee77c2604960bb6bc9155d5a, [effects run 45941634859](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45941634859) | Four typed-control tests and 125 actual native Windows checks passed. Only the probe changed: live list selection before OK, committed dialog value after OK. Cancellation/invalid-value checks remain. NVDA and signal quality are NOT_RUN by this probe. |
| d35fcadd1dbecc4ab9b990e54d59981892d5b17e, [reconciliation run 45941734164](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45941734164) | 107 tests across three source suites passed. Native Import Center preparation/import/reopen and short-display focus scrolling passed. The installed-wheel transcription interface passed 26 synthetic-protocol checks, including separate edits/exports and trust-revoking restore. |
| The same d35 native search probe | Failed after one second of observing native focus. Grid size was 1014 by 0 pixels in a 1020 by 479 panel; focus stayed on Library operation status. The probe did not direct focus to hide the failure. |
| 1d0be8866ea8666e7794685d393a03506c8f4953, [compatibility run 45942181950](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45942181950) | 377 tests across 13 suites passed without failures/skips, including 76 new compatibility cases and real canonical interrupted-publication producers. |
| 14d1624b4ca824fb85531f43d3ea771d69b83f53, [compatibility run 45943179159](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45943179159) | Linux: 387 tests across 13 suites passed, including 86 compatibility cases. Windows installation failed pip check because of unrelated global runner packages, before tests. A successor isolates its dependencies in a fresh virtual environment without removing pip check. |
| 4c17d5c2b7059799f4948e415c0463f6911af345, [focus and integration run 45943256487](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45943256487) | 222 tests across eight source suites passed without skips/failures. Actual native Windows search passed 20 checks, including noncollapsed results and preserved completion focus. Short-display Import Center passed again. The installed-wheel transcription interface passed all 26 synthetic-protocol checks. Manual NVDA and actual private inference remain NOT_RUN. |
| 99dd564a588550882b536bfeaff82a075d509c1c, Windows CPython 3.12 compatibility job | 117 assertions collected: 37 passed, 80 failed, zero skipped. The new preserved-state atomic/stat tests passed; existing pending-import/ASR and untouched canonical producer boundaries rejected cross-API metadata. Windows CPython 3.12 source installation remains unqualified. The packaged target uses the existing CPython 3.11 lock; the original failures are not hidden or reclassified. |
| a5408dabdb1046c3157ca808b023ae5a9de90e34, [ASR diagnosis 45940959905](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45940959905) | 59 tests across three suites passed; 64 independent synthetic worker launches did not reproduce an earlier undiagnosed partial outcome. This was diagnostic evidence, not a demonstrated production fix. |
| [ASR atomic-status reproduction 45942643807](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45942643807) | A real synthetic producer repeatedly replacing its status file reproduced an aborted job after one accepted result. This led to a bounded dynamic-status observer fix; immutable receipts, retained inputs and final completion stay strict. |

Subsequent native and composed-tree results are recorded in the current PR and
the exact CI source-identity records. A correction is not promoted to PASS from
static review or from a predecessor's run.

## Reconciliation decisions

The reviewed canonical source is 1eff701fe848a3bd9eb088c85e5372a7c0a73861.
Its storage/revisions.py and storage/schema_identity.py exactly match the
integrated storage implementation. The bounded import-metadata check and neutral
delivery contract are reused from that source. Root pending-import and child
ownership checks retain their stronger retained-source and attempt bindings.

Canonical v3/v4 backup manifests and historical typed v3/v4 manifests have
different exact field sets. [Compatibility](CANONICAL_STATE_COMPATIBILITY.md)
recognizes those grammars separately. Legacy application state is preserved as
inert, review-required bytes, never activated as a restored provider, engine,
assistant or quotation authority. Version 7 is conditional on that preserved
component; existing typed formats remain accepted exactly. All components share
the existing complete-backup ceilings.

Search now uses the existing wx scrolling mechanism with a usable result height.
Accepted completion updates the shared status without overriding the result
handler's keyboard focus. Validation still precedes completion. The native probe
retains the actual grid/descendant focus assertion.

The composed source also includes session-selected local progress speech through the
existing CPU voice registry and player. Selection does not enable sound, select a
replacement voice or grant authority after restore. The shared dispatcher cancels
stale notices and waits for confirmed notification silence before Studio playback.
Actual native speech qualification is recorded separately from this source inclusion.
See [the progress-voice guide](LOCAL_PROGRESS_VOICE.md).

Desktop backup and restore now forward cooperative cancellation to the actual
producer and preserve a durable committed outcome when cancellation arrives late.
Restored incompatible state is explained before the new library is offered for
opening; source data and the preserved bytes remain unchanged.

## Requirement-to-implementation map

Paths below are relative to src/carr_library unless marked otherwise. An
implemented path does not, by itself, close the acceptance group.

| Requirement / scenario | Existing implementation | Remaining acceptance |
| --- | --- | --- |
| G1–G3, U1–U5: independent libraries, editions, immutable sources, exact quotations, topics and restored history | application.py; catalog/; storage/; citations/; research/; search/ | Actual private corpora and held-out semantic relevance remain corpus-specific. |
| V3-A: retained document formats, isolated conversion and managed OCR | extract/; conversion/; ingest/; ui/import_center.py | Each requested format/model/language and real scanned corpus needs its own fidelity/quality verdict. |
| V3-B: original text versus editable reading and pronunciation copies | reader/narration.py; reader/reading_desk.py; ui/narration_editor.py | Broad formula/table/image descriptions and all reader-format cases are not established by text-edit tests. |
| V3-C/D: local voices, literal reading, roles, chapters, production and reuse | audio/engines.py; audio/production.py; audio/jobs.py; ui/audio_studio.py | Actual owner-engine interoperability, voice/language quality and sustained model throughput remain separate. |
| V3-E: playback, seeking, A–B repeat, playlists, output device | audio/playback.py; audio/player.py; ui/audio_studio.py | Actual output-device and listening acceptance; no-device exit 77 is UNAVAILABLE. |
| V3-E: 21 typed effects, stereo rendering and loudness checks | audio/ffmpeg.py; audio/models.py; ui/audio_effects.py | Effect-by-effect acoustic quality, professional multitrack editing, capture and real-time monitoring remain open. |
| V3-F: saved work, queues, original inputs and recovery | library_backup.py; pending_backup.py; transcription/backup.py; preserved_state.py; audio/backup.py | Semantic migration of preserved incompatible state and final installed recovery acceptance. External runtimes/models use separate installation inventories. |
| V3-G: large libraries and bounded operations | Existing SQLite/FTS5, scoped retrieval, streaming limits and backup budgets | Representative large-corpus throughput/RAM/disk measurements on declared hardware. Bounds are not scale measurements. |
| V3-H: bibliography, citations and document regeneration | catalog/reference_exchange.py; citations/csl.py; citations/regeneration.py; research/academic_library.py | Actual requested Word/Zotero/synchronization workflows and final component rights; do not infer external-editor interoperability from generated files. |
| V3-I: typed assistant execution and local model proposals | assistant/planner.py; assistant/service.py; assistant/llm.py | Exact real-model plan acceptance/quality. Invalid raw model output is rejected; it never authorizes tools. |
| V3-J: commands, keyboard and native controls | keybindings.py; ui/command_palette.py; ui/desktop_app.py and native dialogs | Manual Windows 11/NVDA across the assembled included workflows. Automated native focus checks are separate. |
| V3-K: optional providers, credentials, transfers, costs and recovery | providers.py; assistant/translation.py; ui/api_connections.py | Authorized real provider calls and provider-specific quality/limits. No paid request is authorized by this development work. |
| V3-L/M/N: music and script-to-audio production | audio/music.py; audio/production.py; audio/jobs.py; assistant/audio_bridge.py | Genuine neural music, authorized packs, sustained generation and 1/3/10-hour quality. MIDI/synthetic assembly does not prove neural generation. |
| V3-O: canonical 135 selected languages | language_catalog/; ui/language_catalog_dialog.py | Per-capability, per-engine and per-voice qualification. All unassessed cells remain explicit. |
| V3-P/Q/R: exact runtime identity, signing, secure update and recovery | native_runtime.py; audio_runtime.py; offline_policy.py; packaging/; delivery_contract.py | Actual private security adapter/ABI, signed artifact/update, applicable rights/source obligations and final Windows delivery. No new cryptographic protocol. |
| V3-S: shared progress, real completion, cancellation and quiet behavior | progress.py; ui/progress_view.py; operations/; existing job stores | Local speech notification execution, native NVDA/listening and coexistence with playback/recording. Notifications must never enter rendered assets. |
| V3-T: publishing, interchange and complementary workflows | Existing reader/export/project manifests and command registry | EPUB3 synchronized media overlays, DAISY/Braille, qualified external-editor interchange and any unimplemented complementary workflow remain requirements. |
| V3-U: authorized owner cloning and managed profiles | Existing voice registry, local manifests and owner-engine protocol adapter | Private source is available. Actual current runtime assembly/interoperability, authorized references, cloning/profile workflow, multilingual quality and native accessibility remain open. |
| V3-V: neutral offline entitlements, renewal, accounts and translation parity | delivery_contract.py; providers.py; existing translation/derivative services | Private authority integration, perpetual-owner profile, signed refresh/key rotation/offline recovery, account service and actual Google/local quality. Generic contracts do not issue licenses or choose commercial terms. |
| ASR: supplied worker, transcripts, timestamps, edits, exports and recovery | transcription/; transcription_cli.py; ui/transcription_dialog.py | Actual supplied private worker interoperability, cache/cancel/resume and acoustic qualification. Direct official-engine inference is a separate proof. |

## Private components and delivery

The supplied transcription worker and existing owner voice/security components
have private source provenance. Their identifiers, internals, samples and runtime
assets are deliberately absent from this public record. Source availability must
not be misreported as a generic missing-project blocker.

The reviewed shared LGPL FFmpeg package remains identified by SHA256
f7ba27e7986a2d0835881e742a682dec17896f6dc2cdcf9e596f043d2763d894.
Its record still has public_distribution_clearance=false and
third_party_complete_source_bundle=false. The actual delivery builder continues
to reject an audio-bearing release package until its package-specific rights
path is implemented and qualified. Private use does not silently bypass that gate.

The current evidence does not qualify a signed installer or an end-user release.
Read/open/save/export/backup/restore/recovery and finishing active work remain
independent from an optional entitlement refresh or network failure.
