# Release acceptance
Each gate is NOT_DONE until independent evidence at an exact SHA. Synthetic and real corpus checks are separate.

| Gate | Minimum result | Evidence |
|---|---|---|
| G0 | scope, contracts, claims, backlog | versioned documentation and control issue |
| G1 | synthetic import -> literal search -> source-exact quote -> More -> copy | executable integration tests, reproducible command |
| G2 | OCR/extraction, edition catalog, page mapping, resume, dedupe | golden mixed/scan/native fixtures, error and retry tests |
| G3 | words/phrases and thematic retrieval, short and context modes, accurate citations | exact raw offsets, 100% on golden fixtures; retrieval metrics on held-out queries |
| G4 | English desktop UI, keyboard-only path, NVDA | manual Windows/NVDA evidence, focus and label checks |
| G5 | clean Windows installation, offline use, secure and robust | installer SHA, clean machine tests, CI and security review |
| G6 | actual legally supplied Carr corpus and real queries | verified editions, source pages, independent sample audit; BLOCKED_MISSING_CORPUS |
| G7 | signed-off release candidate | all required gates, exact SHA and independent acceptance |

Hard requirements: OCR to readable text, simple GUI, English query and UI, literal words/phrases, thematic block vs short quotation, structured results, More opening page context, book/volume/page citation. Author and edition identity, raw provenance, offline operation, keyboard accessibility and packaging are engineering decisions. Optional translation editions, evidence-linked summaries, illustrations and table analysis do not replace the core. Not in scope: language-learning course, keyboard trainer, speech synthesis, website or cloud service.

Negative cases: empty corpus, duplicate hash, multiple editions, secondary author, corrupt file, no OCR engine, unknown printed label, Roman page label, cross-page span, Unicode/quotes, injection, stale index, missing source, cancellation/resume, wrong revision, keyboard-only navigation. Report NOT_RUN if environment unavailable. An issue or a review without executed tests is not a pass.
