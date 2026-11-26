# Universal Research Library

A local research application for books and original text from any author. Carr is an optional collection. Source code stays under `carr-library/`; the unrelated project and `main` remain unchanged.

## Product
Import legally supplied digital documents, extract page-aware text (native and OCR), catalog works and editions, search words/phrases or themes, display short source-exact quotations or contiguous thematic excerpts with edition-specific citations. More opens the original page context. Copy quotation and citation. Keyboard-first accessible desktop UI, offline operation and Windows packaging are delivery targets.

## Run the development candidate

Python 3.11 or later is required. From this directory:

```console
python -m pip install .
carr-library --help
```

For the native desktop interface on Windows, install the optional dependencies and launch:

```console
python -m pip install ".[desktop,pdf]"
carr-library gui
```

The native interface has Library, Import, and Research menus. Create a library, import TXT or PDF files or enter original text, create a topic, search, open More, copy a quotation/citation, and save a selected result to a topic. Evidence and Notes keeps the original quotation separate from the editable research note. Library backups include all published workspaces and retained originals; restore always creates a new directory.

The text command interface supports the same underlying services. Each successful create/topic command prints its stable ID; use those IDs in later commands:

```console
carr-library create "History research"
carr-library import WORKSPACE_ID book.txt --title "Known source title" --author "Known author"
carr-library topic WORKSPACE_ID "Railways"
carr-library search WORKSPACE_ID "railway"
carr-library save WORKSPACE_ID TOPIC_ID "railway"
carr-library export WORKSPACE_ID TOPIC_ID --format markdown --output evidence.md
carr-library check
carr-library backup NEW_BACKUP_DIRECTORY --all-libraries
carr-library restore BACKUP_DIRECTORY NEW_DATA_DIRECTORY --manifest-sha256 SAVED_CHECKSUM
```

Use `--data-dir PATH` before the command to select a different private library directory. The default is the current user's application data directory. Data stays separate from the replaceable program files. No books or private notes are uploaded.

## Implemented and remaining boundaries

The current application composes the existing storage, import, catalog, search, citation, topic, and evidence services in one versioned SQLite database. It retains original bytes and durable import checkpoints. Strict UTF-8 TXT citations are independently checked against those bytes on reading, saving, copying, and export. Metadata snapshots remain tied to the exact source revision. A changed original, archived workspace, revoked metadata snapshot, or changed collection selection fails closed.

PDF extraction/OCR remains a review workflow. A successful native PDF import does not approve the extracted text for verified citation. Search defaults to verified text. Select **Include unverified PDF previews** in Sources, or add `--include-unverified` to a CLI search, to discover marked PDF excerpts. They appear after verified results and cannot be saved or copied as verified quotations. Printed labels remain unknown until independently verified. DOCX extraction is an available adapter, not yet a complete end-user import workflow.

Thematic retrieval has an offline BM25 path. The optional neural runtime requires both `python -m pip install ".[semantic]"` and a separately staged, verified model bundle. Set `--model-dir MODEL_DIRECTORY --model-manifest MANIFEST_FILE` before the `gui` or `search` command; CLI search also needs `--mode thematic`. Normal startup never downloads a model. The approved model provenance and measured synthetic probe are documented in [semantic compatibility](bootstrap/semantic_compat/README.md). The standard Windows installation kit includes desktop/PDF dependencies, but excludes the semantic runtime and model.

This is a development candidate. Windows installation and NVDA acceptance still need native execution. Version 1/2 candidate databases are preserved and rejected by the V3 application rather than silently upgraded. Carr-specific acceptance needs lawfully supplied source books; its absence does not prevent generic library development. Only synthetic fixtures and technical metadata belong in this public repository.

The Windows build helper and dependency lock are under `packaging/`. `python packaging/prepare_windows.py NEW_DIRECTORY` assembles an offline installation kit for Windows x64 / Python 3.12 using hash-checked wheels. Cross-platform assembly is distinct from native Windows acceptance.

## Control
- [Control issue #1](https://github.com/Oleksii-debug/AudioTacticalFPS/issues/1)
- [Shared autonomous delivery protocol](automation/COMMON.md)
- [15 lane definitions](automation/ROLES.json)
- [Baseline contracts](docs/contracts/v0.md)
- [Acceptance matrix](docs/acceptance.md)

The repository's current `AGENTS.md` governs shared integration; lane claims are advisory. Changes stay under `carr-library/`, on the application branch. Independent review and verified checks are mandatory. No release claim without G0–G7 evidence.
