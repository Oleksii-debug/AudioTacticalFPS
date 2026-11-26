# Private Windows native dependency supplement

The pinned wxPython Windows binaries import `MSVCP140.dll`. ONNX Runtime also
imports `MSVCP140_1.dll`. The bundled CPython provides VCRUNTIME but not these
two C++ libraries. A wheel installation can therefore succeed while the desktop
fails to load on a clean Windows system.

The optional supplement contains the two unchanged, published DLLs, their
original Microsoft licenses, exact source metadata, and static compatibility
evidence. Its complete size is 872,165 bytes. It does not replace CPython,
VCRUNTIME, UCRT, or the separately assembled MSYS2 OCR runtime.

This supplement is prepared from downloads for the same account holder's
private local installation. It is not a public redistributable release or a
grant of redistribution rights. A public distributor must establish its own
rights under Microsoft's developer terms. Original license files are retained
in the supplement; no native binaries are committed to this repository.

## Reproduce from a verified local download

`native-runtime/source-lock.json` records the exact conda-forge artifact URL,
size, SHA256, original archive paths and selected file hashes. Download that
artifact through an authorized connection, then run on a build host with
Python 3.11+ and `zstandard`:

```console
python packaging/prepare_native_runtime.py --package path/to/vc14_runtime-14.52.37029-hfcedb63_45.conda --output dist/native-runtime
```

The helper verifies the entire archive before reading five selected regular
files. It executes no package script or Windows binary and preserves the
original Microsoft license files. Static import/export evidence demonstrates
that the supplement's required VCRUNTIME symbols exist in the pinned Python
files. This evidence does not replace a real Windows execution test.

Build the complete Windows application bundle with its pinned Python first,
then attach the supplement to a new directory:

```console
python packaging/add_native_runtime.py --bundle dist/windows-offline --native-runtime dist/native-runtime --output dist/windows-private
```

The packager requires a rebuilt application wheel containing `native_runtime`
and `native_checks`. It copies only the source bundle's verified inventory,
leaves that source bundle untouched, verifies the supplement and host runtime
hashes, and binds every added file to the new outer manifest. Research files,
installed environments and caches are not collected.

## Activation and installation checks

The application discovers the optional supplement only through its interpreter
prefixes and their parents. It validates the exact DLL pins, original package
lock, enclosing inventory and pinned host VCRUNTIME identities before calling
`os.add_dll_directory`. A handle remains open for the application's lifetime.
Changing PATH alone does not satisfy Python's extension-loader behavior.

`Install.cmd` and `CheckInstallation.cmd` run the native diagnostic before the
application data self-test:

```console
python -I -B -X utf8 -m carr_library.native_checks --bundle-root path/to/bundle
```

The diagnostic loads actual wxPython, PDFium, lxml and Pillow extensions. For
semantic packages it applies the canonical telemetry opt-out before loading
ONNX; it does not run inference. For OCR packages it delegates to the canonical
bounded synthetic recognition check. Any failure returns a nonzero exit status,
so an unavailable native dependency cannot be reported as successful installation.
Windows execution, model inference, GUI interaction and NVDA are reported
separately. Linux tests and static PE inspection are not Windows acceptance.
