# Optional retained-source page benchmark

The existing cache-storage benchmark keeps its original command and behavior.
Its new `--source-pages` mode measures the current source-page API on a fresh,
deterministic synthetic TXT corpus. It runs only when explicitly requested.
It creates no persistent index, changes no search budget, and reads no existing
user library.

This is a measurement tool. A completed report is **not a performance
acceptance verdict**. The [2026-11-27 measured results](SOURCE_PAGE_BENCHMARK_RESULTS_2026-11-27.md)
retain complete Linux samples and explicitly incomplete Windows 50,000-page setup.

## Run a bounded experiment

From a checkout, using that checkout's implementation:

```sh
python -B -X utf8 scripts/benchmark_semantic_cache.py --source-pages \
  --output-dir /new/source-pages-run --sizes 1000 10000 50000 \
  --hardware-label workstation-a
```

To measure an installed package with the same inspectable harness:

```sh
python -I -B -X utf8 scripts/benchmark_semantic_cache.py --source-pages --installed \
  --output-dir /new/installed-source-pages-run --sizes 1000
```

Use a new output directory. Existing files are never overwritten.
The selected package's Python source inventory and the harness snapshot are
hashed before and after measurements; each child verifies the same admitted
inventory. Installed mode requires the imported package to match the selected
installed directory and does not insert checkout source.

The new mode reuses the existing owned-process boundary. Its supported child
ownership is the current Windows Job Object path or Linux waitid/pidfd path.
Each corpus is prepared by one child; each requested search mode then uses a
fresh child to open that corpus. The actual interpreter path is retained,
including a virtual environment's executable. There is no separate process
supervisor in this script.

## Corpus and sampling

Every original is strict UTF-8 with form-feed-separated logical pages, retained
through `LibraryApp.add_text`. The default recipe uses 512 characters per page,
128 pages per book, and one exact query occurrence every seventh page.
The report records the recipe SHA256, deterministic content SHA256, original
byte count and actual database counts. Synthetic workspace/edition identifiers
are ordinary live application identities; they are not reused as a global
corpus identity.

The default new-mode experiment uses 1,000 pages, phrase and thematic search,
three repetitions, and at most three result pages per repetition.
`--sizes` accepts up to six distinct counts between 1 and 50,000.
The options `--characters`, `--pages-per-book`, `--match-every`, `--modes`,
`--page-budget`, `--character-budget`, `--result-limit`,
`--pages-per-trial`, and `--repeats` make the workload explicit.
Thematic ranking is local to each candidate source window. This tool does not
claim a corpus-wide relevance ranking or retrieval-quality score.

A returned empty page can still have a continuation. The benchmark records that
state and follows the actual opaque token within the selected sample bound.
Only a page whose `complete` field is true establishes exhaustion for that
trial. Finishing the configured sample is not the same as exhausting the
corpus. Every returned result must remain verified; one result per nonempty
page is copied through the live source API and compared exactly, with that
additional verification time reported separately.

Each public `search_page` call still validates the **complete selected source
snapshot**. That cost is included in its latency. The reported candidate
`scanned_segments` value describes the bounded candidate window, not all work
done by snapshot validation.

## Interpret the evidence

`source-pages.json` contains the complete report. `run.json`, the harness
snapshot, each corpus receipt, and per-phase reports remain alongside it.

- Setup time and setup peak RSS are separate from query measurements.
- Search latency includes the real service call, source authorization,
  snapshot validation and result construction. Opening the library and the
  additional sampled copy check have separate timings.
- First-page and continuation latency distributions retain raw observations
  and use nearest-rank p50/p95. A small sample's p95 can be its maximum;
  no timing threshold turns that observation into PASS.
- RSS is the whole fresh measurement process's high-water value, including
  Python imports and source-inventory checking. The report records the value
  before search and after each call. Subtracting two high-water values does
  not measure live allocations or prove the memory cost of one operation.
- OS file caches are not flushed. A fresh process is not a cold-disk test.
  Repetitions retain that process and its OS cache state.
- The hardware ID fingerprints the reported architecture, processor and
  logical CPU fields. It is not a unique physical-machine serial.
  The optional operator label distinguishes otherwise identical descriptions.
  Python, platform and SQLite versions have a separate runtime fingerprint.
- Neural inference, retrieval quality, cold-disk behavior and native
  application acceptance remain explicitly unmeasured.

`--search-timeout` retains the production API's maximum 60-second allowance.
An actual search timeout is recorded as such, without a fabricated empty or
fast result. `--case-timeout` bounds each owned query child (default 600 seconds).
An optional `--build-timeout` sets a separate finite positive allowance for corpus
setup; when omitted it inherits `--case-timeout`, preserving the original
behavior. The configuration digest, complete report and incomplete receipt bind
all three allowances: setup, query child and public search. For a separately
declared Windows setup experiment, for example, use `--build-timeout 1200`
with unchanged `--case-timeout 600 --search-timeout 60` and a new output directory.
The original interrupted experiment stays incomplete and is never resumed or
reclassified by this new run. These are execution limits, not performance thresholds. A child failure or
interruption retains the new synthetic directory and writes
`source-pages-incomplete.json` when possible; it does not produce a completed
aggregate report or delete files whose native owner may still be active.

The focused selector is
`tests/search/test_source_page_benchmark.py`. It exercises actual owned Python
children and retained source/cursor APIs, changed-source and changed-harness
refusal, search-deadline reporting and create-only output. It requires no
FFmpeg, neural model, network request or audio device.
