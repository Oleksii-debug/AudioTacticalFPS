# Explicit local spelling review (READ-10)

The reader's **Review spelling** action checks a selected passage or the current
reading copy with an explicitly selected, same-basename Hunspell `.aff` / `.dic`
pair. It previews one suggestion, applies it through the existing mapped reading
edit, and uses the existing undo/recovery history. Retained sources, quotations
and selected dictionary files are never rewritten. A suggestion is advice from
the selected dictionary, not verified source text or grammar analysis.

No language dictionary, personal vocabulary, model or network service is
selected, downloaded or activated automatically. Language coverage is determined
by the user's dictionary and the backend's supported rules. The selected path,
dictionary basename, scope and counts are visible; hashes remain in the typed
review and diagnostic/provenance evidence. No dictionary-path setting or second
reading store is introduced.

## Reviewed optional component

The implementation uses the Python source from [Spylls 0.1.7](https://pypi.org/project/spylls/0.1.7/),
an existing Hunspell port. Its actual wheel LICENSE and
[upstream README](https://github.com/zverok/spylls) identify MPL-2.0; the wheel's
MIT classifier is inconsistent with those notices. The derived metadata corrects
that classifier and preserves the full MPL license and all upstream Python bytes.

The upstream wheel contains six example `.aff` / `.dic` files. Its English
dictionary notices exist in the upstream repository's
[README_en_US.txt](https://github.com/zverok/spylls/blob/master/spylls/hunspell/data/en/README_en_US.txt)
but are absent from the wheel. The inspected Russian and Swedish directories did
not supply corresponding notices. The product therefore ships **none** of those
six dictionary files; this decision does not assign or infer their redistribution
rights. Select a dictionary you are permitted to use separately.

| Artifact | Identity |
| --- | --- |
| Input wheel | `spylls-0.1.7-py2.py3-none-any.whl`, 1,509,482 bytes |
| Input SHA-256 | `0c7fa4b66615f390bd12fd37939b85934c012309fd3cce8584844c54270b7776` |
| Code-only wheel | `spylls-0.1.7+carr.1-py2.py3-none-any.whl`, 224,323 bytes |
| Code-only SHA-256 | `10b65cc18b2166c2fe4ae2aba1ee0839e7a0a4a0f5aa2a76d13ec653f9c71b66` |
| Source inventory | 21 unchanged Python files, individually hashed in `speech/spellcheck_backend.json` |
| Runtime dependencies | None beyond the existing Python interpreter |

`scripts/repack_spellcheck.py` checks the exact input size/hash before opening the
ZIP, omits all dictionary files, preserves source/license, changes only package
version/license metadata, records provenance and regenerates RECORD. Sorted
uncompressed entries with fixed timestamps/permissions make the result independent
of the host's zlib. Generated wheels are build artifacts and are not committed.
The local package version prevents a plain extras installation from silently
substituting the upstream dictionary-containing wheel.

An explicit build/install example from `carr-library`:

```sh
python -m pip download --no-deps --only-binary=:all: spylls==0.1.7 --dest /chosen/build-input
python scripts/repack_spellcheck.py \
  /chosen/build-input/spylls-0.1.7-py2.py3-none-any.whl \
  /chosen/wheelhouse/spylls-0.1.7+carr.1-py2.py3-none-any.whl
python -m pip install --no-index --no-deps --require-hashes \
  --find-links /chosen/wheelhouse -r packaging/spellcheck.lock
```

The operator chooses these directories; the repacker never downloads files.
`packaging/spellcheck-sbom.json` describes the actual derived artifact with the
MPL identifier, derived hash, unchanged-source statement and upstream identity.
It does not list omitted dictionaries as shipped components. Installed runtime
checks verify the selected distribution version and every backend Python hash.
The worker uses a new private bytecode-cache location with `-B`, so old `.pyc`
files cannot replace the inspected source during the import.

## Bounds and correctness contract

The review accepts at most 200,000 characters and 3,000 tokens by default.
Individual words are at most 96 characters. The dictionary files are bounded to
2 MiB `.aff`, 16 MiB `.dic`, 50,000 affix lines, 200,000 declared dictionary entries
and 8,192 bytes per line. At most 200 issues and eight suggestions per issue are
returned by default. Hitting the issue cap produces an explicitly **partial**
result; exceeding the input/token limits fails with a smaller-scope instruction.

Token positions use the original Python Unicode text, including CRLF and
combining marks. Letters/marks and the dictionary's WORDCHARS are recognized;
internal apostrophes/hyphens stay in the word. This is a bounded text tokenizer,
not full language-specific segmentation. Spylls documents rare compound and
Hungarian limitations in its [coverage notes](https://spylls.readthedocs.io/en/latest/).
This adapter explicitly refuses CHECKCOMPOUNDPATTERN, COMPOUNDSYLLABLE,
SYLLABLENUM and COMPOUNDROOT rather than claiming those dictionaries are fully
supported. Other dictionary/language quality still requires representative
language-specific acceptance by the user or maintainer.

The shared ResourceBroker admits CPU, memory and scratch-disk use before the
selected dictionaries are read/copied. A 30-second default deadline bounds the
actual owned child. POSIX also enforces address-space, CPU and file-size limits;
Windows uses the existing owned Job Object and deadline. On Windows the memory
number is a shared admission declaration, not a claim of an OS per-process
memory cap. The worker forbids network, child commands and file writes.

Opening a file checks regular single-link local paths, descriptor identity,
bounded reads, cancellation and final path identity. Both selected files are
hashed and rechecked after the worker, and again before native Apply. Result
decoding binds text, dictionary, worker and backend identities and validates
every returned span. Apply rechecks the source/library guard and exact existing
ReadingSnapshot. Changed text, source authority, selection or dictionary choice
invalidates the preview. Each accepted change is one mapped reading-copy edit.

If native shutdown is unconfirmed, the existing cleanup owner retains both the
scratch directory and the actual shared reservation until it confirms quiescence.
Native direct and parent-driven deletion cancel pending work and discard late
callbacks without using deleted controls. Saving optional recovery state can
produce an explicit warning while the in-memory edit and undo remain available.

## CI and packaging integration

1. Download the pinned upstream wheel into a temporary build-input directory.
   Run the reviewed repacker to a separate wheelhouse, then check the derived
   size/hash against `speech/spellcheck_backend.json`. The upstream wheel is
   never installed or placed in the application's wheelhouse.
2. Install the **derived** wheel from `packaging/spellcheck.lock` before source
   tests. For a sealed Windows bundle, add its exact pin and the one component/
   no-transitive-edge SBOM entry to the existing pre-seal combined lock/SBOM;
   copy only the derived wheel to the wheelhouse before the builder verifies
   closure. Do not mutate the sealed bundle or its environment afterward.
3. Add these common source/installed selectors: `tests/speech/test_spellcheck.py`,
   `tests/speech/test_spellcheck_handlers.py`, and the reader-hook regression
   once composed. Add `tests/packaging/test_spellcheck_component.py` to source
   and portable packaging qualification. Existing installed-only origin rules
   continue to select the application modules, not checkout sources.
4. Include `carr_library.speech.spellcheck`, `carr_library.speech.spell_worker`
   and `carr_library.ui.spellcheck_dialog` in installed source-byte verification.
   Preserve `speech/spellcheck_backend.json` in the built application wheel.
5. Invoke installed Python with `-I -B -X utf8 scripts/windows_spellcheck_smoke.py`
   inside the existing outbound-denial gate. No audio evidence, model acquisition,
   external service or special input is required; this probe authors a small
   synthetic dictionary, uses the real backend/worker and native controls, checks
   undo/source retention, then delivers real queued results after native deletion.

Source tests and the native probe report their own actual results. A passing
synthetic dictionary test is not a language-quality, physical keyboard, NVDA or
commercial dictionary-rights certification. Exact installed Windows execution
remains a required gate for a shipping Windows build.

## BXD / REX / pronunciation DIC (READ-08)

These pronunciation-rule formats are distinct from Hunspell's dictionary pair.
The inspected official [command-line documentation](https://www.cross-plus-a.com/bconsole.htm)
and [text utility documentation](https://www.cross-plus-a.com/btext.htm) list the
extensions and application switches, but do not define record grammar, encoding,
escaping, flags or version rules. The public overview describes regular-expression
pronunciation correction, which is insufficient to define a compatible parser.
No BXD/REX/pronunciation-DIC adapter is claimed from those pages. A documented
grammar with representative licensed samples is still required before adding an
import adapter. The existing reviewed JSON pronunciation interchange remains
available; this spelling feature does not reinterpret another application's
pronunciation file as a Hunspell dictionary.
