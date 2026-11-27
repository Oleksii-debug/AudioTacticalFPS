# Explicit spelling review in the current reading editor

The reading-copy editor offers **Review spelling…**. The user selects a local,
same-basename Hunspell .aff/.dic pair, reviews the current selection or the whole
reading copy, previews one suggestion and applies it explicitly. Applying a
correction uses the existing NarrationController revision and recovery history.
Undo restores the prior reading text. Original source bytes, source identity,
quotations, and selected dictionary files are unchanged.

A spelling suggestion is a proposal from the selected dictionary. It is not
quotation evidence, a grammar analysis, or a general language-quality claim.
The dialog never chooses a language dictionary automatically. It does not
download or persist a dictionary path, alter personal vocabulary, or create
another reading, speech, or provider store.

## Exact optional code component

This port reuses the reviewed component preparation from PR143, exact source
8c981c7e947f8d144c8706b5ea022990517c7dd9. The
[upstream Spylls project](https://github.com/zverok/spylls) and its
[0.1.7 release page](https://pypi.org/project/spylls/0.1.7/) describe a Python
Hunspell implementation. The upstream LICENSE and README identify MPL-2.0;
the release metadata has an inconsistent MIT classifier. The existing
repacker preserves the full license and all Python bytes, and corrects only
package metadata and the local version.

| Artifact | Exact expected identity |
| --- | --- |
| Input wheel | spylls-0.1.7-py2.py3-none-any.whl; 1,509,482 bytes |
| Input SHA-256 | 0c7fa4b66615f390bd12fd37939b85934c012309fd3cce8584844c54270b7776 |
| Code-only wheel | spylls-0.1.7+carr.1-py2.py3-none-any.whl; 224,323 bytes |
| Code-only SHA-256 | 10b65cc18b2166c2fe4ae2aba1ee0839e7a0a4a0f5aa2a76d13ec653f9c71b66 |
| Source inventory | 21 unchanged Python files in reader/spellcheck_backend.json |
| Runtime dependencies | Existing CPython standard library only |
| Dictionary payload | None: all six upstream example .aff/.dic files are removed |

The 17-package base lock and 53-package full lock remain unchanged. Spelling is
an explicit separate component, recorded in packaging/spellcheck.lock and
packaging/spellcheck-sbom.json. A complete build that selects spelling must add
this exact wheel before sealing its outer inventory and SBOM. It must not add
an upstream dictionary-containing wheel as an equivalent substitute.

The scripts/repack_spellcheck.py helper accepts an already selected input,
checks exact size and SHA before reading ZIP members, writes a new code-only
wheel without overwriting an existing output, and emits its digest and complete
provenance. It does not download anything. The output uses fixed uncompressed
ZIP entries and a regenerated RECORD. Keep generated wheels outside public
source changes.

An explicitly authorized preparation and installation is:

    python -m pip download --no-deps --only-binary=:all: spylls==0.1.7 --dest /chosen/build-input
    python scripts/repack_spellcheck.py \
      /chosen/build-input/spylls-0.1.7-py2.py3-none-any.whl \
      /chosen/wheelhouse/spylls-0.1.7+carr.1-py2.py3-none-any.whl
    python -m pip install --no-index --no-deps --require-hashes \
      --find-links /chosen/wheelhouse -r packaging/spellcheck.lock

The local extra spelling names only the exact local component version; it
cannot silently accept ordinary upstream 0.1.7.

## Current architecture and integrity

The parent validates installed distribution version and every declared source
hash without importing Spylls. It takes a private source snapshot under the
existing shared ResourceBroker reservation. Dictionary source reads use the
existing verified_reader, including Windows write exclusion and native change
identity. The pair is checked again after review and asynchronously before
applying a suggestion.

One isolated CPython worker is launched with -I and -B through the current
audio.capture_process owner. The copied helper is read once, hashed, and
compiled from those exact bytes. The worker captures all approved backend
Python bytes before import. Its narrow source loader compiles that captured
content and refuses undeclared Spylls modules. It never reads an ambient Spylls
.pyc file, even if that file has a matching timestamp.

The child denies Python network, child-command, write-open and filesystem
mutation audit events. These checks are a Python process boundary, not an OS
network sandbox. The worker invokes an explicitly selected private dictionary
pair; no installed example dictionary fallback is available. All subprocess,
diagnostic, deadline and cancellation ownership uses the existing process
implementation. A failed native cleanup retains the actual stage and resource
lease. The UI waits through the same owner's cleanup before completing a
requested close.

The native view uses the current TranscriptionSession and its existing frame
executor. No second executor is added to the product. Source/library identity,
exact narration project object, text, selected range and dictionary choice are
checked again before accepting a result. Native selection positions are mapped
through the existing UTF-16/CRLF-aware live-reading helpers. Background workers
never mutate native controls. Forced parent destruction cancels work and drops
queued UI callbacks.

## Explicit bounds and coverage

| Boundary | Default / maximum |
| --- | --- |
| Selected text | 200,000 Unicode characters |
| Tokens | 3,000 default; 10,000 configurable maximum |
| Token length | 96 characters |
| Issues | 200 default; 1,000 maximum |
| Suggestions per issue | 8 default; 16 maximum |
| Dictionary files | .aff <= 2 MiB; .dic <= 16 MiB |
| Lines / dictionary entries | 50,000 affix lines; 200,000 entries; 8,192 bytes per line |
| Worker reply | 512 KiB |
| Worker timeout | 30 seconds default; 0.05–120 seconds |
| Memory reservation | 768 MiB default; 256 MiB–2 GiB |

POSIX applies address-space, CPU, core and file-size process limits. The
Windows memory quantity is a shared admission reservation, not a per-process
OS memory quota. Resource admission can refuse before reading/copying the
selected dictionary. The parent owns a finite deadline and the entire native
process family, including pathological dictionary regular expressions.

When the issue cap is reached, the typed reply reports the exact checked and
total token counts and marks incomplete coverage. The dialog displays that
partial result explicitly. CHECKCOMPOUNDPATTERN, COMPOUNDSYLLABLE, SYLLABLENUM
and COMPOUNDROOT directives are refused because the selected backend does not
establish their complete behavior. Language coverage depends on separately
selected dictionaries and their supported rules.

## Qualification boundary

This port's new source is NOT_RUN while the executor is unavailable. Donor
evidence is not presented as execution of this integration. Required gates are:

- tests/spelling/test_spellcheck.py: real exact component and isolated worker,
  Unicode/CRLF offsets, affixes, legacy dictionary encoding, partial coverage,
  path/content changes, cancellation/deadline, native cleanup retention,
  reply provenance, bytecode refusal and captured-source loading.
- tests/spelling/test_handlers.py: actual worker and dialog handlers with
  thread/lifetime-checking widget stand-ins, stale replies, revoked scope,
  dictionary mutation, cancellation and parent destruction.
- tests/spelling/test_reader_hook.py: actual current narration closure,
  UTF-16 selection, original CRLF retention, one revision, recovery failure,
  Undo and unrelated edits.
- tests/spelling/test_session.py: actual shared cleanup waiter and asynchronous
  close settlement through the current session.
- tests/spelling/test_component.py: installed component license/RECORD/source
  inventory, no dictionary payload, and exact lock/repacker congruence.
- scripts/windows_spellcheck_smoke.py --native: actual installed wx controls,
  explicit synthetic dictionary, actual backend/owned child, preview/apply,
  native selection, recovery, Undo and cancellation/close.

Run the optional component suites explicitly only after installing the exact
component; absence is a failure of that selected gate, not a skip. The native
probe is synthetic language coverage. It does not qualify NVDA, a real external
language dictionary, grammar accuracy or a private recognition/speech engine.


## Optional component build and CI integration

The canonical Windows builder accepts --spellcheck-wheel with the derived
wheel kept **outside** the unchanged base wheelhouse:

    python packaging/build_windows_bundle.py \
      --wheel APPLICATION_WHEEL --wheelhouse BASE_WHEELHOUSE \
      --lock packaging/windows-semantic-cp311.lock \
      --sbom packaging/windows-semantic-sbom.cdx.json \
      --spellcheck-wheel OPTIONAL/spylls-0.1.7+carr.1-py2.py3-none-any.whl \
      --output NEW_BUNDLE [existing approved runtime/source/model/launcher arguments]

The builder validates the entire 224,323-byte component before reading its ZIP.
It verifies source inventory, provenance, license and the separate lock/SBOM,
then extends the captured base SBOM from 53 to 54 components (or core17 to18)
without changing the original lock, SBOM or wheelhouse. The generated ordinary
install.lock includes the exact additional pin. The full source declaration
and MPL notice are sealed in the existing outer files inventory; no new outer
bundle-manifest flag is introduced.

The standalone installer refuses an unannounced, wrong-version or wrong-hash
component even when someone recomputes its outer inventory. It captures the
manifest once, validates all sealed files, uses the existing offline pip flow,
and checks the installed 21-file spelling source inventory before readiness.
That check imports no Spylls code and selects no language dictionary.

For both source and isolated installed-wheel test gates, install the same exact
optional component into the selected test interpreter. It has no transitive
requirements and does not require changing the base17/full53 lock assertions.
Set CARR_SPELLCHECK_WHEEL to the absolute derived-wheel path for
tests/spelling/test_component.py; this suite exercises the real canonical
preseal builder and existing native manifest reader. The other four suites
need only the exact installed component. No optional test turns absence into
a skip.

Direct upstream input URL (the repacker still enforces exact size and SHA):

https://files.pythonhosted.org/packages/cd/19/ce6fcb4b86e188983bcfc1f84e4ac6f3038ca8d68b6d70a678cdc566f66c/spylls-0.1.7-py2.py3-none-any.whl

All port/preseal tests and actual native Windows execution remain NOT_RUN until
the corresponding selected CI gates finish on this source composition.
