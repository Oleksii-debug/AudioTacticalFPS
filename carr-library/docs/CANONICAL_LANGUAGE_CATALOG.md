# Canonical language inventory and dordiai integration boundary

The selected scope contains **135 unique catalog entries**. Its authoritative
source is [SELECTED_LANGUAGES_UK.md at the pinned upstream revision](https://github.com/Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension/blob/a1c73a5747bc86eb11f0c037120442e0c2593e91/dordiai-voices/SELECTED_LANGUAGES_UK.md).
This selection is distinct from the installed resources, the languages a
particular voice supports, and acoustic or accessibility acceptance.

The new `carr_library.language_catalog` package provides a deterministic,
validated projection of that selection and the explicitly requested DEV2
resource audit. It performs no model loading, network request, acquisition,
voice assignment or speech generation. Full language qualification remains
the final expansion stage after core features and secure delivery.
ASR is explicitly deferred by the finalization specification and is excluded
from the active capability defaults and acceptance scope.

## Exact sources

All four inputs belong to repository
`Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension`, commit
`a1c73a5747bc86eb11f0c037120442e0c2593e91`. Input validation checks the original
byte size, Git blob identity and SHA256 before parsing.

| Source under `dordiai-voices/` | Git blob SHA1 | Purpose |
| --- | --- | --- |
| `SELECTED_LANGUAGES_UK.md` | `caea8c5a8a5698971d75a565cba9a5a9c00b411f` | Authoritative names, selection and geography |
| `selected_languages.json` | `8fc686ec2aeb839c6e7cac81f87384d9e8787964` | Existing upstream stable IDs and regional order; checked against the Markdown |
| `LANGUAGE_COVERAGE_DEV2_UK.md` | `8081b24c7c33eff578e7896052d4221cfff8c684` | Recorded audit scope and limitations |
| `evidence/language_coverage_dev2.json` | `6d96cd6704b6acc3d42e0624949656f1f8a59e8d` | Per-entry resource evidence for that audit |

`SOURCES` records the complete SHA256 identities. Generated `UPSTREAM.json`
is a derived artifact, not an independently maintained language list. Its
canonical JSON SHA256 is
`a551edf490b65585943e755707d68dc7eff3e8468d0598c948f2f297df806a9d`.
Changing a name, geographic membership, audit result or source identity makes
validation fail. A future update needs a reviewed upstream revision and a
new deterministic derivation, with its source and output identities changed
together. The original upstream documents and code are not duplicated here.

## What the recorded audit establishes

The [DEV2 report](https://github.com/Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension/blob/a1c73a5747bc86eb11f0c037120442e0c2593e91/dordiai-voices/LANGUAGE_COVERAGE_DEV2_UK.md)
records 125 entries with registered nonempty resources, including 124 full
selected-entry resource mappings and one partial group variant. Ten entries
are not registered. The audit establishes **zero Windows acoustic tests**;
it does not establish pronunciation, voice compatibility, latency or NVDA
quality. Its 38 additional registry entries do not expand the selected 135.

The six regional membership counts are 37, 4, 8, 54, 39 and 9. Their sum is
151 because languages can belong to several regions. The generated catalog
preserves every geographic association while retaining 135 distinct entries
in first-occurrence order. It keeps Europe, both American sections, Asia,
Africa and Australia/Oceania in the selected order.

Later upstream status documents describe other resource snapshots. They do
not silently replace this pinned DEV2 audit or prove current local installation
state. The searchable report identifies the evidence scope on every result
set; all final capability fields remain `not_assessed`.

### Language identity stays explicit

- Tamazight remains an unresolved group. The recorded `kab` resource is an
  explicit partial Kabyle variant, not an automatic assignment of Standard
  Moroccan Tamazight or coverage of the whole group.
- Comorian remains an unresolved group without registered DEV2 resources.
- Quechua, Fula and Kurdish retain the audit's requirement to review the
  exact standard or variety.
- The selected Norwegian entry remains `no`; the recorded resource code is
  `nb`. Neither `nb` nor the additional `nn` resource becomes a new selected
  entry or an automatic routing rule.
- Mandarin, Filipino, Persian including Dari, Montenegrin and the two Ndebele
  entries retain their selected identities and names.

Catalog IDs are upstream selection identifiers. They are not certified BCP47
locale mappings, voice IDs, accents or script codes. Recorded runtime codes
are exposed as evidence only. No alias-based voice or language fallback is
performed.

## Inspect or regenerate offline

The desktop opens **Options > Language capability catalog** using the existing
command and shortcut registry. Its named native filter, list and read-only
details expose all 135 entries, geographic memberships, recorded resource
status and each unassessed capability. Tab, arrows and Enter navigate the
controls; Escape closes the view. Filtering neither selects a voice nor loads
a model. The installed native smoke tests actual Windows controls separately
from language/acoustic and manual NVDA acceptance.

Search names, countries, regions, explicit IDs and recorded resource codes:

```text
python -m carr_library.language_catalog report --query kab
python -m carr_library.language_catalog report --resource-status not-registered --json
```

To reproduce the projection, provide exact local bytes of the four pinned
public files. The command validates them and writes deterministic JSON to
standard output; it does not download anything or overwrite a file itself.

```text
python -m carr_library.language_catalog derive --selection-markdown SELECTED_LANGUAGES_UK.md --selection-registry selected_languages.json --audit-markdown LANGUAGE_COVERAGE_DEV2_UK.md --audit-registry language_coverage_dev2.json
```

`load_catalog()` returns immutable entries and geographic/resource records.
`matching()` searches them; `report()` creates a separate inspectable result
with the original source identity, resource evidence and unassessed capability
matrix. Editing that result cannot change the sealed catalog.

## Per-voice adapter correction

The upstream [voice package contract](https://github.com/Oleksii-debug/ChatGPT-Autopilot-ExtensionChatGPT-Autopilot-Extension/blob/a1c73a5747bc86eb11f0c037120442e0c2593e91/dordiai-voices/VOICE_PACKAGES_UK.md)
(blob `7ecbc87fe59a613701ed571c3f6b5903c17c6c13`) explicitly declares each
voice's languages, engine, resource roles and dependencies. It explains that
a Holos profile does not imply native Pocket resources for another language.
A global language resource inventory therefore cannot establish that every
profile supports every language.

`DordiaiVoicesEngine` now requires explicit per-profile language declarations.
They may be present as the profile's `languages` array in the verified local
catalog. When a legacy catalog lacks that projection, the separately reviewed
CARR installation manifest can carry the exact declarations from the installed
voice packages in `settings.profile_languages`:

```json
{
  "protocol": "nvda-worker-jsonl-pcm24k-v1",
  "profile_languages": {
    "synthetic_example_voice": ["uk"]
  }
}
```

This example describes the CARR bridge settings, not a replacement upstream
voice-package format. Preparation must use actual reviewed package metadata
and dependency evidence. It must not generate declarations from the selected
135-entry list or from the global runtime resources. Updating a sealed local
installation requires a newly reviewed manifest and complete file hashes;
editing installed metadata to bypass integrity checks is unsupported.

Both declarations must agree when the catalog and bridge provide them.
Missing, empty, malformed or conflicting declarations, and declared languages
absent from installed resources, produce an unavailable voice with a readable
reason. Such voices do not count as usable and the Audio Studio excludes them
from its selectable list. The existing selection is not silently replaced.

Synthesis checks the exact declared language before starting the worker.
`en-US` does not grant `en`, `en-GB` or another locale; case and underscore
spelling are normalized without dropping the regional part. Cache identity
includes the explicit language contract and declarations so old broad claims
cannot reuse the same adapter cache identity.

The worker still requires a separately reviewed complete local installation
and its existing JSON-lines PCM protocol. Its current tests exercise real
subprocess framing using synthetic PCM; they are not a dordiai acoustic model
test. No upstream private code, profiles, voice recordings or weights are
copied or installed by this change. Actual installed worker compatibility,
per-voice/resource dependencies, rights, phonetics and Windows/NVDA listening
remain qualification work.

## Verification boundaries

The source tests validate the 135-entry projection, geographic overlap,
partial groups, exact source identities, changed-artifact rejection, offline
search, and the absence of automatic voice assignments. Adapter tests check
per-profile restrictions, unavailable/legacy profiles, resource mismatch,
exact qualified languages, cache identity and Audio Studio selection.

The development derivation was regenerated from all four fetched exact blobs
and produced the recorded canonical digest. Ordinary source CI validates the
sealed generated artifact without fetching upstream files or executing any
language model. Final language and acoustic acceptance remains open.
