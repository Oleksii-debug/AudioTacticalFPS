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
| Research search | Explicit Boolean/NEAR query mode, keyboard query builder, scoped multi-library search and retained reading positions | Bounded source-authenticated queries and backup round trips; results preserve exact source passages |
| Reading and voices | Source-preserving reading derivatives, pronunciation edits, local voice discovery, playback and SAPI5 native COM worker | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats | Real FFmpeg production and Windows restart; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Documents and citations | Isolated bounded conversion, reviewed BibTeX/RIS/CSL import, local citeproc-js styles/locales, editable DOCX with citation provenance, destination approval and opt-in DOI previews | Actual local processor, generated documents, parser limits and integrity tests; no live Word add-in |
| Recovery | Manifest-bound backup v5 includes pending import bytes, identities, settings, project state and reading positions; older supported backups remain readable | Real backup/restore and resumed import tests; exact publication reconciliation preserves completed imports after late cancellation; externally exported audio and OS credential secrets are separate |
| Transcription | One existing job service for supplied-worker profiles, retained audio/video files or folders, cancellation/resume, verified results, editable derivatives and text/subtitle export | Synthetic child-process and native control checks; private V5 wrapper interoperability uses a controlled backend and does not qualify neural inference or recording quality |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Actual Windows key injection and progress/result regressions; human NVDA and listening review remain required; the recording quiet contract does not implement microphone capture |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references and per-request consent/budget | Local and synthetic provider tests; no paid provider call was made during implementation |
| Translation | Optional Google Basic v2 selected-text derivatives, explicit supported-language query, consent, chunk receipts/resume and new exports | Synthetic transport with real gateway/ledger/reading objects; live API, translation quality, Advanced v3 and local model inference are not qualified |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths and sealed runtime files | Source/packaging tests; checksum integrity is not a publisher signature |
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
- Actual installed dordiai/SAPI4 compatibility, MusicGen/ACE-Step model inference,
  the remaining approved Balabolka-equivalent workflow checklist and synchronized
  publishing/Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The supplied Whisper projects, including the owner's corrected V5 worker, have
been reviewed against the application's existing transcription service. Each
supported protocol has an explicit profile; configuring one does not make other
worker versions compatible. Original media, result receipts and edits stay in the
shared library lifecycle, while private worker/runtime/model files remain local
external dependencies. Actual neural inference, mixed-language quality and human
Windows 11/NVDA use still require separate qualification. See
[transcription](TRANSCRIPTION.md) and the historical
[source review](TRANSCRIPTION_INTEGRATION_REVIEW.md).

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
