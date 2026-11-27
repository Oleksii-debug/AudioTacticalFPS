# Remaining scope after the November 27 source review

This is a bounded source audit of the current public composition and its frozen local playback/effects candidate. It distinguishes missing implementation from missing qualification. It does not narrow the current technical specification or certify completion.

## Immediate composition defects

1. The pending role-effect model uses project schema 3. Existing ClipEditor save/move code forces schema 2. Preserve the current schema when editing a clip and execute mixed schema-2/schema-3 add/edit/move/reopen/remove regressions before accepting the combined candidate.
2. The completed-master playback handoff must carry its declared SHA and size across background verification, GUI dispatch, queue admission and later open. A path and chapter timings alone do not bind playback to the previously verified master. Execute replacement-between-boundaries regressions.
3. The actual portable tail found provider-initiated Dordiai cold-discovery cancellation timing out. A separately reviewed public callback fix is being executed in the bounded [provider proof branch](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/carr/dordiai-discovery-recovery-20261127). Caller cancellation, concurrent admission and child cleanup assertions remain unchanged.
4. The completed local sync and typed-backup candidates still need to be copied into the final composition from their verified manifests. The executor disconnected before that integration and the final whole-source gate.

## Existing implementation that should be reused

- Google Basic v2 and Advanced v3 have an actual account/settings path, explicit capability discovery, preview/consent/budget, source-bound chunk jobs, pause/resume and derivative exports. Advanced accepts a configured OAuth token/expiry, selected model and validated glossary settings.
- Clipboard reading includes a default-off, session-only change watcher. Loading remains explicit, and clipboard text is not persisted into a recovery draft.
- Recorded EPUB/SMIL reading, separate live reading, source-bound editor drafts, pronunciation, spelling, reading units and appearance already have implementation paths.
- Clip editing uses immutable retained inputs and exposes start/trim/duration/gain/pan/fades, role, ordering, scene duration and preview.
- The pending single-PCM candidate adds output-device selection, Play/Pause/Resume/Stop, seek/skip, A–B, sleep, a bounded playlist and chapter controls. Its final composition and native-device qualification are pending.
- The pending offline-effect editor uses the existing DSP controls and typed chains with parameters, reorder and bypass for roles and conversion. Offline pitch/tempo are distinct from live playback controls.
- The local planner uses a loopback model adapter, typed Plan validation and explicit review/execute. It does not authorize arbitrary shell, file or network actions.

## Missing implementation

| Requirement area | Missing code or user path | Existing boundary to extend |
| --- | --- | --- |
| Microphone monitoring | Live PCM monitoring and pause/resume capture are absent. Current recording writes a staged WAV and offers Start, Stop/save, Cancel and saved-recording preview. | Existing RecordingService, microphone resource lease, owned process and CleanupOwner |
| Live player controls | Accessible live volume/mute/pan and independent live pitch/tempo are absent from the pending navigation/player candidate. | Single owned PCM playback backend and existing command registry |
| Timeline editing | Full cut/copy/paste/split/join, markers and general timeline Undo/Redo are absent. | ClipEditor, immutable Cue inputs, ProjectStore revisions and CAS |
| Effect placement | Recorded-clip chains, project master-bus chains, dry/wet comparison and named/user preset management are absent. Role effects do not cover these paths. | Existing typed DSP effects, project/cue schema, renderer and native effect editor |
| General batch synthesis | Split-by-regex/size/duration preview, batch voice/preset assignment, naming templates and complete metadata/lyric modes need typed planning glue. | Existing scenes, resumable production jobs and output publication |
| Structural/batch translation | Provider document/batch conversion preserving DOCX/EPUB structure, multi-document queue, automatic ADC/token refresh, explicit provider-to-canonical-language mapping and direct linked bilingual narration are not established. | Existing Google gateway, credential references, translation jobs and derivative reading |
| Tray/global hotkeys | Native tray lifecycle and global OS registration are absent. | Existing application command registry; opt-in registration, conflict handling and unregister-on-close |
| Managed cloning | User-facing consent/reference QA/profile lifecycle, actual owner-engine invocation, protected profile import/export/revocation and repeatability records remain absent or dependent on supplied runtime contracts. | Existing voice registry and owner-engine adapter boundary; no replacement training pipeline |
| Legacy/language speech | SAPI4 is not implemented. General language-specific stress/number/date/math/table/phoneme preparation is not established by regex pronunciation or spelling. | Existing reader, pronunciation and explicit language capability contracts |
| Professional assistant workflows | Current typed tools do not provide complete reader/evidence/conversion/player/effects operation or a professional direction workflow. | Existing allowlisted Plan model, approvals and service facade |
| Long neural music | Automatic long-form continuation/conditioning is absent; the current neural path rejects duration beyond its selected chunk capability and asks for multiple planned scenes. | Existing optional neural worker adapter, scenes, cache and production planner |

These are implementation gaps, not merely unperformed Windows tests. No status change should mark them complete because another module's tests pass.

## Small subsequent changes using existing services

After the current composition is verified, useful bounded increments include:

- Align the local planner's system prompt with the actually registered transcription tool and its reviewed arguments, without adding permission.
- Add explicit default/reset and the existing narration preset to the effect editor, preserving cancel and owner-scope behavior. This is not a complete named-preset manager.
- Open a completed, receipt-validated translation as an explicitly derived reading document without another provider request or export/import round trip.
- Copy a validated saved assistant plan into a new unapproved draft; never inherit old execution status, source scope or consent.
- Add a precisely defined split operation for a clip using the same retained input and new cue IDs, with CAS, fades, timing and schema/effect preservation.

A split button is not a complete timeline editor; a preset button is not a full preset system.

## Qualification and owner inputs remain distinct

Implemented paths separately require actual Windows output/microphone devices, latency and contention checks; human keyboard/NVDA/listening review; real supplied ASR/Dordiai/cloning/music/local-model runtimes and approved packs; per-language voice and reading quality; optional authorized Google account/quota execution; signing and per-profile distribution inputs. Registry counts, synthetic worker actors and generic source gates do not establish those results.

## Documentation corrections to carry into the final application

- READ11 should describe explicit default-off session clipboard watching and no persistence; “never monitors” is outdated for the composed implementation.
- READ16 may record device/playlist/chapter controls only after the pending PCM candidate is actually integrated. Live volume/mute/pan/pitch/tempo stay open.
- READ12 tray/global hotkeys stay open.
- AUDIO14 capture is present; monitoring, capture pause and full edit history remain open.
- Translation documentation should distinguish implemented plain-text Basic/Advanced workflows from structural/batch/document workflows.

The full specification remains the authority. This review preserves its open work instead of substituting a smaller product definition.
