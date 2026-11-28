# Selected local converters: verified reading copies

**This is limited conversion support for reading and ordinary text import.**
The selected native source is not admitted to the catalog as a native-format
source. A completed conversion preserves its exact original, UTF-8 reading
text and an inert fidelity report together in a new external folder. Read and
Import verify that completed folder before use.

The application reuses the existing Convert menu, shared host worker,
conversion worker, resource broker, Windows Job owner and atomic directory
publisher. It does not install a runtime, discover a new optional converter
on PATH, train a model, remove DRM or create another reader/catalog namespace.

## User workflow

1. Open the destination library and workspace. Choose **Convert → Convert
   documents for reading...**.
2. Select the documents. Explicitly select each needed existing local runtime:
   Calibre's ebook-convert.exe, LibreOffice's program/soffice.com or soffice.exe,
   or Pandoc's pandoc.exe. Cancel at a picker starts no conversion.
3. Choose an output folder outside the library's private data directory.
   Heavy hashes and native conversion run on the existing host worker.
4. Each completed selected-format result has one new directory containing the
   named reading TXT, an original file with the selected suffix, and
   provenance.json. These files are published together. An existing result
   directory is refused, including a competitor containing matching bytes.
5. In **Converted reading copies**, inspect the named original and report.
   **Read verified copy** checks all three files and opens the existing
   editable voice reader as an independent, unbound reading copy. It does
   not speak automatically or create a native source-page locator.
6. **Import reading copy** asks an explicit default-No confirmation. Yes
   verifies the same result again and calls the existing TXT importer for
   the captured library and workspace.

**Keep the external original and report.** A library backup contains the
imported TXT and its normal catalog/import state. It does not silently include
the external legacy original or grant authority to the external report.
Reading or importing converted text does not approve native-source quotations.
Native pagination, coordinates, rendered layout and scholarly quotation
fidelity require a separate supported source-review workflow.

The library object, workspace, directory identities, selected inputs and runtime
identities are captured before queueing. A queued operation or a native
picker/confirmation returning into a different scope is refused. Closing the
result window revokes its queued Read/Import callbacks and signals its owned
work. The shared host and existing reader retain their own cleanup owners.

## Finite conversion matrix

| Selected family | Explicit local runtime | Implemented route and limits |
| --- | --- | --- |
| AZW, AZW3, MOBI | Calibre | Supported unprotected ebook containers to plain UTF-8. False headers and converter refusals fail. No DRM removal, account access or visual-layout reproduction. |
| LIT, TCR | Calibre | Supported unprotected document text. A suffix alias does not admit an unsupported container. |
| PRC | Calibre | Supported PalmDoc/MOBI document families only. Generic Palm application databases are refused. |
| CHM | Calibre | Compiled help text through the selected installed reader. Scripts, interactive navigation and visual layout are not reconstructed. |
| DjVu | Calibre | Existing document text layers only. No OCR of scanned-only DjVu or invented page text. |
| DOC, PPT, XLS | LibreOffice | Fixed input filters produce private ODT/ODP/ODS; the existing strict ODF parser reads those results. This is not Office automation or native-layout import. |
| WPD | LibreOffice | The supported selected WordPerfect family through the fixed ODT route. Other proprietary variants can be refused. |
| PDB | LibreOffice | PalmDoc TEXtREAd document databases through the fixed ODT route. |
| RTF | Pandoc | Fixed sandboxed RTF-to-plain conversion. No custom filters, templates, PDF engine, shell fragment or resource embedding. |

Calibre and LibreOffice are enabled only on the existing owned Windows Job
route. Their helpers must remain in that process family until completion,
cancellation or timeout is settled. Equivalent descendant cleanup is not
claimed on other platforms. Selected RTF uses the existing process owner and
fixed single-tool command.

The catalog's native supported-suffix set is unchanged. Existing TXT, PDF,
DOCX, EPUB and additional-format imports keep their source validators and
storage contracts. An unsupported native source is never merely renamed to TXT.

## Selection, resource and commit boundaries

A selected Calibre installation must have its expected launcher, frozen Python
payload and matching Python DLL family. A selected LibreOffice installation
must have its executable, core binary and fixed filter/security registry
records. File identities are captured before queueing; selected core files are
hashed on the worker and held through native-family settlement. Pandoc binds
the exact executable bytes. Changed files, directories or library roots fail.

This is a checked **selected core**, not a claim that every OS dependency or
every byte in a third-party installation has been independently sealed. Local
runtimes remain external software with their own licenses and parser risks.

The current ResourceBroker admits CPU, declared memory, disk and waiting.
Calibre/LibreOffice default to a 3 GiB declared memory reservation. Selected
sources are bounded at 64 MiB, reading text at 32 MiB and reading records at
10,000, also subject to smaller caller budgets. Diagnostics, staging entries,
private profiles and output growth have finite limits. These are cooperative
reservations and observed byte/time limits, not an OS whole-system memory
sandbox for a third-party process.

LibreOffice receives a fresh private profile and fixed filters. Its supported
configuration properties disable macros, active content, OLE automation,
untrusted referer links and automatic updates. An existing office session,
server/listener, document-supplied CLI or user profile is not reused. Calibre
gets private config/cache/home/temp directories and disabled custom plugins.
The selected Python worker denies sockets. The app does not change system
firewall policy; the native qualification has a separate explicit CI boundary.

Pending native cleanup is settled while source/core readers, staging and the
broker lease remain held. The current no-replace directory publisher provides
one commit point. Cancellation before it leaves no partial three-file result.
A cleanup or durability error after an actual successful publication preserves
the returned completed result and reports attention. It does not relabel
visible files as cancelled or delete/adopt a competitor.

Read/Import accept the live returned result, validate its captured directory
identity, exact canonical report digest, exact three filenames, original/text
hashes and UTF-8. Paths in an external report grant no execution authority.
During canonical import, the held folder and all three identities are checked
again at existing importer boundaries. A completed import actually returned by
that importer remains completed if later reader cleanup reports an error.

## Qualification and current status

**New implementation and gates are NOT_RUN until their exact source and
installed executions succeed.** Runtime pins and static reviews are not
conversion results.

Portable whole suites cover selected conversion 26, Calibre admission 44,
LibreOffice admission 19 and handler/lifetime 17: **106 static cases**. These
use real files, library imports, process owners, broker leases, publication,
cancellation, competitors and changed identities. Controlled native converter
stand-ins in those tests do not qualify actual format conversion.

The existing strict XML suite retains all 88 cases and adds seven real ODF
package cases: **95 static cases**. Empty converter-generated form containers
and inert vector geometry are admitted only in exact supported shapes.
Textual or active content cannot silently disappear inside them.

The separate native route requires:

- **Formats phase:** actual Calibre/LibreOffice-generated synthetic binaries,
  real PalmDoc/WordPerfect fixture bytes, synthetic RTF and two pinned public
  CHM/DjVu documents. All 14 families must preserve original/text/report
  identity, meet reading assertions, refuse clobbering and complete a real
  TXT import. Real descendant cancellation must settle its Job, lease and
  staging. The two public input documents are explicitly not called synthetic.
- **Old-suite phase:** all 34 cases of the existing conversion suite, including
  the actual bounded 127.0.0.1 HTML-denial server. Pandoc is explicitly provided.
  The old OCR-availability behavior remains unchanged and supplies no new OCR
  qualification. Exact JUnit, no failures/skips/XFAIL/XPASS, before/after
  origins inside the actual pytest child and the existing 600-second
  whole-suite limit remain mandatory. This whole suite does not enter the
  common all-socket-denied child policy.
- **Native controls:** the actual DesktopFrame Convert command, selected
  Pandoc, result controls, verified manual reader Save/Close, default-No/Yes
  import, tamper refusal and queued-Read revocation. Only file/folder picker
  answers and a queued-read barrier are controlled. The worker, converter,
  source checks, reader and importer are real.

The runners are scripts/selected_conversion_acceptance.py (formats or
old-suite) and scripts/windows_document_conversion_smoke.py. Each runs for
source and isolated installed packages with exact source bytes before/after.
The headless guard is 1,200 seconds including cleanup; the old whole suite
retains 600 seconds. The native-control guard is 180 seconds through all worker,
handle and filesystem cleanup. All required phases fail on missing runtime or
evidence; no optional successful skip is manufactured.

Retain bounded JSON, text and JUnit only. Runtime installers/binaries, public
documents, generated inputs, converted text and private data do not belong in
the artifact. Manual NVDA, physical audio, visual/linguistic quality,
135-language coverage, DRM bypass, scanned-DjVu OCR and native catalog fidelity
remain separately unqualified.

## Runtime pins and primary sources

The CI-only inventory is
[windows-document-converters.lock.json](../packaging/windows-document-converters.lock.json).
The application does not download from it.

| Runtime | Qualification candidate | Exact archive hash authority |
| --- | --- | --- |
| Calibre | 9.19.0; source d69ad111794cf2032264d2b02b3f9b9adfb139a2 | Official [9.19.0 release](https://github.com/kovidgoyal/calibre/releases/tag/v9.19.0) asset digest. |
| LibreOffice | 26.8.3.1; source 861effc5289d539f15c4c18d1ba8579ffd78728b | Official vendor MSI URL and [Microsoft WinGet manifest](https://github.com/microsoft/winget-pkgs/blob/eb172399c084b8b1e06a868d026405291a0cfe37/manifests/t/TheDocumentFoundation/LibreOffice/26.8.3.1/TheDocumentFoundation.LibreOffice.installer.yaml). The hash authority is separate from the vendor. |
| Pandoc | 3.14; source 3dde36a9ddd68b8677397d9c59c891d9b8763b89 | Official [3.14 release](https://github.com/jgm/pandoc/releases/tag/3.14) asset digest. |

Primary contracts:

- [Calibre CLI](https://manual.calibre-ebook.com/generated/en/ebook-convert.html),
  [format/DjVu/DRM FAQ](https://manual.calibre-ebook.com/faq.html), and pinned
  [text-output plugin](https://github.com/kovidgoyal/calibre/blob/d69ad111794cf2032264d2b02b3f9b9adfb139a2/src/calibre/ebooks/conversion/plugins/txt_output.py).
- [LibreOffice CLI](https://help.libreoffice.org/latest/en-GB/text/shared/guide/start_parameters.html),
  pinned [security schema](https://github.com/LibreOffice/core/blob/861effc5289d539f15c4c18d1ba8579ffd78728b/officecfg/registry/schema/org/openoffice/Office/Common.xcs),
  and [Excel legacy filter](https://github.com/LibreOffice/core/blob/861effc5289d539f15c4c18d1ba8579ffd78728b/filter/source/config/fragments/filters/MS_Excel_97.xcu).
- [Pandoc sandbox manual](https://pandoc.org/MANUAL.html).
- [Pinned PuTTY CHM and accompanying permission](https://github.com/mlocati/chm-lib/tree/9c338155440c40e5830f9ed0d4892d041f22827c/test/samples).
- [Official DjVuLibre public document fixtures](https://github.com/DjVuLibre/djvulibre/tree/5fb9bfc934cb153ff9babe34073d0683dcab5d9a/doc)
  and [COPYING](https://github.com/DjVuLibre/djvulibre/blob/5fb9bfc934cb153ff9babe34073d0683dcab5d9a/COPYING).

GPL and LibreOffice/component terms and notices remain applicable to the
external runtimes. Exact public fixture notices are retained for qualification.
The app bundle and evidence artifact do not redistribute these runtimes or
public documents. A user still needs rights to process their chosen document.
