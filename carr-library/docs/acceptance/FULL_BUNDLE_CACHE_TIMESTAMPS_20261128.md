# Sealed Python cache timestamps — 2026-11-28

## Observed acceptance failure

The historical e828403becce67eead4f7def3ec826f7893a2970 MAIN run
46104046589, Windows job 140852689912, completed the FULL installed tests:
4,881 passed, zero failed or skipped, in 2,650.61 seconds. The early five
UTF-8 fixture regressions passed in the actual isolated embedded interpreter
with preferred encoding cp1252 and UTF-8 mode 0. The exact 158-name final
JUnit requirement and private native DLL path verification also passed.

The following unchanged full-file verifier then refused
runtime/Lib/__pycache__/__future__.cpython-311.pyc because its bytes differed
from the sealed manifest. Standard setup did not run, and the final tested
application wheel retention step was not reached. This is a partial result,
not successful FULL or setup acceptance. The textual evidence artifact is
15250095218 (437,557 bytes), SHA-256
c9131cea6100487f16725d4c2cada9ca1c1ecc6f9adfaa87b8c5be7777bcf502.

## Confirmed copy contract

The pinned Python archive is extracted with its timestamps, but two subsequent
verified payload copies used shutil.copyfile: the base-to-native-supplement
copy in packaging/add_native_runtime.py and the standard setup payload stage
in packaging/windows_installer/build_setup.py. That preserves file bytes but
does not preserve source modification times. A timestamp-based Python cache
can therefore remain byte-identical while becoming stale relative to its
newly copied source. A later interpreter permitted to write bytecode can
replace that cache, making the unchanged integrity check correctly fail.

The log does not identify the last process that wrote the particular upstream
__future__ cache, and it does not print that cache header. Selected test
children without -B are potential writers; the copy-contract regression is
independent of attributing the observed write to one of them.

## Narrow correction and regression

Only those two verified payload loops now use shutil.copy2. Helper copies,
manifest schemas, inventory membership, expected hashes, pinned dependencies,
runtime archives, product flags and deadlines are unchanged. Every original
byte remains subject to the existing post-copy and final integrity checks.

Two regression cases exercise the actual native-supplement and setup staging
functions. Each compiles a real timestamp-mode cache from a synthetic module
whose source has a fixed old modification time, seals both files, copies the
payload, and imports the copied module in a real child. The child starts with
-I -B to protect its own test-host standard library, then enables bytecode
writing only for the fixture module import. Both cache bytes and source
timestamps must survive, and the actual installer verifier must still pass.
The same cases then deliberately invalidate the copied source timestamp,
allow Python to rewrite the cache, and require that verifier to reject the
changed sealed cache. Synthetic DLL pins are validated but never loaded;
these source cases make no native Inno execution claim.

At this checkpoint the new code and regression cases have not been executed.
Fresh required source tests and actual FULL final-seal / standard-setup
acceptance remain required. No runtime files are ignored, removed or resealed
to conceal a changed cache.
