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
| Reading and voices | Source-preserving reading derivatives, portable edition/revision/asset lineage, explicit detached-draft recovery, pronunciation edits, local voice discovery, playback and SAPI5 native COM worker | Real source/backup/restart tests preserve original bytes and never infer identity from equal text; earlier Windows SAPI PCM and local Flite evidence remains specific to its tested build; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats | Real FFmpeg production and Windows restart; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Synchronized EPUB publication | Reused Studio publisher, native controls and CLI export completed schema-1 productions as EPUB 3 media overlays; verified existing MP3 is reused or the current FFmpeg adapter converts the WAV master | Required tests cover exact source/timeline provenance, no-clobber publication, actual child death before/after linking, shared resources, receipt recovery and full backup/restore. Installed native SAPI/EPUB and pinned EPUBCheck gates require fresh exact-head execution; line synchronization does not establish word alignment, reader interoperability, DAISY or Braille qualification |
| Shared operations | Native Tasks and resources view and CLI over the existing import, Studio, ASR and assistant stores; cooperating CPU/RAM/disk/device reservations, shared CUDA exclusion and cancellable admission | Canonical ASR, actual isolated imports, process ownership and controller regressions are required by the exact portable/native/installed gates; disposable metadata cleanup cannot reverse a verified completed output; hardware capacity and hard-crash orphan management remain separate |
| Documents and citations | Bounded BibTeX/RIS import, selected-field metadata review, isolated conversion, local citeproc-js styles/locales and editable DOCX regeneration with citation provenance | Actual parser, local processor, cancellation/rollback, generated-document and overwrite-confirmation regressions; no live Word add-in |
| Recovery | Manifest-bound archive v5 includes pending import bytes, identities, settings, projects, discoverable reader drafts and typed retained media extensions; catalog v5 records immutable request/attempt publication receipts in the import transaction | Real process interruption, competing imports, exact v3/v4/v5 schema validation, rollback, portable-name validation and restore tests; externally exported audio and OS credential secrets are separate |
| Exports | Selected-file approval binds file identity and bytes; new files publish without replacement; completed exports retain success if only temporary cleanup fails | Actual competing creates, equal-size content edits, moved destinations, cancellation and bounded-read tests; existing files above the 64 MiB overwrite-check limit require a new filename |
| Supplied transcription | Existing Whisper worker protocol, strict source preparation, reviewed local runtime/model inventories, isolated processes, immutable input and atomic TXT/SRT/VTT/JSON receipts; native file/folder selection, jobs/status/pause/cancel/resume and separate transcript editing, plus CLI | Actual process/filesystem tests use synthetic recognition, including a separate execution of the supplied worker; real model inference, throughput, native runtime isolation and human NVDA remain unqualified |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Actual Windows key injection and progress/result regressions; human NVDA and listening review remain required; physical capture/monitoring needs a real device test; recording controls use the separate explicit-device capture service |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references and per-request consent/budget | Local and synthetic provider tests; no paid provider call was made during implementation |
| Translation | Optional Google Basic v2 selected-text derivatives, explicit supported-language query, consent, decoded-text receipts, scope-bound resume and new exports | Synthetic transport with the real gateway/ledger, bound UI handlers and complete backup/restore tests; completed saved text remains exportable without another request. Live API, translation quality, Advanced v3 and local model inference are not qualified |
| Delivery preparation | Reviewed native ResearchLibrary.exe in the existing sealed bundle, exact-source wheels, pinned dependency/SBOM closure and parent-manifest binding | Portable C and builder regressions; required Windows gates bind actual process and installed-executable checks to the sealed SHA256. Publisher signing and standard installer acceptance remain open |
| Private integration boundary | Separate versioned entitlement/update protocols and preservation of user-data operations | 45 synthetic boundary tests; no verifier, authority, automatic renewal, production trust or updater is enabled |

The native V3 acceptance script performs installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs while the selected runtime executables have outbound
firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN.

## Requirements still open

The following cannot be labelled DONE by passing the current core tests:

- A signed ResearchLibrary.exe and standard signed installer/uninstall/repair,
  owner-configured publisher trust and a complete private release review.
- Compatible private entitlement authority, account-optional perpetual policies,
  signed unattended refresh and offline renewal, and signed update A-to-B,
  interruption/recovery and key-rotation acceptance.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Actual installed dordiai-voices / SAPI4 compatibility, MusicGen/ACE-Step model inference,
  the remaining approved Balabolka-equivalent workflow checklist, synchronized
  reader interoperability and Braille workflows.
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
Its Java process joins the existing outbound firewall scope during conformance
acceptance; the native publication probe uses the actual installed package and a
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

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
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
