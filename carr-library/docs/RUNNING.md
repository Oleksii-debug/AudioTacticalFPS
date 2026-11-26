# Universal Research Library

An offline desktop research library with separate workspaces, books and
collections, literal and thematic search, source context, saved evidence, notes,
and exports. Quotations retain their source revision and page or text-segment
reference. Unknown printed page numbers remain explicitly unknown.

This is a development build. Windows and NVDA acceptance are recorded separately
from automated checks; a successful installation is not an accessibility verdict.

## Windows offline package

The Windows x64 package targets CPython 3.11. The complete package includes its
own Python runtime. Extract the complete package into a new folder, then open
`Install.cmd`. Installation creates a local
environment inside that folder and uses only included, hash-checked wheels.
It does not contact a package index and does not require administrator access.
The smaller wheel-only variant requires CPython 3.11 through the Windows `py`
launcher. Its manifest identifies whether Python is included.

After installation, open `ResearchLibrary.cmd`. The terminal stays visible if
startup fails, so a screen reader can read the error. `CheckInstallation.cmd`
runs the synthetic application self-test without changing your research data.

The default data location is `%LOCALAPPDATA%\Universal Research Library`.
Program files and research data are separate. To uninstall this portable
package, close the application and delete only its extracted program folder.
Your research data remains in its data directory. Back up that directory before
moving or removing it. The installer refuses to replace an existing environment.

## Installation from a source checkout

Use Python 3.11 or later. From the `carr-library` directory:

```console
python -m venv .venv
.venv\Scripts\python.exe -m pip install ".[desktop]"
.venv\Scripts\python.exe -m carr_library
```

On Linux, the interpreter is `.venv/bin/python`; install a compatible wxPython
build for the system's GTK libraries before using the desktop extra. The core
self-test and command help do not require wxPython.

```console
python -m carr_library --help
python -m carr_library --self-test
python -m carr_library --data-dir "D:\Research data"
```

`--self-test` uses a temporary synthetic library. It does not import or upload
your documents. Search and library data remain on the local computer.

## Optional local semantic search

The `semantic` extra supplies the pinned FastEmbed runtime. A separately
verified local model bundle is required; installing the extra alone does not
download or approve model weights. The application must retain literal and
lexical thematic search when the optional model is unavailable. The repository's
`packaging/MODEL_BUNDLE.md` records the model revision and checksum workflow.
The complete semantic Windows package includes the checked MiniLM model and
loads it from its own `models` folder. It makes no model download on startup.

## Building an offline Windows package

The build host can run Linux or Windows; the resulting wheels target Windows
x64 and CPython 3.11. A native Windows test is still required before release.

```console
python -m build
python -m pip download --only-binary=:all: --platform win_amd64 --python-version 3.11 --implementation cp --abi cp311 --require-hashes -r packaging/windows-cp311.lock -d wheelhouse
python packaging/build_windows_bundle.py --wheel dist/universal_research_library-0.1.0.dev1-py3-none-any.whl --wheelhouse wheelhouse --lock packaging/windows-cp311.lock --output dist/windows-offline
```

For the complete package add `--python-archive path-to-pinned-python.tar.gz`.
The exact Windows CPython 3.11.18 archive URL and publisher SHA256 are recorded
in `packaging/windows-python.json`, sourced from uv 0.13.9's official download
metadata. The bundler checks that SHA256 before unpacking with Python's standard
tarfile data filter and retains the runtime's license files. Runtime downloads
are build-time only.

To include semantic search, resolve/download `windows-semantic-cp311.lock`
instead and pass `--model-dir` and `--model-manifest` to the bundler. It applies
the application's pinned model verifier both before and after copying the
model; the model's nine original files remain unchanged. Its Apache 2.0 license
is included beside the model directory.
`--sbom` includes a native uv CycloneDX 1.5 runtime dependency inventory after
checking that every package and version matches the dependency lock. The file
hash manifest separately covers Python, the application, and optional model
bytes; a dependency inventory alone does not certify a release.

The bundler validates wheel tags, checks every dependency wheel against the
previously resolved hash lock, and copies only those wheels, the application
wheel, the offline installer, and these instructions. It never collects local
libraries, source documents, caches, or credentials. It refuses an existing
output directory. Each bundle includes an inventory of exact file hashes.

## Third-party components

The application uses SQLite/FTS5 from Python, pypdf, PDFium through pypdfium2,
python-docx, platformdirs, and wxPython. Third-party wheel licenses remain in
their distributions. Vendored cairnsearch, filetype, rispy, Rifflux and
wx-accessible-grid license and provenance files are included in the application
wheel. See each corresponding `UPSTREAM` or `REUSE` file in the source tree.
The complete package also contains the Python runtime dependency notices from
its publisher's exact build metadata, plus source-pinned notices for dependency
wheels that omit their own license file. These are retained in `notices`.
