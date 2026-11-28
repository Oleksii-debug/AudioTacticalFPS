# Resonance — reuse decisions and component evidence

Current review: 2026-11-27. This ledger supports the [technical specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md); it is not a second product scope. Historical prototype evidence remains below with its original limitations. A candidate listed here is not automatically integrated, tested or approved for distribution.

## Selection rule

Reuse canonical implementation → reconcile an existing reviewed PR → adopt a pinned upstream engine/library → implement only missing glue. The existing native UI, catalog, immutable citations, extraction, semantic search, voice/model manifests, FFmpeg production, provider gateway, assistant executor and backup producers remain the integration boundaries.

### Reconciliation on 2026-11-27

- [PR136](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/136), head
  `9523886`: reuse the create-only export receipt/cleanup handling and reviewed
  transcript timestamp validation/formatting. Its in-process faster-whisper
  session and alternate audio job/model stack are not adopted: the current
  architecture requires the supplied external worker and canonical durable jobs.
- [PR137](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/137), commit
  `cb966fc`: adopt its source authorization checks before and after the native
  Save As chooser, with the real library authorization regression. Its later
  commit `1601930fc672c7740e0f72715853b38a31a77e9c` adds retained source lineage
  to portable reading copies, verifies each original slice and preserves legacy
  projects without attaching them to a source merely by matching text.
- [PR138](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/138), commit
  `69f1eb359bbbdd0192c44a06b9e51c1822b6b0d5`: adopt only its three-file OCR
  path-identity and streamed-mutation change onto the current canonical code.
  Equivalent model directory paths retain their selected hash expectations;
  no older branch-wide tree is substituted for the current application.
- [PR145](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/145), commit
  `93c09745a6a4dc3d498308d001f214d1ef10ba5c`: reuse portable state-manifest
  validation before backup publication, including Unicode/case and
  file/directory collisions. The combined source-lineage, backup and OCR
  regressions passed 149 tests locally; installed Windows evidence is separate.
- [PR139](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/139), commit
  `74ee2073af2d67aad7bb1fa92e22d4a9627ba37f`: reuse its bounded import
  cancellation/progress-observer handling and typed Studio interruption
  classification. Completed imports remain completed, and a late stop request
  does not conceal an independent publication error. Keep the current shared
  resource admission and speech-progress implementations.
- [PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143), reviewed
  head `ab53b696241a9168b64096fd669cd9416945dcce`: adopt the captured export
  destination/receipt contract and its eight native UI callers, including the
  academic return-value compatibility correction. The same selection includes
  commit `32772117ffb5fcdb972f66f62204585cebe658b8`'s guarded import watermark,
  stopped-worker publication reconciliation and pending-backup validation
  together; an existing/replayed or damaged revision cannot imply completion.
  Reuse the bounded Windows state-replacement retry from `44821a0` without
  replacing any other state protocol. No alternate ASR implementation or
  branch-wide replacement is adopted.
- [PR135](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/135) is an earlier
  ASR-03-only preparation. The privately corrected supplied worker addresses
  all four reviewed integrity findings; do not replace it with the narrower fix.
- Shared runtime admission reuses `filelock`/native OS lock release and
  `durable_state`, while the operation view reads existing typed checkpoints.
  No alternate database, queue, inference engine or model downloader is added.
- Progress speech reuses the canonical selected local-voice provider and WAV player;
  it does not implement another synthesizer or write into rendered audio.
- Integration of canonical `962297eef228870b6eaab3f46163124db8f5045c` retains
  its explicitly selected voice, quiet/media isolation and accessible busy-time
  commands, replacing the earlier duplicate UI-local speech worker. Portable
  backup-name checks and canonical copied-content revalidation both remain.
- Private ZIP refresh reuses the existing exact-source wheel, dependency lock,
  sealed inventory and strict archive verifier. Directory and direct-ZIP
  assembly share one validation/selection path, and the existing OCR source
  companion is reverified instead of creating another large staging copy.

Record exact source commit/release, binary/model hashes, license texts/notices, dependencies/assets, supported hardware/OS, chosen distribution profile and real acceptance results. A code license does not automatically license weights, voices, training/reference recordings, SoundFonts, citation styles or generated content. GPL/AGPL allow commercial use but may impose incompatible source/combination duties for a particular closed application; IPC and a “private” ZIP are not automatic exemptions. Resolve the affected package rather than blocking unrelated platform work.

## Selective Advanced v3 adoption — 2026-11-27

Reuse the Advanced translation slice of [PR112 head
`8bf411718548809793748ed01f458c79f3692e98`](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/8bf411718548809793748ed01f458c79f3692e98),
whose feature implementation originates in
`20e275e5112566cd07c3aa56459bd8634240bc3e`. The broad donor branch is not merged.
The reviewed slice extends `assistant/profiles.py`, `providers.py` and
`translation.py`, the existing provider settings and translation dialog, and adds
`assistant/translation_cli.py` with one main-entrypoint dispatch. Existing
credential renewal is reused unchanged. No new SDK, model, provider queue,
database or dependency is introduced. Existing source lineage, ASR, resource
admission, rendering and export-approval implementations are retained.

The service performs plain-text NMT and applies an already existing glossary,
using a manually supplied expiring OAuth token held in the OS vault. It verifies
the exact numeric project, region, requested model and glossary in the response
before publishing a derivative. This does not implement automatic ADC discovery
or refresh, glossary creation, Cloud Storage batch translation, formatted
document translation or local translation inference.

Evidence: the three integrated provider/Basic v2/Advanced v3 suites passed 122
tests, including unchanged v2 credential-redaction and not-sent replay guards.
The donor's [Windows run45929429163](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45929429163),
job140278375920, passed its separate Advanced native step23. Its broader workflow
has unrelated failures and is not release acceptance. The required new
`windows_translation_v3_smoke.py` uses installed wx controls and the actual
Windows vault but synthetic HTTPS, confirmations and directory pickers; it must
pass again for the exact integrated build. No live Google calls, upload of user
text, charges, independent linguistic quality or NVDA listening acceptance are
claimed. Google account/IAM, quota/billing and glossary rights remain explicit
owner configuration and acceptance boundaries.

Official REST/authentication references were checked on2026-11-27:
[translateText](https://docs.cloud.google.com/translate/docs/reference/rest/v3/projects.locations/translateText),
[supported-language request](https://docs.cloud.google.com/translate/docs/list-supported-languages),
[authentication](https://docs.cloud.google.com/translate/docs/authentication).

The same exact donor's local-model planning slice is selectively adopted in
`assistant/llm.py`, `assistant/service.py`, the assistant dialog and only
`assistant_main` in `studio/cli.py`. The canonical production service, broker and
process handling are retained. Its existing lazy `.voices` property and inventory
resolve on the dialog worker; donor `discover_voices`/`close` methods are not
imported or called. Proposal, review and explicit execution stay separate, using
the existing typed scope/parser and jobs. No model/server download or startup is
added. The three integrated planning suites passed41 cases with actual local
HTTP/CLI and synthetic model responses. The donor Windows job's native planning
step19 and loopback step24 passed; `windows_local_planning_smoke.py` and the
required suites must pass on the integrated build. This evidence does not
establish a particular local model's planning quality, model rights or NVDA.

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
| Local agent | Reuse the typed executor and selected loopback model proposal/review workflow; no runtime/model launch or download | Actual HTTP/native control fixtures use synthetic model responses. The selected installed server, model weights/rights and multilingual planning quality require separate acceptance. [llama.cpp](https://github.com/ggml-org/llama.cpp) |
| Translation | Reuse Basic v2 and the selectively adopted Advanced v3 NMT extension through the existing gateway; independently qualify local engines | Basic v2 uses API keys; Advanced v3 uses explicitly entered expiring IAM/ADC-derived OAuth tokens and existing glossary resources. Automatic token refresh, document/batch translation and local model inference remain unimplemented. Coverage and costs differ by edition/model. [Authentication](https://docs.cloud.google.com/translate/docs/authentication), [languages](https://docs.cloud.google.com/translate/docs/languages) |
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

## Current finalization reuse — 2026-11-27

The composite candidate preserves the current canonical architecture; the
following are additive adoptions, with acceptance tied to its eventual exact
commit rather than the earlier PR's status.

| Existing implementation | Adoption | Evidence boundary |
| --- | --- | --- |
| PR100, d8db53a7ee288c7dff708d639d59b3b0ce612cef | Exact DOI/ISBN providers, existing scoped acquisition service and native review controls; preserve all original tests and source/import contracts | Synthetic response, subprocess, scope and backup tests; live provider execution remains separate |
| PR112, 20e275e5112566cd07c3aa56459bd8634240bc3e | One measured EPUB media-overlay publisher; adapt both original production journals without changing their identities | Actual audio and EPUBCheck gates required on the composite; no word alignment, DAISY or NVDA implication |
| PR152, 112935aa15952682a885b10c322b3b4016c6cc80 | Accessible palette controller and native controls adapted to the active KeyboardActions registry | Original handler, focus, remapped keys, disabled/stale commands and installed Windows checks |
| PR151 reviewed CI runner | Independent strict suite verdicts with bounded concurrency and process-tree cleanup | Empty suites, skips, XFAIL, timeout and cleanup failure remain failing gates |
| Existing bounded folder scanner and private supplied-worker bridge | Folder transcription uses the current queue, input-hash, worker receipt and cancellation contracts | Synthetic worker and native folder-picker execution; real ASR acoustic qualification remains separate |
| Existing defusedxml 0.7.1 and FFmpeg runtime | Reject entity-bearing EPUB metadata and reuse verified audio conversion/playback | No additional production wheel or competing XML/audio engine |

OpenAlex's current singleton/authentication documentation and Open Library's
Books API documentation were checked for this adoption:
https://help.openalex.org/api/get-single-entities/
https://help.openalex.org/api/authentication/
https://openlibrary.org/dev/docs/api/books
The Open Library Books endpoint is documented as legacy. Its adapter is
optional, exact-identifier and fail-closed; no silent endpoint migration or
general search result is accepted as an edition match. The OpenAlex adapter
uses only an explicit singleton request; it does not enroll an account or
enable billing. Provider availability and changing rate policies remain
external-service behavior.

EPUBCheck 5.3.0 is pinned in packaging/epubcheck.json to its exact official
release archive, source revision and BSD-3-Clause notices. It runs only during
conformance acceptance and is excluded from the application dependency locks.

## Standard Windows lifecycle adoption — 2026-11-28

The bounded Install / Repair / Uninstall contribution reuses
[PR99](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/99) at
`fd0cca2405f33c813c4cf43c9661edf1f2e93a55`. It is adapted to the existing
composed package, complete source-wheel verifier and actual
`audio/process.py`, `audio/windows_process.py`, `audio/models.py` and
`audio/cleanup.py`. The existing offline bootstrap, application/dependency
locks, original Python archive, native DLL inventory, bundle seals and
transfer producer remain the same authorities.

| Adopted component | Exact origin and dependency boundary | Receiving acceptance |
| --- | --- | --- |
| Standard setup builder, fixed maintenance helper, native lease and Inno template | The four current process-helper files are copied byte-for-byte as one private helper package and checked against the exact source-bound application wheel. Public entrypoints use the same shared lease; existing cleanup owners must settle before the process can return or maintenance can replace files. | New source and installed filesystem/real-process contracts, plus one actual Windows Install / Repair / Uninstall gate. NOT_RUN at composition; donor tests are not the receiving verdict. |
| Inno Setup 6.7.4, build-time compiler and original setup runtime | [Official release](https://github.com/jrsoftware/issrc/releases/tag/is-6_7_4), artifact `innosetup-6.7.4.exe`, 10,604,576 bytes, SHA-256 `a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c`. Official release metadata was reread and matches the existing donor pin. | `prepare_inno.ps1` additionally requires the expected valid publisher signature. This source review does not establish execution of that signature check or signing of the produced Resonance setup. |
| Inno upstream notices | [Exact upstream license](https://github.com/jrsoftware/issrc/blob/is-6_7_4/license.txt), Git blob `ac4dfe9e6543a360c5422b6f1fb88075e8b6073d`, reread from upstream. Original copyright, attribution and binary notices remain required; the compiler/runtime is used unchanged. | Per-profile distribution review remains separate from a passing installation. No runtime/compiler/setup binary is committed or uploaded by these evaluation jobs. |

The focused Windows profile uses all 19 current core dependency pins and
their matching SBOM. Its Linux source/installed companion excludes only the
two Windows-only packages and retains the other 17 versions. Neither profile
adds a new application Python dependency. The complete Windows aggregate also
runs the same native lifecycle against its already verified full CI bundle;
creating this private test setup does not change the existing rejection of
audio-bearing application transfers.

The native probe reuses the existing owned-process boundary and waits for
every causal cleanup owner before a retained log, installation lease or
rollback context is released. It exercises real Inno, a real local Python
bootstrap, Unicode paths, registered shortcuts, the standard native launcher,
live-job exclusion, Repair, Uninstall, modified/foreign-file preservation and
a populated external SQLite library. The source fixtures remain explicitly
synthetic; they cannot qualify a native installer.

The passive managed-upgrade identity record is data only. This adoption does
not install a second update/entitlement authority or change the existing
DeliveryService/provider contract. Signed A-to-B updates, publisher identity,
private authority deployment, Windows 11/NVDA, a separate unprivileged account
and native network tracing remain independent requirements.
