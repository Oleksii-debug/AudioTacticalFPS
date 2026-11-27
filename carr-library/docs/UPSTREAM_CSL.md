# Offline citation styles: implementation and provenance

This is an adapter to the real citeproc-js Citation Style Language processor.
The application does not reproduce APA, Chicago or other styles by manually
concatenating metadata. The processor executes in a fresh local QuickJS
interpreter with no Python callbacks, filesystem functions, network functions,
module loader, or remote fallback. Source quotations and their verification
services remain separate.

## Exact upstream inputs

| Component | Adopted identity | License and source |
| --- | --- | --- |
| citeproc-js | npm `citeproc@2.4.63`; npm `gitHead` `73bc1b44bc7d54d0bfec4e070fd27f5efe024ff9` | CPAL-1.0 option; upstream additionally offers AGPL-3.0-or-later. [Source](https://github.com/Juris-M/citeproc-js), [published artifact](https://registry.npmjs.org/citeproc/-/citeproc-2.4.63.tgz). |
| CSL styles | `citation-style-language/styles@e27b9b7e204b1a40998f774ff05c8a6ed6a5f7d8` | CC-BY-SA-3.0; unchanged XML retains its named authors, contributors, rights and source URLs. [Source](https://github.com/citation-style-language/styles/tree/e27b9b7e204b1a40998f774ff05c8a6ed6a5f7d8). |
| CSL locales | `citation-style-language/locales@2a7d750cb8b1d01ad838b42a2309d58a4d695c1b` | CC-BY-SA-3.0; unchanged locale XML and upstream licensing README included. [Source](https://github.com/citation-style-language/locales/tree/2a7d750cb8b1d01ad838b42a2309d58a4d695c1b). |
| Python JavaScript runtime | `quickjs-ng==0.18.0.2` | MIT. [Python binding](https://github.com/genotrance/quickjs-ng), [published wheels](https://pypi.org/project/quickjs-ng/0.18.0.2/). Dependency locks contain exact Windows wheel hashes. |

The downloaded npm archive passed both its published SHA-1 and SHA-512
integrity values. Its SHA-256 is
`7f2b7714bfd1f4480aad9c34972eb5d48dc1b85876ba21b5aa9aee74156e2ccf`.
The adopted `citeproc_commonjs.js` is copied byte-for-byte as
`citations/_vendor_citeproc/citeproc.js`, SHA-256
`55abba1a8b8b48c14323fd053f9646013b4b7a0c977938064ecf5195a502b9f1`.
Its code reports `CSL.PROCESSOR_VERSION = "1.4.61"`, despite the published
package version being 2.4.63. Both identities are retained in the result and
manifest; the adapter does not relabel the engine. The package manifest's
AGPL shorthand is not used as license authority: its actual LICENSE text
specifies AGPL version 3 or later as the alternative to CPAL.

The complete source LICENSE, CPAL and AGPL texts accompany the unchanged
processor. Supplemental license texts were inspected at upstream
`56e7ef310154748368665cc8a2be124e3c141832`. Exact hashes of copied code, styles,
locales and notices are in `citations/_vendor_citeproc/UPSTREAM.json`.
The QuickJS binding MIT license and underlying engine copyright/license
notices from its exact source-distribution compiler inputs are retained as
`QUICKJS_NG_LICENSE` and `QUICKJS_CORE_NOTICES`; the source archive SHA-256 is
`a1a890300a5c460d7735481bc38e8ab10e6f06b733a517f7fa9887f6d121df40`.
The runtime verifies every executable/style/locale resource against hashes
embedded in `csl.py` before using it. No arbitrary filename, URL, external
style or executable JavaScript can be selected through the public API.

## Attribution in the application

The CPAL Exhibit B attribution is displayed prominently whenever the citation
styles dialog is opened. It is also available as `csl.ATTRIBUTION` and in each
render result:

> (c) Frank Bennett
>
> citeproc-js implements the Citation Style Language
>
> https://citationstyles.org/

This is a text-accessible attribution, including for NVDA. Packaging must retain
the full notices, source code and style/locale authorship. The unmodified CSL
style and locale files remain under their own CC-BY-SA-3.0 terms; application
code and rendered bibliographic metadata are separate artifacts.

## Included styles and locales

| Application ID | Exact upstream style |
| --- | --- |
| `apa` | APA Style 7th edition (`apa.csl`) |
| `chicago-author-date` | Chicago Manual of Style 18th edition, author-date |
| `chicago-note` | Chicago Manual of Style 18th edition, notes and bibliography |
| `mla` | MLA Handbook 9th edition, in-text citations |
| `harvard` | Cite Them Right 12th edition, author-date/Harvard |
| `vancouver` | NLM/Vancouver: Citing Medicine 2nd edition, citation-sequence |
| `ieee` | IEEE Reference Guide version 11.29.2023 |

Local term/date data is included for `en-US`, `en-GB`, `uk-UA` and `sk-SK`.
The requested locale overrides a style's default locale. The processor may use
the installed `en-US` terms as its normal CSL fallback; it never fetches a
missing locale. This does not translate titles, author names or source text.
The interpreter does not supply ICU/Intl locale-specific collation, so exact
language-tailored alphabetic sorting is not an acceptance claim of this
increment. Bibliographies still use the actual style's sort instructions.

## API and source-integrity boundary

`available_styles()` returns immutable `StyleInfo(id, title, kind, filename)`
records. `render(records, style="apa", citations=None, locale="en-US",
output="text", budget=None)` accepts CSL-JSON records and returns
`RenderedBibliography`. Text and sanitized HTML outputs include entries,
entry identifiers, ordered citation strings, warnings, attribution and exact
processor identity. The result's `bibliography` property joins its entries.

`citations` is an ordered sequence of clusters. Each cluster contains cite-item
dictionaries with `id` and optional `locator`, `label`, `prefix`, `suffix` or
`suppress-author`. Omitting this argument produces one example citation per
record. Passing an empty sequence produces bibliography entries without
citation examples. All selected records remain in the bibliography, even if
uncited. Full `processCitationCluster` operations update earlier citations
when disambiguation changes; the adapter does not use the upstream
`makeCitationCluster` shortcut, which its documentation excludes from
production use. Numeric references are registered by first citation use,
with uncited records following, rather than by import order.

Names are never guessed by this module: literal CSL creators remain literal,
and explicitly structured personal names remain structured. Unknown metadata
fields are rejected instead of silently ignored. This adapter formats the
supplied data; it does not prove that data is correct.

Publication `page` ranges stay in the bibliographic record and are never
copied into a cite-item `locator`. A manual locator is returned with an
explicit unverified-location warning. Official full-note styles may display a
publication extent in the full bibliographic note even without a locator;
such output carries a separate extent-versus-quotation-location warning.
Nothing in this module authenticates printed pages, approves OCR, modifies
saved evidence, or changes the input data. Research notes are excluded by the
application's scoped workbench before rendering.

Grouped references follow the exact pinned processor/style behavior. In a
measured same-author/same-year example, a deliberately reversed cluster
retained `2020b, 2020a` rather than automatically sorting suffixes. Individual
citations and bibliography entries disambiguate correctly as `2020a`/`2020b`.
This implementation is not a claim that every CSL conformance fixture or all
edge cases of a style manual have been independently verified.

## Resource, output and failure behavior

`RenderBudget` defaults to a batch of 2,000 references, 4,000 citation
clusters, 8 MiB of input, 16 MiB of rendered output, a 512 MiB interpreter
heap, a 4 MiB stack and 30 CPU seconds. Input is checked before interpreter
allocation, including cumulative bytes for repeated cite-items. Budgets can
be explicitly configured; they do not cap the size of the persistent library.
Run the formatter on the UI's existing background-job path. A fresh
interpreter per operation prevents residual references crossing workspaces.

The latest official APA and Chicago styles have large macro sets. On the
actual Linux test environment, simple one-book compilation used approximately
150–230 MiB of QuickJS heap and roughly 6–14 seconds; smaller Harvard,
Vancouver and IEEE styles were below one second. These are measured
development observations, not Windows performance claims. A budget failure
is explicit and produces no partial success or simplified fallback style.
Cancellation prevents the UI from accepting a stale result; runtime CPU
limits bound an already executing synchronous interpreter operation.

Records and citation items enter JavaScript only through JSON.parse of a
JSON-encoded string. Record IDs inherited from Object.prototype are rejected.
Directional overrides and isolate controls, unsupported C0/C1 controls and
surrogates are rejected at every text boundary, including creator names and
citation affixes; natural RTL letters and ordinary multilingual text remain
supported. Complete dates must be actual proleptic Gregorian calendar dates,
including leap-year and century rules. Partial dates and the documented
negative/zero year range remain supported without guessing or normalization.
There are no host callbacks or file/network functions in the interpreter.
Input links allow only credential-free HTTP(S) as inert metadata; the adapter
does not fetch them. HTML output is filtered to approved typography and CSL
layout classes, with links, arbitrary styles, attributes and active tags
removed. The workbench applies a second typography-only conversion when
writing Word or standalone HTML output.

## Reproduction and acceptance

From `carr-library/`, with the pinned `quickjs-ng` dependency installed:

```console
python -m pytest -q tests/citations/test_csl.py
```

The suite uses the actual interpreter and actual pinned resources. It covers
all seven styles, four locales with network sockets denied, golden formatted
book entries, year-suffix disambiguation, first-use numeric ordering, uncited
entries, explicit locators, publication extents, source immutability,
corporate creators, absent-data fallbacks, typography-only HTML, inert
JavaScript-shaped text, invalid records/citation clusters, bounded repeated
inputs, genuine interpreter CPU/memory exhaustion, missing runtime and
resource-tampering rejection. It also verifies every copied resource and
license notice against the provenance manifest.

The initial full run caught a real integration defect: registering every item
as uncited before citing caused numeric styles to use import order. The fix
registers first-use citation order separately from uncited records. Test
results must be recorded from the final exact source revision. Native Windows
runtime execution, screen-reader navigation and manual scholarly review are
separate acceptance tasks; a Linux processor test is not evidence of those.
