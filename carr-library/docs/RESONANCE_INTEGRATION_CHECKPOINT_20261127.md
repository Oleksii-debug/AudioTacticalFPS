# Resonance integration checkpoint — 27 November 2026

This is a development checkpoint on PR #99, not a released installer or a declaration that the full V3 platform is complete.

## Authority and architecture

The current canonical base observed during integration is `33dcc42edd6781b9aeb02dcaaf016b386bcf37b6` on `carr-library`. The updated scope is `docs/UNIVERSAL_SCOPE_V3_FINALIZATION.md`. The supplied Whisper worker is an existing required input, not a missing optional future feature.

This checkpoint retains PR #99's existing audio services, QuickJS CSL processor, provider ledger, assistant scope, wx controls and library storage. It reuses compatible canonical fixes and adds narrowly adapted features. Canonical PR #103 uses different speech/studio/academic modules; this checkpoint must be reconciled with that architecture before any canonical merge. Do not transplant both stacks or mark canonical changes as merged merely by adding a parent.

## Integrated source changes awaiting their exact-commit Windows run

- Pending import backups preserve queued sources, staged inputs, ready reviews and checkpoints, including the regression where both the original and first prepared file disappear before the first worker runs. Restore remains cancellation-aware and publishes only a new destination.
- Current and historical backup shapes dispatch by their exact contracts. Unmapped canonical historical state is retained inert with its original manifest and payload hashes instead of silently discarded. The compatible producer preserves those retained bytes on the next backup.
- Translation reuses the existing profile store, OS-vault boundary and budget ledger. Google Basic v2 plain-text requests require an exact text/pair/account/budget approval. Durable receipts and unresolved-charge state prevent a replay from borrowing old permission. Native controls, review, export and command registration use the shared desktop.
- The local LLM proposes a bounded plan inside the caller's existing scope. It cannot submit a job or execute the proposed plan; a separate normal Preview and approval are still required. No live LLM inference is claimed.
- Editable DOCX citations reuse the existing CSL engine and preserve native endnotes, stable citation occurrence context and a bounded embedded metadata snapshot. A checksum does not certify visible Word edits or quotations. This checkpoint's export and readback are not automatic Word field updates.
- Bibliography export binds the destination before rendering and after the user's explicit replacement decision. A competing create/change is rejected. Native Windows create-only publication uses the existing reviewed platform fix.
- Managed OCR accepts an equivalent verified directory path without losing model hash authority. Its mutation regression changes a real unread model tail.
- Native portable-sync confirmation locates actual Windows controls, including bounded UI Automation for DirectUI TaskDialogs; it does not force a dialog return value.
- The pure delivery contract defines reviewable cadence and retry state. It does not supply a private entitlement verifier, issuer key, payment integration, updater, signature, network request or release authorization.
- The public title is Resonance. Existing app-data paths remain stable. Background completion leaves focus with the callback that owns the resulting controls.
- Required installed-package tests cover the new components as well as the existing source suites. Real OCR source regressions receive the exact prepared native OCR runtime in their own test process.

## Evidence from the preceding published checkpoint

The preceding published commit is `4ee7606428d77a854b3c9b8da22981d815e83f7b`.

| Check | Observed result at this checkpoint |
| --- | --- |
| Bibliography workflow, run 45920086417 | Completed successfully, including the wheel package inventory regression. |
| Main Windows workflow, run 45920086425 | Failed: one Windows device-name test assertion and five required suites with missing real-OCR skips. Source runtime discovery and the device test are corrected in this checkpoint; their new results are pending. |
| Keyboard job 140248480514 | Completed successfully on the preceding commit. |
| Portable sync, run 45920086491 | Source 246 passed, installed-package 151 passed; native TaskDialog selection failed. The new native selector still requires its exact-commit run. |
| Audio, run 45920086445 | Native SAPI/controls/vault and player steps passed at observation; the whole workflow was still running. Do not report the whole workflow as passed. |

Tests added or changed in this checkpoint were not executed locally because the executor was unavailable. Their source review is not an execution result. The Windows workflow must establish pass/fail for the published commit; historical counts must not be reused as current evidence.

## Release gates still distinct from source implementation

A final full-platform installer has not been created by this checkpoint. Required remaining evidence includes canonical architecture reconciliation, the current exact-commit Windows/full-bundle run, private integration and actual inference of the supplied Whisper worker, owner-provided voice/cloning engines and model qualifications, real Windows 11/NVDA and listening acceptance, and signed private delivery/update/entitlement inputs where required by the technical project.

The 135-language catalogue is a qualification matrix, not 135 working or quality-approved voices. Native UI probes and synthetic provider tests do not prove NVDA behavior, live Google account charging, actual local LLM inference or audio quality.

Private inputs, source recordings, model weights and installers must stay out of this public repository and public workflow artifacts. Only source changes and non-sensitive synthetic verification evidence belong here.
