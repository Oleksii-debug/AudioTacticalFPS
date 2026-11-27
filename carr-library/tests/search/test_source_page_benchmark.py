"""The optional harness uses retained source APIs and actual owned Python children."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

import carr_library
from carr_library.application import LibraryApp
from carr_library.audio.models import AudioError
from carr_library.audio import process


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'benchmark_semantic_cache.py'


@pytest.fixture
def benchmark():
    spec = importlib.util.spec_from_file_location('_source_page_benchmark_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def options(output):
    # Test the package under test. Installed gates must never inject checkout src.
    package = Path(carr_library.__file__).resolve().parent
    source_package = SCRIPT.parents[1] / 'src' / 'carr_library'
    installed = package != source_package.resolve()
    return ['--source-pages', '--output-dir', str(output), '--sizes', '33',
        '--characters', '128', '--pages-per-book', '8', '--match-every', '7',
        '--page-budget', '4', '--result-limit', '3', '--pages-per-trial', '20',
        '--repeats', '2', '--case-timeout', '90', *(['--installed'] if installed else [])]


def test_real_owned_children_measure_retained_pages_without_a_performance_verdict(
        benchmark, tmp_path, monkeypatch):
    output = tmp_path / 'new-benchmark'
    launches = []
    original = process.run_process

    def observed(argv, **kwargs):
        assert kwargs['owned_tree'] is True
        assert argv[0] == str(Path(sys.executable).absolute())
        assert argv[1:5] == ['-I', '-B', '-X', 'utf8']
        assert 'PYTHONPATH' not in kwargs['env']
        launches.append(tuple(argv))
        return original(argv, **kwargs)

    monkeypatch.setattr(process, 'run_process', observed)
    assert benchmark._source_pages_main(options(output)) == 0
    assert len(launches) == 3
    report = json.loads((output / 'source-pages.json').read_bytes())
    assert report['outcome'] == 'measured'
    assert report['claims']['performance_acceptance'] == 'NOT_ASSESSED'
    assert report['claims']['neural_inference'] == 'NOT_RUN'
    assert not (output / 'source-pages-incomplete.json').exists()
    case, = report['cases']
    corpus = case['corpus']
    assert corpus['actual_pages'] == 33 and corpus['actual_editions'] == 5
    assert corpus['content_sha256'] == benchmark._page_content_id(corpus['recipe'])[0]
    assert corpus['actual_characters'] == 33 * 128
    assert corpus['setup_peak_rss_bytes'] > 0
    assert {row['mode'] for row in case['measurements']} == {'phrase', 'thematic'}
    for measured in case['measurements']:
        assert measured['search_timeouts'] == 0
        assert measured['sampling_complete_trials'] == 2
        assert measured['first_page']['samples'] == 2
        assert measured['continuation_pages']['samples'] > 0
        assert measured['process_peak_rss_bytes'] >= measured['baseline_peak_rss_bytes'] > 0
        assert measured['full_snapshot_scope_pages'] == 33
        for trial in range(2):
            records = [row for row in measured['samples'] if row['trial'] == trial]
            assert sum(row['results'] for row in records) == 5
            assert records[-1]['complete'] is True
            assert all(row['seconds'] >= 0 and row['reported_scanned_segments'] <= 4 for row in records)
    reopened = LibraryApp(output / '33' / 'library')
    found = reopened.search_page(corpus['workspace'], benchmark._SOURCE_PAGE_QUERY,
                                 page_budget=4, limit=3)
    assert found.scope_editions == 5
    assert all(row.verified for row in found)
    assert report['environment']['hardware_id']
    assert report['production_python_inventory_sha256']


def test_actual_source_change_cannot_be_recorded_as_a_successful_measurement(benchmark, tmp_path):
    case = tmp_path / 'case'
    corpus = benchmark._page_build(case, benchmark._page_recipe(8, 128, 8, 2))
    app = LibraryApp(case / 'library')
    with app.read(corpus['workspace']) as db:
        digest, = db.execute('SELECT asset FROM catalog_edition WHERE workspace=?',
                            (corpus['workspace'],)).fetchone()
    source = app.assets.resolve(digest)
    source.write_bytes(source.read_bytes().replace(b'Synthetic', b'Modified!', 1))
    with pytest.raises(ValueError):
        benchmark._page_measure(case, corpus, 'phrase', dict(
            repeats=1, pages_per_trial=2, result_limit=3, page_budget=4,
            character_budget=1000, search_timeout=60))
    assert source.exists()


def test_actual_search_deadline_is_reported_without_fabricating_a_fast_result(benchmark, tmp_path, monkeypatch):
    from carr_library import application
    case = tmp_path / 'case'
    corpus = benchmark._page_build(case, benchmark._page_recipe(4, 128, 4, 1))
    # Expire the real application budget deterministically, independently of
    # host clock resolution. Corpus creation and search/deadline code stay real.
    ticks = []
    def advancing_clock():
        value = 100.0 + len(ticks)
        ticks.append(value)
        return value
    monkeypatch.setattr(application, 'monotonic', advancing_clock)
    measured = benchmark._page_measure(case, corpus, 'phrase', dict(
        repeats=2, pages_per_trial=2, result_limit=2, page_budget=2,
        character_budget=1000, search_timeout=1e-12))
    assert len(ticks) >= 4, 'Both real search budgets must consult the controlled clock.'
    assert measured['search_timeouts'] == 2
    assert measured['sampling_complete_trials'] == 0
    assert measured['first_page']['samples'] == 0
    assert measured['first_page']['p50_seconds'] is None
    assert all(row['outcome'] == 'search-timeout' and 'results' not in row
               for row in measured['samples'])


def test_configuration_digest_is_checked_before_package_or_corpus_access(benchmark, tmp_path):
    config = tmp_path / 'run.json'
    config.write_text('{}', encoding='utf-8')
    case = tmp_path / '33'
    with pytest.raises(ValueError, match='configuration changed'):
        benchmark._source_page_case([str(config), '0' * 64, 'build', str(case), '-'])
    assert not case.exists()


def test_owned_child_refuses_changed_harness_and_keeps_partial_evidence(
        benchmark, tmp_path, monkeypatch):
    output = tmp_path / 'new-benchmark'
    original = process.run_process
    calls = []

    def mutate_after_build(argv, **kwargs):
        result = original(argv, **kwargs)
        calls.append(tuple(argv))
        if len(calls) == 1:
            harness = output / 'benchmark-harness.py'
            harness.write_bytes(harness.read_bytes() + b'\n# Changed after the admitted build.\n')
        return result

    monkeypatch.setattr(process, 'run_process', mutate_after_build)
    with pytest.raises(AudioError, match='harness snapshot changed'):
        benchmark._source_pages_main(options(output))
    assert len(calls) == 1  # The second real child failed its own admission.
    assert (output / '33' / 'corpus.json').is_file()
    assert (output / '33' / 'build.json').is_file()
    assert not (output / 'source-pages.json').exists()
    failure = json.loads((output / 'source-pages-incomplete.json').read_bytes())
    assert failure['outcome'] == 'incomplete' and failure['error_type'] == 'AudioError'


def test_existing_output_is_never_reused_or_overwritten(benchmark, tmp_path):
    output = tmp_path / 'occupied'
    output.mkdir()
    sentinel = output / 'original.txt'
    sentinel.write_bytes(b'Existing user-owned bytes.')
    with pytest.raises(FileExistsError):
        benchmark._source_pages_main(options(output))
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b'Existing user-owned bytes.'


@pytest.mark.parametrize('extra', [
    ['--sizes', '0'], ['--characters', '127'], ['--search-timeout', 'nan'],
    ['--case-timeout', 'inf'], ['--modes', 'semantic'],
    ['--page-budget', '500', '--character-budget', '5000000'],
], ids=['empty-corpus', 'short-page', 'nan-deadline', 'infinite-child',
        'no-unqualified-model', 'must-exercise-source-windows'])
def test_invalid_experiments_create_no_output(benchmark, tmp_path, extra):
    output = tmp_path / 'unused'
    with pytest.raises(SystemExit):
        benchmark._source_pages_main(options(output) + extra)
    assert not output.exists()


@pytest.mark.parametrize('build_timeout', [None, 1200], ids=['unchanged-default', 'setup-only-followup'])
def test_real_build_allowance_is_bound_without_changing_query_limits(
        benchmark, tmp_path, monkeypatch, build_timeout):
    output = tmp_path / 'fresh-setup-measurement'
    arguments = options(output)
    index = arguments.index('--case-timeout')
    del arguments[index:index + 2]  # Exercise the unchanged public default of 600 seconds.
    if build_timeout is not None:
        arguments += ['--build-timeout', str(build_timeout)]
    expected = {'build_seconds': 600 if build_timeout is None else build_timeout,
                'query_child_seconds': 600, 'public_search_seconds': 60}
    observed = []
    original = process.run_process

    def checked(argv, **kwargs):
        action = argv[-3]
        budget = kwargs['budget']
        assert kwargs['owned_tree'] is True
        assert budget.timeout == expected[
            'build_seconds' if action == 'build' else 'query_child_seconds']
        assert budget.max_output_bytes == 768 * 1024**2
        assert budget.max_log_bytes == 2 * 1024**2
        assert budget.max_staging_files == 10000
        assert kwargs['watch'] == ((output / '33',) if action == 'build'
                                  else (output / '33' / (action + '.json'),))
        observed.append(action)
        return original(argv, **kwargs)

    monkeypatch.setattr(process, 'run_process', checked)
    assert benchmark._source_pages_main(arguments) == 0
    assert observed == ['build', 'phrase', 'thematic']
    configuration = (output / 'run.json').read_bytes()
    config = json.loads(configuration)
    report = json.loads((output / 'source-pages.json').read_bytes())
    assert config['execution_timeouts'] == report['execution_timeouts'] == expected
    assert config['settings']['search_timeout'] == report['settings']['search_timeout'] == 60
    assert report['configuration_sha256'] == benchmark.sha256(configuration).hexdigest()
    case, = report['cases']
    for phase in (case['corpus'], *case['measurements']):
        assert phase['configuration_sha256'] == report['configuration_sha256']
        assert phase['outcome'] == 'measured'
    assert case['corpus']['actual_pages'] == 33
    assert case['corpus']['actual_editions'] == 5
    assert all(row['search_timeouts'] == 0 for row in case['measurements'])
    assert report['claims']['performance_acceptance'] == 'NOT_ASSESSED'
    assert not (output / 'source-pages-incomplete.json').exists()


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '3601'])
def test_invalid_build_allowance_creates_no_output(benchmark, tmp_path, value):
    output = tmp_path / 'unadmitted-build'
    with pytest.raises(SystemExit):
        benchmark._source_pages_main(options(output) + ['--build-timeout', value])
    assert not output.exists()
