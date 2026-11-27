# Selected-converter native qualification

This is the explicit Windows CI route for the external-runtime reading-copy
feature. It is separate from the portable 106-case source/installed gate and
the 95-case strict XML suite. A completed route qualifies the named reading
derivatives and observed controls only. Native catalog fidelity, scanned-DjVu
OCR, DRM bypass, linguistic/layout quality, physical audio and NVDA remain
NOT_RUN unless independently tested.

No command below downloads into or changes the application bundle. The caller
owns an isolated temporary runtime directory and retains JSON/text/JUnit only.

## Acquire and bind before restricting native network access

Use packaging/windows-document-converters.lock.json from the exact checked-out
commit. Pin that file, the acceptance/support/native scripts, and
scripts/translation_loopback_check.py to their expected Git blob identities.
The latter is the existing strict literal-loopback receipt validator.

Download only the three named runtime archives and the four named public
fixture/notice files in the lock. Save the exact filenames to a fresh directory
outside the checkout and all evidence/upload directories. Enforce each
maximum/expected byte count while streaming, the 1 GiB combined download
budget, and the listed SHA-256 or Git-blob SHA-1 before extraction. Git-blob
verification includes the normal "blob <decimal byte length>\\0" header.
Do not print authenticated HTTP headers or redirect credentials. The public
URLs in this lock require no user credential.

The exact LibreOffice MSI URL is the vendor's URL. Its immutable archive hash
is recorded from the pinned Microsoft WinGet manifest; do not describe that as
a separately fetched vendor checksum. Calibre and Pandoc asset digests are
from their official release records. A runtime pin is not successful execution.

Use administrative extraction of each verified MSI into a separate fresh
directory with Windows' existing msiexec /a /qn TARGETDIR mechanism. Do not
install it into a system location or reuse a preinstalled user instance.
Extract the verified Pandoc ZIP only after checking every member: reject
absolute/traversing/duplicate/case-alias paths, links/reparse entries and
unbounded member/expanded size. The lock declares 50,000 members and 4 GiB
combined extracted bytes. After each extraction, verify those finite totals
and require exactly one compatible selected executable:

- Calibre: ebook-convert.exe in the extracted compatible installation.
- LibreOffice: program/soffice.com in the extracted compatible installation.
- Pandoc: pandoc.exe in the extracted compatible directory.

The CI-only implementation of this acquisition is
scripts/prepare_selected_conversion_runtime.py --root <fresh-direct-child-of-RUNNER_TEMP>
--receipt <fresh-evidence/acquisition.json>. It binds the exact checked-in lock,
uses the existing bounded CI process cleanup, and records the owned root
identity for later removal. The application has no call to this script.

Keep complete runtime layouts and their original license/notice files.
Capture explicit absolute paths; do not add Calibre or LibreOffice to PATH.
The application checks their selected core layout and versions. Reject
ambiguity, missing components or a different version. Runtime extraction is
not part of the downloadable application/evidence artifact.

Prepare the existing pinned Windows Python/wx/test closure and a verified
application wheel using the existing source-wheel and installed-origin gates.
Source mode adds only the explicitly supplied source root. Installed mode
uses the selected installed interpreter with -I, no checkout injection, and
verifies site-packages origins outside the checkout. These are separate runs.

## Preserve the explicit external-egress boundary

Before running any selected native executable, enumerate every .exe and .com
under the three extracted installation roots, plus the selected/base
python.exe and any present adjacent pythonw.exe. The helper's native_programs
function performs the same bounded no-link inventory and must agree with the
caller. An outbound rule for Python alone does not cover a native descendant.

Reuse the existing Windows native loopback gate's process-scoped rules. Save
the three firewall profile states, enable Domain/Private/Public, create the
exact external-address blocks for every inventoried program, then read back
the live rules, program filters, directions/actions/enabled states, profile
states and remote ranges before attesting a receipt. Required ranges are:

- 0.0.0.1-126.255.255.255
- 128.0.0.0-255.255.255.255
- ::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff

The old suite uses an actual 127.0.0.1-only fixture. The receipt therefore has
status PASS, allowed_test_peer 127.0.0.1, blocked_programs containing every
inventoried executable, blocked_remote_ranges containing exactly these three
ranges, and enabled_profiles containing exactly Domain, Private, Public.
The actual old pytest child also denies other Python socket peers and DNS.
Do not route this whole legacy suite through common ci_check's all-socket
denial, and do not remove its existing real HTTP-denial test.

Retain the firewall through all child/native/UI cleanup. In finally, remove
only rules created by this run and restore/read back the original profile
states. A rule/cleanup failure fails qualification and stays in its reports.
Do not change broader application policy to satisfy this route.

## Run the source and installed commands

The following are argument layouts. The caller supplies verified absolute
paths and a fresh bounded output folder for each mode. Preserve the common
native helper beside the scripts and the source tree for hash comparison;
installed mode must still import only the installed application.

Headless selected-format phase:

    <python> -I -B -X utf8 scripts/selected_conversion_acceptance.py
      --phase formats --mode <source|installed>
      --source-root <checkout/carr-library/src>
      --calibre <selected/ebook-convert.exe>
      --office <selected/program/soffice.com>
      --pandoc <selected/pandoc.exe>
      --downloads <seven-verified-files>
      --runtime-lock <checkout/carr-library/packaging/windows-document-converters.lock.json>
      --firewall-receipt <active-receipt.json> --output <fresh-output>

Whole pre-existing conversion suite:

    <python> -I -B -X utf8 scripts/selected_conversion_acceptance.py
      --phase old-suite --mode <source|installed>
      --source-root <checkout/carr-library/src>
      --calibre <selected/ebook-convert.exe>
      --office <selected/program/soffice.com>
      --pandoc <selected/pandoc.exe>
      --downloads <seven-verified-files>
      --runtime-lock <checkout/carr-library/packaging/windows-document-converters.lock.json>
      --firewall-receipt <active-receipt.json> --output <fresh-output>

Separate native control phase:

    <python> -I -B -X utf8 scripts/windows_document_conversion_smoke.py
      --mode <source|installed> --source-root <checkout/carr-library/src>
      --calibre <selected/ebook-convert.exe>
      --office <selected/program/soffice.com>
      --pandoc <selected/pandoc.exe>
      --downloads <seven-verified-files>
      --runtime-lock <checkout/carr-library/packaging/windows-document-converters.lock.json>
      --firewall-receipt <active-receipt.json> --report <fresh-output/native.json>

Run both headless phases in both modes. Their outer watchdog is 1,200 seconds
per process through cleanup; the old isolated pytest child has its existing
600-second whole-suite limit. A 21-minute step envelope leaves bounded
report/cleanup room around each headless process. Native control processes
retain a 180-second cleanup-inclusive guard and a five-minute step envelope.
The combined Windows step admits all four headless runs plus two GUI runs
within 100 minutes, retaining their individual 1,200/180-second guards. The
285-minute job envelope covers the 105-minute portable source/installed step,
35-minute acquisition, 15-minute dependencies, this 100-minute native step,
owned input cleanup/retention and checkout/setup/artifact overhead. None of
the existing 600-second whole-suite deadlines is increased.

Failure of an independent phase must remain a failed required verdict while
later independent phases may run if their own acquisition/install/firewall
prerequisites succeeded. Do not transfer source results to installed mode or
mask a missing executable, fixture, receipt, test or control as a passing skip.

## Required evidence

For each mode, selected-conversion-formats-<mode>.json must have status PASS,
phase formats, the matching mode, identical 22-module before/after origin
maps, the exact helper/lock/archive identities and all 14 distinct expected
suffixes. Each row must retain original/TXT/report digests, truthful fixture
classification, actual engine and verified-copy/import/no-clobber PASS.
Require actual_native_descendant_cancel, actual_format_conversions,
canonical_reading_copy_import, source_originals_unchanged,
native_children_and_leases_settled, all_owned_jobs_closed and
owned_temporary_cleanup to be PASS. actual_owned_jobs must be a positive int.

The old-suite report must have status PASS, phase old-suite, matching mode,
old_cases exactly 34, exact required base-name Counter/JUnit with zero
failure/error/skip/XFAIL/XPASS, exact test/binary companion hashes, matching
22-module proofs inside the real pytest child and parent, and the retained
child-origin/JUnit hashes. Require old_whole_conversion_suite,
actual_loopback_denial, all_owned_jobs_closed and owned_temporary_cleanup PASS.
Keep the entire tests/conversion/test_conversion.py and its existing
fixtures/ordered_slides.pptx companion; do not substitute extracted tests.

Native controls require the exact final named-check list and 30-module origin
map from their final manifest. All product checks are performed using the
actual host, worker, selected Pandoc and canonical importer; only native
file/folder selections and the queued-read barrier are controlled. A synthetic
nested-dialog cleanup check is explicitly a fixture. It does not itself
qualify the product's default-No, explicit Yes, reader or import behavior.

Closed report fields for format quality/native catalog fidelity/DRM/scanned
DjVu/NVDA remain NOT_RUN. Native controls additionally label physical audio
NOT_RUN and name the controlled file-picker boundary. Receipt/source hashes
are evidence of the exact run, not execution authority for imported records.

Retain only bounded .json/.txt/.xml report files from the selected output
directories. Bound individual files and total inventory, reject links and
unexpected suffixes, and record archive IDs/SHA-256 plus actual checkout
commit/tree. No installer, runtime binary, selected original, public CHM/DjVu,
generated input, converted TXT, library database, model or private asset is
included. Runtime notices may be retained as text with their exact identities.
The app packaging/installer closure remains unchanged.

## Current implementation status

This source and gate plan is STATIC_NOT_RUN. The headless helper/owned-family/
triplet/publication/origin seams have independent static reviews. The actual
Windows inventory at commit ddd372d67fd5c06852c0f29f137fbf49ee10e29e showed
nested native Button handles with ID zero. The current GUI probe uses the
bounded observed descendant-button pattern under its exact title/PID/owner
checks; default-No focus, actual keyboard answers and cleanup still require a
new real native run. The inventory's fixture-only EndDialog was not product
or keyboard acceptance.

The coherent next15 diagnostic executes portable source/installed contracts
and all six required native headless/control phases. Its permanent pull-request
trigger checks out the exact PR head; a separate diagnostic push is unnecessary
when the integration PR is updated. All results remain NOT_RUN until that
actual source revision completes.
