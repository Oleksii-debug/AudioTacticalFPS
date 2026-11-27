# Recording, clips and synchronized publication checkpoint — 2026-11-27

## Composed source

This composition keeps the existing library, typed ProductionPlan, Audio Studio, model registry, provider gateway, assistant, progress host, resource coordinator and backup contracts.

- Explicitly selected microphone capture uses the shared native process-family owner and resource admission. The capture UI freezes its selected device/limits, reserves quiet notification/output state, and waits for confirmed cleanup before releasing it. Original PCM and its typed receipt survive complete backup and restoration.
- Chapter clips add explicit start/trim/duration, track, gain, stereo balance and fades to schema2 plans. Existing schema1 serialization/cache identity remains compatible. Native editing, retained input validation, undo/recovery, selected preview and per-track render output use the current Studio.
- Synchronized EPUB3 and DAISY2.02 export reuse verified completed PCM and exact measured sample intervals. Original scripts remain separately retained; export is new-only and has a recoverable publication receipt. It does not run a second speech model.
- Ingestion, supplied-worker ASR, speech and FFmpeg now share the owned-family start/stop boundary. Read-only stdin and explicit logs remain open until native descendants stop; completion receipts are checked after that boundary. Pending cleanup retains the caller's stage/locks, including interrupted cleanup.
- The generic trusted bundle adapter uses the same retained cleanup wait. Standard managed installers retain their existing exclusive installation lease.
- Canonical fail-closed model inventory scanning and portable Windows/NFC/case path validation are carried into the existing stores. A canonical Studio conversion bridge is still being composed separately.

Reusable public candidate source: capture49437694, ownerae4fa0d8, publicatione904f5bb/c6dc454e, clipsf12985fb, modelae2609da, legacy2beb00d6. Private supplied transcription source and assets remain outside public Git.

## Exact earlier execution

At `7f29e861fe8cef42be302e395f109eccccf18e83`:

- Linux shared contracts: all45 required suites passed, 1,075 passing cases, zero skipped/failed cases. The separate real loopback fixture then had12 passing and2 failing cancellation-exception expectations. The current fixture checks the existing ProviderError and uncertain-ledger contract explicitly.
- Windows audio: actual two-voice SAPI synthesis, FFmpeg stereo mix/stems/chapters, assistant production and verified resume passed;16 native production checks and109 native effect-control checks. Source regression was744 PASS/3 FAIL/7 explicit platform/optional-hardware skips. The fresh-private-root render and snapshot-injection corrections are in this source; this earlier run is not a full audio PASS.
- Windows supplied-worker supervision:112 receipt,41 service,27 backup and28 UI cases passed; one live-lock fixture failed on Windows CRLF. This composition retains both exact platform newline forms.
- Signed-client matrix: each platform passed10/11 required suites; the remaining failure expected another desktop's keyboard-installer name. The current test checks this desktop's real menu-before-registry construction. The downstream native anchor/UI steps did not run in that failed matrix.
- Windows snapshot source:310 passing cases. Installed verification stopped at a missing explicit offline-runner mode before producing JUnit; the current probe supplies that mode and rejects a missing report clearly.
- Bibliography remains independently green at this exact commit. At731cf6c it passed26 suites/992 cases plus108 installed native clipboard/export checks; those earlier counts are not reassigned to later source without its log.

## New acceptance gates

The composed code is **NOT_RUN until its own exact CI runs**. Required strict suites include real owned descendants, retained capture/recovery, clip PCM rendering, publication/backup integrity, portable legacy state and fail-closed model inventory. Native Windows probes cover capture controls with an explicitly synthetic local FFmpeg source, clip parameters and audiobook metadata. They do not open or qualify physical microphones.

Independent EPUBCheck/DAISY validation adapters, canonical Studio conversion and canonical-branch semantic convergence continue as separate scoped work. No alternate Studio/runtime is installed by this checkpoint.

## Remaining qualification

Manual NVDA and physical-device listening/capture, real supplied Whisper/model inference and language quality, owner-authorized cloning assets, the canonical135-entry per-capability assessment, production publisher/signing/site/entitlement configuration, signed managed A-to-B deployment and distribution clearance have no substitute in synthetic/control tests. Nothing here claims release qualification from a source commit.
