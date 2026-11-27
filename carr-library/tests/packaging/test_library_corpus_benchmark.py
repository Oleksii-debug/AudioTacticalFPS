"""Exercise the capacity harness with real SQLite, retained files and owned workers."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp


def harness():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'benchmark_library_corpus.py'
    spec = importlib.util.spec_from_file_location('carr_capacity_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def corpus(tmp_path, books=61, segments=3):
    module = harness()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual capacity sample')
    collection = app.create_collection(workspace, 'Exact corpus')
    evidence = module.seed_corpus(app, workspace, collection, books, segments, module.Deadline(120))
    return module, app, workspace, collection, evidence


def test_fixture_creates_distinct_retained_books_and_exact_source_segments(tmp_path):
    module, app, workspace, collection, evidence = corpus(tmp_path, books=40, segments=5)
    assert evidence['books'] == evidence['distinct_originals'] == 40
    assert evidence['source_segments'] == 200 and evidence['source_bytes'] > 0
    with app.read(workspace) as db:
        rows = db.execute('SELECT id,asset FROM catalog_edition WHERE workspace=? ORDER BY id',
                          (workspace,)).fetchall()
    for index, row in enumerate(rows):
        assert row['id'] == module.book_ids(index)[1]
        retained = app.assets.resolve(row['asset'])
        assert retained.read_bytes() == module.book_text(index, 5).encode('utf-8')
    assert module.measure_catalog(app, workspace, 40, module.Deadline(60))['books'] == 40


def test_real_corpus_search_reopens_cursor_reaches_tail_and_keeps_exact_source_views(tmp_path):
    module, app, workspace, collection, evidence = corpus(tmp_path)
    reopened, search = module.measure_search(app, workspace, collection, 61,
        page_size=7, deadline=module.Deadline(120))
    assert reopened is not app and reopened.root == app.root
    assert search['exact_hits'] == 61 and search['result_pages'] >= 9
    assert search['reopen_with_saved_cursor']['observations'] == 1
    views = module.measure_sources(reopened, workspace, 61, 3, 3, module.Deadline(60))
    assert views['sampled_books'] == 3
    cancellation = module.measure_cancellation(reopened, workspace, collection, module.Deadline(60))
    assert cancellation['status'] == 'PASS' and cancellation['exact_cursor_retry']


def test_isolated_installed_small_corpus_executes_owned_worker_recovery_and_records_limits(tmp_path):
    module = harness()
    output = tmp_path / 'measurement'
    command = [sys.executable, '-I', '-B', str(Path(module.__file__)),
        '--installed', '--output-dir', str(output), '--books', '16',
        '--segments-per-book', '3', '--samples', '3', '--page-size', '5',
        '--timeout', '180', '--source-commit', '0' * 40]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=210)
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-5000:]
    report = json.loads((output / 'library-corpus.json').read_text(encoding='utf-8'))
    assert report['status'] == 'PASS'
    assert report['fixture']['books'] == 16 and report['fixture']['source_segments'] == 48
    assert report['search']['exact_hits'] == 16 and report['worker_recovery']['same_job']
    assert report['worker_recovery']['external_original_removed']
    assert report['startup']['latency']['observations'] == 3
    assert report['claims']['semantic_inference'] == 'NOT_RUN'
    assert report['parent_process_peak_rss_bytes'] > 0
    assert report['phase_sampled_peak_private_file_bytes'] > 0
    before = (output / 'library-corpus.json').read_bytes()
    again = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert again.returncode != 0 and (output / 'library-corpus.json').read_bytes() == before


def test_measurement_percentiles_keep_observation_count_and_actual_tail():
    result = harness().distribution(list(range(1, 101)))
    assert result['observations'] == 100 and result['p50_seconds'] == 50
    assert result['p95_seconds'] == 95 and result['maximum_seconds'] == 100
    for invalid in ([], [float('nan')], [-1], [True]):
        with pytest.raises(ValueError):
            harness().distribution(invalid)
