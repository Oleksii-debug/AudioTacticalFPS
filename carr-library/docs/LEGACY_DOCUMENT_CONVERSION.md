# Selected legacy document reading

DjVu embedded-text reading is **qualified on Windows** for the pinned DjVuLibre
3.5.30-2 MSYS2 UCRT64 build. Its ordinary-admission source and installed gate at
684cc8ddac569fdcf5d685b33302f570481ea6f8 passed 163 portable cases and four real
native cases per mode (run 46024229261). The exact raw artifact was independently
read without executing its contents.

The pinned calibre 9.19.0 fixture-preview gate at
779967bdc700175f770a6f554b6b57292da9e280 passed 179 portable cases and nine real
native cases per mode (run 46026674640). An independent bounded ZIP reader
verified all four exact JUnit function Counters, zero failure/error/skip, source
identities, isolated installed origins, wheel binding and publisher inventory.
The six positive suffixes were AZW, AZW3, LIT, MOBI, PRC and TCR. AZW and PRC used
unprotected MOBI containers; AZW3 used KF8, LIT was freely generated, and TCR was
actual compressed input. The remaining three cases verify protected-input
refusal, output bounds and cancellation after native output. They are not three
additional supported formats.

Ordinary admission for those six suffixes is now qualified at the frozen
08fb93a68ebbdb0c564a1e09b9960d2d718d020e candidate: run 46039340019 passed all
187 portable cases and all nine actual native cases from both source and the
isolated installed wheel. The independent bounded artifact reader
(run 46042627256) verified the exact ZIP digest and all four raw JUnit function
Counters, zero failure/error/skip, publisher inventory, source/tree, wheel and
installed module origins. The fixed wrapper still requires an explicitly
selected installation and rejects every calibre version except 9.19.0.
This evidence applies to those six tested unprotected format/container
combinations; it does not qualify every book or Palm subtype. Later CHM recovery
changes require their own current 221-case portable gate. CHM remains
unqualified while that separate gate is pending.

Selection never follows PATH or enables a runtime silently. Missing DjVu text
layers, indirect/shared references, OCR, arbitrary real books, other Palm
subtypes, printed pagination and quotation verification remain outside the
recorded qualification.

## Implemented scope

| Source | Explicit runtime | Reading behavior | Deliberate limits |
| --- | --- | --- | --- |
| AZW/AZW3/MOBI | calibre 9.19.0 ebook-convert and calibre-debug companions | Plain UTF-8 reading derivative | Actual MOBI/KF8 containers only; encrypted headers, Topaz and KFX are refused |
| PRC | Same calibre installation | TextRead/PalmDoc or MOBIBook reading | Other Palm database subtypes are not inferred from an extension |
| LIT | Same calibre installation | Freely generated unprotected content | EUL licenses and personalized bookplates are refused; only level0 or freely generated level1 with the zero content key is admitted |
| TCR | Same calibre installation | Bounded decompression through its existing input plugin | Actual !!8-Bit!! signature; no renamed text or format guessing |
| DjVu | DjVuLibre djvutxt and djvused | One embedded UTF-8 text record for each verified native page | Standalone/bundled self-contained pages only; INCL shared/indirect references and pages needing OCR are explicit errors |
| CHM | 7-Zip with its local archive component | Extract bounded selected HTML members to stdout, then the existing inert HTML reader | Actual ITSF3; one strict UTF-8 TOC or warned stable filename order; no browser/help viewer, script, ActiveX or attachment execution |

All sources retain their original bytes. Reading copies and reports use the
existing supervised conversion worker, resource lease, bounded output and
report-last publication. They remain unverified for quotation and do not invent
printed pages or claim exact text/layout fidelity.

calibre's documented input support and options are only the vendor capability
map. They are not evidence that this application's integration has passed:
https://manual.calibre-ebook.com/faq.html
https://manual.calibre-ebook.com/generated/en/ebook-convert.html

## Selection and execution boundary

Selection requires an absolute executable path. No PATH discovery, hidden
installer, package manager, remote book fetch, user plugin or DRM-bypass interface
is provided. The selected directory's complete file inventory is bounded to
8192 entries, 2 GiB total and 512 MiB per component. Parent/file identity and
SHA256 are checked before and after execution. Executables, native libraries and
packed code stay open through the existing immutable-reader guard. This binds
the selected installation; it does not claim to seal Windows system libraries.

A bounded private manifest carries that inventory so the established 64KB worker
request limit remains unchanged. The child accepts only the exact fixed private
manifest path and digest. A changed/added/removed selected component, a missing
companion, source change, cancelled worker, invalid output or exhausted budget
prevents a completed publication.

The fixed calibre helper runs through its documented interpreter entrypoint with
fresh empty config/cache/temp directories. It disables Python templates and
installs an audit guard before conversion: network and new subprocess operations
fail, writes remain in the private stage, and unselected document paths cannot
be read through Python file operations. The adapter invokes only the existing
plain-text conversion CLI. It passes no recipe, user metadata file, filter,
plugin, password or decryption key. These controls and native process ownership
are not an operating-system filesystem sandbox.

The reviewed upstream LIT source distinguishes licensed/personalized inputs from
the openly generated level1 format that its own writer emits with a zero content
key. The wrapper refuses the former before the reader runs and rejects any
nonzero level1 content key. No additional cryptographic code is supplied:
https://github.com/kovidgoyal/calibre/blob/v9.19.0/src/calibre/ebooks/lit/reader.py
https://github.com/kovidgoyal/calibre/blob/v9.19.0/src/calibre/ebooks/lit/writer.py
https://manual.calibre-ebook.com/generated/en/calibre-debug.html
https://manual.calibre-ebook.com/customize.html

CHM extraction uses fixed l -slt and x -so -spd arguments, never archive writes
to document-supplied paths. Listing, member, expansion, text, record and time
budgets remain independent. Case aliases, traversal, ambiguous listings,
encrypted entries and truncated member output fail. HTML runs through the
current non-executing parser. TOC order is recorded; unlisted HTML is appended in
stable name order with an explicit warning. A logical member is not a printed
page.

DjVu's native page count must match the bounded IFF page inventory. No hidden
text is fabricated for scans. This version refuses shared-dictionary/indirect
references rather than opening adjacent or remote components:
https://djvu.sourceforge.net/doc/man/djvutxt.html

## Actual fixture gate

.github/workflows/carr-legacy-converters.yml has independent Windows jobs for
calibre, DjVuLibre and CHM. Each builds an exact source-bound wheel, verifies all
installed Python bytes and runs under -I. The current oracle requires 221
portable boundary cases from both source and installed wheel: all 179 exercised
by the recorded calibre run, six explicit-selection regressions, two
strict-version rejection cases and 34 bounded CHM recovery cases. Both DjVu and calibre native fixtures use the
ordinary service admission path with legacy_preview=False. CHM fixtures retain
explicit preview admission until their separate qualification succeeds.

The separate real-tool populations also run from source and installed wheel:

* calibre 9: six actual unprotected output/input formats, protected-input refusal,
  output budget, cancellation after an actual native output exists.
* DjVuLibre 4: standalone and bundled synthetic pages made by the selected cjb2,
  djvused and djvm, a missing text layer, and an external-reference refusal.
* CHM 4: a synthetic ITSF3/PMGL file, the publisher's compressed redistributable
  7-Zip help file from its exact release package, output budget, and unsafe member
  refusal. The synthetic generator does not replace actual 7-Zip execution.

scripts/legacy_conversion_oracles.json fixes all function multiplicities.
Failure, skip, XFAIL/XPASS, timeout, changed installed/runtime bytes or an absent
fixture result fails the job. Raw logs, JUnit and preparation evidence are always
retained. A successful gate does not automatically edit format qualification.

No real user documents, paid services, personal credentials or proprietary
test books are used. Native Windows outbound rules cover all prepared converter
executables during fixtures. Publisher packages stay in the ephemeral runner;
they are not bundled into the application.

## Pinned runtimes and provider constraints

packaging/legacy-conversion-runtimes.json records exact immutable releases.

* calibre 9.19.0 portable installer, official release asset API SHA256 and exact
 259,843,848 bytes. Its published portable installer receives only the fresh
 qualification destination. https://github.com/kovidgoyal/calibre/releases/tag/v9.19.0
* 7-Zip 26.06 Windows x64 MSI, official release asset API SHA256 and exact 2,003,456 bytes.
 Administrative extraction prepares the selected runtime. Source and license:
 https://github.com/ip7z/7zip/tree/26.06
 https://www.7-zip.org/license.txt
* DjVuLibre 3.5.30-2 MSYS2 UCRT64 package, publisher SHA256
 ed51d57f1af30d963a1651ed71faae7ddf8824a3c685acd19203cea73335a641 and a 4 MiB
 download ceiling. This is explicitly an MSYS2 build. Its dependency DLLs are
 copied only after verifying the existing complete OCR manifest/lock closure.
 Actual source and installed native loading passed in run 46018252073;
 provider metadata alone is not execution evidence.
 https://packages.msys2.org/packages/mingw-w64-ucrt-x86_64-djvulibre
 https://djvu.sourceforge.net/

The tools retain their publisher licenses: calibre GPLv3; DjVuLibre GPLv2;
7-Zip LGPL2.1-or-later with its documented component notices/restrictions.
They are selected external programs, not a relicensed application dependency.
No claim of verified detached publisher signatures or reproducible vendor
binaries is made. The official release/source links and fixed checksums identify
what the gate actually obtained.

Observed native correction (2026-11-27): the pinned calibre closure exceeds the
initial 384-reader allowance. The exact source now allows at most 1,024 held
code readers, retaining every code identity and final check. The real publisher
closure and all nine source/installed fixtures must pass before any format is
advertised; no stdio limit or process isolation is altered. The cancellation
fixture uses a synthetic valid literal TCR dictionary because the publisher's
compressor failed on its repetitive input; actual decoding remains required.

Observed CHM correction (2026-11-27): the publisher's compressed help file closes
an already implicitly closed paragraph in cmdline/commands/bench.htm. For the
four declared known legacy HTML doctypes only, the CHM adapter reuses the existing
inert parser and its text/node/token/depth/time budgets to insert an empty start
paragraph before that unmatched closing paragraph. Every other structure check
remains in force, and general HTML import is unchanged. Active and hidden content
stay omitted. Original member bytes and transformed parser bytes have separate
retained SHA256 values, and the report records this normalization. The actual
CHM successor still must pass before .chm can be qualified.

Observed calibre correction (2026-11-27): its pinned safe_atexit component tried
to start a generic cleanup daemon when the actual conversion created a persistent
temporary directory. The fixed wrapper now validates and delegates only that
component's unlink/rmtree registrations for descendants of the private stage to
the existing outer conversion owner. It creates no helper, stored callback or
new job store. Program registrations, outside paths, network and subprocess
execution remain rejected. Temporary bytes stay owned until native settlement
and are then removed by the existing stage cleanup, including cancellation.
The pinned publisher source used for this narrow adaptation is:
https://github.com/kovidgoyal/calibre/blob/v9.19.0/src/calibre/utils/safe_atexit.py
https://github.com/kovidgoyal/calibre/blob/v9.19.0/src/calibre/ptempfile.py
The six actual unprotected formats and the three native refusal/budget/cancel
cases passed at779967 in both source and installed fixture-preview mode.
The ordinary-admission successor08fb93 then passed all nine native cases and
187 portable cases per mode, independently read from the exact artifact.
See evidence/calibre-native-preview-independent-read-20261127.json and
evidence/calibre-ordinary-admission-independent-read-20261127.json for their
separate frozen heads, raw JUnit, installed origins and publisher identities.

## Complete CHM publisher census and bounded HTML recovery

The fixed publisher-fixture diagnostic at20be86 enumerated all70 HTML members
and completed their inert reading checks. Eleven members failed strict nesting;
their raw member hashes, exact offending token classes and source positions are
retained in evidence/chm-publisher-census-recovery-20261127.json. The observed
run remains FAIL (three source native cases passed and one failed; installed
native cases did not run). The diagnostic did not execute document content.

For the four already recognized legacy PUBLIC HTML declarations only, recovery
now reuses the installed lxml HTML parser after the existing inert reader has
admitted the complete original token stream. The admitted additions are the
observed unmatched anchor/bold/list/definition/header/IL end tokens and an
otherwise empty trailing UL. An unknown orphan structural close, malformed
closing-token attributes, unsafe implicit closure, ambiguous attributes,
unsupported declaration or invalid character remains an error. The previous
empty-paragraph normalization and general HTML reader are unchanged.

The lxml parser receives bounded UTF-8 bytes in memory with no network and a
resolver that refuses all external resources; no XML/DTD loading, URL parsing,
browser, XInclude or script execution is introduced. Source, tree node/depth,
token, visible-text, output-byte and absolute deadline bounds apply. Recovered
bytes are passed through the existing strict inert reader again. The ordered
visible-word fingerprint must equal the original admitted preview, so recovery
cannot silently discard, reorder, add or expose visible content. Whitespace
and layout remain unverified. Both original member SHA256 and parsed-member
SHA256 remain in the existing source locator.

The additional34 cases cover every observed malformed shape, declared legacy
charsets and numeric Unicode, active/hidden suppression, pre-recovery refusal
and budgets, serialized expansion, post-recovery text loss/reordering/exposure
and complete worker provenance. The focused Linux source-only lane requires
these34 plus all100 existing legacy adapter cases; it does not qualify a native
Windows converter. The authoritative Windows lane still requires all221
portable cases per mode and all four actual CHM native cases per mode. CHM
remains unqualified until that frozen candidate succeeds.

Primary parser API and its explicit recovery/data-loss caveat:
https://lxml.de/parsing.html
https://lxml.de/apidoc/lxml.etree.html
