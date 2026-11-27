# V3 implementation and private delivery status

This document describes implemented behavior. It does not close the complete
V3 specification or the finalization addendum. A private application bundle and
a signed production release have separate acceptance requirements.

## Implemented and exercised

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped originals, exact quotations, academic references, managed Fast/Best OCR profiles and installed model selection | Actual SQLite and OCR tests; native installed OCR manager runs on Windows |
| Reading and voices | Source-preserving reading derivatives, pronunciation edits, local voice discovery, playback and SAPI5 native COM worker | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats | Real FFmpeg production and Windows restart; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Documents and citations | Isolated bounded conversion, local citeproc-js styles/locales, editable DOCX with citation provenance, opt-in DOI previews | Actual local processor, generated documents and integrity tests; no live Word add-in |
| Recovery | Manifest-bound backup v4 includes completed library content, typed settings, Studio/assistant/translation state and explicitly selected generated PCM cache | Actual typed producer round trips and integrity tests; pending import inputs, externally exported audio and OS credential secrets require explicit separate coverage |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and playback suppression | Automated native/contract tests; human NVDA listening remains required; speech-mode progress is not implemented |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references and per-request consent/budget | Local and synthetic provider tests; no paid provider call was made during implementation |
| Translation | Optional Google Basic v2 selected-text derivatives, explicit supported-language query, consent, chunk receipts/resume and new exports | Synthetic transport with real gateway/ledger/reading objects; live API, translation quality, Advanced v3 and local model inference are not qualified |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths and sealed runtime files | Source/packaging tests; checksum integrity is not a publisher signature |
| Private integration boundary | Separate versioned entitlement/update protocols and preservation of user-data operations | 45 synthetic boundary tests; no verifier, authority, automatic renewal, production trust or updater is enabled |

The Windows acceptance workflows exercise installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs. The installed bundle gate applies outbound firewall rules
to the selected runtime executables. Evidence labels manual NVDA as NOT_RUN.

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
  full Balabolka parity and remaining synchronized publishing/Braille workflows.
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The owner-supplied V4.3 transcription implementation is a required part of full
platform completion under the current finalization scope. Its inspected source
identity is recorded in the transcription integration review. Runtime, protocol,
recovery, native Windows and model-quality acceptance remain separate gates.
A qualified library/audio preview may leave transcription explicitly pending;
that preview must not be described as the complete promised platform. The
existing supplied worker is the reuse target; no competing ASR stack is bundled.

The private build retains local operation and access to user-owned data. This
work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.
