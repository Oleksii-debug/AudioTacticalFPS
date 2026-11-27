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
| Library and OCR | Scoped originals, exact quotations, advanced Boolean/NEAR search, BibTeX and DOI/ISBN workflows, additional document formats, managed Fast/Best OCR | Actual SQLite/parser/import/recovery tests; externally downloaded PDFs bind import and resume to their exact verified receipt; native acceptance is recorded per build |
| Reading and voices | Source-preserving reading derivatives, pronunciation edits, local voice discovery, playback, SAPI5 native COM worker and explicitly configured Dordiai adapter | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles/scenes, versioned named clips with trim/placement/gain/pan/fades, microphone capture, pauses, procedural score, DSP, aligned stems, subtitles and verified resume | Actual FFmpeg PCM, stop/cancel, clip/recording composition and destructive recovery; physical microphone, listening and native evidence remain per build; streaming fixtures do not establish full-length neural inference |
| Documents and citations | Isolated conversion, local citeproc-js, retained citation DOCX regeneration, exact custom style, guarded bibliography exports and opt-in DOI previews | Actual Node/DOCX regeneration, immutable source snapshot, final content recheck and no-clobber tests; native regeneration is separately gated; no live Word add-in |
| Synchronized publishing | EPUB3 chapters, navigation, MP3 audio and line-level SMIL overlays from an exact completed Studio job; immutable source provenance and verified publication receipts | Actual FFmpeg/package/no-clobber/recovery tests; official EPUBCheck and native reader acceptance are recorded separately; no word alignment, DAISY, Braille or WCAG conformance claim |
| Recovery | Manifest-bound backup v4, conditional v5 for reading state, pending import bytes, bookmarks/resume, Studio input assets and transcription state | Real deletion/restore/resume tests; external engine approvals require explicit rebind; external exports and OS credential secrets remain separate |
| Transcription | Supplied reviewed Whisper worker through one service, native dialog and CLI; retained source, four output formats, revision editing and explicit resume | Actual offline tiny.en CPU inference and destructive recovery; one synthetic English fixture only; Windows/NVDA, other models, diarization and word alignment remain separate |
| Keyboard and progress | Configurable menu keys, named controls, measured or indeterminate progress, Off/Beep/Speech/Beep+Speech, explicit local voice and playback suppression | Real private PCM synthesis and bounded feedback-worker tests; terminal notices wait for obsolete speech to stop; human NVDA listening remains required |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, explicit local-model proposal through the existing gateway, protected credential references and consent/budget | Actual local HTTP protocol and CLI tests use explicitly synthetic responses; proposed plans cannot execute without separate review; installed LLM quality and paid providers remain separate |
| Translation | Optional Google Basic v2 and Advanced v3 selected-text derivatives, directional language inventory, explicit OAuth expiry/renewal, reviewed glossary/model binding, consent/budget, chunk receipts/resume and new exports | Actual gateway/ledger/jobs/UI handler/CLI with synthetic REST responses; live Google IAM/billing, translation quality, whole-document fidelity and local model inference remain separate |
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
  the remaining approved Balabolka-equivalent workflow checklist, third-party
  synchronized reader acceptance, DAISY and Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The supplied Whisper Transcribe V4.3 implementation is corrected and integrated.
The exact private worker `5379b889…` has executed through the service and CLI with
the pinned tiny.en model, including genuine interruption, verified reuse and
same-job recovery after deletion of the original library. This establishes a
bounded English CPU compatibility result, not complete language or audio-quality
qualification. The worker/runtime/model remain explicit external selections and
are not published or automatically bundled. See the
[integration and execution review](TRANSCRIPTION_INTEGRATION_REVIEW.md).

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
