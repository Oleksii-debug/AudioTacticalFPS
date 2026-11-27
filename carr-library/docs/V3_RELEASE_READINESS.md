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
| Reading and voices | Source-preserving reading derivatives, pronunciation edits, local voice discovery, playback and SAPI5 native COM worker | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats | Real FFmpeg production and Windows restart; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Supplied transcription integration | Shared UI/CLI jobs, recursive folder intake, explicit receipts/V5 profiles, retained originals, current-run results, word timestamps, cancellation/resume and separate edits | Actual synthetic subprocess/native controls; one real supplied-worker Linux service-to-model execution is recorded separately from V5 protocol and Windows acoustic acceptance |
| Documents and citations | Isolated bounded conversion, local citeproc-js styles/locales, editable DOCX with citation provenance, opt-in DOI previews | Actual local processor, generated documents and integrity tests; no live Word add-in |
| Recovery | Manifest-bound backup v4, or v5 when reading state is included, retains pending import bytes, identities, settings, project state, positions and bookmarks | Real restore/resume tests, stable-content rereads and portable path checks; externally exported audio and OS credential secrets are separate |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Actual Windows key injection and progress/result regressions; human NVDA and listening review remain required; the recording quiet contract does not implement microphone capture |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references and per-request consent/budget | Local and synthetic provider tests; no paid provider call was made during implementation |
| Translation | Optional Google Basic v2 selected-text derivatives, explicit supported-language query, consent, chunk receipts/resume and new exports | Synthetic transport with real gateway/ledger/reading objects; live API, translation quality, Advanced v3 and local model inference are not qualified |
| Local transcription | Shared LibraryApp service, native job list and configuration, file/folder intake, stop/resume, verified results, separate edits and new-folder export; receipts-v1 and V5 manifest profiles | Actual child-process protocol and backup/restore tests use synthetic recognition results. The installed native probe checks module origin, exact source hashes and named controls; exact-build execution evidence and acoustic qualification remain separate |
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
- Actual installed dordiai/SAPI4 compatibility, ACE-Step model inference,
  the remaining approved Balabolka-equivalent workflow checklist and synchronized
  publishing/Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The supplied-worker transcription integration uses one shared application service,
workspace authorization and state/transcription job store. Users explicitly select
the compatible local interpreter, worker, model directory and protocol. Existing
receipts-v1 jobs retain their profile when a V5 worker is configured. Stop preserves
completed files for verified resume; an interrupted file starts again. Recognition
receipts and original media remain separate from user edit revisions and exports.
No ASR worker, model or acoustic qualification is bundled or inferred from synthetic
protocol tests. See [the transcription guide](TRANSCRIPTION.md) and
[the original source review](TRANSCRIPTION_INTEGRATION_REVIEW.md).

The repaired private V4.3 worker completed real Linux CPU inference
through the normal application service and isolated child bootstrap on the exact
source recorded in [the actual qualification](TRANSCRIPTION_ACTUAL_QUALIFICATION_20261127.md).
One short English sample does not establish Windows acoustic operation, V5
acoustic quality, Ukrainian or broad multilingual qualification. The public V5
acceptance uses controlled backends and remains protocol evidence. No competing
recognizer or automatic model download is bundled. See [the workflow](TRANSCRIPTION.md)
and [the source review](TRANSCRIPTION_INTEGRATION_REVIEW.md).

The canonical MusicGen worker passed actual two-second CPU generation on both
Linux and Windows. The complete runtime/model inventories remained unchanged;
Windows executable egress was blocked independently before and after inference.
[Exact source and runtime evidence](MUSICGEN_CANONICAL_ACCEPTANCE_20261127.md)
remains bound to its recorded revisions. Listening quality, unconfigured cold
desktop startup, hours of neural generation and broader redistribution remain
separate requirements. Model weights are not part of the core delivery.

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
