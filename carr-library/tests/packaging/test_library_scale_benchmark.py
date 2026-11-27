"""Actual canonical imports and real isolated children; no large-corpus PASS claim."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from carr_library import application
from carr_library.application import LibraryApp

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'benchmark_library_scale.py'
SPEC = importlib.util.spec_from_file_location('_library_corpus_benchmark', SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def case(**updates):
    value = {'books': 3, 'segments_per_book': 51, 'segment_bytes': 128,
             'result_page_size': 17, 'repeats': 2, 'phase_timeout': 300.0,
             'max_disk_bytes': 128 * 1024 ** 2, 'min_free_bytes': 0}
    return {**value, **updates}


def selection():
    actual = Path(application.__file__).resolve()
    source = SCRIPT.parents[1] / 'src'
    if actual.is_relative_to(source):
        return ['--source-root', str(source)]
    return ['--installed']


def cli(output, *extra):
    executable, env = benchmark.interpreter()
    command = [executable, '-I', '-B', str(SCRIPT), '--output-dir', str(output),
               '--sizes', '2', '--segments-per-book', '3', '--segment-bytes', '128',
               '--repeats', '1', '--phase-timeout', '120', '--min-free-bytes', '0',
               '--expected-application-origin', str(Path(application.__file__).resolve()),
               *selection(), *extra]
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=150, env=env, check=False)


@pytest.fixture(scope='module')
def retained(tmp_path_factory):
    root = tmp_path_factory.mktemp('actual-corpus')
    benchmark.activate()
    result = benchmark.build_corpus(root, case())
    assert result['status'] == 'PASS'
    return root, result


def test_real_imports_source_and_result_cursors_reading_and_cancel_recover(retained):
    root, built = retained
    measured = benchmark.measure_corpus(root, case())
    reopened = benchmark.measure_corpus(root, case(), restart=True)
    assert built['books'] == measured['books'] == reopened['books'] == 3
    assert built['segments'] == measured['segments'] == reopened['segments'] == 153
    assert built['corpus_sha256'] == measured['corpus_sha256'] == reopened['corpus_sha256']
    assert built['source_utf8_bytes'] == 3 * (51 * 128 + 50)
    assert built['imports']['count'] == 3
    traversal = measured['full_traversal']
    assert traversal['results'] == 153 and traversal['source_windows'] == 2
    assert traversal['requests'] > traversal['source_windows']
    assert traversal['exact_no_loss_no_duplicates']
    assert traversal['copied_result_samples'] == traversal['requests']
    assert measured['reading']['indices'] == [0, 1, 2]
    assert measured['first_result_page']['count'] == measured['late_edition_first_page']['count'] == 2
    assert measured['cancellation']['previous_handles_preserved']
    assert measured['cancellation']['cross_scope_cursor_refused']
    assert measured['cancellation']['subsequent_query_completed']
    assert measured['database_unchanged'] and reopened['database_unchanged']
    assert reopened['full_traversal']['status'] == 'NOT_REPEATED'
    assert measured['disk']['bytes'] > built['source_utf8_bytes']
    metadata = benchmark.read_json(root / 'corpus.json')
    app = LibraryApp(root / 'library')
    assert len(app.browse(metadata['workspace'])) == 3
    assert len(app.browse(metadata['excluded_workspace'])) == 1
    actual_page = app.search_page(metadata['workspace'], benchmark.QUERY, mode='terms')
    assert actual_page.results and all(type(row.page) is str for row in actual_page.results)
    assert all(type(app._selections[row.ref].ordinal) is int for row in actual_page.results)
    for index, row in enumerate(app.browse(metadata['workspace'])):
        assert app.assets.resolve(row.asset).read_text(encoding='utf-8') == benchmark.source_text(index, case())


def test_actual_retained_byte_corruption_refuses_reopened_measurement(retained, tmp_path):
    original, _ = retained
    root = tmp_path / 'copied'
    shutil.copytree(original, root)
    metadata = benchmark.read_json(root / 'corpus.json')
    app = LibraryApp(root / 'library')
    row = app.browse(metadata['workspace'])[0]
    asset = app.assets.resolve(row.asset)
    raw = asset.read_bytes()
    asset.write_bytes(raw.replace(b'CARRCorpusNeedle', b'XARRCorpusNeedle', 1))
    before = app.database.read_bytes()
    with pytest.raises(ValueError):
        benchmark.measure_corpus(root, case())
    assert asset.read_bytes() != raw and app.database.read_bytes() == before


def test_actual_hardlink_to_retained_input_is_not_an_accepted_corpus(retained, tmp_path):
    original, _ = retained
    root = tmp_path / 'copied'
    shutil.copytree(original, root)
    metadata = benchmark.read_json(root / 'corpus.json')
    app = LibraryApp(root / 'library')
    asset = app.assets.resolve(app.browse(metadata['workspace'])[0].asset)
    raw = asset.read_bytes()
    alias = tmp_path / 'alias.txt'
    os.link(asset, alias)
    with pytest.raises(ValueError):
        benchmark.measure_corpus(root, case())
    assert asset.read_bytes() == alias.read_bytes() == raw


def test_budget_rejects_same_path_directory_replacement_before_action(tmp_path):
    root = tmp_path / 'corpus'
    root.mkdir()
    budget = benchmark.Budget(root, case())
    root.rename(tmp_path / 'old-corpus')
    root.mkdir()
    with pytest.raises(benchmark.BenchmarkFailure, match='replaced'):
        budget.check()
    assert list(root.iterdir()) == []


def test_real_elapsed_deadline_and_measured_disk_refuse_more_work(tmp_path):
    root = tmp_path / 'corpus'
    root.mkdir()
    budget = benchmark.Budget(root, case(phase_timeout=0.001))
    # Let real time pass; adjacent counter reads can be equal on Windows runners.
    time.sleep(0.02)
    with pytest.raises(benchmark.BudgetExceeded, match='time budget'):
        budget.check()
    (root / 'actual.bin').write_bytes(b'xx')
    budget = benchmark.Budget(root, case(max_disk_bytes=1))
    with pytest.raises(benchmark.BudgetExceeded, match='disk budget'):
        budget.disk()
    assert budget.last_disk['bytes'] == 2


@pytest.mark.parametrize('updates', [
    {'books': True}, {'books': 0}, {'books': 50001},
    {'segments_per_book': 0}, {'segment_bytes': 127}, {'result_page_size': 101},
    {'repeats': 0}, {'phase_timeout': float('nan')}, {'max_disk_bytes': 0},
    {'books': 50000, 'segments_per_book': 1000},
    {'segments_per_book': 1000, 'segment_bytes': 65536},
])
def test_invalid_budget_never_creates_a_library(tmp_path, updates):
    with pytest.raises(benchmark.BenchmarkFailure):
        benchmark.build_corpus(tmp_path, case(**updates))
    assert list(tmp_path.iterdir()) == []


def test_explicit_cancel_before_import_leaves_no_library(tmp_path):
    # A cancelled request must be observed before canonical startup creates DBs.
    with pytest.raises(InterruptedError):
        benchmark.build_corpus(tmp_path, case(), cancelled=lambda: True)
    assert not (tmp_path / 'corpus.json').exists()
    assert not (tmp_path / 'library').exists()


def test_original_output_is_never_overwritten(tmp_path):
    output = tmp_path / 'existing'
    output.mkdir()
    sentinel = output / 'keep.txt'
    sentinel.write_text('Exact prior report Ω', encoding='utf-8')
    options = case()
    options.pop('books')
    with pytest.raises(benchmark.BenchmarkFailure, match='already exists'):
        benchmark.run_benchmark(output, [1], options)
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding='utf-8') == 'Exact prior report Ω'


def test_actual_cli_runs_three_owned_processes_and_binds_parent_import(tmp_path):
    output = tmp_path / 'actual'
    result = cli(output)
    assert result.returncode == 0, (result.stdout.decode('utf-8'), result.stderr.decode('utf-8'))
    report = benchmark.read_json(output / 'library-corpus-scale.json')
    assert report['status'] == 'PASS' and report['requested_sizes'] == [2]
    assert report['claims']['release_capacity'] == 'NOT_ESTABLISHED_BY_THIS_HARNESS'
    assert report['claims']['cold_disk_latency'] == 'NOT_MEASURED'
    assert set(report['origins']) == set(benchmark.MODULES)
    expected_path = str(Path(application.__file__).resolve())
    assert report['origins']['carr_library.application']['path'] == expected_path
    assert report['origins']['carr_library.application']['sha256'] == sha256(Path(expected_path).read_bytes()).hexdigest()
    phases = report['cases'][0]['phases']
    assert [phase['phase'] for phase in phases] == ['build', 'measure', 'restart']
    assert all(phase['status'] == 'PASS' and phase['books'] == 2 and phase['segments'] == 6
               and phase['process_reaped'] and phase['exit_code'] == 0 for phase in phases)
    assert all(phase['origins'] == report['origins'] and phase['pid'] != os.getpid()
               and phase['python_prefix'] == sys.prefix for phase in phases)
    assert len({phase['corpus_sha256'] for phase in phases}) == 1
    assert phases[1]['full_traversal']['results'] == 6
    assert all(phase['process_peak_rss_bytes'] > 0 for phase in phases)
    assert (output / '2/library/library.sqlite').is_file()
    for name in ('configuration.json', '2/corpus.json', '2/build.json', '2/measure.json', '2/restart.json'):
        assert (output / name).is_file()
    assert not list(output.rglob('*.pyc'))


def test_real_cli_budget_stop_is_partial_and_remaining_scale_is_not_run(tmp_path):
    output = tmp_path / 'partial'
    result = cli(output, '--sizes', '2', '3', '--max-disk-bytes', '1')
    assert result.returncode == 2, (result.stdout, result.stderr)
    report = benchmark.read_json(output / 'library-corpus-scale.json')
    assert report['status'] == 'PARTIAL'
    assert report['cases'][0]['status'] == 'PARTIAL'
    assert report['cases'][0]['phases'][0]['reason'] == 'BudgetExceeded'
    assert report['cases'][0]['phases'][0]['progress']['imported_books'] == 0
    assert report['cases'][1]['status'] == 'NOT_RUN'
    assert not (output / '3').exists()
    assert not (output / '2/measure.json').exists()
    assert (output / '2/library/library.sqlite').is_file()  # retained, never auto-pruned


def test_wrong_expected_application_refuses_before_any_output(tmp_path):
    wrong = tmp_path / 'other.py'
    wrong.write_text('# not application\n', encoding='utf-8')
    output = tmp_path / 'refused'
    result = cli(output, '--expected-application-origin', str(wrong))
    assert result.returncode != 0
    assert not output.exists()


def test_actual_child_refuses_origin_tamper_before_creating_corpus(tmp_path):
    origins = benchmark.activate()
    root = tmp_path / 'run'
    root.mkdir()
    directory = root / '2'
    directory.mkdir()
    _, kernel = benchmark.memory_kernel()
    settings = case(books=2, segments_per_book=3)
    settings.pop('books')
    source = SCRIPT.parents[1] / 'src'
    selected = str(source) if Path(application.__file__).resolve().is_relative_to(source) else None
    origins['carr_library.application']['sha256'] = '0' * 64
    configuration = {'schema': benchmark.SCHEMA, 'output': str(root), 'sizes': [2],
                     'case': settings, 'source_root': selected, 'origins': origins,
                     'memory_kernel': kernel, 'harness_sha256': benchmark.file_hash(SCRIPT)}
    path = root / 'configuration.json'
    benchmark.save_json(path, configuration)
    executable, env = benchmark.interpreter()
    result = subprocess.run([executable, '-I', '-B', str(SCRIPT), '--child', str(path),
        benchmark.file_hash(path), 'build', str(directory)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40, env=env, check=False)
    assert result.returncode != 0
    assert not (directory / 'library').exists() and not (directory / 'build.json').exists()


def test_percentiles_are_measured_nearest_rank_and_empty_is_not_zero_latency():
    actual = benchmark.samples([9.0, 1.0, 3.0, 2.0])
    assert actual['count'] == 4 and actual['p50_seconds'] == 2.0 and actual['p95_seconds'] == 9.0
    assert actual['total_seconds'] == 15.0
    assert benchmark.samples([])['p95_seconds'] is None


def test_configuration_replacement_after_precheck_cannot_change_measured_work(tmp_path, monkeypatch):
    origins = benchmark.activate()
    root = tmp_path / 'run'
    root.mkdir()
    directory = root / '2'
    directory.mkdir()
    _, kernel = benchmark.memory_kernel()
    settings = case(books=2, segments_per_book=3)
    settings.pop('books')
    source = SCRIPT.parents[1] / 'src'
    selected = str(source) if Path(application.__file__).resolve().is_relative_to(source) else None
    configuration = {'schema': benchmark.SCHEMA, 'output': str(root), 'sizes': [2],
                     'case': settings, 'source_root': selected, 'origins': origins,
                     'memory_kernel': kernel, 'harness_sha256': benchmark.file_hash(SCRIPT)}
    path = root / 'configuration.json'
    benchmark.save_json(path, configuration)
    expected = benchmark.file_hash(path)
    replacement = {**configuration, 'case': {**settings, 'segment_bytes': 192}}
    staged = root / 'replacement.json'
    benchmark.save_json(staged, replacement)
    assert staged.stat().st_size == path.stat().st_size
    real_hash = benchmark.file_hash
    swapped = []

    def replace_after_hash(value, **kwargs):
        result = real_hash(value, **kwargs)
        if Path(value) == path and not swapped:
            os.replace(staged, path)
            swapped.append(True)
        return result

    monkeypatch.setattr(benchmark, 'file_hash', replace_after_hash)
    with pytest.raises(benchmark.BenchmarkFailure, match='expected digest'):
        benchmark.child_main(str(path), expected, 'build', str(directory))
    assert swapped == [True]
    assert benchmark.read_json(path)['case']['segment_bytes'] == 192
    assert not (directory / 'library').exists() and not (directory / 'build.json').exists()
