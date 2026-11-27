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
| Audio studio | Roles, scenes, explicit voice/position, pauses, procedural score, DSP, chapters, stems, subtitles, verified resume/cache and formats; portable retained clips with trim, placement, gain, pan, fades, preview and direct saved-project rendering | Real FFmpeg production and PCM/source tests; clip restore requires a fresh output selection even at the same library path; latest installed Windows clip checks remain pending; 1/3/10-hour streaming fixtures are not equivalent to full-length neural inference |
| Microphone recording | Explicit DirectShow device selection, bounded capture, Stop/save, cancellation, verified originals, WAV export and backup/restore through the shared library | Actual FFmpeg with synthetic capture, owned-process and source/controller tests; physical devices, driver permissions, listening and the latest native Windows recording checks remain unqualified |
| Synchronized publishing | EPUB3 media overlays from verified completed Studio output, measured speech-line SMIL timing, provenance, no-clobber export and receipt recovery | Actual synthetic-audio source/export tests and EPUBCheck conformance fixtures; latest installed native publishing checks remain pending; external-reader, DAISY and Braille qualification is separate |
| Documents and citations | TXT/PDF/DOCX/EPUB and supported text subsets of HTML/HTM/XHTML, Markdown, EML, FB2/FB3, ODT/ODS/ODP, PPTX/XLSX; bounded conversion, reviewed BibTeX/RIS/CSL import, one pinned offline citeproc-js/QuickJS processor, editable DOCX with citation provenance and opt-in DOI previews | The 64 MiB source limit and parser budgets apply; source-integrity, generated-document and compatibility tests require the exact composed candidate; no live Word add-in or automatic quotation approval |
| Recovery | Manifest-bound common backup format 4 includes typed settings, pending import bytes and identities, project inputs, recording originals, transcription state and reading positions; historical supported formats 1–5 remain readable | Real backup/restore and resumed import tests; exact publication reconciliation preserves completed imports after late cancellation; externally exported audio and OS credential secrets are separate |
| Transcription | One existing job service for supplied-worker profiles, retained audio/video files or folders, cancellation/resume, verified results, editable derivatives and text/subtitle export | Synthetic child-process and native control checks; private V5 wrapper interoperability uses a controlled backend and does not qualify neural inference or recording quality |
| Keyboard and progress | Configurable menu keys, named native controls, measured or indeterminate progress, optional tones and explicitly selected local-voice notices in English/Ukrainian/Slovak; playback/recording quieting and settings backup | Actual Windows key injection and progress/result regressions for previously executed controls; new candidate controls need their exact installed run; human NVDA and listening review remain required |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, local-only default, protected credential references, per-request consent/budget and explicit local-server plan proposals through the existing validated executor | Actual loopback HTTP/CLI and synthetic native-control evidence; no local model weights or planning quality were qualified and no paid provider call was made during implementation |
| Translation | Optional Google Basic v2 and Advanced v3 plain-text derivatives from pasted text or retained source ranges; account-bound language discovery, expiring OAuth credentials, existing glossaries, consent, chunk receipts/resume and new exports | Synthetic transport with real gateway/ledger/reading objects; latest native Advanced completion checks remain pending; live API, linguistic quality, local inference, formatted-document fidelity and canonical-135 mapping are not qualified |
| Delivery preparation | Native ResearchLibrary.exe launcher, exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths and sealed runtime files; transferable archive excludes audio payloads | Source/packaging tests; checksum integrity is not a publisher signature |
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

Recording, portable clip editing, synchronized publishing, local-server planning
and Advanced translation are implemented candidate paths, with source and
synthetic-input verification. Their presence does not transfer a previous
Windows run to the final composition. The exact final installed source/wheel,
native workflows and retained-wheel checks must pass before delivery acceptance.

The canonical assistant also retains the opt-in V4.3 supplied-worker compatibility
adapter. Its controlled SDK exercises establish protocol behavior, not model
inference. The primary transcription UI uses explicit supported profiles and
retained inputs; neither path installs private engines or models automatically.
The optional assistant V4.3 path preserves its plans and checkpoints while its
source audio and exported transcripts remain external. The retained Transcription
workspace backs up its own imported inputs, verified results and edits.
See [the V4.3 adapter contract](SUPPLIED_TRANSCRIPTION_ADAPTER.md).

## Requirements still open

The following cannot be labelled DONE by passing the current core tests:

- A signed ResearchLibrary.exe and standard signed installer/uninstall/repair,
  owner-configured publisher trust and a complete private release review.
- Compatible private entitlement authority, account-optional perpetual policies,
  signed unattended refresh and offline renewal, and signed update A-to-B,
  interruption/recovery and key-rotation acceptance.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Actual installed dordiai/SAPI4 compatibility, MusicGen/ACE-Step model inference
  and the remaining approved Balabolka-equivalent workflow checklist.
- Physical microphone/driver acceptance, live monitoring, capture pause/resume
  and the remaining professional studio/device workflows. Basic recording,
  portable clip editing and offline rendering are implemented; they do not prove
  complete real-time multitrack operation or human accessibility/listening.
- External-reader and listening acceptance for synchronized EPUB, plus DAISY
  interchange and BRF/Braille translation/layout. EPUB media-overlay creation
  and conformance fixtures do not implement or qualify those separate formats.
- Actual local-model planning through the existing validated plan adapter, live
  Google Basic/Advanced qualification and a local translation engine. The local
  server and Advanced protocol paths are implemented; synthetic responses do
  not establish executed model inference or linguistic quality. Automatic ADC
  discovery/refresh, formatted-document and batch translation remain separate.
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
