# Catalog completion and live integration

The changes below are applied to the existing `carr/finish-desktop-20261126`
working tree. Do not replay the host patches onto that already-updated tree.
This PR preserves the reviewed implementation and regression evidence without
introducing another application, storage schema or PR.

## Result

- Exact-edition lookup reuses the existing author-aware SQLite query and the
  caller's authorized snapshot. A frozen metadata view guards stale edits.
- The native metadata editor uses the existing executor. A failed or stale save
  leaves entered fields available; cancellation returns to the selected book.
  The desktop patch replaces the old inline editor and adds `Ctrl+Shift+M`.
- Work-level title/author corrections publish new metadata snapshots for every
  active edition atomically. Sibling edition-specific fields and old saved
  quotations, notes and exports remain unchanged. Invalid author collections
  and damaged prior text digests abort the entire transaction.
- Books uses real keyset pagination: 200 visible editions, at most 201 fetched
  per query, native Previous/Next controls, ordered authors and stable IDs.
  Collection changes reset paging; Previous restores the selected book;
  removing the only book on the last page returns to the preceding page.
  The old 10,000-edition cutoff and overflow patch are superseded.
- A full backup/restore regression confirms corrected active metadata, old
  saved evidence, original bytes and unrelated workspaces all survive.

## Reusing the changes in another assembled checkout

Integrate the actual `catalog/read_views.py`, `ui/catalog_panel.py`,
`ui/catalog_metadata_dialog.py` and their tests from this PR, preserving any
newer independent edits. For an earlier finish-desktop application, check and
apply the two narrow host patches from the repository root:

```sh
git apply --check carr-library/integration/carr02/metadata-revisions.patch
git apply carr-library/integration/carr02/metadata-revisions.patch
git apply --check carr-library/integration/carr02/desktop-metadata.patch
git apply carr-library/integration/carr02/desktop-metadata.patch
```

`metadata-revisions.patch` includes its application-integrity and backup tests.
Do not combine incompatible V3 schemas from other candidate hosts. This repair
uses the existing finish-desktop schema and changes no tables.

## Executed verification

The initial metadata reproducer had 5 failures and 4 passes. The repaired
metadata suite has 13 passing checks, including two actual SQLite page-damage
cases and rollback after a second-edition publication failure. The pager's 14
independent acceptance cases traverse all 10,001 editions without any active
collections. All 11 existing catalog probes also pass.

After surgical integration into the live completion tree, the combined command
below passed **113 tests and 15 subtests in 5.79 seconds**, exit 0. This is the
catalog/application/desktop subset, not a full-product release result. Runtime:
Linux Python 3.12.14, SQLite 3.53.1; sources are synthetic and disk-backed.

```sh
cd carr-library
PYTHONPATH=src:. python -m pytest -q \
  tests/catalog/test_application_metadata_integrity.py \
  tests/catalog/test_catalog_panel_acceptance.py \
  tests/catalog/test_metadata_dialog.py \
  tests/catalog/test_read_views.py \
  tests/catalog/test_metadata_backup_roundtrip.py \
  tests/e2e/test_application.py \
  tests/ui/test_desktop_handlers.py \
  tests/ui/probe_catalog_panel.py
```

Independent reviewers reran the 35 metadata/editor checks and the 14 pager
acceptance checks plus 11 catalog probes. No blocking defect remained in these
reviewed changes. Final pager SHA256:
`5448e32555b041ce0e51f226505b7455ae38de70fccc2634c79b2ec5c7973611`.
Editor SHA256:
`98a97dabbd2a975f99962f0adef407b63a4a0be71a50af40601f3cb9b8f00f4f`.

Native GTK dialog execution was not possible: the environment denied local
display socket creation and rejected the one escalation request. Headless
control checks do not establish native keyboard, NVDA or installer acceptance.
Windows/NVDA and final Windows distribution acceptance remain **NOT_RUN here**.
