# Resonance — reuse decisions and component evidence

Current review: 2026-11-27. This ledger supports the [technical specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md); it is not a second product scope. Historical prototype evidence remains below with its original limitations. A candidate listed here is not automatically integrated, tested or approved for distribution.

## Selection rule

Reuse canonical implementation → reconcile an existing reviewed PR → adopt a pinned upstream engine/library → implement only missing glue. The existing native UI, catalog, immutable citations, extraction, semantic search, voice/model manifests, FFmpeg production, provider gateway, assistant executor and backup producers remain the integration boundaries.

Record exact source commit/release, binary/model hashes, license texts/notices, dependencies/assets, supported hardware/OS, chosen distribution profile and real acceptance results. A code license does not automatically license weights, voices, training/reference recordings, SoundFonts, citation styles or generated content. GPL/AGPL allow commercial use but may impose incompatible source/combination duties for a particular closed application; IPC and a “private” ZIP are not automatic exemptions. Resolve the affected package rather than blocking unrelated platform work.

## Current reuse map

| Area | Reuse decision | Rights / qualification boundary and primary source |
|---|---|---|
| Desktop | Keep existing wxPython native UI and command registry | wxWindows exception supports binary distribution under application terms; preserve notices. [Official license](https://wxpython.org/pages/license/) |
| Catalog/search | Keep SQLite/FTS5/BM25 and existing source/citation services | Do not build a search server or new index. [FTS5](https://sqlite.org/fts5.html) |
| PDF/OCR | Keep pypdf/PDFium/Tesseract and the managed traineddata pipeline | Exact bindings/native/dependency/model notices, Unicode paths and installed offline execution. [pypdfium2](https://github.com/pypdfium2-team/pypdfium2), [Tesseract](https://github.com/tesseract-ocr/tesseract) |
| Documents | Retain already imported cairn extractors and current format adapters | Their original source/license/provenance records remain required. Additional conversion tools are optional, with actual format-fidelity tests. |
| Semantic retrieval | Keep existing FastEmbed/ONNX/MiniLM and bounded cache | FastEmbed Apache2; the selected MiniLM model card Apache2; coverage/quality require tests. sqlite-vec is MIT/Apache2 but add it only if it closes a measured gap. [FastEmbed](https://github.com/qdrant/fastembed), [model](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2), [sqlite-vec](https://github.com/asg017/sqlite-vec) |
| Speech/cloning | Reuse SAPI and existing dordiai-voices/owner-engine adapters | Owner runtime, voice/reference rights and actual multilingual audio remain separate qualification. Do not create another training stack. |
| Piper | Optional explicitly reviewed installed-engine adapter | Current OHF-Voice engine GPL-3.0-or-later; each voice has its own rights. Do not apply a legacy MIT assumption to current packages. [Licensing](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/LICENSING.md) |
| Playback/render/effects | Keep existing FFmpeg adapter and exact reviewed shared build | License depends on configure flags and dependencies; complete corresponding-source/notices/replacement review. No nonfree build for redistribution. [Official guidance](https://ffmpeg.org/legal.html) |
| Extra DSP | Pedalboard remains a candidate only if it fills a real missing effect/hosting need | GPLv3 and dependency terms require an explicit compatible integration decision. Existing FFmpeg effects should not be duplicated. [Upstream](https://github.com/spotify/pedalboard) |
| Neural music | Evaluate ACE-Step as an isolated optional runtime through existing music jobs | Original ACE-Step and 1.5 have different code/checkpoint terms; 1.5 model card says MIT. Pin actual checkpoint/submodels and collect full notices; no blanket family clearance. [1.5 code](https://github.com/ace-step/ACE-Step-1.5), [model card](https://huggingface.co/ACE-Step/Ace-Step1.5) |
| MusicGen | Retain optional adapter for terms-compatible use profiles | AudioCraft code MIT, released MusicGen weights CC-BY-NC4.0. Not the default unrestricted commercial-use pack. Real inference/quality remains separate. [AudioCraft](https://github.com/facebookresearch/audiocraft), [weights license](https://github.com/facebookresearch/audiocraft/blob/main/LICENSE_weights) |
| MIDI | Review FluidSynth/Pretty MIDI where current MIDI path lacks a required capability | FluidSynth LGPL2.1, Pretty MIDI MIT; SoundFonts/dependencies separately licensed. Do not label the current simple synthesis fallback professional sound-library output. [FluidSynth](https://github.com/FluidSynth/fluidsynth), [Pretty MIDI](https://github.com/craffel/pretty-midi) |
| Transcription | Supplied Whisper Transcribe worker adopted through the existing isolated process and state boundaries | `audio/transcription.py` reuses the reviewed worker protocol and exact-source strict preparation from PR135; it does not import the incompatible alternative ASR store/engine from PR136. Atomic content-bound receipts and file-level recovery are exercised with synthetic recognition. Current workers reload the model per file; persistent sessions, real model quality and native runtime qualification remain open. See [source review](TRANSCRIPTION_INTEGRATION_REVIEW.md) and [adapter contract](TRANSCRIPTION_SUPPLIED_WORKER.md). [faster-whisper MIT](https://github.com/SYSTRAN/faster-whisper), [Whisper code/weights](https://github.com/openai/whisper); supplied wrapper/dependency/model distribution rights still need a complete record. |
| Local agent | Keep current typed plans/executor; evaluate llama.cpp for actual model planning | Runtime MIT, selected LLM weights separate. Guided regex commands do not demonstrate LLM planning. [llama.cpp](https://github.com/ggml-org/llama.cpp) |
| Translation | Add official Google through the existing gateway; independently qualify local engines | Basic v2 supports API keys; Advanced v3 requires supported IAM/ADC authentication. Coverage, glossaries/batch and costs differ by edition/model. [Authentication](https://docs.cloud.google.com/translate/docs/authentication), [languages](https://docs.cloud.google.com/translate/docs/languages) |
| Bibliography | Preserve existing RIS/BibTeX/CSL-JSON and candidate citeproc-js/QuickJS integration | citeproc-js CPAL1.0/AGPL alternative and CSL styles/locales have distinct obligations. Verify accessible attribution/source in the shipped package; do not replace a working adapter solely because it is not MIT. [citeproc-js](https://github.com/Juris-M/citeproc-js) |
| Larger applications | Use Zotero/calibre/Audacity and similar tools as interoperability/reference candidates where suitable | Do not embed a second GUI/database or copy an entire licensed application without an explicit integration/distribution decision. Existing standard-format interchange is preferable when it meets the task. |
| Updates/security | Reuse reviewed signed-update/entitlement and credential contracts | Evaluate python-tuf/standard Windows tooling for actual gaps; signatures, key lifecycle, installer/runtime and data migration all require exact tests. No new cryptographic protocol. [TUF](https://theupdateframework.io/docs/security/) |

These are decisions/research, not claims that all named optional engines are installed. No new model acquisition or inference occurred in this review.

## Concrete packaged-component finding

Static inspection of [candidate12e6ce6](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/12e6ce6a9ac0b7d93952a8a990e22191fe5d54fe/carr-library) found an existing FFmpeg adapter and source-lock. Reuse them.

- Source revision: 836df4e85894b757debe268f84ffc08c57623a73.
- Windows shared archive SHA256: f7ba27e7986a2d0835881e742a682dec17896f6dc2cdcf9e596f043d2763d894.
- The package records LGPL-3.0-or-later; its recorded build configuration disables the listed GPL/nonfree options; complete dependency clearance remains pending. Do not misclassify this exact build based on unrelated license files.
- The manifest explicitly records public_distribution_clearance=false and third_party_complete_source_bundle=false. This is an outstanding package clearance item; a successful build does not change it.
- Existing citeproc-js2.4.63 uses the selected CPAL1.0 route, QuickJS MIT and separately licensed styles/locales. Review actual attribution and source obligations in the final artifact.

Evidence: [audio source-lock](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/12e6ce6a9ac0b7d93952a8a990e22191fe5d54fe/carr-library/packaging/audio-runtime/source-lock.json), [CSL provenance](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/12e6ce6a9ac0b7d93952a8a990e22191fe5d54fe/carr-library/docs/UPSTREAM_CSL.md). Read a newer candidate's own manifest before applying this snapshot to it.

## Reused synchronized publication and conformance tool

The bounded Studio publishing component is selected from [existing PR112](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/112), reviewed at donor commit [6da794a2ef83c77d160e7f89a1b457a5f375cf6a](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/6da794a2ef83c77d160e7f89a1b457a5f375cf6a). Only its publisher, native dialog, CLI/menu additions and relevant schema-1 regressions are composed with the current canonical services. Its competing transcription/source/provider stacks are not adopted. The subsequent bounded recording/clip and reader selection is recorded separately below.

The publisher reuses the current completed-production receipts, FFmpeg adapter, resource broker, destination approval and atomic writer. The adaptation verifies already committed output after late interruption, permits explicit read-only verification of the writer's residual hard link, and preserves publication metadata through the current full backup. No second render engine, catalog, job format or backup producer is introduced.

| Item | Exact source / integration | Qualification boundary |
| --- | --- | --- |
| EPUBCheck | [Official v5.3.0 release](https://github.com/w3c/epubcheck/releases/tag/v5.3.0), source commit `029831b8f477e4519e9734c984ee24357547a698`; archive size 33071108 bytes; SHA256 `6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5`; complete machine pin in [packaging/epubcheck.json](../packaging/epubcheck.json) | CI-only validator, excluded from the application and evidence payloads. Upstream BSD-3-Clause license and supplied third-party notices remain recorded separately; this does not clear unrelated application/audio dependencies. |
| Java | Existing CI runtime, executable path/hash and actual version recorded before the conformance process | Not an application dependency. The actual Java executable is included in the tested outbound-firewall scope. |
| Native and audio execution | Current CPython 3.11 / wxPython Windows gates, installed source-origin checks and the existing external pinned FFmpeg/ffprobe runtime | Native SAPI production and synthetic production have distinct evidence. No owner recordings, model weights, new audio package or validator JAR is added to the application bundle. |
| Required acceptance | Actual WAV-to-MP3 publication, byte-preserving existing-MP3 reuse, rejection of deliberately malformed EPUB, real process interruption, complete backup/restore and native controls | Evaluate the exact current commit's reports. Package conformance does not establish synchronized-reader interoperability, word alignment, listening quality, NVDA, DAISY or Braille support. |

## Recording, reader, dordiai, local planning and DAISY composition

Source selection: [PR112 donor6da794a2](https://github.com/Oleksii-debug/AudioTacticalFPS/tree/6da794a2ef83c77d160e7f89a1b457a5f375cf6a).
This is selective reuse on the current canonical implementation. It does not
replace accepted schema1–5 definitions, the supplied ASR stack, translation
receipts, source reading dialects, the common resource broker or atomic writers.

| Selected component | Integration decision and dependencies | Required evidence / remaining limit |
| --- | --- | --- |
| Recording and schema2 clips | Existing FFmpeg DirectShow adapter, scene model, process runner, wave playback and typed backup producers; shared microphone/CPU/RAM/disk admission. Ordinary narration remains schema1. | Actual synthetic capture, trim/fade/preview, child interruption and backup/restore; physical microphone, monitoring latency and listening remain unqualified. No new audio binaries or native libraries. |
| dordiai public bridge | Public JSONL adapter, runtime settings and owned voice-registry lifecycle. Reviewed worker identity and upstream contract remain in [DORDIAI_VOICES_CONTRACT.md](DORDIAI_VOICES_CONTRACT.md). Root asset traversal is bounded, cancellable and fails on unreadable entries. | Real synthetic child processes validate transport/lifetime/cache boundaries. Private worker, model/profile rights, CPU/GPU/VRAM benchmarks, actual acoustic output and 135-language quality remain separate. No private engine bytes are vendored. |
| EPUB recorded reading | Existing EPUB parser, exact imported text normalization and fragment offsets; one shared broker around isolated parsing, source writes, extraction and current FFmpeg conversion. | Unsupported SMIL structures, encrypted/remote media and unverified source revisions fail explicitly. Native WavePlayer and lifetime checks require the exact installed build; interoperability/NVDA are not inferred. |
| Pronunciation and command palette | Existing pronunciation rules and menu command registry; native wx controls, stable action identity and conflict-aware preference migration. | Actual handler and native keyboard/destruction gates; original text, reading formats and saved shortcuts remain covered by regression. |
| Local LLM proposal | Existing provider profile, gateway, scope and typed Plan executor; explicit local profile, preview, then separate Run. No shell or new general agent framework. | Real loopback HTTP fixture and native controls execute before the outbound runtime firewall. Actual model inference/performance and paid provider quality are unqualified. |
| DAISY2.02 | Format-only NCC/XHTML/SMIL/MP3 generation over the same Studio publication authority, approvals and receipts; [DAISY2.02 official specification](https://daisy.org/activities/standards/daisy/daisy-2/daisy-format-2-02-specification/). | Exact source/timeline, no-clobber, actual child death, metadata restore and native format choice. DTD validation is only structural evidence; reader devices, full rules and Braille remain separate. |
| DAISY structural validator | Existing production-locked lxml6.1.3/libxml runtime; [five pinned validation files](../packaging/daisy-validation-schemas.json). W3C XHTML1.0 DTD/entities and SMIL1.0 DTD retained from [DAISY pipeline88d39392](https://github.com/daisy/pipeline-modules/tree/88d39392e9a3e10c1f55f1a82e5e7ea12b0b28b6). | Download only to CI temporary storage before blocking egress, require exact size/SHA256, preserve upstream notices unchanged, resolve only pinned local files. DTDs and generated books are excluded from the application and uploads. The actual validation result belongs to the tested head, not this reuse decision. |

The source composition requires fresh portable and installed execution. A donor
result does not qualify new glue or the installed wheel. The dependency/model
rights and distribution profiles described earlier are unchanged; this selection
does not authorize publishing private implementations, references, voices or packs.

## Historical prototype evidence — preserved, not current readiness


## Adopted and executed
- SQLite FTS5 (https://sqlite.org/fts5.html): tested 3.46.1 with unicode61, BM25, external-content index and insert trigger. Parameterized FTS, post-filter against raw page offsets and scoped collection membership. No separate search server or own inverted index.
- pypdf (https://github.com/py-pdf/pypdf; BSD-3-Clause): tested local 5.9.0 on two-page generated PDF. Candidate keeps physical page index; printed label is unknown unless separately supplied/verified. This is a tested version, NOT a production dependency lock; review security and current stable versions before packaging.
- Python stdlib: SQLite transactions and composite foreign keys, SHA-256, JSON, paths and text. Do not introduce extra dependencies for functions already supplied by stdlib.

## Candidates, NOT integrated yet
- pypdfium2 (https://github.com/pypdfium2-team/pypdfium2), Apache-2.0/BSD-3-Clause bindings and PDFium BSD-style plus third-party notices. Render pages to bounded images; use Tesseract (https://github.com/tesseract-ocr/tesseract; Apache-2.0 plus dependencies) for offline recognition. Validate native Windows deployment, engine and language data hashes, timeouts, cancellation, OCR quality and licensing. OCRmyPDF (https://github.com/ocrmypdf/OCRmyPDF; MPL-2.0) optional separate integration, not mandatory due to dependency overhead and PDF rewrite; preserve originals.
- Zotero (https://www.zotero.org/support/notes and https://github.com/zotero/zotero-docs/blob/main/content/collections_and_tags.md): reference for collections, tags, notes, bibliographic interoperability. Consider export/import formats or an adapter; not a mandatory installation.
- calibre (https://github.com/kovidgoyal/calibre; GPL-3.0), Recoll (https://recoll.org/; GPL), Paperless-ngx (https://github.com/paperless-ngx/paperless-ngx): evaluate complete products first, but direct embedding is not selected at this point because of UI/architecture, deployment or licensing constraints. Reassess interoperability; do not copy source without a concrete compatible licensing decision.
- sentence-transformers (https://github.com/huggingface/sentence-transformers; Apache-2.0 library): optional multilingual semantic search after lexical baseline; model weights need independent license and offline benchmarks. Current candidate thematic mode is keyword/BM25, not semantic embeddings.
- python-docx and an EPUB adapter: test structured heading/paragraph extraction, unsafe archive bounds and stable location anchors; no invented printed pages for reflowable files.
- tk-uia (https://github.com/HuzPro/tk-uia; MIT): Windows accessibility candidate for Tk if selected. Upstream documents UIA tree support, not independently validated NVDA speech. Compare against native widgets/wxPython; physical Windows/NVDA acceptance required. PyInstaller or another packaging tool needs pinned versions, license notices and offline installer tests.

## Existing projects
Inventory: 14 connected repositories' tree/metadata inspected; relevant source excerpts inspected in the shortlist. Existing public projects contain OCR adapters, document extraction/resource bounds, chunking, context search and accessibility audit approaches. Private projects contain book import, reading semantics, local library and stable location workflows. No private source or private repository details copied into this public repository. Do not assume public visibility grants an open-source license. Reuse behavior and independently implemented contracts, or use separately authorized/licensed code after exact-version review. Detailed private inventory may be retained in Drive.

## Executed candidate evidence
Files: bootstrap/universal_library.py and bootstrap/test_universal_library.py.
Command in local Linux/Python 3.13.5 environment: python -m pytest -q
Result: initial test exposed silent duplicate-membership rejection bypass (no data leakage demonstrated); fixed explicit workspace ownership check; second run 2 passed in 0.17s.
SHA256 code: 005b2944d8e3a61cc49a7c770a9faef0fbb4cc33399ec544a3b1b782c108acfd
SHA256 tests: 92cd05adca41a0312eaf81a822f2e39d2a6e354577e34a2d2902a31b65ed632d
Drive roundtrip byte hashes matched original tested files; GitHub readback matched uploaded UTF-8 code. Tests exercise persistent independent workspaces, multiple collections, topic tree and cycle rejection, Unicode, PDF and text import, physical/unknown printed page, source exact quote, note updates, export, cross-workspace rejection and reopening database.
Limitations: prototype only. No GUI, scanned PDF OCR, full semantic ranking, verified printed page inference, source binary archival/revision migration, robust untrusted file sandbox, complete CRUD or backup/restore. FTS rank can be influenced by global corpus statistics despite result scope filtering; production isolation review needed. PDF extraction is native text only and trusted local input; not verified against scan images. No Windows/NVDA run, no real Carr corpus, no production release. Review candidates and integrate into owned module contracts instead of maintaining parallel production implementation.
