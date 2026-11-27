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
| Reading and voices | Source-preserving reading derivatives, reviewed whole-copy/selected-text pronunciation rules with dictionary save/import/export and undo, local voice discovery, playback, SAPI5 native COM worker and explicitly configured Dordiai adapter | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles/scenes, versioned named clips with trim/placement/gain/pan/fades, microphone capture, pauses, procedural score, DSP, aligned stems, subtitles and verified resume | Actual FFmpeg PCM, stop/cancel, clip/recording composition and destructive recovery; physical microphone, listening and native evidence remain per build; streaming fixtures do not establish full-length neural inference |
| Documents and citations | Isolated conversion, local citeproc-js, retained citation DOCX regeneration, exact custom style, guarded bibliography exports and opt-in DOI previews | Actual Node/DOCX regeneration, immutable source snapshot, final content recheck and no-clobber tests; native regeneration is separately gated; no live Word add-in |
| Synchronized publishing and reading | EPUB3 chapters/navigation/MP3/line-level SMIL and DAISY 2.02 NCC/XHTML/SMIL file sets from an exact completed Studio job; retained EPUB overlays play through Reading Desk with segment navigation and source reauthorization | Actual FFmpeg/package/no-clobber/recovery/playback-boundary tests; three official EPUBCheck cases passed without errors/warnings, four DAISY documents passed pinned DTDs; exact-build native probes, third-party readers, word alignment, Braille, listening and WCAG/NVDA qualification remain separate |
| Recovery | Manifest-bound backup v4, conditional v5 for reading state, pending import bytes, bookmarks/resume, Studio inputs and transcription state; portable collision checks; transcription admission/run locks prevent snapshots from disrupting active workers | Real deletion/restore/resume and active/paused-job exclusion tests; external engine approvals require explicit rebind; external exports and OS credential secrets remain separate |
| Transcription | Supplied reviewed Whisper worker through one service, native dialog and CLI; explicit file/folder intake, retained source, four output formats, revision editing and resume; Windows live progress remains indeterminate until verified worker exit | Actual offline tiny.en CPU inference, two-file folder execution and destructive recovery with synthetic English speech; other languages/models, diarization, word alignment and human Windows/NVDA quality remain separate |
| Keyboard and progress | Configurable menu keys and searchable command palette with current command/scope validation, named controls, measured or indeterminate progress, Off/Beep/Speech/Beep+Speech, explicit local voice and playback suppression | Real private PCM synthesis and bounded feedback-worker tests; terminal notices wait for obsolete speech to stop; human NVDA listening remains required |
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
  synchronized reader and full DAISY semantic/interoperability acceptance, and
  Braille workflows. The implemented DAISY exporter has a separate
  [bounded publication profile](STUDIO_DAISY_PUBLISHING.md).
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

## Exact Windows failure evidence and subsequent corrections

Public source `8bf411718548809793748ed01f458c79f3692e98`
[run 45929429163](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45929429163)
completed with failure. Its actual fresh EXE installation, first native launch,
normal close, second launch with unchanged installation marker, retained
source/search state and installed self-test passed. The full-bundle gate later
failed at an obsolete translation-control label; the recording probe expected
an obsolete live progress-gauge name. These successful substeps are not a full
bundle PASS, and the installed EPUBCheck step was not reached.

The source step completed 175 of 183 reports before its 20-minute deadline:
169 successful suites, six failing suites, 4,126 passed assertions, nine failures
and two skips. The interrupted reading-backup suite is not counted. Subsequent
corrections address Windows live-status replacement, equal-metadata changed
export bytes, owned-voice cleanup fixtures and the Windows absolute-path
fixture. POSIX process/filesystem cases have explicit Linux selection; strict
failure/skip checks remain. The source deadline reflects the measured workload.

This history identifies actual exercised paths and failures. It is not
transferred to later source. New recorded-EPUB/DAISY native probes, installed
EPUBCheck, source suites and final sealed-bundle verification are required for
each composed candidate. See [DAISY evidence](STUDIO_DAISY_PUBLISHING.md),
[recorded EPUB reading](EPUB_RECORDED_READING.md) and the finite
[reading workflow inventory](BALABOLKA_WORKFLOW_ACCEPTANCE.md).

The current FFmpeg packs remain available for reviewed local installation and
execution. Their missing distribution clearance is enforced separately by the
existing transfer-archive helper; runtime tests do not authorize an archive
containing uncleared native payloads.

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.

## Reviewed reader and command additions

The composed source at local `646a15e` passed 235 focused cases with zero
failures/skips across pronunciation review/handlers/reader hooks, command palette,
keyboard settings, progress ownership, publication handlers and bounded portable
backup paths. The pronunciation workflow independently passed all 82 cases in six
strict offline suites including existing reading, source and recovery tests.
The palette independently passed 269 composed cases; these sets overlap and are
not added together. The earlier import/music/delivery/transcription/backup
composition passed 367 cases on local `8cceedd`. These are source test results,
not actual native keyboard, installed Windows, model quality or human acceptance.

The new Windows probes exercise actual keys through the normal wx event loop,
verify foreground window/process ownership and release paired keys before the
next action. Both source-installation and sealed-bundle jobs require recorded EPUB
reading, DAISY publication, command palette and pronunciation review. Their result
remains NOT_RUN until a corresponding exact-build Windows run completes. See
[command palette](COMMAND_PALETTE.md) and [pronunciation review](PRONUNCIATION_REVIEW.md).
