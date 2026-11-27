# Canonical research and publication reconciliation — 2026-11-27

## Exact comparison

Canonical source: `966e5ccf892243ead6942c635af5fbab47e72551`, tree
`682b267cb237804c7a15e6ac0285b9b6861b847c`. Its recovered primary103 integration
was compared by repository paths and source bytes with published
`dac0998afa3b5063f4895f675741a0612853ed45`, tree
`10e5c64f1a2fa22d2ebe180382a969c34f187169`. This narrow port starts after
`c2845d07ce8e60176909e10cd8f56c30bbb67f1e`, whose tree is
`30dea5bd0924549a91b3e5b1d28b963a09584924`.

| Area | Exact source finding | Integration decision |
| --- | --- | --- |
| `catalog/` | All module bytes match between the two compared heads. | Retain the current catalog and its database authority. |
| `research/` except `export_file.py` | All module bytes match, including academic library, BibTeX import, source comparison, topic matrix and outline exports. | No second research stack or wholesale branch replacement. |
| CSL processor, styles and locales | Vendor files and style assets match. Our `citations/csl.py` additionally retains stable document citation IDs and note-index contexts. | Keep those current APIs and their editable Word consumers. |
| Bibliography workbench | Canonical adds an optional publication receipt and earlier destination capture; its older surface lacks our editable citations and regeneration. | Port the additive receipt/capture behavior; retain `docx-citations` and regeneration. |
| `reader/reading_state.py` | The canonical difference removes the two compatibility snapshot methods needed by current typed recovery. | Keep the current implementation. Earlier source-anchor/session migration is handled by the existing reading-history bridge. |
| Conversion since the preceding canonical `4b6f` audit | No new conversion source delta in `4b6f..966e5cc`. | Keep the current bounded readers and the separately tested selected Pandoc/raster adapters; the selected-executable identity follow-up is `69d0230eab8c3ab7ce2833fa811b7ad759440640`. |
| Research output publication | Canonical has bounded content fingerprints, native Windows change-time checks, and a successful receipt with a cleanup warning after the output is already published. | Reuse exact canonical `research/export_file.py` and its new boundary cases. |
| Audio final flush | Canonical `5cdd5982817514b7b1b524dd7a33ccd6776d6389` checks Stop after the final flush. The current shared publisher previously had an additional unchecked flush. | Adapted in `c2845d07ce8e60176909e10cd8f56c30bbb67f1e`; no alternate audio process module. |

## Resulting export contract

`write_export` and `write_binary_export` return `ExportReceipt(path,
cleanup_warning=None)`. The atomic move/link is the publication point. A later
failure to remove the temporary POSIX link is reported as a saved output with a
cleanup warning; it is not a reason to overwrite or retry the export. The native
bibliography dialog and CLI request and display this receipt. Existing workbench
callers still receive the exact text/bytes unless they explicitly select
`return_receipt=True`.

The CLI captures destination approval before preview rendering. The workbench
captures it before its snapshot check when no caller approval was supplied.
Existing-file approval includes a bounded SHA256 fingerprint, preserving a
same-size file that another program changed and then reset to its old mtime.
The canonical limit is 32 MiB and a 0.25-second between-read budget; unverifiable
existing targets require a new filename. These checks do not pretend to be a
sandbox against a hostile process that controls the destination directory.

Windows metadata follows the existing canonical `FILE_BASIC_INFO` mapping:
[Microsoft FILE_BASIC_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info).
The content digest remains necessary even when timestamps coincide.

## Actual validation of this composed source

Linux Python 3.12.14, pytest 9.0.2, existing real local QuickJS/CSL implementation:

| Suite | Result |
| --- | --- |
| `tests/reader/test_export_change_time.py` | 24 PASS, strict runner, zero skips |
| `tests/reader/test_export_publication_boundary.py` | 6 PASS, strict runner, zero skips |
| `tests/reader/test_export_no_clobber.py` | 38 PASS, strict runner, zero skips |
| `tests/reader/test_file_export.py` | 54 PASS, strict runner, zero skips |
| `tests/reader/test_binary_file_export.py` | 35 PASS, strict runner, zero skips |
| `tests/citations/test_bibliography_workbench.py` existing plus canonical cases | 29 PASS, strict runner, zero skips |
| Same workbench, new receipt/type cases | 8 PASS in a focused pytest selection; no skips, 29 other cases deselected |
| `tests/ui/test_citation_styles_jobs.py` | 26 PASS, strict runner, zero skips; asynchronous boundary doubles, not native widgets |
| `tests/citations/test_documents.py` | 9 PASS, strict runner, zero skips, real editable DOCX generation/readback |
| `tests/citations/test_regeneration.py` | 21 PASS, strict runner, zero skips, real document regeneration |

There are 250 unique passing cases in this table. The repeated UI suite is counted
once. Source tests include real files, subprocess exit at publication, denied
staging cleanup, a broken log sink, independently edited destinations, retained
private data boundaries, real formatter output and late cancellation after the
actual publication operation. Native Windows execution of this new port and its
installed-wheel gate remain pending; earlier or donor PASS is not transferred.

Required new exact-head gate: `tests/reader/test_export_change_time.py`. Retain
all current research publication, workbench, native bibliography, editable Word
and regeneration gates. This source port changes no dependency or model pack.
