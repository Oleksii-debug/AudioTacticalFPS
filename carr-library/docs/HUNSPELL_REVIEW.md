# Optional Hunspell spelling review

Voice reader → **Spelling review** checks the current reading copy or the text
selected when the review window opened. Select a trusted local native Hunspell
library, a matching `.aff`/`.dic` pair, their language and their origin/license
notice. **Review selected profile** hashes those three files without loading
the library. **Check spelling** explicitly starts the reviewed native engine in
the reader's existing cancellable worker process. No engine or dictionary is
discovered, downloaded, installed or restored as executable approval.

Select an issue to move to its exact reading range. Choose a suggestion or type
your own correction, preview it, and apply explicitly. Each apply is one undoable
reading edit; the immutable original, source authorization, quotations and stored
source anchor retain their existing contracts. A recovery-save failure reports
that the edit is already in memory and asks for Save As; it does not erase the
edit or its undo. Recheck after applying, because later character offsets may
have changed. Closing the review forgets its external profile. Ordinary reading
draft/backup recovery retains the resulting reading copy, not native execution
permission or external dictionaries.

## Reuse and exact boundary

The adapter uses the existing speech process boundary, `ReadingDocument`, reader
worker/cancellation event and source guards. Its C ABI was reviewed against
upstream Hunspell v1.7.2, commit
`2969be996acad84b91ab3875b1816636fe61a40e`:

- [C API declarations](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/src/hunspell/hunspell.h)
- [Upstream license](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/license.hunspell)

This integration adds no Python package, bundled native binary or language pack.
The upstream library offers alternative license routes; a selected dictionary
and the actual binary's dependencies have their own terms. The text entered in
the provenance field is a user-supplied record, not automatic redistribution
clearance. A binary's recorded SHA identifies those exact bytes; it does not
verify every transitive system dependency or establish a version from its
filename. No native version claim is inferred from a SONAME.

The child stages verified copies of all three files in a parent-owned private
directory. It rejects changed bytes, links/reparse points, missing files and
invalid basic dictionary declarations before loading the library. ASCII relative
dictionary basenames support original paths containing Unicode. The native
library never loads in the GUI process. On Windows it must be a compatible
standalone DLL: current-directory/PATH lookup and arbitrary companion DLL
folders are not enabled. A missing dependency or C API reports unavailable.

The parent's existing supervisor owns cancellation, deadline and process-tree
reaping. Its staging context removes copied dictionaries and native code after
child exit, including forced cancellation or timeout. The external originals
are never overwritten. This is an execution/lifetime boundary for explicitly
trusted native code, not an operating-system security sandbox or a guarantee
against malicious native libraries.

## Finite review limits

- Existing reading-copy limit: 2,000,000 Unicode characters.
- One review: at most 20,000 letter-based word ranges and 500 reported issues.
- A word longer than 128 characters is explicitly uncheckable; it is not silently
  accepted. At most ten suggestions of at most 128 characters are shown.
- Library/affix/dictionary size limits: 64/16/128 MiB. The native check has a
  30-second deadline; input and output also use the existing process budgets.
- Dictionary encoding is obtained from Hunspell and checked against its `.aff`
  declaration. Strict encoding/decoding is used. Text unavailable in a legacy
  encoding is an explicit issue, with no replacement decoding.
- Native parser/runtime warnings fail the review. A partial or malformed result
  cannot offer corrections. Changed text, selection, source authority or profile
  settings invalidate stale queued results and previews.

Word ranges use Unicode letters/combining marks and internal apostrophes or
hyphens. This is a defined heuristic, not universal word segmentation, grammar
checking, a proper-name classifier or language-quality acceptance. A dictionary
may legitimately omit names, technical terms or a language's alternate forms.
Suggestions remain optional. No 135-language claim follows from this adapter.

## Executed evidence

The 12 strict reader/spelling/find/pronunciation/source/recovery suites passed
**244 cases with zero failures and zero skips**. The new spelling suites account
for 61 of those cases: explicit profile review, exact native-position hooks,
source/stale-result guards, actual child refusal of changed/unavailable files,
worker cancellation/cleanup contracts, reviewed apply/undo and recovery warnings.
Synthetic protocol and native-widget fixtures are labelled as such.

The separate [actual engine acceptance script](../scripts/hunspell_acceptance.py)
executed **14 checks** with the explicitly selected system native library:

- Native library: 800,320 bytes, SHA256
  `7dc4e1d423b3e2c976359ae84a9bd8f6e2c19816322ceeddace6313244c50958`.
- Actual C API, original tiny UTF-8 English/Ukrainian fixtures, affix expansion,
  suggestions, emoji/CRLF positions and selected-text checks.
- Original legacy-encoding fixture, explicit unencodable words, changed-byte
  rejection, malformed dictionary refusal and unchanged external originals.
- Actual native child cancellation and deadline, followed by verified removal
  of the parent-owned staging directory; explicit source-preserving apply/undo.

The source hashes in the [exact report](evidence/hunspell-native-20261127.json)
bind that result to the exercised modules. These are synthetic dictionaries and
POSIX execution; Windows DLL execution, human Windows 11/NVDA, real dictionary
quality and dependency distribution clearance remain separate requirements.

The installed Windows pronunciation/Find probe additionally opens the real
spelling controls by native key input, reviews actual selected fixture files,
and verifies clear refusal of a deliberately non-executable library. Its report
explicitly says `actual_hunspell_engine: NOT_RUN`. An unavailable-engine check
does not count as successful native Hunspell execution.

To qualify another explicitly reviewed native library:

```text
python scripts/hunspell_acceptance.py --library ABSOLUTE_NATIVE_LIBRARY --output NEW_REPORT.json
```

The probe creates only original synthetic dictionaries, never acquires an
engine, and refuses to overwrite a report.
