# EPUB XML runtime dependency

The EPUB importer and the existing catalog EPUB metadata preview use
`defusedxml==0.7.1` as a required, offline runtime dependency. It was previously
listed only in the test extra. Its universal Python wheel adds no transitive
dependencies or native runtime, and is included in both Windows dependency
locks. No other resolved version or admitted hash was changed.

## Provenance and license

- Upstream release: [defusedxml 0.7.1 on PyPI](https://pypi.org/project/defusedxml/0.7.1/).
- Upstream source: [tiran/defusedxml at ebff1b493751e2f0775314bdd4188d64f07ea184](https://github.com/tiran/defusedxml/tree/ebff1b493751e2f0775314bdd4188d64f07ea184),
  target of the annotated `v0.7.1` tag.
- Wheel: `defusedxml-0.7.1-py2.py3-none-any.whl`, publisher SHA256
  `a352e7e428770286cc899e2542b6cdaedb2b4953ff269a210103ec58f6198a61`.
- License: Python Software Foundation License version 2, preserved as
  `defusedxml-0.7.1.dist-info/LICENSE` inside the unchanged wheel. Its SHA256 is
  `b80ce9da8c42a1f91079627fbbe2bf27210ae108a0ffe5f077d5b08e076c24c8`.

The downloaded wheel was checked against the independently retrieved PyPI
checksum. All eleven packaged Python modules and the LICENSE were compared by
Git blob identity with that exact upstream source tree: twelve matches. No
upstream source was modified or copied into the application namespace.

## Lock and inventory update

The existing `packaging/windows_wheelhouse.py` helper resolved this one added
root from the publisher-verified universal wheel, with an independently
recorded filename/hash approval manifest. Actual uv 0.13.9 performed offline
Windows x64 / CPython 3.11 resolution and a hash-mode target installation dry
run. Its generated two-line requirement was prepended to each existing lock;
the complete previous lock remains byte-identical after that prefix.

uv's existing CycloneDX 1.5 export generated the new component and dependency
edge from a temporary project constrained to Windows. These were added to both
existing inventories with unique references, preserving existing component
versions and edges; the component also records its PSF-2.0 license and verified
wheel hash. This is an incremental inventory update, not a fresh resolution of
all previously pinned dependencies. Lock/SBOM name-version parity, references
and the new root dependency edge were checked for both inventories.

## Executed checks

Linux CPython 3.12.14, pytest 9.0.2 and uv 0.13.9:

```sh
CARR_TEST_PYPDF_WHEEL=/path/to/pypdf-7.1.1-py3-none-any.whl \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python scripts/ci_check.py \
  tests/packaging/test_windows_wheelhouse.py \
  tests/packaging/test_windows_bundle.py
```

The existing strict suites passed 12 and 11 tests respectively, with zero
failures or skips. The real PyPI pypdf fixture was verified by the suite's
existing independently pinned digest. A fresh temporary interpreter also
installed defusedxml from the new hash lock with `--offline --no-index
--no-cache --no-build`, parsed a harmless XHTML doctype, and rejected an
external-entity declaration. These checks are target resolution and Linux
execution, not native Windows or NVDA acceptance; the integrated Windows job
must exercise the updated complete application and bundle.

The importer calls `defusedxml.ElementTree` directly with entity and external
resource access forbidden. It does not call the upstream experimental global
`defuse_stdlib()` function. Container, member, document and text size limits
remain the responsibility of the EPUB adapter and existing archive screening.
