# Selected legacy documents and archived reading text

The existing **Convert → Convert documents for reading** action preserves the
selected original and writes a new UTF-8 text file with its source-hash and
text-hash provenance receipt. Current no-clobber publication, verification,
cancellation and retained native cleanup remain the same.

## Supported source paths

| Family | Reader selection | Supported scenario and limitations |
| --- | --- | --- |
| DOC, PPT, XLS | Explicit installed LibreOffice | Binary compound documents using the fixed Word97, PowerPoint97 or Excel97 import filter, then guarded ODT/ODP/ODS reading. Exact visual layout, printed pagination and spreadsheet formula recalculation are not promised. |
| WPD | Explicit installed LibreOffice | WordPerfect document-header family with the fixed WordPerfect filter. Review conversion fidelity. |
| PDB | Explicit installed LibreOffice | PalmDoc TEXt/REAd containers only; other PDB types are rejected. |
| WRI | Explicit installed LibreOffice | Microsoft Write BE31 text-document header family with the fixed MS_Write filter; OLE-containing/other header families are rejected. Exact runtime acceptance depends on the selected build. |
| MHT, MHTML | Built-in isolated reader | The declared multipart/related text root, identified by its start Content-ID or first direct part. Duplicate/missing root identifiers, malformed MIME and non-text roots are rejected. Attached resources are not opened or fetched. |
| TXTZ | Existing guarded ZIP reader | Exactly one TXT member, with optional omitted non-text metadata/resources. Text encoding follows the existing strict decoder. |
| ZIP | Existing guarded ZIP reader | TXT, Markdown and HTML-family members in lexicographic path order. No filesystem extraction, nested archives, active-code members, encrypted members or unsupported compression. Omitted non-text members are counted in the report. |

The source format suffix is not proof of content. Binary legacy headers and
installed fixed filters are checked before conversion. All reading derivatives
remain unverified for quotation and require source review. Plain-text extraction
does not preserve full document formatting.

## Explicit local office selection

For one of the selected legacy families, the native conversion flow asks for
the actual installed `program/soffice`, `soffice.exe` or `soffice.com`.
There is no PATH fallback, download, document-supplied command or persistent
converter configuration. Choosing Cancel publishes nothing. The queued work
rechecks the chosen library, input files, output directory and converter identity.

The component inventory binds the selected launcher, soffice.bin, known present
launch helpers and the main/Writer/Calc/Impress registries. Fixed import/export
filter availability and inactive-content settings are independently checked in
the worker. This inventory does not seal every installed library or system font.

Each operation uses a private LibreOffice profile with macros, active content,
OLE automation, untrusted referenced links, external updates and update checking
disabled. It performs a fixed local import/export into a guarded ODF intermediate
and uses the existing document reader. The selected native program is not an
operating-system sandbox. No plugin, arbitrary shell command, live office server
or network input is introduced by this adapter.

The native UI selects a declared 4 GiB worker memory budget for this optional
converter; the existing worker enforces address/CPU/file limits on POSIX. Windows
uses the existing owned process family and time/file budgets; the memory value
is not a new Windows process-memory quota. Component and output sizes, private
profile entries, diagnostics and time are bounded. An uncertain native shutdown
retains staging through the existing cleanup owner.

API callers choose their explicit budget:

```python
service = ConversionService(
    destination, input_roots=(source,),
    office=selected_installed_soffice,
    budget=ConversionBudget(memory_bytes=4 * 1024**3, timeout=120),
)
result = service.extract(source, "reading.txt")
```

Use the resulting reviewed reading copy in existing document batches, narration
or translation. The batch record does not retain an execution grant to the
selected LibreOffice installation.

## Evidence and remaining format boundaries

The source admission tests use inert synthetic component inventories and never
claim those files are executable runtimes. The explicit
`tests/conversion/office_native_acceptance.py` gate provisions and selects a
real LibreOffice, generates synthetic DOC/PPT/XLS and text-only WPD/PalmDoc/WRI
fixtures, and checks source preservation, current provenance, cancellation and
bounded failure. It is invoked explicitly with `CARR_SOFFICE`; missing runtime
is an error, not a skipped test.

`test_archive_reading.py` exercises actual isolated MHTML/TXTZ/ZIP conversion
and refusal of ambiguous roots, archive traversal, duplicate names, active code
and expansion excess. Existing conversion and publication suites remain required.
New checkpoint qualification is NOT_RUN until the exact CI evidence exists.

AZW/AZW3, CHM, DjVu, LIT, MOBI, PRC and TCR use the separately selected
[Calibre reading adapter](CALIBRE_READING.md), with its own actual eight-format
gate and explicit fidelity boundaries. Non-ZIP generic archives remain outside
this adapter. No renamed-file compatibility, decryption, DRM bypass, arbitrary
external decoder or unverified universal-format claim is added. Pronunciation
dictionary interchange and verified SAPI4 driver combinations retain their own
reader/runtime acceptance.

Primary interface references:

- [LibreOffice fixed conversion filters](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)
- [MHTML aggregate MIME contract, RFC2557](https://www.rfc-editor.org/rfc/rfc2557)


## Reviewed Office completion boundary

The current gate selects LibreOffice 26.8.3 on both operating systems. Linux uses
the official archive, checked at 221377373 bytes and SHA-256
`e7cd3538e85818ff9dd9bb65386769fde927011242aace5a144989f4000dcabf`.
The older distro 24.2.7 runtime was correctly refused because it lacked a required
macro/OLE control; no control was removed to admit it.

Actual Windows diagnostic fixtures identified two harmless empty nodes:
a static `draw:enhanced-geometry` after the PPT's source text and a disabled empty
XLS `style:header-left`. The parser accepts only their bounded declarative shape;
it never evaluates geometry or suppresses hidden authored text. Geometry tokens
are consumed once, with finite numeric limits and an owned-child malformed-input
regression. Conversion still reports that drawing geometry is not rendered.

The actual prior workspace gate passed all 276 common tests and six of the nine
Windows native cases. The corrected all-nine source/installed gate is
[run46044193352](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46044193352)
at `4b1afc83fbf3e932880e6de71b8861857ab13712`; its attached results,
and the final integrated gate, determine execution status. This document does
not predeclare a queued run as passed.
