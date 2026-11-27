# Independent real-source acceptance at f3e510b

Target: `Oleksii-debug/AudioTacticalFPS`, branch `carr/finish-desktop-20261126`,
commit `f3e510bf792d82be5be406b2b2f8f14672b83b1b`.

Result: **64 checks passed** against the actual committed application, using the
complete original English Alice TXT and the documented Ukrainian Zapovit TXT.
The detached checkout stayed clean. All 321 tracked product files matched their
committed Git blobs before and after execution. Source bytes stayed unchanged.
The existing independent acceptance driver is retained byte-for-byte; its SHA256
is `26c0dbea62df1760af22a0114f724da50b7fe2fc56edf877b5af9d7c1d6ef668`.

The checks cover import, retained originals, author/title metadata, exact Unicode
offsets and source quotations, context, fresh copying, topics/sections, notes,
tags, saved queries, text/Markdown/RIS exports, collection and workspace scope,
archiving, reopening, backup relocation, restore, unchanged restored exports,
source-drift rejection and an incorrect backup-manifest digest.

`acceptance.json` records the executed result, source provenance and limitations.
`source-manifest.json` records every checked source file. `input_definitions.json`
preserves the preselected input metadata, offsets and expected hashes without
publishing books, queries or quotations. Actual source inputs, databases and
exports stay outside the repository.

## Reproduce

Use a separate checkout at the exact target commit with its reviewed runtime
dependencies. Obtain the source bytes identified in `acceptance.json` outside
the repository. The Alice input is the complete pinned `11.txt`, without any
editing. The Ukrainian input is the documented pinned-revision extraction;
verify its SHA256 before use. The source URLs and extraction rules are recorded
in `acceptance.json`. These are logical unpaginated TXT inputs, not verified
printed or physical pages.

Create the private driver manifest from the published definitions and those
two external files. This example reconstructs only the already fixed queries;
it checks their recorded hashes before the application is invoked:

```python
from hashlib import sha256
import json
from pathlib import Path

audit = Path("/path/to/this/audit/directory")
external = Path("/path/to/private/acceptance-inputs")
files = {
    "en-alice-full-novel": external / "11.txt",
    "uk-zapovit": external / "zapovit.txt",
}
definition = json.loads((audit / "input_definitions.json").read_text())
rows = []
for definition_row in definition["sources"]:
    row = dict(definition_row)
    path = files[row["id"]]
    data = path.read_bytes()
    assert sha256(data).hexdigest() == row["source_sha256"]
    text = data.decode("utf-8")
    selected = text[row["start"]:row["end"]]
    digest = sha256(selected.encode("utf-8")).hexdigest()
    assert digest == row["query_sha256"] == row["expected_quote_sha256"]
    row.update(source_path=str(path), expected_match=selected, query=selected)
    rows.append(row)
(external / "manifest.json").write_text(
    json.dumps({"sources": rows}, ensure_ascii=False), encoding="utf-8"
)
```

From the target checkout's `carr-library` directory:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python \
  /path/to/this/audit/directory/real_source_acceptance.py \
  /path/to/private/acceptance-inputs/manifest.json \
  /path/to/private/acceptance-inputs/result.json
```

The driver rejects Python socket connections while it runs. This does not
represent an operating-system network sandbox. The real-source run used Linux,
Python 3.12.14, SQLite 3.53.1, filelock 3.29.0 and platformdirs 4.10.0.

An additional focused run at the same exact commit passed **55 tests and five
subtests** with no skips or failures:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. python -m pytest -q -p no:cacheprovider \
  tests/ingest/test_retained_assets_recovery.py \
  tests/ingest/test_retained_assets.py \
  tests/ingest/test_asset_copy.py \
  tests/packaging/test_install_bytecode.py \
  tests/packaging/test_windows_bundle.py
```

## Verdict limits

This is bounded acceptance of source and research workflows at the named commit.
It establishes no native Windows/NVDA result, shipped-archive installation,
PDF/OCR printed-page accuracy, broad semantic-search benchmark or full release
certification. A later changed source commit or rebuilt package requires its own
acceptance evidence. Prior release findings are not silently closed by this
report.
