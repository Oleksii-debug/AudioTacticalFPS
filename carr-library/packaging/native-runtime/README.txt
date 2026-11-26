Private local native runtime supplement

This folder was prepared from the account holder's verified downloads for that same user's private offline installation. It is not a public binary distribution or a claim that third parties have redistribution rights. The original Microsoft license files are retained without changes. Any public distributor must establish its own applicable rights.

The only DLLs supplied are MSVCP140.dll and MSVCP140_1.dll. They supplement the pinned CPython runtime; they do not overwrite CPython, VCRUNTIME, UCRT, or the separately packaged OCR engine. Static import/export inspection found every requested VCRUNTIME symbol in the pinned CPython files. This is not native Windows execution evidence.

The application verifies exact source-package and file hashes, outer bundle inventory, and the pinned host runtime before registering this directory with the Windows DLL loader. It does not search the current working directory or download/install any software.
