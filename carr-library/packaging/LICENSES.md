# Upstream provenance: PDF preflight dependency only

- pypdf 5.9.0, BSD-3-Clause, https://pypi.org/project/pypdf/5.9.0/ ; publisher commit https://github.com/py-pdf/pypdf/tree/2a91bd4d0b5bda90f2eae741e383813b6cda9721 ; license https://github.com/py-pdf/pypdf/blob/2a91bd4d0b5bda90f2eae741e383813b6cda9721/LICENSE . PyPI wheel SHA-256 is recorded in the lock. The local installed distribution was used; the published wheel itself was not retrieved or verified locally. Preserve the complete upstream license in any future redistribution. Synthetic smoke only; security review and updated runtime pin required for release.
- CPython and its standard-library sqlite3 wrapper: PSF license; SQLite core public domain. Actual SQLite build version is emitted by preflight. pip (MIT) is supplied by the selected Python environment; use its existing hash-checking and offline install options rather than writing a package manager. https://pip.pypa.io/en/stable/topics/secure-installs/ .
- PR #17 also vendors MIT cairnsearch at eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 and has additional dependencies; this preflight does not package them. Its actual vendored paths are `bootstrap/vendor/cairn_extractors/`. Preserve its LICENSE and UPSTREAM.json when adopted; full runtime SBOM remains open.

No third-party source bytes or executable binaries are redistributed by this change.
