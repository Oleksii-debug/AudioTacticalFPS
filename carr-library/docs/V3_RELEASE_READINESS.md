# Resonance V3 implementation and owner-test delivery status

This document describes the implemented library, reading and audio preview of
Resonance by Sceneria Innovative Technologies. It does not close the complete
platform specification or the finalization addendum. An owner-test application
bundle and a signed distribution-cleared release have separate acceptance
requirements. Existing package, launch filenames and data locations remain
unchanged until a verified migration; visible application titles use Resonance.

## Implemented and exercised

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped originals, exact quotations, academic references, managed Fast/Best OCR profiles and installed model selection | Actual SQLite and OCR tests; native installed OCR manager runs on Windows |
| Reading and voices | Source-preserving reading derivatives with explicit source lineage, pronunciation edits, local voice discovery, playback and SAPI5 native COM worker | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; saved/recovered reading copies retain exact source spans; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats | Real FFmpeg production and Windows restart; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Documents and citations | Isolated bounded conversion, local citeproc-js styles/locales, editable DOCX with citation provenance, opt-in DOI previews | Actual local processor, generated documents and integrity tests; no live Word add-in |
| Recovery | Manifest-bound composite backup v5 includes pending import bytes, identities, settings and both accepted project/state formats; historical v1–v4 formats remain readable | Real backup/restore and resumed import tests; restored external engines/jobs require review; externally exported audio and OS credential secrets are separate |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Actual Windows key injection and progress/result regressions; human NVDA and listening review remain required; the recording quiet contract does not implement microphone capture |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references and per-request consent/budget | Local and synthetic provider tests; no paid provider call was made during implementation |
| Translation | Optional Google Basic v2 selected-text derivatives, explicit supported-language query, consent, chunk receipts/resume and new exports | Synthetic transport with real gateway/ledger/reading objects; live API, translation quality, Advanced v3 and local model inference are not qualified |
| Supplied transcription | Isolated reviewed worker protocol on shared jobs/progress, bound local model inventory, six validated exports, separate edit revisions, cancellation/resume and restored-job review | Actual Windows native controls and synthetic worker subprocesses pass; the supplied original has a separate private repair adapter; real Whisper acoustic inference remains unqualified |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths and sealed runtime files | Source/packaging tests; checksum integrity is not a publisher signature |
| Private integration boundary | Versioned entitlement/update protocols, optional worker-based host lifecycle, one-use approval and complete backup before installation; user-data operations remain available | Synthetic provider and real-host/backup gates pass; no production verifier, authority, trust root or operating-system installer is enabled by default |

The native V3 acceptance script performs installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs while the selected runtime executables have outbound
firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN.

## Current completion pass

The authoritative specification was rechecked against canonical source
`3beebd5aca900b8bf2e86498ae79db4015c96705`, with the current finalization
specification at blob `db8adbdc483a03c262885928c7ed214ca70bbaa7`. The next candidate retains the
full requested platform scope. It adds native A/B and sleep controls, retained
document/range translation, explicit owner voice profiles, accessible local
audio-runtime setup and an optional provider-injected delivery lifecycle.
Those implementations are **INTEGRATED source**. Executed portable evidence is
recorded per change; the latest adopted backup, publication and preparation
safeguards passed their composed portable gates. **Exact final Windows execution is
pending** until the complete successor result is recorded. Private engine/authority source,
weights, voice recordings, signing material and actual service policy are not
part of public source.

Earlier predecessor `90496fad8f397dbd007d2a9531709be82c9d9f2b` passed its native
launcher, bibliography and dedicated transcription workflows and all 221
required integrated source suites. Its aggregate Windows and V3 workflows
still failed; it is not an accepted delivery wheel. The concrete failures and
the successor corrections are recorded in COMPOSITE_V3_INTEGRATION.md.

Later predecessor `1e2eebc41589a9fe462f10bc3a2359de05e536be` passed all
241 required source suites and the dedicated transcription, bibliography and
native-launcher workflows; its aggregate Windows/V3 jobs still failed. At
`77093c8fc1d124bc6bb52c93edce69214098adde`, the targeted Windows correction
job passed actual descendant-process ownership (27 cases), interpreter
ownership (6), import publication boundaries (45), the native timer lifecycle
probe (4 assertions) and the unchanged actual MCI reader probe (108 assertions).
The separate recovery/voice checkpoint at `07e1e7fcdae89d27b6c8a8a1cd62a95e66e0c363`
passed 80 source and 80 installed cases. None of these targeted results is a
substitute for the final complete Windows aggregate. See
[canonical reconciliation](CANONICAL_RECONCILIATION_2026-11-27.md) for later
composition work and exact pending states.

At `d0a4a30a97c75ec90f545b5213776716dbb9badb`, all 19 source suites and
477 installed cases passed without skips or expected failures. Native reader
keyboard dispatch passed 34 source and 35 installed assertions. Its native
Save All cancellation checks failed because the assertion preceded dispatch
of the queued key event. The corrected probe subsequently passed at ee4f293a,
and the complete compatibility checkpoint 16a18b2aad8faccdeae30aa27a3e32b434a9c0f0
passed all 36 source suites, 1026 installed cases and six native lanes without
failure or skipped contracts. The subsequent b2c171 voice-preview checkpoint
passed 37 source suites, 1043 installed cases and seven native lanes, including
44 native productivity assertions. Later resource-admission changes must still
pass on the enclosing combined source. The current candidate also composes asynchronous captured-destination
export, inert legacy voice-helper recovery and typed transcription plans through
the existing worker service. Its new 24-case transcription-plan suite and the existing
bridge/assistant/workflow/recovery suites passed on portable Python 3.12;
this is source evidence, not final Windows or acoustic qualification.

The canonical transfer producer refuses audio-bearing bundles. The owner
application delivery therefore excludes that runtime and offers an explicit
native local setup workflow for the exact reviewed package. The same package
can be used for local execution and acceptance without being distributed as
part of an application archive. This preserves the distinction between runtime
qualification and permission to redistribute each component.

## Additions awaiting the composite execution gate

The integrated candidate contains the following source changes. Portable
follow-up gates now cover the export, storage migration, publication recovery
and reader/publisher repairs; exact results and the three corrected DOCX cases
are recorded in COMPOSITE_V3_INTEGRATION.md. The predecessor passed native
launcher and dedicated transcription checks but failed aggregate Windows
gates. Only the complete successor Windows result can qualify its application
wheel for delivery.

| Area | Prepared implementation | Remaining acceptance |
| --- | --- | --- |
| Exact academic acquisition | OpenAlex DOI and checksum-valid Open Library ISBN lookup, independent provider consent, immutable scoped previews and native review | Composite source/native tests; live provider availability separately |
| Synchronized publishing | One measured EPUB publisher for scene and primary Director production journals, complete artifact/source receipts, restored-publication review | Actual FFmpeg output plus pinned EPUBCheck 5.3.0 on the installed application |
| Synchronized reading | Bounded verified-publication reader, chapter/utterance navigation and seek, explicit playback through existing FFmpeg/WavePlayer | Composite reader/controller/native tests; actual audio-device listening and manual NVDA separately |
| Workflow productivity | Palette using current menu/shortcut registry; named typed plans and reuse with fresh scope/preview; allowlisted diagnostics preview and exact export | Composite source/native tests; human accessibility acceptance separately |
| Export responsiveness | Bibliography, academic references, import reports and keyboard preferences capture destination bytes on existing background jobs, then confirm on the UI thread | Cancellation, stale scope, replacement race and native regressions |
| Direct-import recovery | Canonical publication receipts and schema V6 request registrations attest the same import transaction; completed DOCX/EPUB/extended imports validate sealed revisions; interrupted direct jobs reuse the existing isolated verifier | Real-child backup/restore/resume, competing-publication and cancellation regressions; latest canonical installed recovery gate retained |

The synchronized reader accepts the application's verified publication graph,
not arbitrary EPUBs. Its existing PCM player requires RIFF32-compatible audio;
the reader rejects an over-limit decoded frame count before conversion and
checks the actual resulting WAV. The long Studio render capability does not
imply that every full-length result fits this player. DAISY/Braille and generic
third-party EPUB interoperability remain separate requirements.

## Requirements still open

The following cannot be labelled DONE by passing the current core tests:

- Remaining implementation in the finite reader checklist: RW-09 per-reader
  output-device selection, RW-12 complete native find/replace, RW-13 the
  speech-reader pronunciation manager, RW-15 spell checking, and RW-25 the
  remaining presentation/tabs workflows. These are code/workflow gaps rather
  than claims that owner input alone would complete them; see
  [the bounded reader inventory](READING_WORKFLOW_ACCEPTANCE.md).
- Professional microphone/track editing, the remaining shared cross-engine
  resource-admission work and qualification at the specified platform scale.
  The implemented production disk allowance and bounded traversal do not
  constitute a complete platform resource broker.

- A signed ResearchLibrary.exe and standard signed installer/uninstall/repair,
  owner-configured publisher trust and a complete private release review.
- Compatible private entitlement authority, account-optional perpetual policies,
  signed unattended refresh and offline renewal, and signed update A-to-B,
  interruption/recovery and key-rotation acceptance.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Actual installed dordiai/SAPI4 compatibility, MusicGen/ACE-Step model inference,
  the remaining approved Balabolka-equivalent workflow checklist, independently
  verified synchronized publishing/reading, DAISY and Braille interoperability.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The supplied Whisper Transcribe V4.3 implementation has been received and
reviewed; transcription is part of the approved platform scope. The public
bridge, shared job host, native controls, output validation and separate edit
revisions are integrated. A private adapter preserves the original supplied
Worker and corrects its coordination, cache and publication boundary. Its tests
load that unchanged Worker in an actual isolated process with a deliberately
synthetic model API. Neither those tests nor the native controls qualify real
Whisper inference, CUDA, acoustic quality or manual NVDA. The reviewed real
model/runtime environment remains a separate requirement; no competing ASR
stack or unverified runtime is bundled. See
[the source review](TRANSCRIPTION_INTEGRATION_REVIEW.md).

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
