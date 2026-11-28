# Canonical 135-language catalog and qualification matrix

`carr_library.language_qualification` derives the user-approved **135 unique
entries** directly from the immutable pinned source. It does not maintain a
second editable list of languages or turn locale/voice counts into languages.

## Source identity

Both source documents are copied byte-for-byte from the approved public commit
[`a1c73a5747bc86eb11f0c037120442e0c2593e91`](https://github.com/Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension/tree/a1c73a5747bc86eb11f0c037120442e0c2593e91/dordiai-voices).
They are inert source evidence, not downloaded runtime code or models. The
loader checks both SHA256 and the exact Git blob before parsing on every load.

| Source | Git blob | SHA256 |
| --- | --- | --- |
| `SELECTED_LANGUAGES_UK.md` | `caea8c5a8a5698971d75a565cba9a5a9c00b411f` | `bdfcdd8f03e52df649e74091f4481769a7e55479f76b9334b73788dd4fd83cbb` |
| `LANGUAGE_COVERAGE_DEV2_UK.md` | `8081b24c7c33eff578e7896052d4221cfff8c684` | `696dd21a89b30e3ef09c1b581aca4432386c2aea8e8073bc206e29bf41b01067` |

The first document is authoritative for selection and geography. The second is
an **upstream inventory report**, recording 125 entries with registered nonempty
resources, 10 missing, 124 full-entry resource mappings and one partial group
mapping. It establishes **zero Windows/NVDA acoustic tests**. None of these
numbers is evidence that those resources are installed or work in this CARR
installation. Extra upstream languages do not add selected entries.

Regional counts are 37 Europe; 4 North/Central America and Caribbean; 8 South
America; 54 Asia; 39 Africa; 9 Australia/Oceania. Their total is 151 memberships
because shared languages overlap. The unique selected count stays 135.

## Stable identities and ambiguities

Entries retain their full Ukrainian labels, original order, source row text,
source line and every regional/country association. IDs are
`selected-` plus the first 24 hex characters of SHA256 of the NFC-normalized
canonical label. They are independent of array position and geographic repeats.
An intentional future rename needs a reviewed identity migration; there is no
fuzzy name merge.

The parser uses the **last** em-dash separator in each selection row, preserving
the full names of standard Mandarin and the Filipino standard. Persian/Dari
remains one selected entry. Northern and Southern Ndebele remain separate.
Tamazight, Comorian, Quechua, Fula and Kurdish retain unresolved group-standard
caveats from the audit. A Kabyle resource can only be a partial candidate for the
chosen Tamazight group. Norwegian `nb` and optional `nn` are not extra selected
languages. National accents remain separate from language and voice identity.

The canonical source supplies **no canonical locale or voice ID**; both fields
remain `None`. This implementation does not invent 135 ISO/BCP47 mappings.

## Read-only API

```python
from carr_library.language_qualification import (
    load_catalog, build_capability_matrix, DeclaredLanguageBinding,
)

catalog = load_catalog()
assert len(catalog.entries) == 135
ukrainian = catalog.search("Українська")[0]
europe = catalog.search(region="europe")
matrix = build_capability_matrix(catalog=catalog)
print(matrix.summary())
```

`LanguageCatalog`, entries, regional associations and capability records are
frozen dataclasses. `to_dict()` returns an independent generated report, not a
source that the loader will accept back. There is no catalog editor, model
download, helper installation or acoustic execution in this package.

For already enumerated local voices, the caller may supply **explicit reviewed
tag associations**, retaining the review evidence. Use the existing voice
registry; the matrix does not perform discovery itself:

```python
english = catalog.search("Англійська")[0]
bindings = [DeclaredLanguageBinding(
    english.id, "en", "The selected installed adapter explicitly declares en; pronunciation remains untested."
)]
matrix = build_capability_matrix(
    catalog=catalog,
    voices=already_loaded_voice_registry.voices(),
    bindings=bindings,
)
```

Matching is exact after case normalization. A binding to `en` does not match
`en-US`; scripts, regions and variants are never dropped. Legacy underscores
are normalized only on an adapter's declaration. Similar voice names do not
establish language identity. One exact tag cannot silently map to two selected
entries. Group-standard matches remain partial. Unmatched voices are separate
inventory and do not increase coverage or selected-language count.

`canonical_language_tag()` validates BCP47 **structure**, not registration in
IANA, CLDR, ICU or a model's actual language list. Grandfathered and ambiguous
tags need a separately reviewed mapping. No semantic locale/voice equivalence
is inferred from structural validity.

## Qualification states

Every entry has 22 named capability rows: UI/menu/help translation, Unicode
text, RTL and vertical layout, OCR, TTS, phonemization, stress, tone, punctuation,
segmentation, lexical and semantic retrieval, translation, accessible reading,
keyboard input, number/date rules, model availability and voice availability.

Initially TTS/voice availability have no confirmed local binding, and other
capabilities are untested. An exact declared local voice match becomes a
**candidate**, or **partial** for unresolved groups. A declared phoneme or
punctuation control is only a candidate for that control. It does not verify
pronunciation, stress, tone, symbols, latency, naturalness, model version,
licensing compatibility, code switching or native accessibility. The default
`verified` filter remains empty. Explicitly selected recorded evidence can show
**Verified for the recorded configuration** only after a separate local human
assessment of its exact observation. This is a recorded assessment, not proof
of authenticated reviewer identity, genuine independence, automatic execution
or current-device qualification.

The summary keeps selected entries, distinct matched voices, candidate entries,
upstream resource counts and qualified entries separate. Default qualification
count is **zero**. In an explicitly selected app/environment, an entry counts
only when all 22 capabilities have accepted complete PASS observations for the
same exact language tag, scope and coverage. Each row still identifies its own
engine and pack. No real language-quality acceptance is shipped with this
software; synthetic regression fixtures do not qualify any of the 135 entries.
No source-language substitution or silent fallback is performed.

## Native view

`install_language_actions(frame)` from `ui.language_matrix` adds the existing
native language matrix and its passive evidence workflow. Search, region and capability/status filters
retain canonical order. A separate installed-voice inventory is refreshed only
on the user's explicit action, using the existing configured local registry and
the frame's background-job mechanism. Refresh does not assign voice tags to
selected languages or claim acoustic acceptance. Existing explicitly enabled
local voice adapters may run their normal bounded enumeration probes; no new
upstream runtime is fetched or executed and no text is synthesized.

Package `carr_library.language_qualification` with `data/*.md`. Repository
`.gitattributes` preserves exact bytes across Windows/Linux checkout. Tests
verify source hashes, all 135 identities, all geographic rows, overlap, group
ambiguity, immutable reports, search, exact tag matching and zero false
qualification. Native Windows/NVDA listening and language-reviewer acceptance
remain separate required evidence.

## Recording evidence without granting execution authority

The **Recorded qualification evidence** section uses the current library's
existing private state and worker. Opening it loads records without creating
state, enumerating engines, fetching models or running tests. Choose one exact
**Recorded application and environment** before viewing reviewed statuses.
Selecting a context only filters recorded data; it does not inspect this
computer or assert that the named engine or pack is installed.

1. Choose **Import passive observation...** and one local JSON observation.
   The default-No confirmation displays the canonical language, capability,
   reported result and complete observation/file digests. Import stores a claim
   only. A claim with an asserted PASS remains a candidate until reviewed.
2. Select the saved observation and inspect its complete configuration, test
   input/result, hashes and history. The detail pane shows the latest 32 reviews;
   the report preserves the complete bounded history.
3. Choose **Record human assessment...**. Record reviewer identity, provenance
   reference, role, assessment and limitations. Confirm that the decision binds
   the complete displayed observation. Acceptance also requires the explicit
   independent-assessment acknowledgement and a reviewer identity different
   from the executor after Unicode compatibility/case normalization.
4. Record **Accept**, **Reject** or **Revoke**. The decision binds the complete
   canonical observation digest and receives a local UTC time. Reject/revoke
   remain in history; importing another observation cannot silently replace the
   selected reviewed record. A later explicit review can select another exact
   observation for that capability and context.
5. Choose **Export evidence report...** to create a new file outside the
   library. Reports retain the observation/review history and selected matrix,
   and are deliberately not accepted as observation or approval imports.

Reviewer names, roles, provenance references and independence acknowledgements
are **recorded human assertions**. Comparing two strings does not authenticate
two people or establish real independence. The application checks the bounded
record and its internal consistency; it does not verify an external person,
laboratory, receipt, model, acoustic result or document merely because its
digest is supplied. A real qualification still needs the actual relevant
experiment and a competent independent reviewer.

### Exact observation schema and API

Use the frozen types in
[language_qualification/evidence.py](../src/carr_library/language_qualification/evidence.py).
One imported document has schema `carr-language-observation/1` and exactly:

| Field | Required meaning |
| --- | --- |
| `catalog_sha256` | Exact pinned canonical selection digest, not another language list. |
| `language_id`, `language_tag`, `language_scope` | Existing canonical entry ID, structurally valid canonical tag spelling, and the exact standard/variety being assessed. |
| `coverage` | `full-selected-entry` or `partial-variety`; a partial variety cannot qualify the whole selected entry. |
| `capability` | One of the existing 22 capability IDs. |
| `context` | Exact application name/version/SHA-256, platform, architecture, environment description and environment-record SHA-256. |
| `engine`, `pack` | Name/version/SHA-256, or explicit null identity fields with a reason that the artifact is not applicable. The application itself cannot be inapplicable. |
| `test` | Test ID/version, executed versus declaration, executor identity/provenance/role, UTC execution time, exact bounded input text and its matching digest, expected result, outcome, result description and result-artifact digest. |

Artifact identity objects have exactly `name`, `version`, `sha256` and
`not_applicable_reason`. Contexts have exactly `application`, `platform`,
`architecture`, `environment` and `environment_sha256`. Person provenance has
exactly `identity`, `source` and `role`. Test records have exactly `identity`,
`version`, `kind`, `executor`, `performed_utc`, `input_text`, `input_sha256`,
`expected_result`, `outcome`, `result` and `result_sha256`.

Digests are lowercase SHA-256. The input digest is checked against the retained
UTF-8 input itself. The result-artifact and configuration digests remain
passive references for the reviewer; the application does not open a path or
download a URL to resolve them. A declaration must use `not_run`, with no
execution time or completed result digest. Executed outcomes distinguish
`pass`, `partial`, `fail` and `not_run`. Approval/status/review fields are not
permitted in imports; generated state and reports use different closed schemas.

For API consumers that already have a genuinely observed, typed record:

~~~python
from carr_library.language_qualification.evidence import EvidenceStore, import_observation
from carr_library.language_qualification import build_capability_matrix

store = EvidenceStore(current_library_root, guard=current_library_guard)
snapshot, token = store.load(cancelled=cancelled)
observation, original_file_digest = import_observation(selected_json, cancelled=cancelled)
snapshot, token = store.add(observation, expected_token=token, cancelled=cancelled)

# Explicitly selecting the record's context still yields a candidate until a
# separate deliberate local review has been recorded.
matrix = build_capability_matrix(
    evidence=snapshot, recorded_context=observation.context.fingerprint)
~~~

A store review uses `store.review(observation.fingerprint, reviewer,
decision=..., independent_assessment=..., statement=...,
confirmation=decision.upper() + " " + observation.fingerprint,
expected_token=token, cancelled=cancelled)`. The native dialog constructs that
exact binding from the displayed immutable record after explicit confirmation;
it does not ask users to retype a 64-character hash.

An accepted full PASS can become `verified` only within that recorded context
and language scope. Accepted partial coverage/results remain `partial`;
accepted failed tests remain `failed`; accepted unexecuted declarations remain
`untested`. Rejected and revoked assessments remain visibly distinct.
Unreviewed claims and restored positive historical reviews cannot verify rows.

### Shared state, conflicts and backup

The sole producer file is
`state/language-qualification/evidence.json`. It uses the existing bounded
snapshot reader, local ordinary-file checks, state lock, atomic writer
and exact-byte compare-before-save token. Every captured directory identity is
checked again. Changed bytes with an unchanged revision still conflict. The
last callback is followed by a fresh token/scope check before publication;
cancellation does not erase a completed save. A visible late durability error
returns a committed-state receipt and tells the user to reload instead of
repeating the change.

Each observation is limited to 64 KiB; the collection is limited to 32 MiB,
8,192 distinct observations and 16,384 reviews. Unknown fields, duplicate JSON
keys, non-finite values, unrecognized catalog/capability IDs, mismatched input
digests, invalid review references, links and reserved namespace aliases are
refused. These bounds allow the complete 135-by-22 capability matrix without
introducing an editable catalog.

Both existing library and state backup routes validate the same typed
collection and exact staged hashes. Restore preserves every observation,
review, failed result and revocation as passive history. It advances a
`dormant_through` boundary over all prior reviews; a positive historical review
must receive an explicit new local assessment before it can be shown as
verified again. Rejections and revocations remain visible. Restore never
reopens an engine, pack, test input or provenance reference.

This closes the software path for recording and reviewing evidence. It does
not supply actual 135-language qualification, authenticated reviewer identity,
physical playback, native screen-reader listening, model rights or acoustic
quality. Those remain separate evidence requirements. The finite requested
reading/synthesis workflow checklist is maintained in
[REQUESTED_READER_WORKFLOWS.md](REQUESTED_READER_WORKFLOWS.md).
