# Current optional local translation adapter

This component implements the optional local translation boundary required by V3.
It uses the existing connection profiles, provider ledger, translation checkpoints,
scoped source capture, export publisher and complete backup validator.

## Available operations

An explicitly selected LibreTranslate origin on a numeric loopback address exposes
only GET /languages and POST /translate with plain-text q/source/target/format.
No server, runtime, model, downloader or discovery process is bundled or launched.
API keys, OAuth tokens, remote origins, path prefixes and glossary fallback are
refused. A local runtime label is a user declaration, not a model-file attestation.

The source-direction inventory is fetched only after an explicit separate approval.
A zero-cost LOCAL_ONLY approval binds the exact profile, text chunks, language pair
and call count. Existing Google Basic and Advanced approvals retain their original
payload fingerprints and schemas. Before each new local chunk, the cached directional
inventory must still match the job. Accepted automatic detection must declare a
supported source-to-target direction.

Selected text uses carr-translation-local-job/1. Current document, EPUB chapter and
batch selections remain carr-translation-scope/1 and reference these ordinary child
jobs. The separate local schema avoids confusing this implementation with another
structured job/4 proposal. All current source locators, retained originals, decoded
ledger binding, whitespace assembly and no-clobber exports remain enforced.
Language caches use carr-translation-languages/3.

Accepted chunks survive pause/restart. Uncertain requests and missing accepted
results never trigger automatic retransmission. Complete backup retains typed
local jobs, scoped plans and inventories, while profiles, keys, live ledger
identity and permission to contact an old endpoint are not restored. Completed
derivatives remain readable/exportable as archived-unverified receipts.

## User access

AI/API connections can save a credential-free Local LibreTranslate profile.
Selected-text and document/chapter/batch translation dialogs list it alongside the
existing Google profiles. The local form disables price and remote reservation;
an exact preview and a separate explicit send approval are still required.

The installed research-library-translate command and
python -m carr_library.translation_cli expose profiles, explicit languages refresh,
preview, queue, inspect, run, read and export. Queue accepts a current profile
fingerprint and preview digest. Run requires the exact saved checkpoint digest and
--allow-network, including for local TCP. These commands use the same services as
the native interface. Scoped selectors accept an existing workspace and explicit
edition identifiers, never a new unreviewed import.

Example sequence for a profile already created in the application:

    research-library-translate --data-dir C:\Research profiles
    research-library-translate --data-dir C:\Research languages --profile local --refresh --allow-network --approve-profile REVIEWED_FINGERPRINT
    research-library-translate --data-dir C:\Research preview --profile local --input C:\Text\source.txt --source en --target uk --budget-micros 0

Use the returned digest with queue; inspect the saved job before an explicit run.
For retained documents replace --input with --workspace ID --edition ID.
Use --scope-mode chapter --chapter N for an EPUB chapter, or --scope-mode batch
with repeated --edition for an ordered batch. Run/inspect/export require --scoped
for a scoped plan.

## Evidence boundary

The protected acceptance gate retains all 268 existing source and 268 installed
translation cases, adds 38 real numeric-loopback HTTP cases in each population,
and requires all four existing native flows plus four local native flows. The
native local probes use synthetic replies and actual current Windows widgets,
jobs, exports and cleanup. The loopback fixtures exercise actual HTTP requests,
uncertainty, directional discovery, immutable sources, decoded receipt tampering,
passive backup, current controllers and CLI.

Execution status is recorded separately by the exact-commit Actions receipts.
This document does not claim those gates have passed. No supplied real local
engine or language model has been qualified here; linguistic quality, runtime
identity, server egress and manual NVDA remain unqualified.

Protocol references reviewed against the public upstream API:
- https://docs.libretranslate.com/api/operations/languages/
- https://docs.libretranslate.com/api/operations/translate/
- Protocol donor: LibreTranslate v1.10.0, commit 29aea44cc80100401497fb62db813a3f3a504e84.
