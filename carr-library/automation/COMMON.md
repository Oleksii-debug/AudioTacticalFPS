# Resonance — common autonomous worker instructions

Revision: 2026-11-27. This replaces the layered Carr-only/V2 worker prompts. Product: «Резонанс» / Resonance by Sceneria Innovative Technologies. The internal CARR lane IDs, repository, Python package and existing data formats remain stable.

## Current authority and scope

Read the current root AGENTS.md, docs/UNIVERSAL_SCOPE_V3_FINALIZATION.md (current scope/architecture), detailed V3 audio/agent requirements, retained V2 library contracts, docs/acceptance.md, this file, ROLES.json, the current branch and control issue1. Later explicit owner instructions prevail. Do not infer current requirements from old comments or a historical test count.

Canonical project: Oleksii-debug/AudioTacticalFPS, branch carr-library, product directory carr-library/. Preserve main and unrelated projects. Relevant existing CARR workflows can be maintained; do not modify unrelated automation, credentials or repository settings. Never force-push or overwrite another worker's changes.

The intended platform includes professional owner-engine voice cloning and multilingual synthesis; accessible reading/editing; audiobooks/podcasts; music/audio studio and long production; local music; typed local AI planning/execution; supplied transcription integration; optional Google/local translation; OCR/conversion; scientific libraries/search/citations/bibliography; full recovery; accessible secure distribution/updates and neutral offline entitlements. Carr is an optional collection. The 135-language catalog is a qualification target per capability, not a working-language guarantee.

## Reuse first, integrate what already exists

Inspect canonical code, open/merged PRs and exact upstream identities before creating files. Prefer existing implementation, then a reviewed upstream component, then the smallest missing adapter. Reconcile overlapping work; do not make parallel catalogs, voice registries, jobs, backup formats, provider gateways, DSP/ASR engines or agent executors. Do not train models from scratch.

For an adopted dependency record its exact revision/hash, source and asset licenses, transitive/native dependencies, integration path, actual API/Windows/offline test and the gap it fills. Research, adapter presence, execution, quality and distribution clearance are separate evidence fields. Consult REUSE_DECISIONS_V2.md; its old prototype results are historical.

Use the existing headless Whisper Transcribe worker as the supplied ASR starting point after the review in TRANSCRIPTION_INTEGRATION_REVIEW.md; source is now available, so do not keep a generic missing-project blocker. Owner voice-cloning runtime availability is separately verified. Do not publish user recordings/transcripts/jobs, private engines, virtual environments, caches or unreviewed model packs.

## Coordination without idle gates

Root AGENTS.md takes precedence: no repository-defined worker, branch, PR or work-in-progress cap; no exclusive integrator; no mandatory ownership lease. ROLES describes advisory primary responsibilities. Check for overlapping work, reconcile with its current source or choose a useful independent path. Claims provide provenance, never permission or blocking locks.

Record exact base/head/tree and relevant file identities. Use expected-head updates, sequential writes to a shared path, non-destructive reconciliation and readback. Any permitted worker may perform a justified integration; preserve adequate independent review and relevant checks. Do not merge failing product changes or claim a release merely because a branch is mergeable. A documentation-only correction does not retroactively pass failing application tests. Respect externally enforced GitHub protections.

If CI is pending or the executor is unavailable, continue source review, upstream verification, integration preparation, docs and other safe useful work. Record runtime tests NOT_RUN. Do not stop a whole run because one private engine, review, corpus or CI dependency is unavailable. Keep an exact current candidate ledger, not a permanent claim that PR97/99/125 or any moving branch is always authoritative.

## Shared architectural boundaries

Use existing domain services from GUI, CLI and typed assistant tools. Preserve immutable originals, edition/revision/physical-vs-printed-page identity, source offsets and authorization. Corrections, translations, transcripts and narration are separate derived assets. A model must never fabricate or approve a quotation.

Extend existing jobs/journals through shared operation identity, progress, cancellation, resource admission and recovery. Keep completed vs cancelled-after-commit semantics correct. Engine/model capability manifests determine actual languages, formats and controls. Isolate heavy inference dependencies; no long inference inside a database write transaction or the UI thread. Reuse existing runtime integrity checks and safe cache identities.

Keep deterministic planning as a reliable fallback; LLM plans use the same validated allowlisted tools. No arbitrary shell, network or path authority from documents/model output. Backup manifests declare included/excluded/external state; preserve resume inputs, user work and schema compatibility. Bounded memory/disk and safe filesystem behavior require measured evidence.

## Rights, privacy and optional network

Public GitHub contains only authorized source, synthetic/lawful fixtures, neutral requirements and non-confidential evidence. Do not commit private Drive URLs, user file paths, personal books/audio/transcripts, secrets, issuer keys, account data or confidential commercial configuration. Keep code/evidence in GitHub; do not upload repeated archives, reports, model caches or environments to Drive.

Review exact code/binaries/weights/voices/styles/fonts/SoundFonts separately. MusicGen NC weights are not a default unrestricted commercial-use pack. GPL/AGPL/CPAL/LGPL require their actual compatible use/distribution obligations; optional process isolation or private delivery does not waive them. Reuse the existing LGPL FFmpeg build and CSL adapter subject to their exact outstanding clearance; do not replace working components merely for a generic license label.

Local core operations work without external inference. Optional official Google Translation/other providers require explicit data-transfer and cost authorization, per-operation budgets, secure credentials and supported-language discovery; writing an adapter does not authorize paid calls. No scraping consumer endpoints or hidden remote fallback. Signed entitlement refresh, offline validity/grace, update installation and billing are independent; never lock user-owned data or interrupt a save/render because connectivity fails. Real policies and endpoints remain private.

## Verification and durable completion

For each result retain requirement→module→exact SHA/tree→dependency/pack hash→command/run→actual outcome→review. Separate source tests, installed Windows tests, manual NVDA, real model/acoustic quality, corpus acceptance and distribution rights. Do not sum overlapping tests or carry PASS between different source trees. No screenshot/AST check substitutes for actual keyboard/NVDA use; synthetic long audio is not hours of neural generation.

Deduplicate findings and attach concrete reproduction/evidence to existing issues. Prioritize observed failures and already prepared corrections over new features. Independent auditors retain independent verdicts; if they fix code, another reviewer must validate that fix. Archive superseded work only after provenance confirms inclusion; no deletion or closing of useful unresolved work merely to reduce counts.

A successful worker run is not a released platform. Complete the promised requirements or state the exact declared preview scope; keep full-platform gaps visible. When actual work is complete, maintain regressions without inventing new scope. At each run save meaningful progress in GitHub and give a short Ukrainian result: what changed, link, actual PASS/FAIL/NOT_RUN and the next concrete remaining dependency. Avoid repetitive reports about unchanged blockers.
