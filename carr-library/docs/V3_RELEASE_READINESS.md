# Resonance V3 implementation and owner-test delivery status

This document describes the implemented library, reading and audio preview of
Resonance by Sceneria Innovative Technologies. It does not close the complete
platform specification or the finalization addendum. An owner-test application
bundle and a signed distribution-cleared release have separate acceptance
requirements. Existing package, launch filenames and data locations remain
unchanged until a verified migration; visible application titles use Resonance.

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
| Documents and citations | Bounded BibTeX/RIS import, selected-field metadata review, isolated conversion, local citeproc-js styles/locales and editable DOCX regeneration with citation provenance | Actual parser, local processor, cancellation/rollback, generated-document and overwrite-confirmation regressions; no live Word add-in |
| Recovery | Manifest-bound archive v5 includes pending import bytes, identities, settings, projects, discoverable reader drafts and typed retained Studio recording/clip inputs; catalog v5 keeps immutable import publication receipts | Existing migration, portability and source-link guards remain. Complete snapshot acquires retained capture locks, preserves declared input hashes and validates typed archive-v5 extensions while retaining older supported envelopes. Restored jobs require explicit review before resuming. External published audio/books and OS credentials remain separately declared |
| Exports | Selected-file approval binds file identity and bytes; new files publish without replacement; completed exports retain success if only temporary cleanup fails | Actual competing creates, equal-size content edits, moved destinations, cancellation and bounded-read tests; existing files above the 64 MiB overwrite-check limit require a new filename |
| Supplied transcription | Existing Whisper worker protocol, strict source preparation, reviewed local runtime/model inventories, isolated processes, immutable input and atomic TXT/SRT/VTT/JSON receipts; native file/folder selection, jobs/status/pause/cancel/resume and separate transcript editing, plus CLI | Actual process/filesystem tests use synthetic recognition, including a separate execution of the supplied worker; real model inference, throughput, native runtime isolation and human NVDA remain unqualified |
| Keyboard and progress | Configurable menu keys, searchable command palette over the existing command registry, named native controls, measured progress and optional selected local-voice notices | The palette preserves explicit saved shortcut conflicts and stable command identities. Audio playback/capture quiets notifications. Cold voice discovery is cancellable and stays off the UI thread; failed worker cleanup retains ownership for retry. Exact-head native probes are required, and manual NVDA remains NOT_RUN |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Existing typed scoped executor and deterministic parser plus an explicit local compatible-LLM proposal path, profile selection, plan preview and separate Run action in UI/CLI | Capability, profile identity, scope, budget and plan validation surround the real gateway. Edited goals discard pending proposals. Synthetic loopback HTTP and native controls are tested before the runtime firewall; actual model inference and paid provider quality remain unqualified |
| Translation | Optional official Google Basic v2 and Advanced v3 plain-text translation, explicit source ranges, directional language inventory, existing glossary selection, scoped consent, decoded-text receipts, resume and export through the same gateway | Exact source/account/model/glossary/approval bindings and short-lived OAuth expiry are covered by synthetic provider, CLI and native-handler cases. Completed saved derivatives remain exportable offline. Actual API/IAM, glossary language coverage, formatted-document fidelity, automatic token renewal and local inference remain separate |
| Delivery preparation | Native ResearchLibrary.exe, exact-source sealed bundles and the existing Inno Setup wrapper: per-user Start-menu launch, versioned repair generations and normal uninstall preserving unknown/data files | Required Windows gates bind build/source/executable identities and actual install, failing bootstrap, repair with an old interpreter alive, uninstall and final bundle seal. This is an unsigned development installer; exact-head native acceptance, publisher signing and distribution clearance are separate |
| Portable snapshots | Explicit snapshot publication, inspected transfer codes, optional password encryption and restore into a new folder over the complete typed backup producers | AES-GCM, scrypt and HKDF use the pinned cryptography library. Actual encrypted roundtrips, competing writers, process interruption, changed destinations and committed-cleanup outcomes are covered by source tests. Windows DPAPI/native controls and the installed artifact need exact-head qualification |
| Public trust client | Versioned Ed25519 entitlement verification, signed-file renewal, configurable idle refresh, protected credential references, replay anchors and local TUF update verification | Public clients and synthetic policy fixtures are integrated; production authority, endpoints, trust roots, account/billing service and signing material remain separate owner configuration. Managed installations continue to use their approved installer path; an update is not activated merely by checking status |

The native V3 acceptance script performs installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs while the selected runtime executables have outbound
firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN.

## Current composition and evidence states

The final integration also selects compatible reader tools and Studio controls
from the reviewed PR112 source, plus the public trust and portable-snapshot
clients from the reviewed PR99 source. It retains the current source reader,
resource broker, project versions, publication receipts and archive-v5 producer.
Find/Replace uses an isolated bounded regex worker; clipboard capture is explicit;
dictionary and keymap exports use the existing destination approval and completion
receipts. Studio pronunciation edits use revision and fingerprint checks.

The latest integrity corrections check cancellation immediately before publishing
a staged Studio artifact, probe every pending-import root without suppressing
access errors, and enumerate restore contents against the bounded manifest.
Recorded-EPUB cleanup retains its input, lock and resource lease while an actual
worker or playback device still owns them. A verified committed output is
reported as saved when only subsequent cleanup or directory durability fails;
changed or foreign outputs do not acquire that status.

Each current CI run identifies its exact source commit, installed Python module
bytes and dependency pack. The earlier evidence snapshots below are historical
inputs; they do not qualify newly composed code. Required native probes include
reader tools, keyboard identities, conversion controls, Studio playback and
pronunciation, trust status and encrypted portable restore. Human NVDA and
acoustic/model/profile qualification remain distinct evidence.

The source composition reconciles these immutable input revisions:

- Current shared composition `6a28a7ecdcfdf7dab2d5b948608c42803fe3e8a0`, including PR149/PR165 document/search/acquisition, live reading, canonical dordiai settings, retained media, translation and standard installation.
- Independently reviewed selected PR112 reuse snapshot `042e44820bd3ec1ec0ab7c17236139ced58c9d25`, including recording/publication recovery, reader lifetime, pronunciation/palette, local planning and DAISY. This snapshot was never substituted wholesale for the later shared head.
- Canonical `4b6f8169986c4a58b43e4a23bc081fbefadbbdcb`, including predecessor `c532323459157ae389d9c64d234f00beb1616151`'s exact bounded OCR inventory and three installed regression identities, plus bounded complete music-runtime inventories. The shared head's retained temporary-process ownership and isolated Numba cache are preserved.

Both original-data and saved-state contracts are retained. The canonical
per-library dordiai declaration, live reader and archive-v5 extension producer
remain authoritative; only their missing lifecycle/integrity behavior is
reconciled from the earlier selected work. No parallel ASR, provider store,
catalog, queue, updater or private engine is substituted. No private model or
voice assets are copied.

For this addition, SOURCE_REVIEWED and ADAPTER_PRESENT are established by review;
INTEGRATED refers only to the published source composition. EXECUTED remains
NOT_RUN until its exact commit's source and installed reports are available.
QUALIFIED and DISTRIBUTION_CLEARED are separate decisions and are not inferred
from source tests, catalog counts or successful synthetic processes.

The predecessor e07545a source run passed 1619 tests and 14 subtests. Follow-up
source defd4fc passed 1620 tests and 14 subtests with zero failures or skips in
[V3 run45944559713](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45944559713).
Its native recovery and integrated desktop runs were cancelled by the later shared-head update before completion; they do not establish a complete Windows PASS.
The earlier e07545a Windows
installed recovery run exposed one failure among 630 cases: the Win32
MoveFileExW path cannot replace an open destination solely because its reader
shares delete access. The follow-up defd4fc keeps the fully flushed stage and
legacy retry bound, then uses FileRenameInfoEx with POSIX replacement semantics.
The regression now executes the exact SHA-bound production writer in a real
child and checks old/new coherent snapshots, an ordinary-reader negative
control, Unicode paths and refusal cleanup. That fix also needs its own native
execution; no predecessor PASS qualifies the later source composition.

The new native acceptance covers actual installed controls and process/file
behavior with synthetic sources. Recording probes do not certify a physical
microphone. Local planning probes use an explicit loopback response fixture and
do not certify an actual LLM. dordiai probes do not certify the private engine.
All new probes preserve the final sealed-bundle checksum check and exclude
external audio runtimes, DTDs, validators and generated media from uploads.

## Measured audiobook output and current preservation fixes

Chaptered M4B uses the current FFmpeg runtime and Studio cache/publication
contracts. The adapter escapes bounded FFmetadata, remuxes the encoded stream,
and reads actual ffprobe chapter/title/timeline properties before publication.
Chapter titles and measured intervals form part of the cache key. Same-byte
foreign files, changed inspected containers and unowned hard links cannot borrow
this attempt's completion. A verified published M4B remains saved if later
notifications or temporary cleanup fail; the existing job stores those notices.
Native cleanup uncertainty retains the existing process-owned staging lease.

`script.lrc` uses the existing measured narration-line onsets. It does not invent
word timing or transcribe recorded clips. The normal job artifact receipt and
backup producer retain that sidecar; reading an old completed job does not add
new output or rerender it. Source and installed gates select actual FFmpeg/Flite,
child-exit, resume, foreign-file and cleanup regressions. These are pending
exact-composition execution, not an acoustic or audiobook-player certification.

Selected PR166 preservation checks propagate unreadable state directories and
root lookup errors; missing state is optional only at the explicitly allowed
first probe. They retain the current scoped archive-v5 producer and capture lock.
PR172 honors cancellation after a blocking durable flush, before the create-only
publication point. PR174 validates the existing explicit local/private runtime
policy before acquisition and reports those same sealed declarations. Neither
policy metadata nor a passing synthetic case grants redistribution clearance.

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

The supplied Whisper Transcribe V4.3 implementation now has an integrated adapter
and an exercised process/publication boundary. This advances source integration;
real recognition quality and an approved runtime/model pack remain separate gates.
No private worker bytes, recordings, virtual environment or model weights are
bundled in the public repository. See [the updated source review](TRANSCRIPTION_INTEGRATION_REVIEW.md)
and [the exact adapter contract](TRANSCRIPTION_SUPPLIED_WORKER.md).

Current integration is tracked in [PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143).
Its Windows source and installed-package workflows select the transcription,
reader recovery, import-publication, export and shared-resource regressions.
Native launcher acceptance binds committed source, build evidence and the exact
EXE bytes. The fresh-bundle gate requires absent `.venv`, launches the real EXE
through the existing installer, opens and normally closes the owned Resonance
window, repeats startup, and checks the original synthetic library. These are
required automated gates, not a claim that their latest composition has passed.
The complete installed bundle also opens the native Tasks and resources view.
Shared-resource output tests use a pinned external private FFmpeg runtime; those
binaries are not added to the sealed application or evidence uploads.
A final unchanged-bundle
check remains mandatory after all installed tests, including real crash fixtures. Read the checks for
the exact final head: a predecessor's passing run does not qualify a later tree.

Windows state-file readers retain bounded descriptor checks while allowing an
atomic replacement to proceed beside an active read. Draft autosave stops on the
owner's captured native destruction identity, including a rewrapped event; queued
callbacks cannot reach a deleted window. Required Windows probes wait for real
timer delivery, then check Close and direct Destroy beyond the original period.
Captured native IDs also detach pending reader, language, Studio, publication and
assistant callbacks when a native parent deletes its children. Modal chooser
returns recheck their owner before reading or changing controls.
The OCR runtime inventory also bounds directory traversal and checks cancellation
between entries; an unreadable directory cannot silently hide runtime payloads.

New translation chunks bind the decoded text and detected language to the gateway
usage receipt. Resume and export check that independent receipt, including after
a full backup/restore. A saved selection restores its account, text, language pair
and budget before any queued request. Changed fields require a new selection;
completed offline exports do not require a new credential lookup or paid call.
Legacy wire-only receipts remain explicitly distinguishable from decoded-text
proof and are never upgraded by inventing evidence.

Synchronized publication uses the current Studio job, resource policy, export
destination approval and create-only writer. A verified committed EPUB remains a
success when only final receipt or temporary cleanup is interrupted, with a
separate warning. A prepared receipt can verify exact saved bytes after a real
process exit, including the writer's residual hard link, without deleting another
name or relaxing generic source/state link guards. Complete backup retains the
publication receipt metadata; the external EPUB/audio is not silently copied.
After restore to another location, the user explicitly selects the existing EPUB
for byte and provenance verification. This does not rerender the old production.

EPUBCheck 5.3.0 is pinned by archive size and SHA256 in the CI-only tool manifest.
DAISY structural checks reuse the locked lxml runtime with an explicit five-file
DTD/entity set pinned by size and SHA256. The resolver accepts only those exact
local files and declared public identifiers, with network resolution disabled.
An intentionally invalid XHTML must fail the same validator. This is structural
evidence, separate from full DAISY-rule and reader interoperability qualification.
The EPUBCheck Java process joins the existing outbound firewall scope during
conformance acceptance; the native publication probe uses the actual installed package and a
hash-bound external test audio runtime. The validator archive, Java installation,
audio binaries and synthetic output are excluded from the evidence uploads and
sealed application. Conformance, native SAPI, source/installed regressions and the
final unchanged-bundle check must succeed for the exact source being assessed.

The complete-backup format advances to archive v5 for typed retained-media extensions, while earlier supported archives remain readable. Catalog schema v5 adds only import
publication receipts; the exact earlier schema definitions remain unchanged.
Opening a supported v3 or v4 library validates and migrates it transactionally.
Restoring an older snapshot preserves its original version until that normal
application-open migration. Recovery never infers completion from the timing of
another import or from a matching pre-existing revision.
The canonical v5 layout uses `import_publication`, with the exact schema and
immutable revision/request/attempt receipt contract accepted in PR150. The
unmerged draft v5 layout named `publication_receipt` is incompatible and is
rejected without rewriting the database. No parallel receipt store is created.
A worker freezes its launch identity before waiting for resources, rechecks the
retained input before execution, and validates that identity before publication.
Restoring pending work clears the old path-bound attempt and candidate; a resumed
import must establish a fresh attempt and cannot borrow an earlier receipt.
Automatic stopped-worker recovery also requires its intact matching candidate
hint. A missing or damaged hint grants no completion authority; explicit resume
must revalidate the saved source and journal. A receipt alone does not establish
successful recovery after arbitrary request-file loss.

Existing v1 reading projects remain readable. All three reviewed v2 source
dialects are checked: current portable `source_anchor`, the earlier
`carr.narration-source.v1` anchor with `text_verified_at_read`, and literal
`source_origin` projects with their established recovery identity and family
hashes. Reading an older project does not rewrite it or infer missing source
identity; edits and undo history remain available. A pre-release format notice
and detached autosave state preserve the old file and matching recovered edits.
No dialect grants quotation authority. Matching text alone never attaches a
draft to another source.
A discovery-link failure after a committed draft, or a recovery-draft failure
after a committed Save As, is reported separately from the successful save.

Every deployment profile retains local operation and access to user-owned data.
Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.


## Current platform composition

The current integration composes PR149's reviewed extended document adapters,
advanced research search and semantic cache, source reading transport, explicit
recording controls, nondestructive clip timeline, retained media inputs and
typed archive-v5 extensions with PR143's exact source recovery, resource broker,
publication receipts, protected exports and source-bound native launcher.
The archive format version is independent of the catalog schema: catalog V5
continues to mean the canonical immutable `import_publication` layout. Archive
v5 retains supported earlier envelopes and validates typed extension schema3.

Source and installed acceptance include the previous complete gate plus the
added component cases; no prior source, native or unchanged-bundle seal is
removed. The new native probes exercise controls and synthetic engine contracts;
physical recording/playback, real ASR/music/LLM inference and human NVDA are
separate qualification requirements. Read the exact published head and its
checks in PR143; component or predecessor evidence is not final acceptance.
