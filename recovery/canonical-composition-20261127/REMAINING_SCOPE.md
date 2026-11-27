# Remaining scope after the November 27 source review

This is a bounded source audit of the archived composition and its frozen local playback/effects candidate. It distinguishes missing implementation, existing public candidates awaiting reconciliation, and missing qualification. It does not narrow the technical specification or certify completion.

The later public review inspected canonical `e69ba2403515f21509686d66f4bc8b3673756d81`, shared PR143 `4fae6fa69daf86fc9566f8847d74bccc8a4dc5a5`, and the separate Studio candidate `55725629bc0cb79b62f7f3979842a573d5fd868f`. Capabilities on that other candidate are recorded below; they are not automatically present in the audited composition.

## Immediate composition defects

1. The pending role-effect model uses project schema 3. Existing ClipEditor save/move code forces schema 2. Preserve the current schema when editing a clip and execute mixed schema-2/schema-3 add/edit/move/reopen/remove regressions before accepting the combined candidate.
2. The completed-master playback handoff must carry its declared SHA and size across background verification, GUI dispatch, queue admission and later open. A path and chapter timings alone do not bind playback to the previously verified master. Execute replacement-between-boundaries regressions.
3. The actual portable tail found provider-initiated Dordiai cold-discovery cancellation timing out. The reviewed callback fix reproduced the original negative and passed all 72 cases from four suites on Ubuntu/Python 3.11.18 in [run45986810416](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45986810416), job140464461514. The Windows counterpart remains pending at this checkpoint. Caller cancellation, concurrent admission and child cleanup assertions are unchanged; final application integration is still pending.
4. The completed local sync candidate still needs recovery from its exact manifest. Seven typed-backup changes were reconstructed from complete retained patches against public base blobs and fetched back exactly. Their fresh public composition requires its own finite execution; the separate local 118-test run is not transferred to reconstructed hashes. The executor disconnected before final integration and the whole-source gate.
5. Reconcile the newly observed canonical export, ASR/restore-trust, resource/process and import-publication guards, plus shared4fa's actual PCM geometry attestation, without replacing stronger retained-owner or native delete-sharing behavior. See [the current public overlap inventory](evidence/latest-public-overlap.json).

## Existing implementation that should be reused

- Google Basic v2 and Advanced v3 have an actual account/settings path, explicit capability discovery, preview/consent/budget, source-bound chunk jobs, pause/resume and derivative exports. Advanced accepts a configured OAuth token/expiry, selected model and validated glossary settings.
- Clipboard reading includes a default-off, session-only change watcher. Loading remains explicit, and clipboard text is not persisted into a recovery draft.
- Recorded EPUB/SMIL reading, separate live reading, source-bound editor drafts, pronunciation, spelling, reading units and appearance already have implementation paths.
- Clip editing uses immutable retained inputs and exposes start/trim/duration/gain/pan/fades, role, ordering, scene duration and preview.
- The pending single-PCM candidate adds output-device selection, Play/Pause/Resume/Stop, seek/skip, A–B, sleep, a bounded playlist and chapter controls. Its final composition and native-device qualification are pending.
- The pending offline-effect editor uses the existing DSP controls and typed chains with parameters, reorder and bypass for roles and conversion. Offline pitch/tempo are distinct from live playback controls.
- The local planner uses a loopback model adapter, typed Plan validation and explicit review/execute. It does not authorize arbitrary shell, file or network actions.

## Implementation missing from this composition or awaiting reconciliation

| Requirement area | Missing code or user path | Existing boundary to extend |
| --- | --- | --- |
| Microphone monitoring | Live PCM monitoring and pause/resume capture are absent. Current recording writes a staged WAV and offers Start, Stop/save, Cancel and saved-recording preview. | Existing RecordingService, microphone resource lease, owned process and CleanupOwner |
| Live player controls | Accessible live volume/mute/pan and independent live pitch/tempo are absent from the pending navigation/player candidate. | Single owned PCM playback backend and existing command registry |
| Timeline editing | Cut/copy/paste/split/join and draft Undo/Redo exist in public Studio557, but are not integrated into this Project/Cue composition. Markers and the complete general timeline workflow remain unconfirmed. | Reconcile the existing public ClipEditor operations with immutable Cue inputs, ProjectStore revisions and CAS |
| Effect placement | Public Studio557 implements a master chain, dry/wet and bounded named presets. These require reconciliation with this composition. Recorded-clip effect chains remain unconfirmed. | Reuse the public typed editor/mastering code while preserving this project/cue schema, renderer and captured native owner guards |
| General batch synthesis | Split-by-regex/size/duration preview, batch voice/preset assignment, naming templates and complete metadata/lyric modes need typed planning glue. | Existing scenes, resumable production jobs and output publication |
| Structural/batch translation | Provider document/batch conversion preserving DOCX/EPUB structure, multi-document queue, automatic ADC/token refresh, explicit provider-to-canonical-language mapping and direct linked bilingual narration are not established. | Existing Google gateway, credential references, translation jobs and derivative reading |
| Tray/global hotkeys | Native tray lifecycle and global OS registration are absent. | Existing application command registry; opt-in registration, conflict handling and unregister-on-close |
| Managed cloning | User-facing consent/reference QA/profile lifecycle, actual owner-engine invocation, protected profile import/export/revocation and repeatability records remain absent or dependent on supplied runtime contracts. | Existing voice registry and owner-engine adapter boundary; no replacement training pipeline |
| Legacy/language speech | SAPI4 is not implemented. General language-specific stress/number/date/math/table/phoneme preparation is not established by regex pronunciation or spelling. | Existing reader, pronunciation and explicit language capability contracts |
| Professional assistant workflows | Current typed tools do not provide complete reader/evidence/conversion/player/effects operation or a professional direction workflow. | Existing allowlisted Plan model, approvals and service facade |
| Long neural music | Automatic long-form continuation/conditioning is absent; the current neural path rejects duration beyond its selected chunk capability and asks for multiple planned scenes. | Existing optional neural worker adapter, scenes, cache and production planner |

These are implementation or integration gaps, in addition to unperformed qualification. Another branch's source or passing tests do not establish a complete merged application.

## Existing public Studio candidate

[Studio557](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/55725629bc0cb79b62f7f3979842a573d5fd868f) uses `audio.models.ProductionPlan` / `audio.production` / `ui.audio_studio`. This differs from the frozen `studio.project.Project` / Cue model. Its seven workflows were queued when reviewed; no runtime result is attributed here.

| Candidate capability | Existing implementation | Reconciliation requirement |
| --- | --- | --- |
| Cut, copy, paste, split, join and draft Undo/Redo | `audio/clips.py`, `ui/audio_clips.py`, `ui/audio_studio.py`; retained-asset SHA/root identity, exact samples, processing/fade checks and draft fingerprint | Transfer the existing operations through the current immutable inputs, CAS and native owner lifecycle; preserve schema-3 effects |
| Master effects and dry/wet | `audio/mastering.py`, `audio/models.py`, `audio/production.py`; measured frame count, exact chapter slices and pre-master stems | Preserve the current render/resource/cleanup contracts and master identity |
| Named presets | `audio/effect_editing.py`, `ui/audio_effects.py`; bounded typed load/review/apply and explicit approved save | Preserve captured-ID and nested-modal guards in the composed native editor |

This candidate does not resolve the two immediate PCM issues: closing from the worker callback can still acknowledge closure before backend cleanup, and master playback admission is path-based without the declared SHA/size. Its `ProductionPlan.schema_version=3` has different semantics and does not fix the frozen Project/Cue schema downgrade.

## Small subsequent changes using existing services

After the current composition is verified, useful bounded increments include:

- A [canonical-only planner correction](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/942e2b914223329320946cfc9c5b26d76da78948) now describes the registered transcription tool and adds six proposal/boundary cases. Its [fresh source proof](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45988474768) reproduced the original omission and passed 64 cases across three suites, with no failures/errors/skips and unchanged source. Canonicale69 has the registry/dispatch contract; shared4fa does not, so the prompt alone must not be copied onto shared4fa.
- Reuse the public typed preset/editor implementation, including any appropriate default/reset controls, while preserving cancel and owner scope. Integrating that code still requires native lifetime and resulting-source verification.
- Open a completed, receipt-validated translation as an explicitly derived reading document without another provider request or export/import round trip.
- Copy a validated saved assistant plan into a new unapproved draft; never inherit old execution status, source scope or consent.
- Reuse the public split/edit operations through the same retained input and new cue IDs, with CAS, fades, timing and schema/effect preservation; avoid a second independent timeline implementation.

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
