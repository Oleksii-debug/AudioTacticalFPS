# Release acceptance

The current requirements are [scope V3](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md), including
the private noncommercial MusicGen and multi-hour production clarification.
V2 research acceptance remains required. Speech synthesis is now in scope. Carr
is an optional collection; absence of a Carr corpus is not a universal-product
release blocker. Each gate needs independent evidence at an exact source SHA.
Synthetic, actual-engine, real-corpus, native-control and human-listening evidence
are separate. A development integration is not a declaration that all V3 gates pass.

| Gate | Minimum result | Evidence |
|---|---|---|
| G0 | scope, contracts, claims, backlog | versioned documentation and control issue |
| G1 | synthetic import -> literal search -> source-exact quote -> More -> copy | executable integration tests, reproducible command |
| G2 | OCR/extraction, edition catalog, page mapping, resume, dedupe | golden mixed/scan/native fixtures, error and retry tests |
| G3 | words/phrases and thematic retrieval, short and context modes, accurate citations | exact raw offsets, 100% on golden fixtures; retrieval metrics on held-out queries |
| G4 | English desktop UI, keyboard-only path, NVDA | manual Windows/NVDA evidence, focus and label checks |
| G5 | clean Windows installation, offline use, secure and robust | installer SHA, clean machine tests, CI and security review |
| G6 | legally supplied representative corpus and real research queries | verified editions, source pages, independent sample audit; any Carr-specific corpus run is optional and reported separately |
| G7 | signed-off release candidate | all required gates, exact SHA and independent acceptance |

Research requirements: OCR to readable text, native keyboard-operated GUI, English
UI, multilingual sources, literal/advanced/thematic retrieval, structured results,
source context and verified citations. Author and edition identity, raw provenance,
offline operation, accessibility and packaging remain mandatory. Reading/audio
derivatives never replace authenticated sources or saved evidence. A language-learning
course, keyboard trainer or website is not this product.

Negative cases: empty corpus, duplicate hash, multiple editions, secondary author, corrupt file, no OCR engine, unknown printed label, Roman page label, cross-page span, Unicode/quotes, injection, stale index, missing source, cancellation/resume, wrong revision, keyboard-only navigation. Report NOT_RUN if environment unavailable. An issue or a review without executed tests is not a pass.

## V3 acceptance ledger

These are requirements and evidence categories, not blanket PASS labels. See the
[working audio guide](AUDIO_STUDIO.md) and the precise implementation limitations.

| Gate | Required evidence | Present acceptance boundary |
|---|---|---|
| V3-A | multilingual OCR, safe document formats and actual language packs | Existing PDF/TXT/DOCX/EPUB and selected OCR engines; expanded format parity and automatic language/region selection need their own tests. |
| V3-B | reading derivatives, pronunciation and language quality | Source-preserving derivative and deterministic pronunciation services; contextual restoration and native-listener benchmarks remain separate. |
| V3-C | actual installed voices, live reading, editor and navigation | SAPI5/eSpeak adapters, PCM player and narration editor; SAPI4 and dordiai-voices require actual supported engine contracts and Windows evidence. |
| V3-D | audiobook/dialogue production and Balabolka workflow inventory | Saved typed scenes, roles, pauses, chunk reuse, chapters and captions; the full parity inventory is not complete. |
| V3-E | measured DSP and accessible editing/player/device workflows | Actual FFmpeg DSP/mixing tests; interactive editing, recording and device/listening acceptance are separate. |
| V3-F | full-library/project/settings backup and crash recovery | Library backups plus durable audio-project backups; complete sidecar/settings interchange and all power-loss cases are not yet accepted. |
| V3-G | measured 1k/10k/50k corpus scale and multi-GB workflows | Scoped bounded retrieval and integrated advanced search; target corpus scale is not inferred from small fixtures. |
| V3-H | official DOI/OA, CSL, Word, sync and grounded local reasoning | Academic catalogs, reference exchange, matrices and exports; external integrations require separate implementation and tests. |
| V3-I/J | local assistant permissions, receipts and accessibility | Durable scoped local tools; general natural-language local-model planning and human NVDA completion are not established by the typed plan runner. |
| V3-K | provider/key/budget controls and authorized API use | LOCAL_ONLY and restricted approved requests; mock tests are not real vendor compatibility or billing evidence. |
| V3-L | actual local music, rights and scene suitability | Actual FluidSynth symbolic generation; MusicGen requires approved installed weights/runtime and actual inference. |
| V3-M | multi-scene production, caching, recovery and listening | Actual speech/mix/restore probes and signal measurements; native-listener/NVDA approval remains explicit. |
| V3-N | actual MusicGen and many-hour assembly/resume | Synthetic 10-hour RF64 assembly proves bounded streaming only, not ten hours of original neural generation or musical continuity. |

The Windows workflow publishes test evidence only. Do not upload private installers,
model weights, voices, user texts or generated recordings to public artifacts.
Only an independently accepted exact build can be called a finished release.
