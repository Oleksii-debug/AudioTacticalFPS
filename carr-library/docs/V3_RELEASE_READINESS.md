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
| Private integration boundary | Separate versioned entitlement/update protocols and preservation of user-data operations | 45 synthetic boundary tests; no verifier, authority, automatic renewal, production trust or updater is enabled |

The native V3 acceptance script performs installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs while the selected runtime executables have outbound
firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN.

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
