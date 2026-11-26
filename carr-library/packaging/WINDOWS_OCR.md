# Portable Windows OCR runtime

The complete bundle can include the unchanged MSYS2 UCRT64 build of Tesseract
5.5.4-1. No OCR engine is compiled by this project. It runs as a separate
command-line process through the existing extraction adapter and bounded import
worker. The Python application does not link to the native OCR DLLs.

## Pinned distribution

The publisher's package metadata is at
<https://packages.msys2.org/packages/mingw-w64-ucrt-x86_64-tesseract-ocr>.
The exact engine archive is
<https://repo.msys2.org/mingw/ucrt64/mingw-w64-ucrt-x86_64-tesseract-ocr-5.5.4-1-any.pkg.tar.zst>,
SHA256 `a38f3f00d23ff9e05f2e6df87a65ec7f10b6735b131548e87aa15016a79538b2`.

`windows-ocr.json` records the preserved MSYS2 repository database hash, every
resolved binary package URL/filename/hash/signature, source archives, licenses,
native imports, models, and the exact final file inventory. The repository
metadata resolves to 48 binary packages. Inspection with the existing GNU
`objdump -p` tool identifies the actual executable import closure: Tesseract
plus 40 DLLs from 34 packages, 31,348,627 bytes. Training tools and unrelated
executables are omitted. Remaining imports are listed Windows/UCRT system DLLs;
this bundle targets Windows 10/11 x64. This inspection is not a native Windows
execution test.

All binary package bytes were checked against their exact published SHA256
before reading their archives with the existing `zstd` and Python `tarfile`
tools. No MSYS2 package scripts were executed and no system package was
installed. Original `.BUILDINFO` and `.PKGINFO` records are retained.

## Licenses and corresponding source

This is **not an Apache-only runtime**. Tesseract is Apache-2.0; its DLL closure
also includes GPL/LGPL components and GCC runtime libraries with the upstream
GCC Runtime Library Exception. Original package notices are under
`ocr/share/licenses`. In particular, JBIG's original source header permits
GPL version 2 or later, even though the package's abbreviated license field
says GPL-2.0. No OCR component is copied into or linked with the Python host.
The giflib, libidn2 and lz4 binary packages omit standalone notices; seven
unchanged notice texts were therefore extracted from their already-pinned
corresponding-source archives. `share/licenses/SUPPLEMENTAL_SOURCES.json`
records each exact source archive, nested member and checksum.

The bundle includes **32 complete corresponding-source archives**, covering
every runtime package base, including GCC and MinGW. They contain upstream
source, original build recipes and patches. They are delivered alongside the
runtime under `ocr/share/corresponding-source`, with exact URLs and SHA256s in
the manifest. Do not remove these archives or notices when redistributing the
OCR bundle. They account for 311,507,434 bytes of its total 367,339,043 bytes.

Each build recipe was checked against the `pkgbuild_sha256sum` recorded in the
verified binary package's `.BUILDINFO`. One publisher source archive, ngtcp2,
contains a recipe from before an added `openssl-3` build dependency. The exact
matching recipe from MSYS2 commit
`f6fe8c6a81c3f6e51c900d9b0ceb4bf87f615b17` is also supplied under
`share/corresponding-source/recipes/mingw-w64-ngtcp2/PKGBUILD`. Its SHA256 is
`e0b9dd96ad451722cab78354a35eeadcf83c7a7aaed1adb658ec89689fe0ac1d`.
The difference was checked to be that single dependency line; source URLs,
source hashes, patches and compile/package functions are unchanged.

## Language data and discovery

The four model files are copied unchanged from the existing approved
`tessdata_fast` revision
`87416418657359cb625c412a48b6e1d6d41c29bd` (Apache-2.0). Their sizes and SHA256
values match `extract/pinned_tessdata.py` for `eng`, `ukr`, `slk` and `osd`.
The upstream license bytes also match the recorded Git blob. They live under
`ocr/share/tessdata`; the engine's original text/TSV configuration files are
retained there as well. Source URLs are pinned to the exact Git revision.

The launcher prepends `ocr/bin` to its process `PATH` and sets
`TESSDATA_PREFIX` to `ocr/share/tessdata`. The isolated worker retains those
non-Python environment variables. No machine-wide PATH, registry or installed
OCR is required. Choosing the OCR language remains an explicit application
configuration; merely including a language file does not select it.

## Verification and reproduction

`windows_ocr.verify_runtime(tree, manifest)` requires the supplied manifest to
equal `windows-ocr.json`, verifies every file's size/hash, and rejects missing,
extra, symbolic-linked, hard-linked and Windows reparse-point files. The bundle
builder invokes it before and after copying. The installer separately checks
the enclosing complete bundle inventory.

```console
python packaging/windows_ocr.py --directory /path/to/ocr --manifest /path/to/ocr-manifest.json
CARR_TEST_OCR_DIR=/path/to/ocr python -m pytest -q tests/packaging/test_windows_ocr.py
```

The staged runtime passed the actual byte-verification test; all
eight fixture tests passed, including changed metadata, same-size corruption,
unexpected files and link attacks. The strict release run supplies the actual
runtime path and permits no skipped runtime test. The source archives and model files were
downloaded from their original pinned publishers; the manifest and preserved
repository database contain the input identities required to reproduce this
inventory with standard archive tools. No OCR/compiler/resolver was
reimplemented. Native Windows execution remains **NOT_RUN**. The existing
real Linux OCR tests validate the application review/resume behavior separately;
they do not certify this Windows binary or historical-book OCR quality.
