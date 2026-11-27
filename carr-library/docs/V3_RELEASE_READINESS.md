# Resonance V3 implementation and owner-test delivery status

This document describes the implemented library, reading and audio preview of
Resonance by Sceneria Innovative Technologies. It does not close the complete
platform specification or the finalization addendum. An owner-test application
bundle and a signed distribution-cleared release have separate acceptance
requirements. Existing package, launch filenames and data locations remain
unchanged until a verified migration; visible application titles use Resonance.

## Implemented source and evidence boundaries

The table separates implemented behavior from model, hardware and release
qualification. Exact combined commit/tree identities and actual acceptance
results are recorded in [the integration review](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/169).
Component tests do not qualify a later assembled tree. Automated native Windows
execution and manual Windows 11/NVDA review remain separate evidence.

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped originals, exact quotations, academic references, managed Fast/Best OCR profiles and installed model selection; isolated text extraction from the canonical extended document formats | Actual SQLite and OCR tests; native installed OCR manager runs on Windows |
| Research search | Explicit Boolean/NEAR query mode, keyboard query builder, scoped multi-library search and retained reading positions | Bounded source-authenticated queries and backup round trips; results preserve exact source passages |
| Reading and voices | Source-preserving reading derivatives, pronunciation edits, local voice discovery, playback, canonical dordiai-voices connection and SAPI5 native COM worker | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, retained clips, trim/fade/gain/pan editing and verified resume/cache | Real FFmpeg production and retained-input recovery tests; CPU/RAM/disk admission is cooperative. 1/3/10-hour streaming fixtures are not full-length neural inference |
| Recording | Explicit microphone selection, bounded PCM capture, separate Stop/save and Cancel/discard, retained originals and recording receipts | Synthetic capture uses actual FFmpeg; device contention and pending native cleanup retain leases and staging. Physical microphones, listening quality and real-time monitoring remain unqualified |
| EPUB publication | EPUB 3 audio overlays from measured line or clip intervals, original script and publication receipts, exact-byte verification | Three real synthetic-tone/FFmpeg publications passed pinned EPUBCheck 5.3.0 and an invalid publication was rejected; reader interoperability, sentence alignment, DAISY, Braille and manual NVDA remain separate |
| Documents and citations | Isolated bounded conversion, reviewed BibTeX/RIS/CSL import, the canonical QuickJS citeproc-js processor/styles, editable DOCX with checked citation provenance, destination approval and the shared opt-in DOI acquisition service | Actual local processor, generated documents, parser limits and integrity tests; no live Word add-in |
| Recovery | One common typed format-4 writer includes canonical settings, drafts, reading state and explicit media/ASR/translation/resource producers; historical exact state envelopes 3–6 retain bounded compatibility validation | Real deletion/restore/rerender tests retain source and SQL receipt bytes. Restored jobs require a new output directory; old external paths require review. Unreadable state or pending roots reject the backup; OS credential secrets remain external |
| Transcription | One existing job service for supplied-worker profiles, retained audio/video files or folders, cancellation/resume, verified results, editable derivatives and text/subtitle export | Synthetic child-process and native control checks; private V5 wrapper interoperability uses a controlled backend and does not qualify neural inference or recording quality |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Native probes and callback regressions cover normal Close and direct owner/parent destruction; the exact new native probes require their own Windows run. Human NVDA and listening review remain required |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, guided fallback and explicit local-model proposals through the existing validated plan adapter; execution requires a separate action | Real loopback HTTP and cancellation/destruction tests with synthetic responses; no automatic server/model startup, arbitrary model commands or model-quality claim |
| Translation | Basic v2 and Advanced v3 bounded plain text, whole retained reading sources or position ranges; exact account/model/glossary consent, chunk receipts, resume and source-bound reading derivatives | Synthetic HTTPS with the real gateway, independent decoded-text ledger and source checks. Advanced uses an explicitly supplied expiring OAuth token. Live Google access, linguistic quality, formatted documents, document batches and local inference remain unqualified or unimplemented as stated below |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, sealed runtime files and a native Windows executable with real installation/restart checks | MSVC/Zig launcher and installed bundle evidence are tied to exact source and executable hashes; publisher signatures, a standard signed installer and distribution clearance remain separate |
| Private integration boundary | Optional Ed25519 entitlement verification, protected monotonic anchors, crash recovery, selected-repository TUF verification, verified package acquisition and accessible status controls; private operation and user-data actions remain available | Component selection: 175 passed, with the Windows credential-vault case explicitly not run locally. Synthetic trust roots are test-only; exact assembled Windows acceptance, production authority and transactional activation remain outstanding |

The native V3 acceptance script performs installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs while the selected runtime executables have outbound
firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN.

## Requirements still open

The following cannot be labelled DONE by passing the current core tests:

- A signed ResearchLibrary.exe and standard signed installer/uninstall/repair,
  owner-configured publisher trust and a complete private release review.
- Owner-configured private entitlement authority and account-optional perpetual
  policies; exact assembled Windows verification of the implemented client;
  production renewal acceptance and transactional signed update A-to-B
  activation, interruption/recovery and release key-rotation acceptance. Package
  verification/download exists; automatic installation/activation remains a
  software gap, not merely a missing signing key.
- Owner-supplied cloning runtime and an integrated consent/enrollment/versioned
  clone-profile/revocation workflow, authorized reference recordings and
  independent multilingual inference/listening. Registering a synthesis helper
  does not implement enrollment or qualify voice likeness.
- Actual installed dordiai/SAPI4 compatibility, MusicGen/ACE-Step model inference,
  the remaining approved Balabolka-equivalent workflow checklist, real EPUB
  reader interoperability, DAISY/BRF/Liblouis and acoustic sentence alignment.
- Physical microphone/device qualification, real-time input monitoring and the
  remaining professional multitrack workflow. Bounded capture, retained clip
  editing and offline scene production do not establish those capabilities.
- Actual selected local LLM inference and model quality, an approved local
  translator, formatted-document and multi-document translation, canonical
  language mapping and integrated bilingual narration. Implemented Basic/v3
  text protocols and synthetic loopback tests are distinct from these requirements.
- Shared admission for remaining heavy import/OCR paths. Existing Tasks exposes
  their saved state but does not make those workers participate in the
  Studio/transcription/media resource pool. Declared budgets are not measured
  peak memory or operating-system memory enforcement.
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
