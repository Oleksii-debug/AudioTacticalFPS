# Offline PDF/SQLite runtime preflight (candidate)

## Integrated desktop build update — 2026-11-26

The current application pins pypdf 7.1.1 (publisher wheel SHA256 `04c5680d4b4cfa04ab505b09393fc949860c34a4dad6565284d11a2f5c3ce8b1`), replacing the historical synthetic-only 5.9.0 smoke pin. The offline PDF smoke and tampered-wheel test use that same pin. The native uv resolver/SBOM wrappers now pin uv 0.13.9, upstream commit `6b2d39dcc390293d51c9c0d2bbcd650f4bcbfdce` (MIT/Apache-2.0), and preserve hash verification, Windows target selection and negative checks. The earlier evidence below remains historical. Current packaging entrypoints and offline installation instructions are in `docs/RUNNING.md`; application wheels retain all vendored license files.


This tests a small, real dependency path from PR #17, not the complete app, OCR, Windows installer or security acceptance. The pypdf 5.9.0 lock reproduces the currently available local test runtime; it is for synthetic fixtures only, not approval to open arbitrary PDFs. A newer security-reviewed version must be selected before release.

On a connected build host, using a clean Python 3.11+ environment:

```
python -m pip --isolated download --index-url https://pypi.org/simple --only-binary=:all: --no-deps --require-hashes -r packaging/requirements-pdf-smoke.lock -d wheelhouse
```

Transfer only the checked wheel and this source tree to the offline host. From `carr-library/`, on Windows CMD:

```
py -3.13 -m venv .smoke-venv
.smoke-venv\Scripts\python.exe packaging\offline_pdf_smoke.py --install-from wheelhouse
.smoke-venv\Scripts\python.exe -m unittest discover -s tests\packaging -v
```

On Linux use `python3 -m venv .smoke-venv` and `.smoke-venv/bin/python` for the same two commands. No administrator permission, source build, OCR executable, external API or existing library is needed. The synthetic database and PDF are created under a temporary directory containing spaces and Unicode and closed before cleanup. The optional Tesseract check is only executable discovery, not OCR or validation of a trusted engine. Do not use `--install-from` on an existing application environment; create a disposable venv. Removing that venv does not remove library data; production data-location and uninstall policies remain open.

Evidence (2026-11-26): Linux, Python 3.13.5, SQLite 3.46.1, pip 25.1.1, installed pypdf 5.9.0. `python packaging/offline_pdf_smoke.py` PASS, `python -m unittest discover -s tests/packaging -v` 5 PASS. Includes real pip dry-run rejection of a tampered wheel, FTS5 reopen, synthetic PDF extraction, a mocked socket connect denial and missing OCR simulation. A genuine wheel download failed in the available environment; clean venv install from the pinned wheel is NOT_RUN, Windows/NVDA NOT_RUN. The socket mock is not OS-level network isolation. No full dependency lock, app artifact, CI, upgrade/rollback or release status is claimed.

Next: consolidate the candidate's complete runtime/test dependency set after integration, use platform-specific hash-locked wheels and pip's install report (or a standard SBOM generator), test on real Windows and Linux CI, then package the production entry point. Separate build and user-data paths; never ship private libraries.
