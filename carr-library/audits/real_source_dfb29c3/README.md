# Real-source acceptance of the final integrated candidate

Exact tested source: `dfb29c3ac37d4d2b55bef4bd9aff91dfb12f1100` on the existing
`carr/finish-desktop-20261126` branch / PR71.

The unchanged independent driver completed **64 checks, 0 failures** using the
complete public-domain English Alice text and the documented Ukrainian
Zapovit text. It checks actual import, original-byte retention, scoped phrase
retrieval, exact quotation and offsets, truthful unpaginated locators, notes,
tags, saved queries, separate bibliography/notes exports, membership changes,
workspace isolation, reopen, archive, relocated backup restoration, identical
exports, and rejection of damaged sources and incorrect backup checksums.

All 379 tracked application files matched the exact committed Git blobs before
and after execution. The worktree stayed clean; all 43 imported application
modules originated in that exact checkout. The driver and both input digests
were unchanged. No native model runtime was imported.

`acceptance.json` records the results, environment and source provenance;
`source-manifest.json` records every checked application file. No source text,
quotation, query, library database, export, or private runtime path is published.

## Reproduction

Reuse the byte-identical driver and source reconstruction metadata already
preserved in `../real_source_f3e510b/`. Its SHA256 is
`26c0dbea62df1760af22a0114f724da50b7fe2fc56edf877b5af9d7c1d6ef668`.
Prepare the two legally sourced texts and private input manifest as documented
there, check out the exact source above, and run that driver with the checkout's
`carr-library/src` on PYTHONPATH. Retain source/input hashes before and after.

The execution used Linux CPython3.12.14 and the existing Python socket denial
in the driver. This record does not claim OS-level network isolation, native
Windows execution, OCR/PDF-page verification, held-out retrieval quality, NVDA,
or execution of a shipped ZIP. The full native Windows workflow and private
downloadable bundle have separate evidence on PR71. Prior acceptance records
remain intact and are not relabelled as this commit's results.
