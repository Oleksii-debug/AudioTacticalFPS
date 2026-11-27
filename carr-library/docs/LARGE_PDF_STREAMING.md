# Supported large PDF originals

This change extends the existing PDF import, original viewing and typed backup
path. It does not extend the byte-based TXT, EPUB, DOCX or other document adapters.
Their 64 MiB input limits and archive/decompression checks remain unchanged.

## Original identity and scope

A PDF above 64 MiB is represented internally by a `FileAsset`: its canonical
local path, SHA256, exact size and the device/inode/size/mtime/ctime identity.
Every reopen uses the existing verified input handle and that expected identity.
Copies use 1 MiB chunks, exclusive creation, complete hash checks and fsync.
The target fd identity is captured at creation; failure cleanup and successful
return require the same regular inode/device, a single link and no reparse point.
An unknown replacement or newly hard-linked target is preserved.
Retention keeps the existing hash-addressed namespace and no-replace link
publication; recovery still requires complete accounting for owned staging links.
Symlinks, reparse points, hard-linked originals, alternate streams and foreign
relative paths remain invalid.

The representation is not an authorization token. Workspace, edition, revision,
selection and topic checks still run through the current application. An external
viewer receives an independent verified copy. Parser success does not approve
source text, OCR, printed pagination or a quote. PDF review binds the same complete
original checksum and actual rendered page, including after backup restoration.

## Finite budgets and compatibility

`LibraryApp.import_file(..., pdf_max_bytes=...)` optionally selects a PDF byte
budget. It is stored in the existing request and included in its immutable
publication identity. An explicit resume cannot change it. A request without
this optional field retains its previous canonical identity.

The maximum supported PDF container size is 4 GiB minus one byte: the common
32-bit length boundary of PDFium's FPDF_FILEACCESS Windows ABI. This is an
admission ceiling, not a claim that every PDF at that size has been qualified.
The launch receives a possibly smaller allowance that is covered by the exact
shared disk reservation. Growth beyond the admitted allowance is rejected by
the child. The operator's current resource policy and free-disk checks remain
mandatory.

The parser's seekable view rejects unbounded reads and individual reads above
16 MiB. The native PDFium callback fills an already allocated destination in
1 MiB chunks and returns failure on bounds, short reads or cancellation. It
retains the owned stream and callback until the native document closes.

Existing parser page, pixel, checkpoint, archive, time and worker-process limits
remain. The POSIX worker's existing address-space bound remains; Windows shared
memory admission is a declaration, not an added address-space sandbox. This
change does not promise bounded native memory for arbitrary compressed or complex
PDF page objects. Existing parser rejection and process deadlines still apply.

The bytes `catalog.intake.snapshot` and `RetainedAssets.retain(bytes,...)` APIs
are unchanged. Small originals retain their existing bytes representation.
Full backup versions 1–5 and the current pending component keep their formats;
only PDF size validation and copying use the streaming representation. Every
other component's byte/count limit and final verification remains in force.

## Executable evidence

Two standalone strict selectors contain 22 source-derived cases:

- `tests/ingest/test_streaming_pdf.py`: 17 cases, including actual fresh owned
  processes with valid 72 and 144 MiB synthetic PDF containers. Each contains a
  painted text page and a real unreferenced stream object. No sparse or truncated
  substitute is used. The process runs native text extraction, PDFium rendering,
  retention and full readback. It records platform/CPU/Python, actual peak resident
  memory, baseline peak, traced Python peak and elapsed time. Resident increase
  must stay below 64 MiB; traced allocations below 32 MiB.
- `tests/e2e/test_large_pdf_original.py`: five cases covering the existing actual
  import worker, original copy/launch boundary, explicit review, full backup,
  pending-source restoration/resume, cancellation after real copied bytes and
  refusal when a source exceeds its persisted request budget, and aggregate
  pending-byte refusal before any original copy.

These tests also preserve non-PDF limits and reject replacement, hardlinks,
forged digests, oversized parser reads, native callback failure and changed
request budgets. New scans must forward their live cancellation callback to
`planned_snapshot(path, cancelled=...)`; the returned file representation owns
no live handle between operations.

At this source checkpoint these cases are **NOT_RUN**. There is no real-corpus,
multi-gigabyte execution, p50/p95, external viewer, NVDA or general performance
qualification claim. Measured values must come from the exact committed tests
and JUnit properties on the actual executing host.

## Pinned parser interface audit

The primary py-pdf/pypdf source at tag 7.1.1 was inspected across 32 reader,
generic, page, utility, encryption and text-extraction modules. The actual
file-backed read path uses read/seek/tell. Its binary-mode attribute check is
conditional, and PdfReader.close closes a stream only when the reader itself
opened a path. No readline/closed interface is required on this pinned path.
The stream-size helper uses seek/tell when getbuffer is absent.

The pinned reader's cross-reference repair and notebook-display helpers can
request an entire stream. This path intentionally refuses those requests; it
does not convert a malformed large PDF into an unbounded in-memory fallback.
A changed parser dependency needs renewed interface and native tests.

Primary sources:
- https://github.com/py-pdf/pypdf/blob/7.1.1/pypdf/_reader.py
- https://github.com/py-pdf/pypdf/blob/7.1.1/pypdf/_utils.py
- https://github.com/py-pdf/pypdf/blob/7.1.1/pypdf/generic/_data_structures.py

## Scan-continuation composition

The separately reviewed bf603 continuation requires a one-line merge at its new
visit function: pass both live cancellation and its page deadline into
planned_snapshot. The callback returns True so InterruptedError preserves the
previous durable cursor; it must not raise TimeoutError into the rejected-row
catch. The old scan already forwards cancellation in this source patch.

Two additional composition-only cases are supplied as an exact Git blob and
oracle: a real 5001-file scan including one >64MiB PDF, and cancellation after an
actual PDF hash chunk followed by continuation of the unchanged cursor. They
require both this patch and the bf603 scan API; they are not claimed as standalone
coverage on the earlier base.
