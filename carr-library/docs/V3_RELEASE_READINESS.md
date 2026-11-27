# Resonance V3 implementation and delivery status

Resonance by Sceneria Innovative Technologies combines the research library,
reading tools and audio workflows. This document separates source integration,
executed tests and real device/model acceptance. A complete signed release is
not established by passing synthetic-engine tests. Package names, launcher
filenames and data locations keep their existing compatibility contracts.

## Current canonical convergence

The source composition reconciles canonical
`e5c64a5245583f8f8b404a0f042f6ca438c24d7f`, the earlier PR143 feature input
`198a488b72c73b57e7e8bad87d69c3915bfd5e31`, and the reviewed integrated
reader/conversion/effects/recovery, retake, dictionary, schema3 clip, independent
playback, verified-download continuation and typed local-tool additions. It follows the unchanged current
`UNIVERSAL_SCOPE_V3_FINALIZATION.md` and retains catalog V5, original bytes,
source lineage, import publication receipts and both exact saved ASR codecs.
The committed files are a source composition. Execution verdicts belong to the
Actions run and installed artifacts for this exact commit; they are not
predeclared by this document.

The convergence uses one current backup writer, one bounded citation executor,
one transcription service with explicit historical/current codecs, one command manager and the shared progress
contract. Legacy formats are decoded explicitly. Their presence never grants
permission to execute restored engines, change user shortcut profiles or
accept source/receipt identities inferred from matching text.

## Completed public implementation additions

The current source includes three previously open implementation groups:

| Requirement | Current route | Verification boundary |
| --- | --- | --- |
| V3-03 / V3-05 independent listening | Native reader/Studio settings for tempo, pitch, volume, mute and balance reuse the existing player, worker and admission owner. Tempo/pitch prepare bounded private PCM; gain/mute/balance affect only queued PCM for the selected output. | Source audio, synthesis settings and other players stay unchanged. Existing MCI is the default; explicit levels use an owned WinMM handle. Exact queue bounds and device limitations are documented in [independent playback](INDEPENDENT_PLAYBACK.md). |
| V3-09 typed local tools | The existing Assistant now has 21 allowlisted tool names, including current-source/evidence selection, voice/effect discovery, inspected serial audio conversion and current-reader status/Pause/Stop. | Preview and explicit Run use the same durable executor and exact artifact receipts. Completed side effects are verified before recovery; a late journal warning cannot invent rollback or authorize replay. The existing reader bridge never creates a second player. See [local tools](ASSISTANT_LOCAL_TOOLS.md). |
| V3-18 Pause / Resume / Cancel | The existing signed updater retains a bounded inert prefix, refreshes TUF metadata on Resume and verifies the full signed length and hashes before making an installable package. | Actual local/TLS Range cases and native keyboard/progress/lifetime probes are included. Opening/restoring a checkpoint neither resumes nor installs. See [verified continuation](VERIFIED_UPDATE_RESUME.md). |

The same composition retains canonical Save All behavior, exact ASR runtime-file
inventory, both saved transcript codecs, and source-preserving no-clobber exports.
Office conversion uses the reviewed short native workspace; the XML reader admits
only the recorded empty static geometry and disabled empty header shapes. Active
content, unsupported drawing formulae, unexpected text and unknown geometry
attributes remain explicit refusals. Spreadsheet formula cells retain the existing
explicit omission warning; their evaluated values are not presented as source text.
The native ASR page stops and detaches its timer before an owned worker can outlive
the deleted window.

Native EPUB acceptance has two explicit modes. Its default executes WavePlayer
against actual Windows output. The controlled-output mode uses the existing
disclosed PCM fixture while executing actual publication/import, prepared-WAV
ownership, native keys, source revocation and cleanup. It reports physical output,
WavePlayer and listening as NOT_RUN; it is never an automatic fallback after a
physical failure. Actual device inventory is preserved independently.

Actual hosted Windows evidence at `0c3068672884058d3479144431369120e68ca4ee`,
[run46041427289](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46041427289),
reported zero WinMM output devices and refusal of all four tested output formats;
the physical EPUB attempt failed with MCI326. The same run passed all 108 source
cases and the native retained-timer direct/parent deletion checks. CI therefore
selects the explicit controlled EPUB mode while keeping that failed physical
attempt and the real-device requirement visible.

## Implemented components and acceptance boundaries

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped originals, exact quotations, academic references, managed Fast/Best OCR profiles and installed model selection | Actual SQLite and OCR tests; native installed OCR manager runs on Windows |
| Reading and voices | Source-preserving reading derivatives, portable edition/revision/asset lineage, explicit detached-draft recovery, native pronunciation review and bounded author-observed BXD/DIC/REX text-rule import, recorded EPUB/SMIL playback and the existing SAPI5 COM worker | Historical formats and original bytes remain intact. Recorded overlays use exact fragment ranges, isolated bounded parsing and shared admission before snapshot writes, extraction and conversion. Native lifetime/keyboard gates require exact-head execution; physical listening, reader interoperability and human NVDA remain separate |
| Session playback controls | Explicit Windows output selection and opt-in tray/global keys for one current live reader; no hidden clipboard watcher or background daemon | Actual device inventory and OS key registration are native gate subjects. Session selections are temporary, owner-bound and revoked on native destruction; physical routing/keypresses/listening remain separate |
| dordiai-voices bridge | Explicit connection to a separately installed reviewed local runtime; bounded JSONL discovery/PCM, persistent worker, content-bound cache fingerprints and cancellable inventory | Public adapter only: no private worker/model bytes are copied. Synthetic actual-child tests cover changed assets, cancellation, cleanup failure and retained ownership. An installed owner runtime, authorized reference material, hardware capacity and acoustic quality still need independent qualification |
| Audio studio | Existing roles/scenes/narration/DSP, explicit microphone capture controls, optional selected-output dry monitoring, retained PCM and retake receipts; schema3 sample-range editing, crossfades, markers, loop and dry/wet preview, and typed effects on clip/role/scene/master buses | Schema1/2 wire fingerprints remain unchanged. Retakes and rendered joins create new retained assets. Real FFmpeg range/resampling, old-cache rejection, lifecycle and backup tests are required; exact installed native gates exercise keyboard/loop/ownership. Physical microphone quality and measured monitoring latency remain separate. See [clip editing](STUDIO_CLIP_EDITING.md) |
| Ordered effects and presets | Typed supported offline effect chains, reordering, bypass, bounded undo/redo and named local presets; exact preset files included in complete backup | Installed FFmpeg capabilities and numeric limits are rechecked at execution. This extends the existing conversion path and does not establish live microphone effect monitoring |
| Synchronized EPUB and DAISY publication | One Studio publisher, native format selector and CLI export completed schema1 narration and schema2/3 clip productions as EPUB3 media overlays or a DAISY2.02 NCC/XHTML/SMIL/MP3 file set; DAISY requires explicit publisher metadata | Both formats retain the same resource, destination, atomic publication and prepared-receipt recovery authority. Exact-head tests include real process death, backup/restore and native controls. Pinned EPUBCheck and offline DAISY DTD checks are required; DTD validation does not certify all DAISY rules, hardware readers, word alignment or Braille |
| Shared operations | Native Tasks and resources view and CLI over the existing import, Studio, ASR and assistant stores; cooperating CPU/RAM/disk/device reservations, shared CUDA exclusion and cancellable admission | Canonical ASR, actual isolated imports, process ownership and controller regressions are required by the exact portable/native/installed gates; disposable metadata cleanup cannot reverse a verified completed output; hardware capacity and hard-crash orphan management remain separate |
| Research search and acquisition | Extended isolated document adapters, explicit advanced query builder, scoped lexical/semantic search and cache; opt-in DOI/ISBN metadata and source acquisition through current catalog/import services | Original assets remain unmodified. Provider statements do not approve quotations or establish rights. Malformed-document, scope, cache, consent and publication tests are required; corpus scale, live endpoint behavior and full format fidelity remain separate |
| Documents and citations | Bounded BibTeX/RIS/CSL-JSON import, selected-field metadata review, isolated conversion, seven current CSL styles and historical editable DOCX regeneration through one bounded QuickJS executor; original style/processor provenance retained | Actual parser, local processor, cancellation/rollback, generated-document and overwrite-confirmation regressions; no live Word add-in |
| Workflow templates | Typed assistant, Studio-project and retained-batch templates; explicit parameters and per-item overrides, reviewed expansion, import/export and passive backup | One existing execution host and current domain services. New use rechecks scope, inputs, voices and output permission; restoring or opening a template executes nothing. Native keyboard/lifetime acceptance is independent of model quality. See [templates](WORKFLOW_TEMPLATES.md) |
| Selected legacy reading | Explicit checked LibreOffice and Calibre routes, plus isolated MHTML/TXTZ/readable ZIP text | Fixed supported families, retained originals and the current no-clobber derivative/provenance pair. Dedicated actual six-family Office and eight-family Calibre gates are required; no visual fidelity, hidden OCR, decryption or automatic converter selection. See [legacy](LEGACY_DOCUMENT_READING.md) and [Calibre](CALIBRE_READING.md) |
| Retained document batches | Existing conversion workers and reading copies, exact stream splitting, portable filename previews, explicit review/approval, no-clobber text/provenance and Studio output; UI and CLI share one service | Completed artifacts are reverified on resume; restored destinations require fresh review. Estimated duration is not measured speech timing, and imported prose is never executed as a Studio script |
| Recovery | One common format-4 writer for database, originals, supported settings, pending imports, reading drafts, projects and typed retained Studio/ASR inputs; strict readers for supported historical formats 1–5 | Namespace checks stream and stop at the first undeclared entry; cancellation is honored before staging. ASR cache requires explicit manifest inclusion while retained inputs, accepted results and edit revisions remain ordinary user data. Restored engines stay inert and typed media/source/receipt identities remain authoritative |
| Exports | Selected-file approval binds file identity and bytes; new files publish without replacement; completed exports retain success if only temporary cleanup fails | Actual competing creates, equal-size content edits, moved destinations, cancellation and bounded-read tests; existing files above the 32 MiB overwrite-check limit require a new filename |
| Supplied transcription | Existing Whisper worker protocol, strict source preparation, reviewed local runtime/model and complete recorded-distribution inventories, isolated owned processes, captured export/edit identities and atomic TXT/SRT/VTT/JSON receipts; native file/folder jobs, pause/cancel/resume and transcript editing, plus CLI | Actual-child regressions distinguish the unchanged native deadline from later synchronous validation: timely native exits remain timely; living roots and descendants are stopped at the original limit. Every final cancellation and content check remains required. Synthetic recognition does not establish real model quality, throughput or human NVDA |
| Keyboard and progress | One command manager and contextual keymaps, searchable command palette, one shared progress contract with compatibility facades, measured or indeterminate native indicators and optional selected local-voice notices | The palette preserves explicit saved shortcut conflicts and stable command identities. Audio playback/capture quiets notifications. Cold voice discovery is cancellable and stays off the UI thread; failed worker cleanup retains ownership for retry. Exact-head native probes are required, and manual NVDA remains NOT_RUN |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Existing typed scoped executor and deterministic parser plus an explicit local compatible-LLM proposal path, profile selection, plan preview and separate Run action in UI/CLI | Capability, profile identity, scope, budget and plan validation surround the real gateway. Edited goals discard pending proposals. Synthetic loopback HTTP and native controls are tested before the runtime firewall; actual model inference and paid provider quality remain unqualified |
| Translation | Optional official Google Basic v2 and Advanced v3 plain-text translation, explicit source ranges, directional language inventory, existing glossary selection, scoped consent, decoded-text receipts, resume and export through the same gateway; ordered reviewed document batches reuse those exact child jobs, approvals and provider ledger | Exact source/account/model/glossary/approval bindings and short-lived OAuth expiry are covered by synthetic provider, CLI and native-handler cases. Completed saved derivatives remain exportable offline. Actual API/IAM, glossary language coverage, formatted-document fidelity, automatic token renewal and local inference remain separate |
| Signed maintenance | Explicit owner-source HTTPS/TUF check and download, separate reviewed activation, protected-anchor-bound portable and standard update/recovery, native maintenance status and current owned launch entry | Default installation invents no publisher authority. Synthetic signed fixtures and actual setup/health/repair/uninstall gates are separate from production signing, hosting and credentials. Successive updates retain an independently bound generation history and existing research data. See [maintenance](SIGNED_UPDATE_MAINTENANCE.md) |
| Delivery preparation | Native ResearchLibrary.exe, exact-source sealed bundles and the existing Inno Setup wrapper: per-user Start-menu launch, versioned repair generations and normal uninstall preserving unknown/data files | Required Windows gates bind build/source/executable identities and actual install, failing bootstrap, repair with an old interpreter alive, uninstall and final bundle seal. This is an unsigned development installer; exact-head native acceptance, publisher signing and distribution clearance are separate |
| Portable snapshots | Explicit snapshot publication, inspected transfer codes, optional password encryption and restore into a new folder over the complete typed backup producers | AES-GCM, scrypt and HKDF use the pinned cryptography library. Actual encrypted roundtrips, competing writers, process interruption, changed destinations and committed-cleanup outcomes are covered by source tests. Windows DPAPI/native controls and the installed artifact need exact-head qualification |
| Public trust client | Versioned Ed25519 entitlement verification, signed-file renewal, configurable idle refresh, protected credential references, replay anchors and local TUF update verification | Public clients and synthetic policy fixtures are integrated; production authority, endpoints, trust roots, account/billing service and signing material remain separate owner configuration. Managed installations continue to use their approved installer path; an update is not activated merely by checking status |

| Reader appearance and spelling | Explicit font, colors, high contrast, 50–400% zoom/reset and bounded saved preferences; code-only spelling engine with explicit local dictionaries, suggestion preview and undo | Current source regressions passed at96294e; native lifetime/installed composition still needs exact-candidate acceptance. Dictionaries are not downloaded or bundled implicitly |
| Audio metadata and cover | Native review and bounded metadata/cover editing through the existing audio tools and destination publication authority | New common and installed/native probes are part of the candidate; source review is complete, integrated execution pending |

## Current required gate inventory

| Gate | Required evidence | Separate limitation |
| --- | --- | --- |
| Portable source integrity | All selected existing and new suites, zero failures/skips, actual declared OCR/audio dependencies and real POSIX races | Synthetic recognition/model peers do not establish inference quality |
| Native Windows V3 | Installed source identity, citation/backup/import/ASR tests, actual native controls and verified runtime egress denial | Posted input and native controls do not replace physical keyboard, listening or NVDA |
| Full Windows bundle | Exact wheel/tree, sealed install, native probes, source and installed suites, EXE restart, repair/uninstall and final seal | Unsigned development installer; distribution and production trust require separate clearance |
| Native launchers | Both MSVC and pinned Zig for this exact committed application source | Compiler execution does not prove publisher signing |
| Bibliography | Installed import/export, seven retained styles, original/adapted runtime receipts and historical DOCX regeneration | No live Word add-in or external provider acceptance |
| Selected legacy converters | Actual six-family Office and eight-family Calibre conversion with selected executable identities, private worker ownership, cancellation and unchanged source bytes | Authored fixtures establish their exact supported text routes; visual fidelity and third-party protected material are outside these probes |
| Current feature integration | 114 common suites, eight POSIX and four Windows-specific suites, plus twelve installed native workflow probes and one actual output-device inventory, all from one committed union | Focused evidence supports diagnosis; the complete installed bundle and source matrix remain required |

The Actions checks attached to [PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143/checks)
and their exact-commit artifacts provide the current verdict. A failed,
unfinished or skipped required job is not a PASS. The focused component review
runs are diagnostic evidence and do not replace the complete matrix.

## Executed predecessor evidence

| Exact revision | Scope | Result |
| --- | --- | --- |
| `96294e97498f2133bf93f02ce72f84b6e709c72b` | Portable source integrity, actual declared dependencies | 5001 passed plus 14 subtests; zero failures and skips; [run45969316754](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316754) |
| Same96294e revision | MSVC and pinned Zig launchers executed on native Windows | Both jobs passed; [run45969316748](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316748) |
| Same96294e revision | Full Windows integrated desktop and installed bundle | Reader/bookmark and DAISY URI acceptance failed; integrated source exposed the isolated fixture import and temporary-report cleanup failures before exceeding its 20-minute bound. No complete Windows PASS is claimed; [run45969316892](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316892) |
| `d94e8800261f74c238fc8a4ff6684f84f8040b00` | Real-file export publication, approval, cancellation, full-content hash and native Windows ChangeTime | 138 passed on Linux and 138 passed on Windows, zero failures/skips; [run45974691968](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45974691968) |
| `42eb288fcebb39e36eb374f338b2fb159ccd5a77` | Pinned citation adaptation, unchanged seven-style oracles, original/adapted sorting and historical-receipt regeneration on Windows | 64 passed, zero failures/skips; [run45988886603](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45988886603). This component result does not qualify the full composition. |
| `9e7cb2ac5a58a7cf9ff3bedb6270b86300eb345d` | Earlier complete portable source selection | 7011 passed and 65 failed, with 17 passed subtests; [run45984540473](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45984540473). Its fixture/observer corrections are included in the subsequent composition; this failed run remains failed. |
| `7530a3632c2d57f76baee5ebf17de818a8f7c9ab` | Bounded dictionary import, existing review/handler/worker boundaries and 49 captured author-tool observations | Source 159 and isolated installed 159 passed, zero failures/skips; [run46028769389](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46028769389). The extended native dialog and final union require their own gates. |
| Current committed composition | All new seams together, source and installed origins, fresh native bundle | Require the gate inventory above for this exact commit. Predecessor counts cannot be reassigned. |

The earlier export attempt `c27b8b8` passed Linux 138 but failed 38 Windows
cases because its real-descriptor observer used the unavailable
`os.O_ACCMODE` constant. The d94e correction uses the exposed write-mode bits;
the actual production reads and assertions remain in place. Six additional
canonical bibliography CLI/workbench cases are restored for the complete
candidate and require its own run.

Each CI evidence artifact identifies the exact source commit and dependency
closure. Installed tests resolve actual product modules from the installed
environment and compare their bytes with the sealed application wheel. The
Windows workflow preserves native controls, source-opening events, fresh EXE
bootstrap, normal shutdown, restart, standard installation/repair/uninstall and
the final unchanged-bundle seal check. Failed or skipped steps are not evidence
of success. Human NVDA, listening and physical devices remain separate.

The focused correction commit `1dd1c6b3611aa21c5463d251257542b7eeeece36`
has now passed its Windows export, real descendant cleanup, isolated EPUB batch
and Reader constructor regressions, followed by the actual installed full-source
reader/bookmark native probe. Its Linux run and the complete canonical
composition remain separate gates. The earlier V3 installed recovery timed out
at 70% of its 25-minute run without a recorded test failure; its incomplete job
was cancelled by the 75-minute job bound. Expanded bounds preserve every required
suite and verdict rather than treating unfinished work as passed.

## Backup and import authority

The current writer emits common format 4. Supported historical formats 1–5 readers
are strict, read-only decoders with their original envelope and typed-producer
rules. Literal prior-writer fixtures verify compatibility independently of the
new writer. A backup includes only declared typed state and retained inputs,
with size/hash, path, source and publication checks. Unreadable inventory
subtrees fail visibly; they are not treated as absent. Generated audio is an
explicit choice, and external exports need their own copies.

Catalog schema 5 is independent of the archive format. It keeps the canonical
immutable `import_publication` receipt layout. The incompatible pre-release
`publication_receipt` dialect is rejected without rewriting the database.
A worker freezes its launch identity before resource admission and rechecks the
retained input before publishing. Recovery requires the actual matching
request/attempt receipt. A similar existing revision or a timing watermark
does not prove completion.

Restore creates a new folder and never resumes work automatically. Old
path-bound attempt hints are discarded; explicit resume validates the source
and obtains a new attempt. Reader projects preserve their supported source
dialects, history and detached drafts without inventing quotation authority.
Saved bookmarks keep their exact library/edition/revision/asset/text identities.

## Publication, cancellation and resource ownership

All research exports use captured destination approval, a bounded full-content
fingerprint and fresh scope checks before publication. Existing files over
32 MiB, or files whose inspection exceeds the checking budget, require a new
name. These are replacement-inspection bounds, not limits on new export sizes
or guarantees about filesystem latency. Text and binary publication retain
actual no-clobber and native ChangeTime checks.

Studio and transcription preserve verified committed output when later cleanup
or a directory durability operation reports an error. They do not turn corrupt,
missing or foreign equal-byte replacements into successful output. Cancellation
before publication remains effective. An operation that has already committed
reports its saved result accurately.

Shared admission accounts for cooperating CPU, memory, disk and exclusive
devices. Studio also checks retained logical bytes, destination capacity and
incremental inventory bounds. These declarations do not claim system-wide hard
RAM limits. A child, playback device or provider that has not confirmed closure
retains its owner, files, lock and lease for retry. Progress observer failures
cannot reverse business completion or replace an output location with a
misleading operation failure.

## Audio and citation packaging

The main transferable bundle is audio-free. `SetupLocalAudio.cmd` first shows a
plan and prepares the pinned public FFmpeg inputs only after explicit
confirmation, into a new folder outside the application. The prepared runtime
is not a redistribution clearance. CI attaches it to a separate fresh bundle
copy for real runtime verification under the existing egress rules; the
original transferable bundle remains independently checked.

Current bibliography and historical document regeneration use the same
installed QuickJS executor with CPU, memory, stack and input/output bounds.
Historical processor, style and locale bytes remain exact for document
regeneration; no stored URL or path is followed. New documents also record the
separately pinned three-edit sort-macro runtime adaptation, its upstream revision,
hashes and notice. Historical receipts remain accepted by their exact grammar;
a new regeneration records the current adaptation explicitly. The primary bundle
does not need Node. Exact source, dependency wheels, full notices and the
version-aligned base and semantic SBOMs remain part of the delivery closure. Checksum integrity
does not establish a publisher signature.

Measured M4B chapters and LRC use actual verified output timing. Synchronized
EPUB3 and DAISY2.02 publication share the existing typed Studio receipts and
atomic publisher. Pinned EPUBCheck and offline DAISY DTD tests provide structural
evidence. They do not certify every reader, word alignment, Braille or acoustic
quality. Long 1/3/10-hour streaming fixtures do not establish equally long
uninterrupted neural inference.

## Qualification and owner dependencies

The following cannot be labelled DONE by passing the current core tests:

- The required exact-commit installer/uninstall/repair gate must pass in the
  attached run. A signed ResearchLibrary.exe/installer, owner-configured publisher
  trust and distribution review for the selected profile remain additional
  conditions even after that automated gate passes.
- Owner provisioning and qualification of the actual private entitlement/update
  authority, production trust, endpoints and protected credentials. The public
  account-optional/perpetual, signed refresh/offline renewal and signed update
  clients are integrated; their synthetic policies and actual native update gates
  do not establish a deployed production service or cross-version migration.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Physical microphone/output monitoring latency and listening, verified
  legacy/SAPI4 combinations and live reader interoperability. Source integration
  of dry monitoring, reviewed workflow templates and selected legacy converters
  is distinct from those device/runtime qualifications. Supported dictionary grammar and per-format limits remain explicit in the
  finite reading checklist and their dedicated format guides.
- Actual installed dordiai-voices and MusicGen/ACE-Step model inference, the
  explicitly limited scenarios in the [finite reading checklist](READING_WORKFLOW_CHECKLIST.md),
  chaptered-audio device/interoperability qualification and optional Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

## Supplied transcription and private components

The supplied Whisper Transcribe V4.3 worker has a reviewed source preparation
and an exercised synthetic process/protocol boundary. The public
`transcription.service` coordinator dispatches the canonical profile/UUID and
historical hash64 jobs by exact paths and typed records. Its historical adapter
delegates to that one current service, isolated process and retained lease owner.
Native file/folder selection, saved jobs, pause/cancel/resume, separate transcript
editing and typed recovery remain part of the public implementation. Real
recognition quality, throughput and a verified owner runtime/model installation
require their own evidence. Diarization and word alignment are not inferred
from ordinary segment output.

The public dordiai bridge remains an adapter to an explicitly selected local
installation. Restored declarations stay inert, including legacy declarations.
No private worker/model bytes, recordings, credentials or voice profiles are
copied into this repository. Public entitlement/update clients likewise require
owner-configured authority, endpoints, trust roots and signing material before
production use.

## Guides and project tracking

Use [the Ukrainian quick start](START_V3_UK.txt),
[document boundaries](DOCUMENT_FORMATS_V3.md),
[academic search](ACADEMIC_SEARCH.md),
[bibliography](V3_BIBLIOGRAPHY.md),
[historical DOCX provenance](academic_integration.md),
[keyboard profiles](KEYBOARD_SHORTCUTS.md),
[reading workflow checklist](READING_WORKFLOW_CHECKLIST.md) and
[transcription source review](TRANSCRIPTION_INTEGRATION_REVIEW.md),
[retained document batches](DOCUMENT_BATCHES.md), [effect presets](ORDERED_AUDIO_EFFECTS.md),
[playback outputs](OUTPUT_DEVICES.md), [session keys/tray](READ12_SESSION_CONTROLS.md),
[workflow templates](WORKFLOW_TEMPLATES.md), [ordered translation batches](TRANSLATION_BATCH_UK.md),
[selected legacy documents](LEGACY_DOCUMENT_READING.md), [Calibre reading](CALIBRE_READING.md)
and [signed maintenance](SIGNED_UPDATE_MAINTENANCE.md),
[independent listening](INDEPENDENT_PLAYBACK.md), [typed local tools](ASSISTANT_LOCAL_TOOLS.md)
and [verified download continuation](VERIFIED_UPDATE_RESUME.md).
The exact published source and checks are tracked in
[PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143).

Every deployment profile keeps access to user-owned data. This implementation
does not publish private content, acquire paid services, sign an application
with an invented identity or authorize external distribution. See the actual
candidate's reports before treating any source-reviewed addition as executed
or qualified.
