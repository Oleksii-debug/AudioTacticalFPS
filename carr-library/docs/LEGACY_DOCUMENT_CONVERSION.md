# Selected legacy document reading

Authoring status: **implemented, native qualification NOT_RUN**. No new legacy
format is advertised as available yet. conversion/legacy.py keeps
QUALIFIED_SUFFIXES empty. The desktop adds a chooser only for a format whose
exact source-bound native fixture result is subsequently recorded and reviewed.
The explicit legacy_preview=True API is for this controlled fixture evaluation;
selecting a binary alone cannot promote an untested format.

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
installed Python bytes and runs under -I. Each requires the existing 81 plus
new 54 portable boundary cases from both source and installed wheel.

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
 Actual native loading still has to pass; provider metadata is not execution.
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
