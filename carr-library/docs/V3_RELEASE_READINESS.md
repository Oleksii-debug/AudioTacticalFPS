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
| Reading and voices | Source-preserving reading derivatives, portable edition/revision/asset lineage, explicit detached-draft recovery, pronunciation edits, local voice discovery, playback and SAPI5 native COM worker | Real source/backup/restart tests preserve original bytes and never infer identity from equal text; earlier Windows SAPI PCM and local Flite evidence remains specific to its tested build; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats | Real FFmpeg production and Windows restart; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Documents and citations | Isolated bounded conversion, local citeproc-js styles/locales, editable DOCX with citation provenance, opt-in DOI previews | Actual local processor, generated documents and integrity tests; no live Word add-in |
| Recovery | Manifest-bound backup v4 includes pending import bytes, identities, settings and project state; snapshot byte recheck, discoverable reader drafts and durable import-publication reconciliation | Actual same-size/mtime mutation, post-commit worker interruption and fresh-session/backup recovery tests; externally exported audio and OS credential secrets are separate |
| Exports | Destination state bound before one native confirmation; atomic create-only publication, scope checks through staging and truthful completed-save receipts | Real filesystem races preserve intervening files; persistent temporary-cleanup warnings do not turn a completed export into failure; final Windows head requires its own CI evidence |
| Supplied transcription | Existing Whisper worker protocol, strict source preparation, reviewed local runtime/model inventories, isolated processes, immutable input and atomic TXT/SRT/VTT/JSON receipts; native file/folder selection, jobs/status/pause/cancel/resume and separate transcript editing, plus CLI | Actual process/filesystem tests use synthetic recognition, including a separate execution of the supplied worker; real model inference, throughput, native runtime isolation and human NVDA remain unqualified |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and playback suppression | Automated native/contract tests; human NVDA listening remains required; speech-mode progress is not implemented |
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

The supplied Whisper Transcribe V4.3 implementation now has an integrated adapter
and an exercised process/publication boundary. This advances source integration;
real recognition quality and an approved runtime/model pack remain separate gates.
No private worker bytes, recordings, virtual environment or model weights are
bundled in the public repository. See [the updated source review](TRANSCRIPTION_INTEGRATION_REVIEW.md)
and [the exact adapter contract](TRANSCRIPTION_SUPPLIED_WORKER.md).

Current integration is tracked in [PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143).
Its Windows source and installed-package workflows select the new transcription,
reader recovery, import-publication and export regressions. Read the checks for
the exact final head: a predecessor's passing run does not qualify a later tree.

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
