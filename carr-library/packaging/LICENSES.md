# Upstream provenance: PDF preflight dependency only

## Integrated desktop build update — 2026-11-26

The current application pins pypdf 7.1.1 (publisher wheel SHA256 `04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1`), replacing the historical synthetic-only 5.9.0 smoke pin. The offline PDF smoke and tampered-wheel test use that same pin. The native uv resolver/SBOM wrappers now pin uv 0.13.9, upstream commit `6b2d39dcc390293d51c9c0d2bbcd650f4bcbfdce` (MIT/Apache-2.0), and preserve hash verification, Windows target selection and negative checks. The earlier evidence below remains historical. Current packaging entrypoints and offline installation instructions are in `docs/RUNNING.md`; application wheels retain all vendored license files.


- pypdf 5.9.0, BSD-3-Clause, https://pypi.org/project/pypdf/5.9.0/ ; publisher commit https://github.com/py-pdf/pypdf/tree/2a91bd4d0b5bda90f2eae741e383813b6cda9721 ; license https://github.com/py-pdf/pypdf/blob/2a91bd4d0b5bda90f2eae741e383813b6cda9721/LICENSE . PyPI wheel SHA-256 is recorded in the lock. The local installed distribution was used; the published wheel itself was not retrieved or verified locally. Preserve the complete upstream license in any future redistribution. Synthetic smoke only; security review and updated runtime pin required for release.
- CPython and its standard-library sqlite3 wrapper: PSF license; SQLite core public domain. Actual SQLite build version is emitted by preflight. pip (MIT) is supplied by the selected Python environment; use its existing hash-checking and offline install options rather than writing a package manager. https://pip.pypa.io/en/stable/topics/secure-installs/ .
- PR #17 also vendors MIT cairnsearch at eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 and has additional dependencies; this preflight does not package them. Its actual vendored paths are `bootstrap/vendor/cairn_extractors/`. Preserve its LICENSE and UPSTREAM.json when adopted; full runtime SBOM remains open.

No third-party source bytes or executable binaries are redistributed by this change.


## Speech preparation — pinned Slovak cardinal source

The reading preparation component redistributes the unmodified Slovak runtime closure from num2words 0.5.14, upstream commit `6abe2c64714a38e41de23fe20fa319d0ad2fd845`, under LGPL-2.1-or-later. The exact retained files are `lang_SK.py`, `base.py`, `utils.py`, `compat.py` and `currency.py` in `src/carr_library/reader/_vendor_num2words/`. Complete copyright headers and `COPYING` remain in the source and wheel. `UPSTREAM.json` records each original Git blob and the upstream source URL. The local initializer imports no converters; the application imports the Slovak class explicitly. Upstream's eager all-language initializer and its docopt-based command-line entry point are not shipped or used. The retained closure uses only Python's standard library.

The source is distributed as ordinary, replaceable Python modules, not an embedded binary. Recipients receive the complete corresponding source and LGPL text with the application; this attribution does not replace those terms. The current distribution's component inventory must list this vendored closure in addition to resolved wheel dependencies. Existing English and Ukrainian finite profiles are reused from this repository's PR99 candidate; they are separate from the LGPL upstream code.
