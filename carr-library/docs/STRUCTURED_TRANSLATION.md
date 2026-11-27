# Document, EPUB chapter and batch translation

This component adds explicit scopes to the existing Google Basic v2 and
Advanced v3 translation service. The selected-text workspace, provider transport,
credentials, per-request ledger and existing job schemas remain the same.

## Native workflow

Tools → **Translate documents, chapters or batch** opens a separate native
workspace. The user checks explicit documents. Catalog pages keep checked
documents selected and list the resulting transfer order. A literal title
filter and the next-page action avoid silently limiting selection to the first
catalog page.

The three scopes are:

- **Whole document:** every extracted source page/segment of one exact sealed
  revision.
- **EPUB chapter:** one actual declared EPUB chapter selected from its contents.
  A PDF page or text segment is never relabelled as a chapter.
- **Document batch:** an explicit ordered set of up to 100 documents; the program
  does not expand a folder, collection or search query into a remote transfer.

The workspace shows exact extracted source text, original headings, selected
revision identities, per-document and per-block characters, expected request
counts, and allocated integer USD reservations. An optional owner-entered
contract price supplies estimates; no current price is assumed. Every item's
estimate must fit its allocation. Local reservations are not a provider bill or
a provider-enforced spending limit.

Fetching supported languages asks separate permission and sends no document
text. A second approval binds the exact document manifest, language pair,
provider profile, endpoint/model, optional existing Advanced glossary and
per-item reservation. Any change requires a new preview. The product supplies
no Google credentials and performs no unattended language/translation call.

## Source structure and durable execution

Selection uses the existing authorized full-source reader and sealed EPUB
contents, with retained-original authentication. Snapshots record the actual
workspace, edition, revision, source-byte digest, original section order,
source-text hashes, original headings and physical/logical locators. They do
not create citation approval. Generic sources retain real page/segment
boundaries; chapter titles and heading offsets come only from EPUB metadata.

Declared heading boundaries split the selected text into structural blocks.
Each nonblank block is an ordinary existing TranslationService job, using the
same chunking, gateway, native ledger and accepted-response receipts.
Whitespace-only blocks remain unchanged and never create billable requests.

The exact scoped plan is durable before child creation. An interrupted creation
remains visibly **preparing**; missing children are never silently recreated.
A queued, paused or attention plan must retain every approved child. Before
remote continuation, the current library, retained selection, provider
fingerprint and original ledger identity must still agree. Authorization is
checked again between accepted chunks.

Pause keeps accepted child output. Resume never repeats accepted requests.
Uncertain transport outcomes and paid output lost after a response remain
attention states requiring review; they never trigger automatic retry or
provider fallback. A second process cannot borrow another process's active
scope/child locks.

## Derived exports

A completed plan publishes a new directory outside the library containing:

| File | Meaning |
| --- | --- |
| original-selected.json | Exact selected extracted source and structural metadata |
| translation-structure.json | Ordered document/section/block derivative with source locators and receipts |
| translated.txt | Readable translated sections |
| bilingual.txt | Original/translated blocks paired in source order |
| bilingual.html | Inert escaped headings and bilingual blocks; no script or external resource loading |
| translation-provenance.json | Final complete-export marker, file hashes, source/profile/request bindings and receipt integrity |

Original retained files are never overwritten. Source heading metadata and
source offsets remain original metadata; the application does not fabricate
translated word offsets, printed page numbers or layout fidelity. A translated
passage is not source quotation authority. HTML text is escaped even when a
document contains script-shaped text.

The provenance file is written last. Cancellation or a write failure can leave
a partial new folder, but cannot publish a complete marker for incomplete
payloads. An existing destination is not reused.

## Backup and restore

Typed scope files use state/translation/scope-ID.json and the existing
translation backup producer. No second backup family or secret sidecar is
introduced. The shared validator checks source structure, exact approvals,
child identities, profile/ledger bindings, allocations and complete child
status. Existing snapshot/job locks also cover scope IDs.

Complete restored derivatives can be exported offline. Their archived response
receipts are labelled archived-unverified when the original live ledger is
unavailable. Saved remote permissions keep their original library and ledger
binding; restore does not reconnect an account or authorize more requests.
Actual credentials and provider-ledger state remain outside a complete backup.

## Explicit bounds

- At most 100 selected documents and 512 structural blocks per plan.
- At most two million selected source characters and 16 MiB selection JSON.
- At most eight million translated characters for a complete derived export.
- Scope checkpoints at most 20 MiB each, at most 100 scope plans.
- The existing combined translation snapshot limit remains 64 MiB, with the
  existing 1000 ordinary child/selected-text jobs and 100 language caches.

Oversized selections fail before whole-page text materialization when the
stored length already exceeds the source-character bound. The user chooses a
smaller explicit chapter/selection; the program does not truncate and send only
an undisclosed prefix.

## Qualification contract

scripts/translation_scope_oracles.json names the exact functions and case
multiplicities. Its independent Windows workflow requires:

- 215 existing Basic/Advanced selected-text and provider cases;
- 41 structured-source/service/backup cases and 12 additional native-controller
  cases;
- all **268 cases from source and all 268 from the isolated installed wheel**,
  with no skipped, failed, xfailed or xpassed case accepted;
- the unchanged selected-text Advanced v3 native probe;
- actual native document, EPUB chapter and two-document batch flows, with real
  widgets/focus, temporary Windows vault entries, synthetic approved replies,
  durable ledgers, immutable retained sources and verified bilingual exports.

The wheel is compared with the exact Git commit and every installed Python
file is compared with that wheel. Native probe module origins and hashes are
checked against the installed inventory. Raw JUnit, logs, provenance and
native JSON are retained on failure as well as success.

**Authoring status:** source reviewed; runtime tests and native qualification
are NOT_RUN until a matching workflow execution completes. The runner uses only
synthetic source text and synthetic transport replies. A successful synthetic
run does not qualify real Google service availability, paid-account billing,
linguistic quality, manual NVDA operation, or preservation of original visual
document layout. Those claims are not emitted by the gate.
