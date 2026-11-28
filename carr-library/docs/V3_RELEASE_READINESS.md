# Resonance V3 implementation and qualification status

This document describes the implemented library, reading and audio preview of
Resonance by Sceneria Innovative Technologies. It does not close the complete
platform specification or the finalization addendum. An owner-test application
bundle and a signed distribution-cleared release have separate acceptance
requirements. Existing package, launch filenames and data locations remain
unchanged until a verified migration; visible application titles use Resonance.

## Implemented workflows and qualification boundaries

The table records behavior present in the integration source. It is not an
aggregate test verdict. Each executed result must identify its exact source
commit and tree, installed artifact, dependency or model pack, command and
actual outcome. The [acceptance gates](acceptance.md) and [current technical
specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md) retain the full promised scope.

| Area | Implemented behavior | Evidence boundary and current guide |
| --- | --- | --- |
| Library, OCR and search | Scoped immutable originals, source/revision-bound quotation offsets, bibliography, managed OCR selection, bounded scan/import continuation and paged Boolean/NEAR search. Unicode mapping preserves the original boundaries and cancellation checks. | Source and installed tests must use the exact candidate. The retained-corpus benchmark measures actual library operations; safety limits alone are not scale acceptance. See [search](ACADEMIC_SEARCH.md), [paging](SEARCH_PAGING.md) and [capacity](backup_capacity.md). |
| Reading and saved state | Derived reading copies, pronunciation edits, local voice discovery, source-aware presentation, persistent tabs/bookmarks/positions, voice preferences and saved playback queues. | SAPI, Flite and optional engines have separate execution and quality evidence. A registry entry does not qualify a voice, device or language. See [reading desk](READING_DESK.md), [tabs](READING_TABS.md), [presentation](READING_PRESENTATION.md) and [voice preferences](VOICE_PREFERENCES.md). |
| Voice packages and references | Existing owner-API integration for reviewed local packages: inspect, approve, activate a version, export, protected import/export, revoke and delete. Explicit OmniVoice reference creation uses an existing PCM recording and transcript. Selected reviewed OmniVoice and OpenVoice V2 worker paths extend the existing speech adapter. | These are implemented lifecycle and protocol paths. Actual private runtime/model identity, authorized references, supported language pairs, inference and listening remain separate. No private engine or model is distributed. See [voice package management](VOICE_PACKAGE_MANAGEMENT.md). |
| Audio studio and books | Existing scenes, roles, jobs and FFmpeg rendering; typed clip/role/scene/master effects; sample-based gain, mono-pan and width envelopes; split/cut/rejoin, copy/cut/paste, atomic crossfade of adjacent clips, named sample-aligned scene markers, undo/redo and saved revision browsing; explicit A–B repeat of the current rendered preview; book batches, chapters, stems, subtitles, verified resume/cache and measured outputs. | Each operator keeps its documented scope and timing restrictions. Synthetic long-file rendering does not establish hours of neural generation, arbitrary effect automation, device quality or complete live multitrack operation. See [envelopes](STUDIO_ENVELOPES.md), [clip history](STUDIO_CLIP_HISTORY.md) and [book batches](BOOK_BATCHES.md). |
| Microphone recording | Explicit DirectShow input, bounded capture, segment-based Pause/Resume, Stop/save, cancellation, retained originals, WAV export and shared backup/restore. Optional dry monitoring feeds the same capture into an explicitly selected Windows output. | These software paths are present. Real device enumeration, output advancement, capture and listening/latency require actual supported hardware and drivers. Missing output cannot pass a playback or monitoring gate. See [recording](recording.md). |
| Publishing and accessible interchange | EPUB3 measured media overlays, recorded-audio reading paths and bounded DAISY 2.02 interchange from the existing Studio timeline; separate source-bound six-dot BRF export through explicitly selected Liblouis tables. | Format validation, actual external-reader behavior, timing/listening, tactile language/layout, physical embossing and manual accessibility are distinct. DAISY export and BRF are implemented bounded profiles, not claims of arbitrary format or language support. See [recorded EPUB](EPUB_RECORDED_AUDIO.md), [DAISY implementation](../src/carr_library/studio/daisy.py) and [BRF](BRAILLE_BRF.md). |
| Documents and citations | Retained text/document formats, bounded legacy conversion, BibTeX/RIS/CSL import, the pinned offline citation processor, generated DOCX and an optional reviewed-change bridge to an already-open Word document. | The Word bridge's native synthetic protocol checks do not qualify Microsoft Word, Office layouts or NVDA. Actual Office uses its separate disposable-document probe and explicit review. Format subsets and archive/parser bounds remain visible. See [conversion](DOCUMENT_CONVERSION.md), [bibliography](V3_BIBLIOGRAPHY.md), [Word bridge](WORD_BRIDGE.md) and [its validation contract](WORD_BRIDGE_VALIDATION.md). |
| Recovery | The shared complete backup includes typed settings, required pending import inputs, project inputs, recording originals, transcription state and reading positions. Restored authority is passive until the relevant fresh review. | Recovery must preserve original bytes, provenance, committed results and cancelled/uncertain states. External runtime/model/reference folders, externally exported media and OS credential secrets retain explicit separate treatment. See [primary-state backup](PRIMARY_STATE_BACKUP.md). |
| Local schedules | Explicitly reviewed, session-armed backup and watched-folder rules use the existing Vault and import-batch services. Rules and passive history persist through the current state/backup boundary. | The application and owning schedules window must remain open. This does not install an OS service, schedule work while closed, or modify ChatGPT automations. See [local schedules](LOCAL_SCHEDULES.md). |
| Transcription | The existing service integrates the supplied worker profiles with retained media, batch/cancel/resume, verified result receipts, editable derivatives and TXT/SRT/VTT/JSON exports. The optional provider path retains per-operation consent and budgets. | Supplied source review and actual protocol/native tests do not establish neural inference, speaker quality, mixed-language accuracy or paid-provider execution. Private workers, models, recordings and transcripts remain external/private. See [transcription](TRANSCRIPTION.md) and [approved remote transcription](REMOTE_TRANSCRIPTION.md). |
| Keyboard and progress | Native named controls, shared commands/shortcuts, measured or indeterminate progress, optional tones and explicitly selected local-voice notices; cancellation and cleanup remain tied to the real worker/owner. | Exact installed keyboard/native probes supplement human Windows/NVDA and listening review. A hidden window, accepted Close or successful source test alone does not prove native destruction or child quiescence. See [keyboard profiles](KEYBOARD_SHORTCUTS.md). |
| Assistant and providers | Shared typed scoped tools, durable jobs, deterministic fallback, local-only default, protected credential references, explicit consent/budgets and local-server plan proposals through the validated executor. | Loopback/synthetic protocol execution does not qualify actual LLM planning or authorize paid calls. Documents and model text never acquire shell, filesystem or network authority. |
| Translation | Google Basic v2 and Advanced v3, selected loopback local translation, retained-document/range batches, durable chunk receipts and offline export. Explicit desktop OAuth refresh credentials are supported. Bounded formatted DOCX profiles cover paragraphs/tables and a separate rich profile with selected images/notes; EPUB has its own layout profile. | Each source-format profile keeps its explicit exclusions and revalidates the reconstructed derivative. There is no guarantee of identical repagination or arbitrary mixed formatting. Explicit refresh is separate from automatic ADC/browser discovery. Actual provider/model execution, linguistic quality and full 135-language coverage remain unqualified without their own evidence. See [translation workstation](translation_workstation.md), [formatted translation](FORMATTED_TRANSLATION.md) and [OAuth adapter](../src/carr_library/assistant/google_oauth.py). |
| Language inventory | The canonical catalog contains 135 target entries with separate capabilities and searchable evidence. | Inventory completeness never establishes 135 working voices, translators, OCR models, ASR models or interface localizations. Each capability/language pair needs its own qualification. |
| Delivery preparation | Native launcher, ordinary installer/uninstall/repair paths, exact-source wheel checks, pinned dependencies/SBOM, notices/source closure, strict paths, sealed runtime files and configured update-host integration. | Compiled installers and exact checksums are separate from publisher signatures, private production trust, complete native A-to-B execution and per-profile distribution clearance. See [managed updates](MANAGED_UPDATES.md). |
| Signed permissions and renewal | Ed25519 envelope verification, product/subject/device/policy/terms binding, supported perpetual/renewable/evaluation profiles, protected monotonic state, interrupted-publication recovery and explicit review of changed terms. Nonce-bound renewal uses the existing worker, cancellation/backoff and an opt-in idle scheduler. | This is a configured client and provider interface. The repository supplies no production issuer, account service, deployment keyring/terms or renewal endpoint. Synthetic signers/transports do not qualify a private authority or live unattended renewal. Reading, save/export, backup/recovery and authorized in-flight work retain the defined data-preservation contract. See [trusted client](TRUSTED_CLIENT.md). |
| Configured signed updates | Independent explicit configuration enables offline or approved-HTTPS Check/Download/resume, signed release review and a sealed external maintenance host from verified installed A. After ordinary Close, the existing idle/installation-lease handoff, backup, managed activation/recovery and checked optional restart use the installed application. Background checks are opt-in; Apply is a separate explicit decision. | Ordinary source/installed tests, local TLS transport and the full compiled/native A-to-B cycle are separate evidence. Actual preparation, Apply, backup/recovery and the restarted window/lease must be observed on the exact candidate. Production signing, roots, targets, rollout policy and user installation are not supplied or qualified automatically. See [managed updates](MANAGED_UPDATES.md). |
See the [Ukrainian quick start](START_V3_UK.txt), [document-format boundaries](DOCUMENT_FORMATS_V3.md),
[advanced search syntax](ACADEMIC_SEARCH.md), [bibliography workflow](V3_BIBLIOGRAPHY.md)
and [keyboard profiles](KEYBOARD_SHORTCUTS.md) for the implemented user paths.
The archive-based owner-candidate route uses both Application and OCR Sources
ZIPs extracted into their shared program folder; `ocr-corresponding-source.tar`
remains intact for verification before `Install.cmd` runs. The standard installer
and configured update route have their own acceptance. Archive assembly and
checksums alone do not establish a qualified owner build or signed release.

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

Implemented software, exact automated execution, real engine/device quality
and distribution clearance are independent states. The following remain
required even when a focused source or installed suite passes:

- Exact final integrated source, installed-wheel and required native workflow
  acceptance, including cleanup, recovery and the configured A-to-B installer
  cycle. Cancelled, skipped or pending work cannot satisfy those gates.
- Publisher signing and a reviewed exact release profile, including dependency,
  native binary, model, voice, sample, font/style, notices and corresponding
  source obligations. The existing installer is not a signed distributable
  solely because it builds or installs.
- The configured private entitlement/account authority, intended production
  policies and trust, signed unattended renewal/offline recovery and actual
  update/rollback/key-rotation acceptance for that deployment. The software
  adapters do not provide the owner's production keys or service configuration.
- Actual authorized owner-engine inference and independent multilingual
  intelligibility/similarity, long-form consistency and hardware measurements.
  Supplied source and managed reference/profile workflows are present; a
  synthetic protocol or generated PCM fixture cannot qualify the model.
- The selected dordiai/SAPI4 combinations, optional MusicGen/ACE-Step model
  inference and actual acceptance of the [finite requested reader workflow
  checklist](REQUESTED_READER_WORKFLOWS.md). Each advertised control and language
  must match the actual chosen engine.
- Physical microphone/driver use, actual output advancement, dry-monitoring
  latency and listening, plus any remaining professional live studio workflows.
  Pause/Resume, dry monitoring, typed envelopes and saved clip/history editing
  are implemented; their presence does not establish complete device or
  real-time multitrack acceptance.
- External-reader/listening acceptance for EPUB and DAISY, and independent
  tactile language, layout and printer review for the supported BRF profiles.
  The implemented exporters keep their documented format/language limitations.
- Real local-model planning and translation, live Google Basic/Advanced
  qualification, actual Office compatibility and visual review of supported
  translated documents. The current protocols, Word bridge, OAuth refresh
  and formatted DOCX/EPUB profiles are implemented; unsupported format features
  and unexecuted model/provider/version combinations remain explicit.
- Actual corpus measurements on the intended support profile, including
  import/search/open latency, memory/disk, cancellation and recovery. A passing
  smaller corpus or instrumented diagnostic cannot qualify a larger corpus.
- Independent qualification of every required capability for the canonical
  135-language target; catalogs and registries are not language-quality results.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution supplies separate evidence and cannot replace that review.
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
