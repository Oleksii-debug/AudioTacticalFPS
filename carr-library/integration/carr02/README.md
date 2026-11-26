# Catalog completion handoff

These fixes extend the existing `carr/finish-desktop-20261126` application. They do
not introduce another application, storage schema, extraction engine or PR.

## Changes

- `catalog.read_views.get_edition` reads the exact edition inside the existing
  authorized snapshot. It reuses the author-aware browsing query.
- `ui.catalog_metadata_dialog` supplies the native editor used by the desktop
  action. Atomic saves run on the existing executor; rejected saves retain the
  entered text and return focus to the error. The old inline editor is removed
  by the desktop patch.
- `metadata-revisions.patch` corrects work-level title/author publication for all
  active editions, retains edition-specific metadata and historical saved
  citations, rejects malformed author sequences, and guards stale edits. It checks each prior page digest before staging a new revision, preventing damaged text from being republished under a fresh digest.
- `catalog-overflow.patch` preserves collection navigation when a view exceeds
  10,000 editions. It never displays a partial list as a complete catalog.
- Both behavioral patches include independent regression tests in the product's
  normal catalog test directory.

## Integration

First incorporate this PR's `read_views.py`, new metadata editor and associated
tests. In the existing completion checkout, apply the three patches from the
repository root, checking each patch before applying it:

```sh
git apply --check carr-library/integration/carr02/metadata-revisions.patch
git apply carr-library/integration/carr02/metadata-revisions.patch
git apply --check carr-library/integration/carr02/catalog-overflow.patch
git apply carr-library/integration/carr02/catalog-overflow.patch
git apply --check carr-library/integration/carr02/desktop-metadata.patch
git apply carr-library/integration/carr02/desktop-metadata.patch
```

The desktop patch replaces the existing metadata menu handler and inline form
with the shared editor. It adds `Ctrl+Shift+M`; no second menu or persistence
service is created. Title and authors apply to all editions of the work;
language, edition and volume apply only to the selected edition.

## Executed verification

The immutable snapshot before repair had application SHA256
`24b5ce32027f7f1b9ca588c6dc45da2337fb5bd1edcfe91dc64784c947ac2735`
and catalog panel SHA256
`61d23544462ee093442c7c81973262ed6c460ff9a2db071d113b01b1272655bd`.
The checkpoint was based on assembled commit
`7924b6fd46887e626b876ce7040f0d35ce5b3a7e`, not a published release.

Metadata reproducer: 5 failed, 4 passed before repair. With correction and stale
edit coverage: 13 passed, including two actual SQLite page-damage regressions. Catalog overflow reproducer: 2 failed, 5 passed before
repair; all 7 passed after repair. Native-dialog handler tests with wx doubles:
22 passed. Together with the existing 9 application end-to-end checks: 51
passed, exit 0, Linux Python 3.12.14 / SQLite 3.53.1.

```sh
cd carr-library
PYTHONPATH=src:. python -m pytest -q \
  tests/catalog/test_application_metadata_integrity.py \
  tests/catalog/test_catalog_panel_acceptance.py \
  tests/catalog/test_metadata_dialog.py \
  tests/e2e/test_application.py
PYTHONPATH=src python -m unittest discover -s tests/catalog -p test_read_views.py -v
PYTHONPATH=src python tests/ui/probe_catalog_panel.py -v
```

Exact-edition read-view checks: 9 passed. Existing catalog handler probes: 11
passed. These tests use real temporary SQLite databases and synthetic sources.
They do not replace Windows/NVDA acceptance or validate the full installer.
