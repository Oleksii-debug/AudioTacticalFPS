# Retained-library capacity measurement

The executable probe constructs 1,000, 10,000 and 50,000 distinct synthetic books
using the existing catalog, retained-original and immutable revision writers.
Each default book has 20 source segments; the largest profile therefore has
1,000,000 actual searchable segments and 50,000 separate original files.

The fixture construction batches 250 catalog registrations in each transaction
and then runs the full Storage integrity check. It is explicitly separate from
the measured public import_file samples. It is not a claim that every fixture
book was individually imported through the supervised desktop workflow.

## Recorded measurements

- Full catalog continuation and every exact query hit, not a sampled search.
- Independent source Copy at the first, middle and last book.
- Saved result cursor reopened halfway through the corpus.
- ReadingDesk source positions and preparation of exact retained-original views.
- Real cooperative cancellation and exact continuation after cancellation.
- Sampled public supervised imports at the existing corpus size.
- Actual owned worker cancellation after durable checkpoints, removal of only
  the new external synthetic input, and resume of the same job from retained bytes.
- Fresh isolated process startup, P50/P95 and observation counts.
- Parent process peak RSS/working set and phase-sampled private logical disk bytes.

OS filesystem caches are not evicted. Child process peak RSS, transient disk
high-water marks, native GUI startup, NVDA, physical audio, semantic inference,
real book quality and distribution clearance are not established by this probe.
Complete machine backup is a separate acceptance boundary until its actual
large-profile backup/restore measurement is included in a recorded run.

## Source change

Scoped locator validation now puts revision=? directly into the SQLite query,
instead of using a nullable OR predicate. Full validation still scans all pages;
scoped publication validates only its revision. The real SQLite instruction
counter regression verifies that adding 20,000 unrelated pages does not increase
the chosen revision's work, while retaining sequence/physical/printed-label
refusals and rollback checks.

## Run

Use the actual installed package matching the selected checkout:

    python -I -B scripts/benchmark_library_corpus.py --installed --output-dir NEW_DIRECTORY --books 50000 --segments-per-book 20 --timeout 7200 --source-commit EXACT_40_HEX_COMMIT

The destination must be new. The bounded report is library-corpus.json; no corpus
or private input is uploaded by CI. A failed phase retains a FAIL report and cannot
be relabelled as a successful measurement. Current source-only additions are
NOT_RUN until an exact-source Actions receipt proves execution.

The CLI requires --installed. Socket refusal covers the parent and startup Python
probe; it does not establish OS-level or native-child egress isolation.
