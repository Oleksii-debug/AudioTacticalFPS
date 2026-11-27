# Reviewed plain-text translation

The Translation workspace supports Google Cloud Translation **Basic v2**, with
NMT selected explicitly. It uses the existing AI/API profiles, Windows
Credential Manager, provider gateway and budget ledger. Opening the workspace,
loading saved jobs, previewing text and exporting completed derivatives do not
contact Google.

Advanced v3 with IAM/ADC, local translation models, formatted documents,
glossaries and other models are separate capabilities. This adapter does not
silently select one of them.

## User workflow

1. In **Settings → AI and API connections**, add **Google Cloud Translation
   Basic v2 (NMT)**. Name the account and enter its API key in the masked
   credential prompt. The endpoint and model are fixed by the adapter.
   Multiple named accounts remain supported.
2. Open **Tools → Translate selected text**. Paste only the text to transmit,
   or use a reading action that supplies an explicit selected passage.
3. Choose the account and explicitly approve **Fetch languages**. This sends
   the fixed supported-language query without the selected text. The returned
   NMT inventory is cached for that exact profile.
4. Choose the source language, or automatic detection, and a supported target.
   Enter a maximum USD reservation. An optional current contract rate per
   million characters produces an estimate; no current price is supplied or
   promised by the application.
5. Choose **Preview**. Review the account, endpoint, model, pair, character/call
   counts and budget, then choose **Translate / resume** and confirm this exact
   text transfer. Editing a reviewed value requires another preview.
6. Pause between chunks or close the dialog to signal cancellation. Saved jobs
   retain accepted chunks. Resume verifies the original account, library and
   ledger bindings before any new request.
7. Review the result and choose **Export derivative**. A new folder outside the
   private library holds the result, original and provenance.

Supported codes come from the explicit NMT discovery response. This is
provider-declared capability, not independent translation-quality qualification
or a promise that a particular number of languages has been tested.

## Permission, credentials and costs

The default remains LOCAL ONLY. A translation-specific JobApproval binds the
exact provider fingerprint, source chunks, language pair, maximum call count,
currency and integer-micro-USD reservation. Discovery uses a separate one-call,
zero-reservation approval and cannot borrow selected-text permission.

The only endpoint is https://translation.googleapis.com/language/translate/v2
and the model is nmt. Translation is a JSON POST with format=text. Discovery is
a GET to /languages?target=en&model=nmt. The API key is read from the existing
Windows vault and sent only in an X-goog-api-key header. It is not placed in a
URL, profile JSON, job, cache, receipt or export.

The existing transport retains HTTPS hostname/certificate checks, public-address
resolution and address pinning, bounded responses/time, no proxy, no redirect
and no automatic transport retry. Response validation occurs before its charged
receipt is accepted. Malformed, wrong-model, command-shaped or credential-
reflecting responses cannot become a saved translation.

The local budget limits reservations, not Google's eventual bill. Actual billed
cost remains unknown. A durable intent and gateway reservation precede each
request. Only cancellation known to precede transport entry can reuse the exact
original operation, request, approval and amount. Reserved, uncertain and
completed operations are not repeated.

If the provider completed but output publication failed, resume stops instead
of buying that response again. **Charge history** shows local states and hashes.
Review the provider account before considering a separately approved replacement.

## Source and derivative exports

| File | Meaning |
| --- | --- |
| original.txt | Exact immutable selected text, including line endings. |
| translated.txt | Accepted plain-text machine translation. |
| bilingual.txt | Original/translated chunks, labelled as a derivative. |
| translation.narration.json | Existing NarrationDraft format: source hash, reversible edit and approximate anchors for changed spans. |
| translation-provenance.json | Source/response hashes, pair, adapter/model and file hashes; written last as the complete-export marker. |

The provenance has quotation_authority=false. A translation is a reading
derivative, not an original-source quotation. Credential references and approval
payloads are not exported. The service never rewrites an original book/source.

A cancelled or failed export can leave a partial new folder without its final
provenance marker. Original files and previous export folders remain unchanged.

## Typed state and restore contract

Translation never writes records into the assistant's tool-plan JobStore.

- state/translation/<32hex>.json: carr-translation-job/1.
- state/translation/languages-<profileid>.json:
  carr-translation-languages/1.
- Existing api-connections.json and api-operations.sqlite remain the shared
  connection-reference and reservation authorities.

assistant.translation_state exposes is_state_path, state_limit, state_paths,
validate_state_document, validate_state_tree and snapshot_guard. Publication
takes the application writer followed by the translation state guard. Backup
already holds the application writer, then snapshot_guard takes the state and
sorted actual job guards without reacquiring the writer. Only a live same-
process/thread owner may lend its job lock.

Limits are 1,000 jobs, 100 language caches, 64 MiB per job, 1 MiB per cache and
64 MiB for the complete durable inventory. Unknown files, links, duplicate JSON
fields, invalid receipts, changed source hashes, mismatched approvals and
oversized inventories fail validation.

Restore preserves approval bytes and their original library/profile/ledger
bindings. It does not rebind a saved remote permission. Completed restored
derivatives can be inspected/exported offline; resuming remote work after a
move, restore or connection change requires a separately reviewed job.

## Desktop and packaging integration

The main desktop installs install_translation_actions(frame) after its shared
Tools menu, before the existing productivity/keyboard collector. No competing
command registry is installed.
Reading actions can call show_translation(frame, initial_text=selected_text,
title=source_title). The dialog uses the shared frame worker, cancellation and
progress_tracker/progress_outcome contract. Paused jobs retain paused progress;
only an accepted complete result can finish verified progress at 100 percent. Existing AI/API settings route Google profiles into it.

New modules reside in the existing assistant and ui packages. No new dependency
is required. Full backup connects the typed translation_state adapter to its
existing sidecar inventory without increasing its global byte/file ceilings.
The translation guard remains held while capturing the shared provider ledger.
Restore checks the archive read-only and never creates translation lock files.

## Verification

Focused synthetic suites:

- tests/e2e/test_google_translation_gateway.py
- tests/assistant/test_translation.py
- tests/ui/test_translation_dialog.py
- tests/storage/test_backup_translation.py

They cover exact authority/header routing, LOCAL ONLY, unchanged existing
request hashes, reflected keys, cancellation/uncertain charges, restart, lost
paid output, changed bindings, immutable exports, typed backup validation,
real native locks and late UI callbacks. The real library-backup tests include
queued, paused and complete translations, disconnected restored profiles,
known-unsent reservations, archive no-write checks, active-job rejection and
shared aggregate byte/file limits. Existing tests/e2e/test_provider_gateway.py,
tests/storage/test_backup_provider_state.py and tests/ui/test_desktop_handlers.py
remain required regression gates. No Google request or real key is used.

On Windows, after installing the wheel:

~~~powershell
& "$bundle\.venv\Scripts\python.exe" -I scripts/windows_translation_smoke.py --bundle $bundle --output translation-native.json
~~~

The native script verifies installed modules against this checkout and drives
actual wx controls through DesktopFrame.run_job, with a bounded failure watchdog,
through language
discovery, preview, approval, result display and real file export. Provider/vault
responses and confirmation/folder-selection decisions are explicitly synthetic
boundaries. It does not claim real Google calls, actual vault acceptance or NVDA
coverage.

**This port was prepared during executor unavailability. Its new source suites
and native Windows script were NOT_RUN at this source checkpoint.** Run the
named gates on the composed tree before claiming acceptance.

## Primary provider references

- [Basic v2 translate](https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate)
- [Basic v2 languages](https://docs.cloud.google.com/translate/docs/reference/rest/v2/languages)
- [REST API-key headers](https://docs.cloud.google.com/docs/authentication/rest#api-keys)
- [Translation authentication](https://docs.cloud.google.com/translate/docs/authentication)

These establish API shape and authentication, not this build's acceptance,
current account price or translation quality.


## Reuse and integration checkpoint

This slice reuses the typed translation service/state and synthetic boundary
tests from commit 9fe6273d3e814b5f16abdaa67091c8befb20ccb6. PR99 was rechecked at
6c7afdccf18e010c8deb16a61f22b367df19c339 before adaptation; the intervening
translation change connected that dialog to its branch-specific keyboard
registry. This application keeps its existing Tools menu and productivity
collector instead.

The provider adaptation preserves the existing strict profile decoder, portable
SQLite validation, snapshot API, absolute record bounds and archive sanitation.
A known-unsent reservation can be reused only with its original operation ID,
approval digest, exact request and amount; all actual/uncertain requests remain
charged in local history. Translation independently rejects unresolved pending
intents. No second provider ledger, credential store or assistant tool executor
is introduced.

Receiving source and installed-wheel gates must include the four focused suites
listed above. The installed native command requires both --bundle and --output.
The final bundle must also ship this guide. Tests and native acceptance remain
NOT_RUN until the exact composed tree is executed; this record does not claim
paid-service, real-vault, translation-quality or manual NVDA acceptance.
