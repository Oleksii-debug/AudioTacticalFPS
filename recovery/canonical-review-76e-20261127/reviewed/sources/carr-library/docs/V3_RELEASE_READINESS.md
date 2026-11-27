# Resonance V3 implementation and delivery status

Resonance by Sceneria Innovative Technologies combines the research library,
reading tools and audio workflows. This document separates source integration,
executed tests and real device/model acceptance. A complete signed release is
not established by passing synthetic-engine tests. Package names, launcher
filenames and data locations keep their existing compatibility contracts.

## Current canonical convergence

The candidate reconciles the accepted feature head
`96294e97498f2133bf93f02ce72f84b6e709c72b` with canonical
`3beebd5aca900b8bf2e86498ae79db4015c96705`. Both contain the updated
`UNIVERSAL_SCOPE_V3_FINALIZATION.md`; this work retains the current catalog,
job stores, provider ownership, source reader, original bytes and import
publication receipts while incorporating its missing canonical user paths.

The convergence uses one current backup writer, one bounded citation executor,
one transcription service, one command manager and the shared progress
contract. Legacy formats are decoded explicitly. Their presence never grants
permission to execute restored engines, change user shortcut profiles or
accept source/receipt identities inferred from matching text.

## Implemented components and acceptance boundaries

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped originals, exact quotations, academic references, managed Fast/Best OCR profiles and installed model selection | Actual SQLite and OCR tests; native installed OCR manager runs on Windows |
| Reading and voices | Source-preserving reading derivatives, portable edition/revision/asset lineage, explicit detached-draft recovery, native pronunciation review, recorded EPUB/SMIL playback and the existing SAPI5 COM worker | Historical formats and original bytes remain intact. Recorded overlays use exact fragment ranges, isolated bounded parsing and shared admission before snapshot writes, extraction and conversion. Native lifetime/keyboard gates require exact-head execution; physical listening, reader interoperability and human NVDA remain separate |
| dordiai-voices bridge | Explicit connection to a separately installed reviewed local runtime; bounded JSONL discovery/PCM, persistent worker, content-bound cache fingerprints and cancellable inventory | Public adapter only: no private worker/model bytes are copied. Synthetic actual-child tests cover changed assets, cancellation, cleanup failure and retained ownership. An installed owner runtime, authorized reference material, hardware capacity and acoustic quality still need independent qualification |
| Audio studio | Existing roles/scenes/narration/DSP plus explicit DirectShow microphone capture, retained recording receipts, schema-2 trim/position/fade clip editing, preview, mix/stems, measured chaptered M4B/LRC and typed input backup | Ordinary narration remains schema1 with unchanged historical fingerprints. Actual synthetic FFmpeg capture/clip and crash-recovery tests are required; physical microphone quality and real-time monitoring are not established. The existing 1/3/10-hour streaming fixtures are not full-length neural inference |
| Synchronized EPUB and DAISY publication | One Studio publisher, native format selector and CLI export completed schema1 narration and schema2 clip productions as EPUB3 media overlays or a DAISY2.02 NCC/XHTML/SMIL/MP3 file set; DAISY requires explicit publisher metadata | Both formats retain the same resource, destination, atomic publication and prepared-receipt recovery authority. Exact-head tests include real process death, backup/restore and native controls. Pinned EPUBCheck and offline DAISY DTD checks are required; DTD validation does not certify all DAISY rules, hardware readers, word alignment or Braille |
| Shared operations | Native Tasks and resources view and CLI over the existing import, Studio, ASR and assistant stores; cooperating CPU/RAM/disk/device reservations, shared CUDA exclusion and cancellable admission | Canonical ASR, actual isolated imports, process ownership and controller regressions are required by the exact portable/native/installed gates; disposable metadata cleanup cannot reverse a verified completed output; hardware capacity and hard-crash orphan management remain separate |
| Research search and acquisition | Extended isolated document adapters, explicit advanced query builder, scoped lexical/semantic search and cache; opt-in DOI/ISBN metadata and source acquisition through current catalog/import services | Original assets remain unmodified. Provider statements do not approve quotations or establish rights. Malformed-document, scope, cache, consent and publication tests are required; corpus scale, live endpoint behavior and full format fidelity remain separate |
| Documents and citations | Bounded BibTeX/RIS/CSL-JSON import, selected-field metadata review, isolated conversion, seven current CSL styles and historical editable DOCX regeneration through one bounded QuickJS executor; original style/processor provenance retained | Actual parser, local processor, cancellation/rollback, generated-document and overwrite-confirmation regressions; no live Word add-in |
| Recovery | One common format-4 writer for database, originals, supported settings, pending imports, reading drafts, projects and typed retained Studio/ASR inputs; strict readers for supported historical formats 1–5 | Historical envelopes retain their own validators; reading does not activate engines or resume jobs. Retained-media locks, exact source identities and catalog-v5 immutable import publication receipts remain authoritative. External exports and OS credentials are separate |
| Exports | Selected-file approval binds file identity and bytes; new files publish without replacement; completed exports retain success if only temporary cleanup fails | Actual competing creates, equal-size content edits, moved destinations, cancellation and bounded-read tests; existing files above the 32 MiB overwrite-check limit require a new filename |
| Supplied transcription | Existing Whisper worker protocol, strict source preparation, reviewed local runtime/model inventories, isolated processes, immutable input and atomic TXT/SRT/VTT/JSON receipts; native file/folder selection, jobs/status/pause/cancel/resume and separate transcript editing, plus CLI | Actual process/filesystem tests use synthetic recognition, including a separate execution of the supplied worker; real model inference, throughput, native runtime isolation and human NVDA remain unqualified |
| Keyboard and progress | One command manager and contextual keymaps, searchable command palette, one shared progress contract with compatibility facades, measured or indeterminate native indicators and optional selected local-voice notices | The palette preserves explicit saved shortcut conflicts and stable command identities. Audio playback/capture quiets notifications. Cold voice discovery is cancellable and stays off the UI thread; failed worker cleanup retains ownership for retry. Exact-head native probes are required, and manual NVDA remains NOT_RUN |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Existing typed scoped executor and deterministic parser plus an explicit local compatible-LLM proposal path, profile selection, plan preview and separate Run action in UI/CLI | Capability, profile identity, scope, budget and plan validation surround the real gateway. Edited goals discard pending proposals. Synthetic loopback HTTP and native controls are tested before the runtime firewall; actual model inference and paid provider quality remain unqualified |
| Translation | Optional official Google Basic v2 and Advanced v3 plain-text translation, explicit source ranges, directional language inventory, existing glossary selection, scoped consent, decoded-text receipts, resume and export through the same gateway | Exact source/account/model/glossary/approval bindings and short-lived OAuth expiry are covered by synthetic provider, CLI and native-handler cases. Completed saved derivatives remain exportable offline. Actual API/IAM, glossary language coverage, formatted-document fidelity, automatic token renewal and local inference remain separate |
| Delivery preparation | Native ResearchLibrary.exe, exact-source sealed bundles and the existing Inno Setup wrapper: per-user Start-menu launch, versioned repair generations and normal uninstall preserving unknown/data files | Required Windows gates bind build/source/executable identities and actual install, failing bootstrap, repair with an old interpreter alive, uninstall and final bundle seal. This is an unsigned development installer; exact-head native acceptance, publisher signing and distribution clearance are separate |
| Portable snapshots | Explicit snapshot publication, inspected transfer codes, optional password encryption and restore into a new folder over the complete typed backup producers | AES-GCM, scrypt and HKDF use the pinned cryptography library. Actual encrypted roundtrips, competing writers, process interruption, changed destinations and committed-cleanup outcomes are covered by source tests. Windows DPAPI/native controls and the installed artifact need exact-head qualification |
| Public trust client | Versioned Ed25519 entitlement verification, signed-file renewal, configurable idle refresh, protected credential references, replay anchors and local TUF update verification | Public clients and synthetic policy fixtures are integrated; production authority, endpoints, trust roots, account/billing service and signing material remain separate owner configuration. Managed installations continue to use their approved installer path; an update is not activated merely by checking status |

| Reader appearance and spelling | Explicit font, colors, high contrast, 50–400% zoom/reset and bounded saved preferences; code-only spelling engine with explicit local dictionaries, suggestion preview and undo | Current source regressions passed at96294e; native lifetime/installed composition still needs exact-candidate acceptance. Dictionaries are not downloaded or bundled implicitly |
| Audio metadata and cover | Native review and bounded metadata/cover editing through the existing audio tools and destination publication authority | New common and installed/native probes are part of the candidate; source review is complete, integrated execution pending |

## Executed evidence and remaining gates

| Exact revision | Scope | Result |
| --- | --- | --- |
| `96294e97498f2133bf93f02ce72f84b6e709c72b` | Portable source integrity, actual declared dependencies | 5001 passed plus 14 subtests; zero failures and skips; [run45969316754](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316754) |
| Same96294e revision | MSVC and pinned Zig launchers executed on native Windows | Both jobs passed; [run45969316748](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316748) |
| Same96294e revision | Full Windows integrated desktop and installed bundle | Reader/bookmark and DAISY URI acceptance failed; integrated source exposed the isolated fixture import and temporary-report cleanup failures before exceeding its 20-minute bound. No complete Windows PASS is claimed; [run45969316892](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969316892) |
| `d94e8800261f74c238fc8a4ff6684f84f8040b00` | Real-file export publication, approval, cancellation, full-content hash and native Windows ChangeTime | 138 passed on Linux and 138 passed on Windows, zero failures/skips; [run45974691968](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45974691968) |
| Complete canonical composition | All new seams together, source and installed origins, fresh native bundle | NOT_RUN at this checkpoint. Component and predecessor results do not qualify a different tree. |

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
Historical processor, style and locale bytes remain available for genuine
document regeneration; no stored URL or path is followed. The primary bundle
does not need Node. Exact source, dependency wheels, full notices and the
57-component base SBOM remain part of the delivery closure. Checksum integrity
does not establish a publisher signature.

Measured M4B chapters and LRC use actual verified output timing. Synchronized
EPUB3 and DAISY2.02 publication share the existing typed Studio receipts and
atomic publisher. Pinned EPUBCheck and offline DAISY DTD tests provide structural
evidence. They do not certify every reader, word alignment, Braille or acoustic
quality. Long 1/3/10-hour streaming fixtures do not establish equally long
uninterrupted neural inference.

## Requirements still open

The following cannot be labelled DONE by passing the current core tests:

- Exact-head standard installer/uninstall/repair acceptance, a signed
  ResearchLibrary.exe and installer, owner-configured publisher trust and a
  complete distribution review for the selected profile.
- Compatible private entitlement authority, account-optional perpetual policies,
  signed unattended refresh and offline renewal, and signed update A-to-B,
  interruption/recovery and key-rotation acceptance.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Real-time microphone monitoring, the remaining requested timeline/effect/player
  controls and format workflows, supported legacy/SAPI4 combinations and live
  reader interoperability. Capture, preview and an effect list do not complete
  those separate scenarios.
- Actual installed dordiai-voices and MusicGen/ACE-Step model inference, the
  remaining [finite reading-workflow checklist](READING_WORKFLOW_CHECKLIST.md),
  chaptered-audio device/interoperability qualification and Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

## Supplied transcription and private components

The supplied Whisper Transcribe V4.3 worker has a reviewed source preparation
and an exercised synthetic process/protocol boundary. Its historical adapter
delegates to the existing current TranscriptionService and job/lease owner.
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
[transcription source review](TRANSCRIPTION_INTEGRATION_REVIEW.md).
The exact published source and checks are tracked in
[PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143).

Every deployment profile keeps access to user-owned data. This implementation
does not publish private content, acquire paid services, sign an application
with an invented identity or authorize external distribution. See the actual
candidate's reports before treating any source-reviewed addition as executed
or qualified.
