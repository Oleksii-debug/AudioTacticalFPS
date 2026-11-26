# Integrated desktop candidate — 2026-11-26

Version: `0.1.0.dev1`. Integration branch: `carr/finish-desktop-20261126`.
The starting assembly is `7924b6fd46887e626b876ce7040f0d35ce5b3a7e` on
`carr/01-assembly-v2-a450bd9eb97b11764f0fe2d70d72efbf9788aa5d`.
The final distribution records its exact source commit in `bundle-manifest.json`.
All repository changes remain inside `carr-library/`.

## What is assembled

The native wx desktop now connects the existing catalog, extraction, search,
citation, topic and evidence services to one persistent schema and one launcher.
Workspace and topic authorization are rechecked in each transaction. Immutable
originals, historical metadata, saved quotation spans and editable research
notes retain their distinct storage roles. Legacy and versioned evidence codecs
work together without rewriting historical checksums.

PDF and DOCX parsing, and PDF review rendering, run in bounded subprocesses.
Durable request IDs support cancellation, retained-original resume and recovery
after a committed import loses its completion reply. Scanned pages remain in
review until an explicit independent comparison binds the exact original bytes,
extracted text and rendered page. No OCR confidence or model score approves a
quotation. Printed page labels require a separate approval; unknown labels stay
unknown. DOCX remains unverified logical text with unknown printed pagination.

Native desktop source operations run on a worker, while clipboard writes,
dialogs and focus changes stay on the UI thread. Failed note saves and failed
refreshes retain the draft. Late callbacks cannot repopulate a closed or changed
scope. Export destinations cannot overwrite the private library, and a failed
export preserves the previously selected output file.

The offline package contains CPython 3.11.18, exact Windows dependency wheels,
the verified multilingual MiniLM model, and Tesseract with English, Ukrainian,
Slovak and orientation data. It includes licenses, dependency inventories and
the OCR runtime's corresponding source archives and build recipes. The package
does not collect user libraries or original documents. ONNX Runtime and Hugging
Face telemetry are explicitly disabled before model sessions are constructed.

## Reproducing the complete local gate

Use a complete CPython environment with `.[test,semantic]`, a compatible wx
runtime, local Tesseract, and all pinned test inputs. The Linux verification
environment used CPython 3.13.18 and wxPython 4.2.3. wx was imported for handler
checks; native desktop interaction was not exercised.

Set these variables to the separately verified local inputs:

| Variable | Input |
|---|---|
| `TESSDATA_PREFIX` | Local directory containing the pinned eng/ukr/slk/osd files |
| `CARR_TEST_MODEL_DIR` | Verified MiniLM directory |
| `CARR07_MODEL_DIR` | The same verified MiniLM directory |
| `CARR07_MODEL_MANIFEST` | The sealed MiniLM manifest |
| `CARR_TEST_OCR_DIR` | Verified portable Windows OCR directory |
| `CARR_TEST_PYPDF_WHEEL` | The pinned pypdf 7.1.1 wheel |

```sh
python scripts/ci_check.py tests bootstrap docs/contracts/probes \
  tests/ui/probe_page_context_dialog.py \
  tests/ui/probe_verified_clipboard.py \
  tests/ui/probe_paged_search_panel.py
```

The entrypoint rejects missing/empty suites, skips, XFAIL and XPASS. It disables
model telemetry and rejects Python socket connections and DNS resolution during
tests. This is not an operating-system network sandbox. The first broad run
was stopped by automatic approval review after an upstream telemetry connection;
it is not recorded as a completed test run. The explicit telemetry policy was
then added before repeating the gate.

Final verification counts are recorded below after the frozen-source run.

## Remaining release evidence

This is a reviewable development candidate, not a signed-off release. Native
Windows installation, Windows model/OCR execution, keyboard-only navigation and
NVDA interaction remain **NOT_RUN**. The native probes and the manual acceptance
matrix must run on Windows. Linux unit/integration tests and static PE dependency
inspection do not satisfy those gates. Actual user-corpus acceptance and held-out
retrieval quality also remain separate from synthetic fixtures.

Backups cover completed libraries, metadata, evidence and retained originals.
Unfinished import journals and pending OCR reviews are excluded; finish those
imports before moving the complete working state by backup. Restores publish to
a new folder and never overwrite the existing library.

No root GitHub Actions workflow, repository protection, release tag or release
status is changed. Local verification results must not be represented as an
active GitHub CI or native Windows pass.
