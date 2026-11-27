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
| Library and OCR | Scoped originals, exact quotations, academic references, Boolean AND/OR/NOT and bounded NEAR source search, managed Fast/Best OCR profiles and installed model selection | Actual SQLite and OCR tests; native installed OCR manager runs on Windows |
| Reading and voices | Source-preserving reading derivatives, pronunciation edits, local voice discovery, playback and SAPI5 native COM worker | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats | Real FFmpeg production and Windows restart; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Documents and citations | TXT/PDF/DOCX/EPUB and supported text subsets of HTML/HTM/XHTML, Markdown, EML, FB2/FB3, ODT/ODS/ODP, PPTX/XLSX; local BibTeX/RIS/CSL-JSON preview, seven bundled CSL styles and editable DOCX with citation provenance | Existing 64 MiB source limit and parser budgets apply; actual local processor, document and integrity tests; no live Word add-in or automatic approval of extracted quotations |
| Recovery | Manifest-bound backup v4 includes pending import and transcription inputs, transcript revisions, settings and project state; restore revokes external ASR execution trust | Portable-path validation and copied-content revalidation, real backup/restore and resumed-job tests; externally exported audio, external engines/weights and OS credential secrets are separate |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Native key-injection probes and progress/result regressions require acceptance for the exact candidate; manual NVDA is NOT_RUN and listening review remains required; the recording quiet contract does not implement microphone capture |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references and per-request consent/budget; explicit local model proposal, review and separate execution in native UI/CLI | Proposal, handler and actual-loopback tests exercise the implemented contract; synthetic model responses establish protocol and permission handling, not model planning quality. Donor native component passed; integrated installed native gate remains required. No model launch/download or paid provider call occurred |
| Translation | Optional Google Basic v2 and Advanced v3 NMT selected-text derivatives; explicit supported-language query, consent, chunk receipts/resume, existing glossary selection and new exports; matched reviewed CLI | Source regressions use synthetic transport and real gateway/ledger/reading objects; donor Advanced native component passed, integrated native gate remains required. V3 uses explicitly entered expiring OAuth tokens, without automatic ADC discovery/refresh; live API, billing, translation quality, document/batch translation and local model inference remain unqualified or unimplemented as documented |
| Transcription | Existing local Python/worker/model inspection and explicit trust, isolated supplied-worker protocol, retained audio, durable jobs, pause/cancel/resume, transcript edits and TXT/SRT/VTT/JSON | Real subprocess/file-protocol tests use a synthetic peer; the unchanged corrected private worker also completed the adapter protocol with a synthetic installed recognition backend. No model download, real acoustic accuracy or language qualification is inferred |
| Shared operations | Recovery view over canonical import/assistant/translation/studio/ASR checkpoints, explicit resource policy and native crash-released admission | Actual multiprocess contention/crash checks; cooperative declarations are not an OS sandbox or measured CPU/GPU/RAM usage |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths, sealed runtime files and direct ZIP assembly with verified OCR source-companion reuse | Source/packaging tests; checksum integrity is not a publisher signature |
| Private integration boundary | Separate versioned entitlement/update protocols and preservation of user-data operations | 45 synthetic boundary tests; no verifier, authority, automatic renewal, production trust or updater is enabled |

See the [Ukrainian quick start](START_V3_UK.txt), [document-format boundaries](DOCUMENT_FORMATS_V3.md),
[advanced search syntax](ACADEMIC_SEARCH.md), [bibliography workflow](V3_BIBLIOGRAPHY.md)
and [keyboard profiles](KEYBOARD_SHORTCUTS.md) for the implemented user paths.
The complete private installation needs both Application and OCR Sources ZIPs
extracted into their shared program folder; `ocr-corresponding-source.tar`
remains intact for verification before `Install.cmd` runs.

Progress speech now uses the explicitly selected, installed local voice through
the existing speech adapter. Choose **Use voice for progress** in the Voice
reader or Studio, then **Options → Progress feedback settings**. This is a
session-only choice, cleared when the library data folder changes; enabling a
mode does not acquire a voice. **Ctrl+Shift+Q** queries the current operation.
Default feedback is Off, optional playback quieting remains available, and
progress sounds never become project audio. These software paths do not establish
manual screen-reader or acoustic acceptance.

Replacing an existing export requires approval captured before confirmation and
rendering. The file's identity and bounded full-content fingerprint are rechecked
before publication. Existing files over 32 MiB, or files that cannot be verified
within the checking budget, require a new filename. This is a replacement-check
limit, not a new-export size limit or a guaranteed maximum filesystem latency.
A completed save with a staging-cleanup warning remains a completed save.

The Windows acceptance workflows exercise installed SAPI synthesis, scene
production, output verification, restart, local CSL/DOCX, isolated text conversion
and named native dialogs. Dedicated runtime stages use outbound firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN.

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
  the remaining reading/synthesis workflows in the [finite checklist](speech_runtime.md#requested-reading-and-synthesis-workflow-checklist),
  and synchronized publishing/Braille workflows. Completing the checklist does
  not complete its explicitly pending product functions.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The supplied Whisper Transcribe V4.3 implementation has been received, reviewed
and privately corrected. The platform now invokes its versioned file protocol
through the shared durable-state, bounded-process and resource services. The
native workstation and CLI select the owner's existing local installation; no
worker source, weights or virtual environment is copied into the public package.
Real acoustic inference with the owner's model, Windows 11/NVDA listening and
an exact redistributable ASR runtime/rights profile remain unqualified. See
[the source review](TRANSCRIPTION_INTEGRATION_REVIEW.md) and
[the integration contract](transcription_runtime.md).

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
