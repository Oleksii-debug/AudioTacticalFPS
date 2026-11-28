# Shared canonical assistant and evidence test disposition

This is a bounded assertion-level comparison of canonical source **e5c64a5245583f8f8b404a0f042f6ca438c24d7f** against selected current source **5070b0e9808e1242936fac314bab9a8d9f20213a**. It covers the four shared test modules requested for reconciliation, not merely absent filenames. The companion JSON maps all **55 canonical test functions**, identifies source blobs and function-body hashes, names their exact current successors, records semantic differences, and distinguishes newly restored assertions from current ones.

| Canonical shared module | Canonical functions | Current treatment |
| --- | ---: | --- |
| tests/assistant/test_local_assistant.py | 15 | 14 function bodies remain identical. The old native-resume callback test is adapted to the current dialog owner in the planning-handler suite. |
| tests/assistant/test_planning_handlers.py | 10 | Compare each old authority, cancellation, late-result and voice-inventory assertion with the current actual handler and scheduling fixture. Preserve current separate model proposal → Preview → Run behavior. |
| tests/assistant/test_translation.py | 16 | Map assertions to the selected typed translation store and current provider gateway/ledger. Restore explicit cache-preservation, retry-binding, unresolved-state and HTTP-status matrices. |
| tests/ui/test_evidence_draft_recovery.py | 14 | 12 function bodies remain identical. Restore the original two Save All functions as four current-compatible cases, retaining the two newer timer-disposal cases. |

## Concrete assertions restored

The additions contain **25 cases in eight functions**, with exact identities and multiplicities in scripts/canonical_shared_assistant_oracle.json.

| Current module | Existing cases | Added | Resulting declared cases |
| --- | ---: | ---: | ---: |
| tests/assistant/test_planning_handlers.py | 21 | 3 | 24 |
| tests/ui/test_evidence_draft_recovery.py | 15 | 4 | 19 |
| tests/assistant/test_translation.py | 63 | 4 | 67 |
| tests/e2e/test_google_translation_gateway.py | 48 | 14 | 62 |
| All four selectors | 147 | 25 | 172 |

The two saved-job resume cases distinguish a pending worker from a completed worker whose native completion callback is still queued. Both keep Preview and Run unavailable despite the old saved-complete summary. After the callback, they verify the actual three-step job's results, retained edition identities and source bytes without duplicate imports. A separate goal-mutation case requires a new Preview even when an approved goal changes without a text event.

The four Save All cases use the existing current recovery fixture and coordinator. They check the actual transaction, the exact saved/remaining summary, retained newer typing, and the busy, cancelled and changed-view boundaries. The changed-view case recognizes that the database may already be committed even though the old native view can no longer acknowledge the result.

The four translation cases reflect synthetic credential material through HTML or JSON-Unicode response encoding for text and language discovery. They require the previous language cache to remain byte-identical, an uncertain ledger without accepted wire/decoded hashes, no completed job receipt, and no secret in saved translation JSON or diagnostics.

The fourteen gateway cases retain all six original unsent-reservation mutations, all three nonretryable states, and five explicit HTTP statuses: 302, 400, 401, 429 and 500. They use the current exact request/profile fingerprint, typed approval and durable ledger. The controlled HTTP path verifies a single request, one credential read, connection closure, no automatic retry and the exact plaintext automatic-source payload with a header-only credential. No real network or paid service is contacted. Two literal Unicode character/call-count assertions are also restored to the existing current preview test without changing its case count.

## Intentional owner and API differences

The selected application exposes a separate model-proposal action. Its result remains an inert instruction until a new explicit Preview succeeds; the donor's combined model-selected Preview is not restored. Guided Preview remains on the current worker because validation may inspect local runtimes. The current late-result comparisons additionally include byte, file and time budgets and selected scope.

The removed donor create_assistant_dialog and windows_assistant_smoke.py are not recreated. The callback boundary is tested through the actual current show_assistant handler with deterministic worker and GUI queues. Native Windows qualification remains a separate gate.

The donor's persistent studio.render.ProductionService.close API is not part of the selected family. Current LocalProduction delegates each request to the existing render_project cancellation/resource owner; current dialog lifecycle tests verify cancellation, executor shutdown and refusal of late widget updates, while current production cancellation tests verify retained renderer state and resume. The disposition records this ownership difference instead of claiming identical cases.

Translation uses current NarrationDraft derivatives and the typed translation producer. Export retains the exact original text, separate derivative authority and file hashes, while additionally checking undo, no overwrite, forbidden private export and secret-free provenance. The current proven-unsent state is cancelled_before_send; it preserves the original exact-reservation condition without copying the alternate ledger API.

## Qualification and gate composition

No product source is changed by these assertion additions. The source review found no further concrete missing product behavior within these four families after the explicit restorations above. The scope does not supersede the other canonical-family reviews.

The new cases are **NOT_RUN** until an exact composed source and isolated-installed gate collects and executes them. Existing successful runs are tied to their frozen commits and do not cover these additions.

The assistant-transcription selected population increases from **684 to 691**, retaining its 30 native checks. The workflow-template population increases from **518 to 525**, retaining its 24 native checks. Each gains the three planning-handler cases and four translation-cache cases. Other component oracles must be updated only where they already select one of the modified modules; the full gate receives the exact 25-case delta. Frozen old gate refs and their observed results remain unchanged.

This review makes no claim about model/language quality, paid provider execution, physical audio or NVDA.
