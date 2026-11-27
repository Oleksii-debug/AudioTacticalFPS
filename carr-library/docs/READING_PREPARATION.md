# Explicit reading preparation: Hunspell and number/date/unit/symbol previews

## Scope and qualification status

This component adds explicit preparation of a selected range in the existing
reading-copy editor. It keeps the catalog source and the original text unchanged.
A proposed spelling or normalization change becomes one reading revision only
after the user reviews it and chooses Apply. The same Undo, Redo, recovery draft,
Save As, source history, pronunciation rules and unified backup contracts remain
in force.

The implementation and its acceptance gate are source-reviewed. They have not
been executed in the authoring environment, which has no working executor.
The dedicated Windows workflow must produce its own source, isolated installed
and native evidence before this component can be described as qualified.
Previous live-reader and playback results do not qualify these new operations.

There is no claim of native-speaker quality, 135-language coverage, NVDA
qualification or a production dictionary. The qualification dictionary is a
small public synthetic CC0 integration fixture. Real community dictionaries
remain an explicit asset selection and license decision.

## Native workflow

Open an existing source-bound reading copy, select the exact reading characters,
and choose **Prepare selected text and spell-check…**. The dialog shows the
selection as read-only text. It provides named keyboard-accessible controls for
the exact language, operation, number mode, written date order, unit meaning and
symbol, Ukrainian grammatical context, optional voice scope and explicitly
selected runtime/dictionary manifests. The field area scrolls while the action
buttons remain available.

Choose a reviewed local manifest with the file picker. No tool is downloaded,
discovered on PATH, inferred from a book or silently installed by this dialog.
An empty selection disables the corresponding operation.

For normalization, choose **Preview number/date/unit/symbol**. For spelling,
choose **Check selected spelling**, select one reported occurrence and a
suggestion, then choose **Preview selected spelling suggestion**. The review
shows the exact before/after text, character range, engine identity and selected
manifest SHA256. Apply changes only that reading copy. Closing the dialog leaves
the existing copy unchanged.

**Accept this word for project/language/voice** adds an identity rule to the
existing scoped pronunciation dictionary. It does not modify the installed
Hunspell dictionary. Existing explicit pronunciation overrides take priority:
covered spelling tokens are excluded and overlapping normalization selections
are refused until the user reviews the rule.

Inputs are captured when an operation starts and disabled while it owns the
request. Changing the context invalidates an earlier proposal. Cancel requests
owned cancellation. Close waits for the worker to settle, discards partial or
late results, and then closes. The caller rechecks its source/session guard
before accepting a result.

## Selected mature engines

| Operation | Selected implementation | Explicit limits |
| --- | --- | --- |
| Spelling | Hunspell 1.7.6 native C API | One explicitly selected UTF-8 AFF/DIC pair and exact en/uk/sk language label |
| Numbers | num2words 0.5.14 library API | Cardinal, ordinal where implemented, individual digits, or year |
| Numeric parsing and dates | Babel 2.18.0 locale/CLDR APIs | Explicit en/uk/sk locale and ymd/dmy/mdy date order |
| Units | Babel 2.18.0 unit formatting plus num2words | Explicit unit meaning and exact written symbol; no unit conversion |
| Symbols | Qualified CPython Unicode database | One selected punctuation/symbol character; English Unicode names only |

The adapters call the selected engines; they do not implement a second spelling
or language engine. Number parsing rejects exponent notation, unsupported
punctuation, excessive precision and ambiguous leading zeroes. Individual-digit
mode explicitly preserves identifiers such as 0012. Dates require a four-digit
year and a valid Gregorian date. An ambiguous or unsupported selection stays
unchanged.

For Ukrainian cardinal output the user chooses gender and grammatical case.
These controls are inactive when they do not apply. The selected Slovak adapter
does not implement ordinals, so that operation is disabled with an explanation.
Unicode symbol names are English only; Ukrainian and Slovak users can use the
existing explicit pronunciation dictionary instead. A generated proposal still
requires human grammatical and pronunciation review.

Normalization is limited to 512 UTF-8 bytes per selection. Spelling accepts at
most 64 KiB, 2,048 tokens and 512 distinct words. Numeric/code tokens, words over
128 bytes and non-BMP letter tokens are counted as skipped instead of being
passed to unsupported native dictionary paths. A single accepted change is
bounded to 16 KiB before and after. Requests and responses have bounded JSON
inventories; there is no partial bulk correction.

## Optional runtime and process ownership

The core and production dependency locks, package counts, SBOM and notices are
unchanged. Babel and num2words are not installed into the host core environment.
The separately selected optional pack contains their reviewed library files,
required metadata, license notices, Babel global data and the selected
root/en/uk/sk locale data. The num2words command-line entry point is not selected;
its CLI-only docopt dependency is not introduced.

The fixed application worker is launched with the qualified host interpreter as
`python -I -B -S preparation_worker.py`. The normalizer adds only the verified
optional package directory. Site initialization, user site packages, .pth
processing and host site-package fallback remain disabled. The parent verifies
reported module paths and SHA256 against the selected pack; the worker reports
its actual isolated/no-site flags. An import failure does not fall back to a host
installation.

The existing LocalModelManifest contract supplies explicit license/trust
decisions and complete file identities. Every runtime file and the manifest are
opened through the existing verified readers for the full owned operation.
On Windows those handles exclude ordinary concurrent writers and replacement.
All bytes are checked again before execution. Unknown files, changed files,
unreviewed assets, mismatched dictionary language and unavailable versions are
refused. The runtime inventory is limited to 384 files and 128 MiB.

The existing process supervisor owns deadlines and cancellation. Default
operation time is 15 seconds, with an explicit finite 0.05–60 second API budget.
The fixed worker blocks Python socket and nested-process audit events and uses
the existing offline environment. This is not presented as a general hostile
native-code sandbox. The qualification workflow separately installs and reads
back an actual Windows Firewall outbound rule for the exact Python executable.

Hunspell loads the explicitly selected DLL through its C API. Windows DLL lookup
is limited to the DLL directory and system libraries, without PATH or the current
directory. The reviewed upstream MSVC file-opening implementation receives
extended UTF-8 local paths for AFF/DIC files so that Unicode dictionary paths use
its UTF-16 branch. Native DLL execution is separately bounded by the owned
process; a failed or timed-out worker cannot publish a reading revision.

## Durable provenance and backup

A prepared revision stores inert provenance:

- The engine and reported version, plus the exact optional manifest SHA256.
- Original, complete input-revision and complete output-revision SHA256.
- The exact previous reading range and its before/after text.
- Explicit language, mode, date/unit/grammar/voice context, or dictionary ID,
  dictionary revision and license.

No executable path or activation authority is restored from this record.
Validation replays each prepared revision against its immediately previous
reading revision and unchanged original. A recomputed outer backup checksum
cannot authorize a broken inner history. Stale previews cannot be applied to
changed text.

Projects with preparation records use reading-copy format version 5. Existing
unprepared versions 1–4 keep their old field shapes and validation contracts.
The existing narration producer validates and includes the new records in
unified backup, restore and rebackup. Restoring a reading project does not
install a runtime, load a dictionary or execute an operation.

## Reproducible optional asset selection

`packaging/reading-tools/source-lock.json` records the reviewed source identities,
license selections, fixture scope and initial NOT_RUN status.
`packaging/reading-tools/optional-worker.lock` pins the two optional wheels.
Their primary publisher pages are:

- [num2words 0.5.14](https://pypi.org/project/num2words/0.5.14/)
- [Babel 2.18.0](https://pypi.org/project/babel/2.18.0/)
- [Hunspell 1.7.6 publisher release](https://github.com/hunspell/hunspell/releases/tag/v1.7.6)
- [Exact Hunspell release metadata](https://api.github.com/repos/hunspell/hunspell/releases/424666040)

The selected Hunspell source revision is
`15faca15532b34db3106d50f162e5edc2503d6d6`. Its release tarball is 1,167,432 bytes,
SHA256
`9c650ace31d4d8cc1da925a9507c3de1d50f1a54857af2f72429f517d5d53467`.
The workflow builds an x64 shared library with the upstream CMake build and
records the actual generated DLL identity in its optional manifest. It does not
claim a publisher-supplied binary digest.

`packaging/prepare_reading_tools.py` accepts already acquired local wheels or a
reviewed source tarball and separately built DLL. It does not download anything,
run pip or execute the DLL. It requires explicit `--reviewed-assets`, verifies
publisher wheel/tarball hashes and native PE architecture, extracts only bounded
selected files, preserves license notices, and writes a new complete manifest
into a fresh output directory.

Hunspell source notices include COPYING, COPYING.LESSER, COPYING.MPL, AUTHORS,
license.hunspell and license.myspell. The selected library license route is
LGPL-2.1-or-later. Babel's included Unicode/CLDR notices and both package metadata
are retained. This integration record does not replace distribution review of a
future production dictionary or packaged native binary.

The synthetic dictionary demonstrates native suggestions, a simple plural affix,
UTF-8 words and Unicode file paths. It is deliberately too small to serve as a
production language dictionary.

## Required qualification and exact oracle

The machine-readable oracle is
`scripts/reading_preparation_acceptance_oracle.json`.

| Required selector | Expanded cases |
| --- | ---: |
| tests/reader/test_reading_preparation.py | 40 |
| tests/ui/test_narration_preparation.py | 12 |
| tests/storage/test_preparation_backup.py | 4 |
| packaging/reading-tools/test_optional_runtime.py | 20 |
| Dedicated source run | 76 |
| Dedicated isolated installed run | 76 |
| Actual Windows native named checks | 29 |

The first three selectors are ordinary contract/integrity regressions. They do
not require optional assets and must be included in the full source and installed
candidate gates. Their synthetic engine responses are labeled as such. They also
exercise a real supervised timeout and actual isolated host-fallback refusal.
The helper `tests/reader/preparation_fixture.py` must accompany installed copies
of these tests.

The 20 optional cases live outside the normal tests directory so the core profile
does not acquire undeclared tools. The dedicated gate must provision both exact
packs; those cases never skip or substitute mocks. They call real num2words,
Babel and native Hunspell, including explicit language results, CLDR dates and
unit plurality, invalid selections, Unicode paths/words, PATH exclusion and
tamper refusal.

The 29-check native probe opens actual wx controls, exercises focus and disabled
controls, uses actual optional tools, explicitly applies two source-bound
revisions, saves/reloads their typed history and verifies cancellation/close
ownership. It does not claim physical playback, native-speaker quality or NVDA.

The workflow `.github/workflows/carr-r9-reading-preparation.yml` installs the
unchanged pinned core, builds separate optional packs, records the exact commit
and tree, enables and reads back every firewall profile, applies the unique
outbound Python rule, and runs source, isolated installed and native qualification.
The JUnit checks require exact per-function Counters with no failures, errors or
skips. Installed/native origins for all six changed product modules must be under
the installed prefix and hash-identical to current source. Actual optional
manifest identities must also match.

Firewall cleanup removes only the workflow's own rule, restores every original
profile and verifies the actual state. Cleanup failure fails the job. Evidence
is uploaded even after failure; native executables and large source fixtures are
not included. A green result for this dedicated component does not override an
unrelated failure in the full candidate gate.
