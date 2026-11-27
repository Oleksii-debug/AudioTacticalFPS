# Export publication and recoverable cleanup

This selected port follows owner source `078d254bd4ab4dbe9de4ea80e1e26731cb8ac9e8`
on the reviewed r6 candidate. It preserves the existing Word citation controls,
source-bound previews, cancellation and destination-selection rules.

An approval for replacing an existing export now binds its bounded content hash
as well as filesystem identity. The current limit is 32 MiB and 0.25 seconds
between reads. Larger or unverifiable existing files require a new output name.
On Windows, native change-time metadata is also checked; it does not replace
the content hash. A same-length edit whose old modification time was restored
therefore does not inherit a previous overwrite approval.

The atomic move or no-replace link remains the publication point. A later
temporary-link cleanup failure cannot turn a saved document into a failed
export. Writers return an additive `ExportReceipt`; its `cleanup_warning`
describes any remaining private staging cleanup. Bibliography CLI and native
dialog consumers request and display that receipt. Existing workbench callers
still receive rendered content unless they explicitly request a receipt.
Opening live Word citation controls remains available.

The port includes exact owner regression cases for change-time/content checks,
no-clobber publication, successful publication with cleanup failure, queued
destination replacement, and accessible receipt notices. Those owner results
are provenance only. This composition requires source and installed execution
on its own frozen candidate; no runtime PASS is declared by this document.
