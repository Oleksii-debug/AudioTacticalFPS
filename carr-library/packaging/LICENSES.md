# Upstream provenance: PDF preflight dependency only

## Integrated desktop build update — 2026-11-26

The current application pins pypdf 7.1.1 (publisher wheel SHA256 `04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1`), replacing the historical synthetic-only 5.9.0 smoke pin. The offline PDF smoke and tampered-wheel test use that same pin. The native uv resolver/SBOM wrappers now pin uv 0.13.9, upstream commit `6b2d39dcc390293d51c9c0d2bbcd650f4bcbfdce` (MIT/Apache-2.0), and preserve hash verification, Windows target selection and negative checks. The earlier evidence below remains historical. Current packaging entrypoints and offline installation instructions are in `docs/RUNNING.md`; application wheels retain all vendored license files.


- pypdf 5.9.0, BSD-3-Clause, https://pypi.org/project/pypdf/5.9.0/ ; publisher commit https://github.com/py-pdf/pypdf/tree/2a91bd4d0b5bda90f2eae741e383813b6cda9721 ; license https://github.com/py-pdf/pypdf/blob/2a91bd4d0b5bda90f2eae741e383813b6cda9721/LICENSE . PyPI wheel SHA-256 is recorded in the lock. The local installed distribution was used; the published wheel itself was not retrieved or verified locally. Preserve the complete upstream license in any future redistribution. Synthetic smoke only; security review and updated runtime pin required for release.
- CPython and its standard-library sqlite3 wrapper: PSF license; SQLite core public domain. Actual SQLite build version is emitted by preflight. pip (MIT) is supplied by the selected Python environment; use its existing hash-checking and offline install options rather than writing a package manager. https://pip.pypa.io/en/stable/topics/secure-installs/ .
- PR #17 also vendors MIT cairnsearch at eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 and has additional dependencies; this preflight does not package them. Its actual vendored paths are `bootstrap/vendor/cairn_extractors/`. Preserve its LICENSE and UPSTREAM.json when adopted; full runtime SBOM remains open.

No third-party source bytes or executable binaries are redistributed by this change.

## Offline native SAPI5 bridge — 2026-11-26

Windows now pins comtypes 1.5.0 (MIT), upstream commit `7732dc3de25797e658d26f375e78310bd7febbda`. The exact PyPI wheel hash is in both Windows locks; its complete LICENSE.txt is retained in `wheel-notices/comtypes-LICENSE.txt` and in the wheel. It has no mandatory third-party dependencies. The application invokes the native Windows COM SAPI5 classes in a bounded local worker and does not download voices.

## Offline automatic OCR language review — 2026-11-27

The optional automatic OCR mode uses py3langid 0.4.0 under BSD-3-Clause, from
https://github.com/adbar/py3langid/tree/3b99caf00d0dcbc9416a9d06f8e5b690e919a74e .
Both Windows locks admit only the reviewed universal wheel SHA256
c25bd038147951ba3a89efe9ec12cc7d2116cb6f311ff8af84ade398a8bc0616.
The existing NumPy pins remain 2.4.6 in the ordinary runtime and 2.3.5 in the
semantic runtime; py3langid's dependency is NumPy >= 2.0.

The complete original license is retained at
src/carr_library/extract/PY3LANGID_LICENSE and included in the application wheel's
declared license files. The package data file ocr-language-detector.json records
the exact implementation/model identities, upstream commit and original identity
evidence. The existing Windows SBOMs retain their prior resolver inventory and
mark this reviewed package addition explicitly; no new resolver run is claimed.

This code does not fetch models while processing documents, contact a language
service, or turn a classifier score into source or quotation approval. Runtime
acceptance and language-quality evidence are tracked separately in
docs/AUTOMATIC_OCR.md.
