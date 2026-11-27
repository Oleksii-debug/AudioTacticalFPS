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

The companion `evidence/language_coverage.json` at that same upstream commit
reports an older 83-resource inventory. It is not imported or silently combined
with the selected Markdown audit. The historical 125 figure in this view is
explicitly attributed to the exact required Markdown document; current local
availability is obtained separately from the existing installed adapters.

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
licensing compatibility, code switching or native accessibility. `verified`
filters intentionally return no rows until a separately implemented evidence
workflow records actual independent acceptance.

The summary keeps selected entries, distinct matched voices, candidate entries,
upstream resource counts and qualified entries separate. Current qualification
count is **zero**. This is an honest foundation for the final expansion stage,
not acceptance of 135 working voices. No source-language substitution or silent
fallback is performed.

## Native view

`install_language_actions(frame)` from `ui.language_matrix` adds a read-only
native language matrix command, registered as `language.capabilities` in the
existing shared command registry. It reuses the Audio menu when available;
the command palette and keyboard preferences can assign a shortcut. Search,
region and capability/status filters
retain canonical order. A separate installed-voice inventory is refreshed only
on the user's explicit action, using the existing configured local registry and
the frame's background-job mechanism. Refresh does not assign voice tags to
selected languages or claim acoustic acceptance. Existing explicitly enabled
local voice adapters may run their normal bounded enumeration probes; no new
upstream runtime is fetched or executed and no text is synthesized.

This host reuses the canonical implementation from AudioTacticalFPS commit
`33dcc42edd6781b9aeb02dcaaf016b386bcf37b6`, adapting its inventory boundary to
the accepted `audio.engines.Voice` and `VoiceRegistry`. A voice's complete
language tuple, availability, declared controls and adapter revision remain
distinct. An unavailable voice cannot become a candidate. One multilingual
voice retains one identity even when multiple exact declarations match. There
is no dependency on the competing `speech.runtime` registry.

The separate OCR tab reuses the existing `ui.ocr_selection` inventory worker
and `extract.model_catalog` labels. It preserves text-model, script and auxiliary
identities and does not map an OCR identifier to a selected language by name or
guess. Enumeration is not a recognition test; each actual import verifies its
selected model bytes through the existing extraction pipeline. Explicitly
missing or broken runtimes fail visibly without a substitution or download.

Both scans use the current frame worker. Results belong to the original
library object, root path/inode, live dialog and request generation. Cancellation
is checked again before publishing; closing or destroying the view invalidates
queued callbacks. A scan owns its cancellation token, so closing an old view
cannot cancel a later unrelated operation. Existing voice probes have their own
bounded timeouts; the current whole-registry API cannot interrupt a probe in
progress. Cancel therefore discards its result after that probe returns. OCR
uses its existing active process-cancellation support.

Package `carr_library.language_qualification` with `data/*.md`. Repository
`.gitattributes` preserves exact bytes across Windows/Linux checkout. Tests
verify source hashes, all 135 identities, all geographic rows, overlap, group
ambiguity, immutable reports, search, exact tag matching and zero false
qualification. Native Windows/NVDA listening and language-reviewer acceptance
remain separate required evidence.
