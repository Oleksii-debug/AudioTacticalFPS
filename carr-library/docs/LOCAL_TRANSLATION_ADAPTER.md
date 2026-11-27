# Optional local translation adapter

The same Translation workspace and CLI now accept an explicitly configured
LibreTranslate-compatible loopback origin. This is a client protocol adapter;
it neither installs nor starts a server, downloads a model, discovers localhost
services, attests model files or establishes translation quality.

The reviewed narrow protocol is GET /languages with exact directional targets
and POST /translate with a bounded q, source, target and format=text. The current
official API is documented at:

- https://docs.libretranslate.com/api/operations/languages/
- https://docs.libretranslate.com/api/operations/translate/

The adapter does not expose optional alternatives, file upload, glossary or a
cloud fallback. New server capabilities require separate explicit review.
The pure request/response mapping is reconciled from this repository's reviewed
PR173 source ed74d168c81a21e68bbe1673a729181dfef96ee4. No upstream engine code or
weights are copied.

## Configure and run

Open AI/API connections, choose Local LibreTranslate, enter the exact loopback
IP origin and your reviewed runtime label. A label is not cryptographic proof of
a runtime. Credentials are refused; a service requiring API keys is outside
this bounded adapter. Saving or opening the profile sends nothing.

Open Translation, explicitly fetch the selected origin's language inventory,
choose a declared direction, preview, then approve the exact selected source.
The local path uses zero remote reservation and LOCAL ONLY with explicit
loopback permission. Existing source selection, immutable sections, accepted
receipts, cancellation, partial resume, jobs, CLI and exports are shared with
the current translation implementation.

A local /languages declaration lists directional pairs. It is not evidence for
all-to-all translation, 135 languages or actual quality. Before every new local
chunk, the exact saved inventory hash and selected pair are checked. Automatic
detection must return one finite bounded receipt and a direction declared in
that captured inventory. A changed/missing inventory blocks new work while
retaining previously accepted output. No uncertain attempt is replayed.

Full backups preserve typed local jobs and cached inventory as inert data.
They omit active connection profiles and the provider ledger. Completed
derivatives export offline after restore; queued work needs a newly reviewed
current job. The export explicitly records that local runtime identity was not
attested by the API.

## Evidence boundaries

The added strict suite uses a real TCP loopback HTTP server with synthetic
responses. It checks exact route/body/headers, no secret access, directional
pairs, literal entities, no redirects/fallback/retry, cancellation, exact CLI
approval and complete backup/restore. It does not execute a translator model.

The existing installed native wx translation probe has a separate --local mode.
Its approvals and responses are injected synthetic fixtures; actual controls,
worker completion and durable files are exercised. Neither source review nor
these authored probes are execution evidence until exact-head CI passes.
Manual NVDA, model identity, server egress policy, licensing and real language
quality remain separate qualification.


## Execution admission

The real HTTP contract suite lives in `tests/loopback/test_translation_local_adapter.py` and is executed only by the existing numeric-loopback CI policy. Recursive offline assistant suites continue to forbid all sockets. Both modes retain separate evidence. The injected native UI probe does not claim engine inference.
