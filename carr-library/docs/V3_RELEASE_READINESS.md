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
| Recovery | Manifest-bound backup v4 includes pending import bytes, identities, settings and project state | Real backup/restore and resumed import tests; externally exported audio and OS credential secrets are separate |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Native key-injection probes and progress/result regressions require acceptance for the exact candidate; manual NVDA is NOT_RUN and listening review remains required; the recording quiet contract does not implement microphone capture |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references and per-request consent/budget | Local and synthetic provider tests; no paid provider call was made during implementation |
| Supplied transcription | Opt-in adapter to the exact owner-supplied V4.3 worker through existing typed assistant jobs; verified TXT/SRT/VTT/JSON, input/settings binding, cancellation and publication recovery | Actual supplied worker protocol exercised with an isolated synthetic SDK; real model inference, desktop configuration, Windows/NVDA and complete ASR migration remain pending |
| Translation | Optional Google Basic v2 selected-text derivatives, explicit supported-language query, consent, chunk receipts/resume and new exports | Synthetic transport with real gateway/ledger/reading objects; live API, translation quality, Advanced v3 and local model inference are not qualified |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths and sealed runtime files | Source/packaging tests; checksum integrity is not a publisher signature |
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
- Actual installed dordiai/SAPI4 compatibility, MusicGen/ACE-Step model inference,
  the remaining approved Balabolka-equivalent workflow checklist and synchronized
  publishing/Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The supplied Whisper Transcribe V4.3 implementation now has an opt-in backend
adapter using the existing assistant JobStore and bounded audio process runner.
The reviewed worker is corrected on a private staging copy; it is not bundled.
Three executions of that exact worker with a synthetic isolated SDK verified
selected Ukrainian exports, rejected silent language fallback and rejected a
required-SRT failure even when the worker claimed success. The primary integration
passed 117 tests covering the adapter, assistant and common-v4/schema recovery.
These are protocol and application tests, not acoustic recognition evidence.

Real pinned SDK/model installation, native Windows offline execution, accessible
desktop configuration and transcript editing, ASR source/output backup migration,
long-input resource measurements and independent audio quality remain open.
Diarization and word alignment are not implemented by this adapter. Existing GUI
constructors leave the engine unconfigured. See the [source review](TRANSCRIPTION_INTEGRATION_REVIEW.md)
and [exact adapter contract and limitations](SUPPLIED_TRANSCRIPTION_ADAPTER.md).

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
