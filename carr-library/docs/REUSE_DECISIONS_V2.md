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
| Transcription | Adopt the supplied Whisper Transcribe headless worker after targeted corrections | Uses faster-whisper; retain model session and existing file-job protocol through a versioned adapter. Source review is in TRANSCRIPTION_INTEGRATION_REVIEW.md. [faster-whisper MIT](https://github.com/SYSTRAN/faster-whisper), [Whisper code/weights](https://github.com/openai/whisper); supplied wrapper/dependency/model distribution rights still need a complete record. |
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
- The package records LGPL-3.0-or-later; its build configuration disables GPL/nonfree dependencies. Do not misclassify this exact build based on unrelated license files.
- The manifest explicitly records public_distribution_clearance=false and third_party_complete_source_bundle=false. This is an outstanding package clearance item; a successful build does not change it.
- Existing citeproc-js2.4.63 uses the selected CPAL1.0 route, QuickJS MIT and separately licensed styles/locales. Review actual attribution and source obligations in the final artifact.

Evidence: [audio source-lock](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/12e6ce6a9ac0b7d93952a8a990e22191fe5d54fe/carr-library/packaging/audio-runtime/source-lock.json), [CSL provenance](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/12e6ce6a9ac0b7d93952a8a990e22191fe5d54fe/carr-library/docs/UPSTREAM_CSL.md). Read a newer candidate's own manifest before applying this snapshot to it.

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
