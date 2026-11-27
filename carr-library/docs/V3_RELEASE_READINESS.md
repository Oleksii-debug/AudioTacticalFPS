# Resonance V3 implementation and owner-test delivery status

This document describes the implemented library, reading and audio preview of
Resonance by Sceneria Innovative Technologies. It does not close the complete
platform specification or the finalization addendum. An owner-test application
bundle and a signed distribution-cleared release have separate acceptance
requirements. Existing package, launch filenames and data locations remain
unchanged until a verified migration; visible application titles use Resonance.

**Final composed Windows acceptance remains pending.** The authoritative
[requirement snapshot of 27 November 2026](RESONANCE_REQUIREMENT_STATUS_20261127.md)
includes local composition `d32d0436e7924f8a37bbea8689d8aef28d22b837` and separates source
integration, exact component execution, runtime qualification and distribution.
The evidence below does not promote those independent states to a final PASS.

## Implemented and exercised

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped originals, exact quotations, academic references, managed Fast/Best OCR profiles and installed model selection | Actual SQLite and OCR tests; native installed OCR manager runs on Windows |
| Reading and voices | Source-preserving reading derivatives, reviewed literal/regex Find/Replace, local voice discovery, playback, skip/percentage/A–B/sleep controls and SAPI5 native COM worker | Snapshot E8/E10 adds strict reader, native-position and lifetime regressions; earlier real Windows SAPI PCM and local Flite evidence stays separate. Final new native controls, output devices and other engine assets require qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats; explicit microphone capture and retained clip trim/placement/gain/pan/fades | Actual synthetic FFmpeg recording, mix and cleanup tests are recorded in snapshot E3/E4; physical microphone, monitoring, listening and final Windows acceptance remain open. Historical 1/3/10-hour fixtures do not establish full-length neural inference |
| Synchronized publishing | Source-bound EPUB3/SMIL from measured narration or retained audio clips, verified MP3 and new-file publication | Snapshot E5/E6 records actual EPUBCheck 5.3.0 on three generated publications and publication handlers; external readers, DAISY/Braille and human accessibility remain unqualified |
| Documents and citations | Isolated bounded conversion, local citeproc-js styles/locales, editable DOCX with citation provenance, opt-in DOI previews | Actual local processor, generated documents and integrity tests; no live Word add-in |
| Academic acquisition | Exact DOI/ISBN lookup, five explicit metadata-provider choices, reviewed bibliography/book-metadata updates and separately approved OA PDF acquisition through the existing import service | Snapshot E11 exercises provider fixtures, consent, public-host/TLS boundaries, transfer budgets, retained-source authority and ambiguous-edition refusal. Existing DOI JSON export remains. Real provider behavior and final installed native acceptance remain separate |
| Recovery | Manifest-bound backups include pending import bytes, settings, projects and personal reading positions/bookmarks; exact V3/V4 catalogs migrate transactionally to V5 | Real backup/restore and resumed import tests; transaction-bound request/attempt receipts identify the first publisher. A stopped replay without a new receipt requires explicit resume. External audio and OS credential secrets remain separate |
| Keyboard and progress | Configurable menu keys, existing-registry command palette, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; operation-owned playback/recording quieting and settings backup | Existing native key/progress regressions and snapshot E3/E8/E10; complete composed Windows, human NVDA and recording contamination/listening review remain required |
| Tasks and resources | A shared saved-job view, scoped handoff to the original transcription window and cooperative CPU/RAM/GPU/disk admission around the existing Studio/ASR services | Snapshot E9/E10 includes actual process/lock contention, cleanup, post-wait authorization and retained-source checks. Saved counts do not certify outputs; declared budgets do not establish measured hardware capacity. Installed native qualification remains pending |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, optional enabled local model proposals through the existing review/executor boundary, local-only default, protected credential references and per-request consent/budget | Snapshot E7 identifies the integrated proposal adapter; an actual local model and planner quality require separate qualification. Provider protocol tests do not claim paid or live account execution |
| Translation | Optional Google Basic v2 and Advanced v3 for selected text or an authorized retained document/range; explicit OAuth profile, existing glossary, consent, source/ledger receipts, resume and new bilingual exports | Snapshot E1 records real service/CLI/handler objects with synthetic responses and owned loopback; live Google accounts, quality, local translation models and final native acceptance remain unqualified |
| Transcription | One durable application/CLI service for the supplied worker, explicit files or a flat folder, retained media, verified receipts, cancellation/resume, separate edit versions, exports and native Save all | Snapshot E2 covers real folder preparation/cancellation; synthetic worker/UI regressions and historical private Linux tiny-model evidence remain distinct. Final installed Windows neural inference and model/language quality remain unqualified |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths and sealed runtime files | Source/packaging tests; checksum integrity is not a publisher signature |
| Private integration boundary | Separate versioned entitlement/update protocols and preservation of user-data operations | 45 synthetic boundary tests; no verifier, authority, automatic renewal, production trust or updater is enabled |

The native V3 acceptance script performs installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs while the selected runtime executables have outbound
firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN. The
presence of that script and earlier component runs do not establish success of
the final composed source, installed wheel or either delivery profile.

## Package profiles

The library transfer profile contains Python, the local search model, OCR and
its corresponding source archive, the verified native launcher/runtime and the
local citation processor. It omits FFmpeg. Library, research, OCR, bibliography
and reading through installed Windows SAPI voices remain separate from Studio
mixing, music and media conversion that require an explicitly selected local
FFmpeg/FFprobe installation.

The existing transfer helper rejects both current FFmpeg payload layouts and
audio declarations until their exact corresponding-source/notices review is
complete. A private destination does not override that guard. The locally
assembled audio profile retains its full native audio acceptance. The library
transfer profile must separately pass fresh offline installation, native
launcher/reader/catalog, actual semantic/OCR/citation and absent-audio checks
before its own manifest and installed wheel are captured. Evidence for one
profile does not certify the other, and a captured wheel never substitutes for
success of the complete workflow at the exact source commit.

## Requirements still open

The following cannot be labelled DONE by passing the current core tests:

- A signed ResearchLibrary.exe and standard signed installer/uninstall/repair,
  owner-configured publisher trust and a complete private release review.
- Compatible private entitlement authority, account-optional perpetual policies,
  signed unattended refresh and offline renewal, and signed update A-to-B,
  interruption/recovery and key-rotation acceptance.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Actual owner-engine dordiai inference, unavailable SAPI4 compatibility,
  MusicGen/ACE-Step inference and remaining approved reader/editor workflows.
  The integrated voice bridge is not acoustic, language or cloning evidence.
- Physical microphone/device recovery, monitoring/meters, the complete clip/track
  editor and requested real-time effects. [Recording](MICROPHONE_RECORDING.md)
  and [typed clips](STUDIO_CLIP_EDITING.md) are integrated; their synthetic PCM
  tests do not establish those remaining device and professional workflows.
- External-reader/accessibility acceptance of [EPUB3/SMIL publishing](STUDIO_EPUB_PUBLISHING.md),
  DAISY interchange and BRF/Liblouis qualification.
- Local translation inference, batch documents/layout fidelity, live Basic/v3
  account acceptance and language quality. Integrated document/range translation,
  Advanced v3 and local planning adapters still require their actual providers,
  models and final native tests; credentials alone do not qualify output.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

This short list does not replace the snapshot's complete U/G/V3/ASR trace.
Its open format, editor, backup/encryption, corpus-performance, academic/sync,
remaining resource/operation coverage, plugin and security requirements remain open as well.

The supplied Whisper Transcribe V4.3 implementation has been received, reviewed
and connected through the existing shared jobs and native transcription window.
The compatible private repair has retained real Linux CPU tiny-model inference,
verified cache reuse and application-service execution evidence. Those results
do not establish Windows acoustic or language-quality acceptance. The recorded
qualified runtime and model weights are currently unavailable after cleanup,
so this application package does not include a ready-to-run neural engine pack.
Restore reviewed pinned inputs and qualify the actual final installed Windows
host before advancing that capability. No competing recognizer or automatic
model download is selected. See the [workflow](TRANSCRIPTION.md) and
[original source review](TRANSCRIPTION_INTEGRATION_REVIEW.md).

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
