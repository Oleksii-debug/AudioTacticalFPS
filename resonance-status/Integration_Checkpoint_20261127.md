# Resonance integration checkpoint — 27 November 2026

Status: development candidate; not a qualified release or a completed platform.

This composition builds on integration head `dd533fa64bddd63ca13ffe23dbdab07f0ef4d0a5` and retains canonical `e3cf74c243d4ef930b526b5648ca9003c0900057`. Both incoming DAI-003 commits remain intact: the speech writer preserves its existing cancellation checkpoints when no validator is supplied, and explicit Reader WAV publication rechecks the exact reading copy and retained source before the final link. Those narrow changes are composed into the reviewed voice-lifecycle and listening successors. A further six-line writer guard rechecks staging, destination and the original parent identity after the potentially slow validator; three real-filesystem regressions reject same-path parent replacement and newly introduced hardlink aliases. The adjacent source map records the exact base, file identities, lineage and review evidence. The actual candidate identity is the checkout commit recorded by its own CI.

## Software composed in this candidate

### Reader, audio and local engines

- Optional BRF export uses the explicitly selected, hash-checked official Liblouis archive through its C API and the existing source/publication boundary. English UEB grade 1 and Ukrainian six-dot logical BRF are the implemented table profiles. The configured source is rechecked after slow translation and before publication. The CLI/native JSON pipe is ASCII escaped while user files retain their exact UTF-8 text. No embosser layout, tactile-language quality or NVDA result is inferred.
- Listening speed and pitch use a bounded private processed copy and the existing WavePlayer, shared worker and resource broker. The original recording remains unchanged. Device selection, original-frame mapping and failed-cleanup ownership are retained. Closing or replacing a reader cannot release a still-owned native output or publish a late result into a different library.
- A saved playback queue retains managed relative PCM identities, ordered labels, original-frame cursor and passive settings. Restart and restore do not start audio. The explicit standalone player command uses the same transport; live volume, mute and stereo balance affect only the owned output handle. Full and standalone backup routes verify copied PCM hashes and dimensions without opening old sources or devices. A visible save with unconfirmed durability remains dirty until an explicit successful retry.
- Dordiai/voice settings retain failed owners for retry, reject closed-registry selection and publication, and prevent blocked discovery from installing a late host after close. The existing adapter and installed engine stay authoritative; no replacement speech engine, model download or private source is added.

### Studio and conversion

- Studio schema 3 persists ordered typed clip, role, scene and master effects. Empty-effect projects retain existing schema 1/2 behavior, fingerprints and cached-job semantics. Rendering uses the existing DSP/process/admission services. Track stems and mastered chapter copies have explicit processing boundaries and bounded copy allowance. Unsupported timeline-changing combinations fail explicitly.
- Effect editing, project revision checks and backup validation are joined to the existing Studio and managed-input services. Copied project records must match the required retained input and effect closure. No arbitrary FFmpeg expression or second production service is introduced.
- Explicit ordered conversion batches wrap the current single-file converter. Preview binds source bytes, typed settings, selected runtime, folder identity and resource allowance; running requires that exact review. Only a returned, durably recorded success receipt is reusable. Matching bytes after a crash or a competing create do not prove ownership: uncertain files remain untouched for review.
- The GUI and CLI share the batch service. A native default-No review lists the selected files and outputs. Cancel and receipt-write failure preserve actual completed outputs and report their state. The external conversion manifest is not silently included as restored execution authority.

### Installer, trust and observed failure corrections

- Installed signed-client evidence now binds actual collected file paths and exact local test identities to JUnit, independently of the caller's pytest root. Missing, duplicate, foreign, skipped and failed cases fail the gate.
- Installer diagnostics read bounded, owned logs only after their process tree and writers settle. Unicode shortcuts are inspected through the read-only Windows shell-link API and checked against the exact installed target. The setup explicitly registers `UninstallDisplayName=Resonance`; fixed nonsecret health tags expose the precise failed boundary.
- Managed-update baseline A is the immutable historical `665acf2c1fdb5a30a05ef31c482eb02dcaf1e141`. A specific retained historical A→B run establishes that old baseline's compatibility. The current A→candidate B installation, rollback, repair and uninstall must still run; old success is not transferred.
- Three native probes now read the existing public `CommandSpec.command_id` field. Translation fixtures check the actual explicit destination-approval message, numerical zero cost and rejection without late UI mutation. The ASR export fixture explicitly requests the four formats it subsequently exports.
- Native Tasks destruction waits for the actual HWND to cease existing before queuing the late controller callback. The idle-renewal regression keeps both autonomous renewals on the same running host, proves the first accepted sequence through the second real request, and reads the final protected state only after the host and worker stop. Product locks, timers and lifecycle contracts are unchanged.

## Required execution and evidence

Every newly composed behavior is **NOT_RUN** until this exact candidate is executed. Source review and byte-exact blob readback are not Python parsing, pytest collection, native acceptance or installed-package qualification. The local execution environment remains unavailable.

The main integrated source selection retains all336 published predecessor selectors and adds18 whole modules, for354 selectors. The installed selection retains all166 predecessor selectors and adds the same18 modules, for184. These additions cover BRF, persisted Studio effects, lifecycle, listening, trust-collector, saved-queue and conversion-batch regressions.

An independent Windows source regression job runs27 relevant whole modules in parallel with the long bundle job. It uses the same locked dependencies, existing verified codec preparation and strict per-suite runner. Its purpose is earlier execution feedback; it cannot replace the full installed/native gates or qualify a distributable.

Source and installed gates require the complete selected suites with no failures, skips or expected failures, plus exact origin and case inventories. Reader usability now requires33 cases; reader playback52; standard installer102. New queue suites require31/21/36/24 cases; conversion batch suites36/21/4. These are static expected inventories until actual collection confirms them.

The existing real MCI probe remains mandatory. Additional BRF, persisted Studio effects, saved queue and conversion-batch native blocks preserve exact installed source checks and their required named actions. Independent failures accumulate and still fail the compound gate before wheel retention. Existing updater, launcher, transcription, translation, narration, publication, backup, networking boundaries and final seals remain required.

## Actual predecessor evidence

These observations belong only to `ed74d168c81a21e68bbe1673a729181dfef96ee4`:

- Syntax compiled847 Python files and checked31 explicit packages. Keyboard, Linux encrypted transfer and both Flite jobs completed successfully.
- Windows signed-client source tests passed324 cases; installed tests passed322 with2 default-codepage fixture failures. Linux source and installed tests passed323 each, but the evidence wrapper rejected its configuration-dependent JUnit mapping.
- V3 installed recovery reported1355 passed and3 failed, with no skips. Installed publication reported781 passed with no failures/skips. Native document/book/effect probes then exposed the stale command field described above.
- Linux ASR reported382 passed and1 failed, with no skips: its fixture planned only TXT but attempted a four-format export.
- The main native Tasks gate reached successful controller detachment and then timed out waiting for the hidden dialog's destruction event. The combined installed trust pass exposed the two Unicode fixtures and an independent periodic-read race in the idle-host test.
- Windows Audio and AudioEndpointBuilder were running, but WinMM enumerated **zero output devices**. Format queries returned an out-of-range device ID and actual MCI open returned326. An audio output is still an unresolved test-environment prerequisite; changing PCM rate, mocking playback or marking that probe skipped would not qualify it.
- The managed source suites passed131 cases across5 modules, and both installer builds completed. Installation of the previous A failed. The revised immutable baseline and Unicode/registration corrections still need their own real run.

Main, V3, Windows ASR and Windows transfer runs were cancelled when the integration branch advanced to the incoming DAI-003 commits. This was not evidence of a test timeout or overall success.

## Continuing software work and final qualification

Multi-book orchestration is being adapted to the current BookAudioService and existing production jobs, with immutable plan membership and passive restore. It is not part of this frozen candidate.

Remaining software scope includes microphone pause/resume and explicit monitoring, recoverable reader tabs/bookmarks/presentation and voice preferences, bounded search continuation, durable large-input/scan continuation, scheduled application backups/watch folders, authorized cloned-profile lifecycle, formatted-document translation and the remaining optional integration surfaces. The detailed technical specification remains authoritative; these items are not reclassified as merely missing external inputs.

The supplied private-engine review archives remain supplied/reviewed; their presence is not model or acoustic qualification. Actual authorized engine/profile operations, real-model language quality, the canonical135-language matrix, physical listening/microphone, manual Windows/NVDA, tactile/layout checks and measured corpus-scale targets remain separate acceptance evidence. Paragraph translation does not preserve arbitrary source page/table/image layout, and protocol fixtures do not establish an installed model's quality.

No private engine code, model weights, voice samples, credentials or personal jobs enter public Git or CI artifacts. Audio-runtime/driver inspection inputs stay outside the application and transfer payload. A separate inspection of a signed virtual test endpoint may establish a viable CI environment; it is not yet an installed or qualified dependency.

The canonical merge and owner transfer package require actual exact-source, installed and native gates. No final package is published from this checkpoint. All15 existing schedules and their cadence/enabled state remain unchanged.
