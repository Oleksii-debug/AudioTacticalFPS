# Canonical e69 semantic reconciliation

## Authorities and scope

The comparison is the exact canonical range
`924ae83316a3cf9482aaf2ec51a6e6a5adb614c3..e69ba2403515f21509686d66f4bc8b3673756d81`:
21 commits and 103 changed paths, with no truncated file list. The current
integration baseline is `e4aa3ab89a9a600bfecf8c64ae9c4d6f232abdd8`.
The canonical finalization specification is unchanged, blob
`db8adbdc483a03c262885928c7ed214ca70bbaa7`.

This records behavior and data authorities. It does not authorize a wholesale
replacement with the alternate assistant, provider, studio or ASR service trees.
Root retains the final ancestry/workflow/version reconciliation responsibility.
A source or adapter match is not EXECUTED or QUALIFIED evidence.

## Runtime and user behavior map

| Canonical delta and authority | Current owner / integration decision | Remaining acceptance |
| --- | --- | --- |
| Export publication and completion, canonical research/export_file.py blob c60b6f387c086c51f4de7ed535e9fd5d675f88a4 | Ported in 0759d851 and composed into e4aa. Cancel during overwrite hashing, refuse new-file conflict before reads, retain no-clobber through commit, and propagate completed cleanup notices through the existing academic/catalog/reading/import-report handlers. Current editable DOCX/regeneration remains intact. | Exact e4aa source/installed/native results; donor results do not transfer. |
| Advanced v3 profiles/providers/translation | Already adapted to root providers.py and assistant/translation.py/translation_state.py. Current v2 remains compatible; project/location/model/glossary, vault expiry/renewal, directionality, exact review/budget/ledger receipts remain authoritative. Current v3 cache-path correction is present. Do not copy the alternate assistant/providers.py stack. | Actual account/key/paid-request evidence remains separate. |
| Local typed-plan proposals, canonical assistant/llm.py blob ee73d023bc8bb8932239a4e569f6a5e812dcf1a5 | Current assistant/llm.py uses the existing ProviderGateway, saved-profile binding, explicit scope/approval and durable ledger; rejects duplicate/nonfinite JSON, tool calls and model-provided authority. Existing UI previews and CLI proposals do not auto-run. Current input bounds are explicit and stricter, not silently raised to donor limits. | Existing current loopback/native planning gates. Real owner-selected model quality is external evidence. |
| Resource models/broker, models blob 2cbf7ebddfa4492ca2b0774a1cf9e6be42f915af | Models are byte-identical. Current broker preserves donor admission plus the relative application-policy binding, cancellation at initial admission and relocation behavior. Current _state delegates to the reviewed shared durable publisher. | Existing current broker/admission/policy/relocation gates. |
| Tasks/resources facade and native UI | Adapted to actual assistant, flat translation, transcription-jobs, managed audio-projects, import batches/requests and recording receipts. Bounded metadata discovery, stable selected identity and recorded-versus-verified status remain. No fictional persistent Studio pause or automatic resume. | Current native Tasks + real policy tests; do not import donor alternate-store glob routes. |
| Canonical studio/render.py resource wrapper | Existing current audio.production/operations.admission already owns render resources and native-family cleanup. Preserve current clips, master DSP, capture receipts, plan-bound publications, FULL FFmpeg resolver and engine manifests. | Current render/capture/master/publication native gates; actual optional models and long-corpus quality remain separate. |
| audio/process.py reader-start cleanup | Current audio/capture_process.py allocates/starts its pipe readers inside the owned try/finally, closes unstarted streams, and retains cleanup callbacks/admission until native-family quiescence. This is the corresponding implementation, not a missing duplicate audio/process module. | Current actual descendant/cancel/timeout/reader-start contracts, including Windows owner-drain tests. |
| durable_state.py Windows sharing retry | Current audio.jobs shared-delete readers and bounded native rename publisher provide the required behavior; importing a second durable-state writer would lose later Windows corrections. | Current real Windows atomic replacement and identity tests. |
| Pending publication watermark compatibility | Current pending_backup already recognizes the old scalar, validates its type/range, clears it on restore, and relies on the V5 immutable DB publication receipt for authority. Current streamed-profile and scanner-v2/v3 extensions remain. | Existing publication identity, pending restore, streamed-source and scanner tests. |
| New canonical state/transcription schema and common-v4 backup bounds | Real data gap. Closed by the additive typed inert recovery adapter in this candidate; exact original profiles/jobs/inputs/results/edits/history survive without activating old paths. Current prepare is reused only after explicit current runtime/settings/subset review. See CANONICAL_TRANSCRIPTION_RECOVERY.md. | New suites/native probe are NOT_RUN pending exact-source CI. |
| Canonical translation CLI | Concrete command-line glue gap at e4aa. No current translation_cli module/route exists. Root owns adaptation of reviewed preview hash, explicit remote consent, jobs/resume/export to the current TranslationService. Native UI existence is not CLI parity. | Must add current-service CLI routes/tests; owner keys not needed for offline contracts. |
| Canonical transcription CLI | Concrete command-line glue gap at e4aa. audio_cli.py has speech/render flags only; __main__.py has no ASR route. The existing current service can provide compatible job preparation/review/run/control/export commands without the alternate ASR service. | Follow-up current-service CLI adaptation and offline process/receipt tests, separately from this recovery patch. |
| Native platform/local-planning/translation probes | Keep current installed/native probes and strict union, adapting actions to current stores. The new historical recovery probe uses actual current preparation and explicitly synthetic inventory bytes. | Actual exact-head Windows execution; manual NVDA is separate. |
| Version/SBOM/entrypoint metadata | Reconcile source version and manifests coherently in the final package. Preserve the current reviewed 53-pin/full-FFmpeg/source/runtime policy; a donor version bump is not evidence that dependencies or qualification changed. | Final installed-wheel/bundle/source identity gates. |

## Data ownership map

| Older namespace or schema | Current disposition | Authority that cannot transfer |
| --- | --- | --- |
| state/transcription/profiles and jobs, resonance-transcription-job/1 | Exact typed inert archive; full retained-input/result/edit cross-reference validation. Common-v4 manifests remain verbatim hashed history. | Runtime trust flags, absolute interpreter/model/source paths, old run status and worker receipts. |
| transcription-jobs, carr-supplied-transcription-job/1 | Current immutable relative input definition, fresh reviewed runtime/config/run binding, current hardened receipt, source/result rehash and relocated read-only completion verifier. | No old canonical cache or accepted result becomes a current completion. |
| state/translation/jobs with old canonical job envelope | Existing historical retention; current active jobs use the current flat typed translation namespace. Keep separately reviewed migration, not a generic JobStore copy. | Provider/vault/ledger/root approval bindings cannot be rebound by restore. |
| state/studio/jobs and canonical project schema | Existing explicit import-as-new bridge maps representable roles/scenes/clips to the current Studio with exact retained original JSON and copied owned assets. | External asset paths, runtime paths, cache IDs and unsupported music do not activate automatically. |
| state/speech reader/project snapshots | Existing reader-session/origin archive bridge retains immutable source/history and validates live locations against the current DB. | Old source identifiers and voice paths alone cannot establish current source authority. |
| state/audio/engines.json, providers and credentials | Current local runtime registration is separately reviewed. Old declarations can remain inert provenance; actual engine registration and secrets stay excluded from portable backup activation. | Executable/model/vault authority and redistribution rights. |
| state/operations/resources.json | One shared typed policy producer; live leases and runtime accounting stay excluded. | Archived lease/PID data is never live ownership. |

## Core research and pending scope

Within this exact 21-commit range, the only changed product path under
research/citations/catalog/reader/extract/conversion is research/export_file.py.
There is no new parser, citation renderer, conversion reader or search algorithm
to import from this range. Earlier current ports remain necessary: bounded
search continuation, 5003-file scan successors, original-preserving streamed
UTF-8 intake, editable DOCX/CSL, source-bound reading, Office/OCR adapters,
synchronized publications and exact metadata/quotation checks.

That is not a claim that all finite V3 requirements are complete. The independent
large-corpus acceptance harness, still-unintegrated legacy format families,
actual model/language quality, physical device/NVDA acceptance, keys/samples and
distribution rights remain separately tracked. The prior finite qualification
matrix and large-source plan remain applicable. Do not replace them with a
single green adapter or test-count label.

## Follow-up outside this comparison

The subsequent source-reviewed PR173 incremental Vault/transport publication and
postcommit cleanup corrections are a separate root-owned lane. Preserve them
when creating the eventual two-parent canonical reconciliation. They are not
part of e69 and must not be discarded by copying its older sync/provider files.
