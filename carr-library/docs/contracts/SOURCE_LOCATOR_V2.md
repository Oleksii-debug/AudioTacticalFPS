# Source locator and immutable evidence handoff — V2 candidate
2026-11-26; CARR-01; base a450bd9eb97b11764f0fe2d70d72efbf9788aa5d; superseding scope UNIVERSAL_SCOPE_V2.md. Status REVIEW_REQUIRED, no gate closed.

## Contract
- Intake (CARR-02, PR #20): one bounded immutable byte snapshot and SHA-256; pass that snapshot to extraction (CARR-03/10). Never reopen the original path between hashing and parsing. For an isolated worker use a bounded private staging copy, verify digest before extraction and at commit; no network, original untouched. Header detection is not structural validation. Separate safe archive preflight from parser.
- Locator discriminated union: PDF page = kind=physical_page, physical_index integer >=1, printed_label nullable, printed_label_verified boolean; only verified label is displayed as printed page. Nonpaginated source = kind=logical_segment, segment_index integer >=1, physical_index=null, printed_label=null, printed_label_verified=false. Never display logical segment 1 as physical page 1. Existing native PDF page indices unchanged. TXT form feed may designate logical segments unless original pagination verified. DOCX/HTML/CSV/XLSX extracted text is not page proof. UI/exports render "Segment N (unpaginated)" or "PDF page N; printed page unknown". Document extraction transforms must remain identified by engine/version/config and cannot imply character-level correspondence to original bytes.
- Immutable anchor: workspace_id, edition_id, asset_sha256, locator kind/index, revision_id, raw_text_sha256, start/end (half-open Unicode code point offsets), exact raw slice. Validate all against same committed revision and source membership; quote is never normalized or generated. Preserve cited edition identity and a citation metadata snapshot/version; subsequent author/title/edition corrections must not silently change an already saved citation. Note/tag membership editable separately. Original citation plus explicit corrected revision if requested. Deletion fails closed or retains resolvable tombstone. OCR candidate/review_required not eligible for verified quotation until independently accepted. Printed labels never inferred from physical count.
- CARR-05/04 enforce anchor in transaction; CARR-06/07 return same anchor + raw offsets, workspace scope on every read; CARR-08 persists citation snapshot and separates annotations, CARR-09 renders locator and review status accessibly, CARR-10 commits verified revision atomically with resumable checkpoint. CARR-11 package licenses and isolate PDF/OCR workers; CARR-12/13/14 independent negative tests and accessibility/security review.

## Reproduced on PR #17 exact head 4c2ee5ba43a778a2b160325cfe48678eeab213d3
Local executable blobs: universal_library.py 803f463fd4ae92f3bf97af50e1b892c4b187cd47; external_reuse.py e8db699528710cd4930507ead147ae75da63cfb5; test_universal_library.py dceacef0f7b8330b6a6895af21199532e11cfe1c; test_external_reuse.py 265bbf8875321bb831eec1b1029b5d07d69947e4. Git hash-object matches remote.
Minimal synthetic reproducer from candidate root:
```python
from bootstrap.universal_library import ResearchLibrary
from bootstrap.external_reuse import import_external
from tempfile import TemporaryDirectory
from pathlib import Path
import json
with TemporaryDirectory() as d:
    p=Path(d)/'note.md'
    p.write_text('Synthetic railway note.', encoding='utf-8')
    lib=ResearchLibrary(Path(d)/'db.sqlite')
    w=lib.workspace('A'); t=lib.topic(w,'T')
    b=import_external(lib,w,p,'Note','Synthetic author','1')
    h=lib.search(w,'railway')[0]; lib.save(w,t,h)
    print(h['physical'], h['printed'])
    before=json.loads(lib.export_topic(w,t))[0]
    lib.db.execute('UPDATE book SET edition=?,author=? WHERE id=?',('2','Different author',b))
    lib.db.commit()
    after=json.loads(lib.export_topic(w,t))[0]
    print(before['author'],before['edition'],after['author'],after['edition'])
    print(before['quote']==after['quote'],before['page_digest']==after['page_digest'])
    lib.close()
```
Actual: physical=1, printed=None for Markdown; attribution changes from Synthetic author/1 to Different author/2 while quote and page digest are identical. Expected: logical segment with physical=null; immutable citation snapshot or explicit revision drift refusal. Direct DB write is a negative integrity test, not a claim of an exposed UI exploit. CARR-04/05/08/10 must implement protections rather than copying prototype schema unchanged. Keep #17 as reuse candidate.

## Verification ledger (Linux Python 3.13.5; separate candidate trees, NOT integrated)
- #17 head 4c2ee5ba43a778a2b160325cfe48678eeab213d3: python -m pytest -q bootstrap = 4 PASS, 0.50s. All eight original MIT cairnsearch files including LICENSE fetched via GitHub at upstream eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 and compared against pinned PR manifest: 8/8 equal blob SHA and text. Local seven executable vendor blobs match manifest. Source provenance verified; do not rewrite them.
- #18 head c118f8e7ba848f8f3c2286d1d0df7f87be788e70: PYTHONPATH=src python -m pytest -q tests/extract = 5 PASS, 2.60s; local code/test blobs 530ac42da7e255b2e0f5262f5bd5f6a8e36b203a and 65a3e2a7772b4fada5cc62d89c8703367dc0c0b4 match PR evidence. Needs snapshot adapter and candidate isolation.
- #19 head 5c4853d8dce3bf397e4f2c240727ac03ee7fc48b: python -m unittest discover -s tests/packaging -v = 5 PASS, 1.183s; local code/test blobs 35f00e6236434cbfedcc9661ba7cf5e7cd2a7846 and d50ed8c79a454ac40e109097656d8bd7418d4fc4. Clean verified wheel installation NOT_RUN.
- #20 head 4694b0d31cce3635b80d22ae7c13eb67b76ed7a1: PYTHONPATH=src python -m pytest -q tests/catalog/test_intake.py = 21 PASS, 0.05s, 18 deprecation warnings; local code/test blobs match PR evidence. Safe archive support not yet present.
- Total 35 existing tests PASS in separate trees; two additional reproduced contract defects above. No approved independent reviews or CI workflow/status results returned for any of these four heads. Do not merge yet. Integrated Windows/NVDA and offline install NOT_RUN. No G/U gate promoted to DONE. G6 Carr collection remains BLOCKED_MISSING_CORPUS, not a generic release blocker.

## Additional public upstream triage (pinned license verified; not adopted or executed)
- https://github.com/inyoungoh-cde/hyji/tree/7dc68a37c900b599542621439eb6c97c0a02f176 MIT, LICENSE blob 4021352ce81c68ae1ff1e188f2a4906ccdb4b3ce: promising complete local research hub, page search, notes, export, backup. CARR-09/08 evaluate reuse before writing comparable GUI/export.
- https://github.com/linxiv-dev/linXiv/tree/bc34d524615eea17b78f88926cf811f2e315767e Apache-2.0, LICENSE blob 261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64: desktop Tauri research app, SQLite, notes and export. Higher stack integration cost; evaluate only reusable components.
- https://github.com/alessandrabrondetta/paperlib/tree/c88ca9c0b0699ae2f66936f88effc8fe53100b1f MIT, LICENSE blob 90922427a009120ee39ea1f86adca3e05f1a397c: local CLI FTS, evaluate ranking/metadata if it adds capability beyond existing FTS5.
- https://github.com/FF-TEC/NeuronCite/tree/fe323f4c25c0031955af8d7adf1a611a8d3f9b70 AGPL-3.0, LICENSE blob be3f7b28e564e7dd05eaf59d64adba1a4065ac0e: optional separate integration only pending compatibility review; do not copy into project. No model downloads or external processing without consent.
- No license means no copying: Text-seeker explicitly proprietary; Atlas still not authorized. Source archive download via direct git blocked by DNS in Linux; pinned GitHub reads worked. Existing cairnsearch actual source import remains preferred for supported formats. No claim of new adoption of these four candidates.

Next: CARR-04/05/08/10 coordinate immutable locator and snapshot; CARR-11 set up trusted CI, CARR-12/13/14 review exact heads. CARR-01 recheck latest head and independent reviews before merge.
