"""Actual retained fixture setup and public-operation measurements; no scale PASS claim."""
from contextlib import closing
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from carr_library import application
from carr_library.application import LibraryApp
from carr_library.storage.revisions import Storage

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'benchmark_library_scale.py'
SPEC = importlib.util.spec_from_file_location('_retained_capacity_measurement', SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def case(**updates):
    return {'books': 3, 'segments_per_book': 2, 'segment_bytes': 128,
            'result_page_size': 17, 'repeats': 1, 'phase_timeout': 300.0,
            'max_disk_bytes': 256 * 1024 ** 2, 'min_free_bytes': 0, **updates}


def setup(root, **updates):
    root.mkdir()
    settings = case(**updates)
    result = benchmark.build_retained_fixture(root, settings)
    return settings, result, benchmark.read_json(root / 'corpus.json')


def test_actual_fixture_uses_retained_catalog_pages_and_existing_full_cursor_reader(tmp_path, monkeypatch):
    audits = []
    actual = Storage.check
    def checked(storage):
        result = actual(storage)
        audits.append(storage.db.execute('SELECT count(*) FROM page').fetchone()[0])
        return result
    monkeypatch.setattr(Storage, 'check', checked)
    settings, built, metadata = setup(tmp_path / 'fixture', segments_per_book=51)
    assert audits[-1] == 154  # 153 selected segments plus the real isolation import.
    assert built['construction'] == 'retained-fixture'
    assert built['setup']['maximum_books_per_transaction'] <= 250
    assert built['imports']['count'] == 0 and built['imports']['p50_seconds'] is None
    assert built['imports']['status'] == 'NOT_MEASURED_BY_FIXTURE_CONSTRUCTION'
    app = LibraryApp(tmp_path / 'fixture/library')
    with app.read(metadata['workspace']) as db:
        assert db.execute('SELECT count(*) FROM catalog_edition WHERE workspace=?',
                          (metadata['workspace'],)).fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM active_page WHERE workspace=?',
                          (metadata['workspace'],)).fetchone()[0] == 153
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
        # Fixture construction did not fabricate ordinary ingestion receipts.
        assert db.execute('SELECT count(*) FROM job').fetchone()[0] == 1
    for index, view in enumerate(app.browse(metadata['workspace'])):
        expected = benchmark.source_text(index, settings).encode('utf-8')
        assert view.asset == sha256(expected).hexdigest()
        assert app.assets.resolve(view.asset).read_bytes() == expected
    measured = benchmark.measure_corpus(tmp_path / 'fixture', settings)
    restarted = benchmark.measure_corpus(tmp_path / 'fixture', settings, restart=True)
    assert measured['full_traversal']['results'] == 153
    assert measured['full_traversal']['source_windows'] == 2
    assert measured['full_traversal']['exact_no_loss_no_duplicates']
    assert built['corpus_sha256'] == measured['corpus_sha256'] == restarted['corpus_sha256']
    assert measured['database_unchanged'] and restarted['database_unchanged']


def test_fixture_batch_failure_rolls_back_only_uncommitted_batch_and_has_no_ready_receipt(tmp_path, monkeypatch):
    root = tmp_path / 'interrupted-fixture'
    root.mkdir()
    actual = Storage.stage_revision
    def fail_after_stage(storage, workspace, edition, revision, asset, config, pages):
        value = actual(storage, workspace, edition, revision, asset, config, pages)
        if edition == benchmark.edition_id(250):
            raise RuntimeError('Actual staged fixture transaction interrupted.')
        return value
    monkeypatch.setattr(Storage, 'stage_revision', fail_after_stage)
    with pytest.raises(RuntimeError, match='staged fixture transaction'):
        benchmark.build_retained_fixture(root, case(books=251, segments_per_book=1))
    assert not (root / 'corpus.json').exists()
    app = LibraryApp(root / 'library')  # Actual cold audit, not an unchecked receipt.
    with app.read() as db:
        assert db.execute("SELECT count(*) FROM catalog_edition WHERE id LIKE 'bench-edition-%'").fetchone()[0] == 250
        assert not db.execute('SELECT 1 FROM revision WHERE id=?',
                              ('bench-fixture-revision-00000250',)).fetchone()
        assert db.execute("SELECT count(*) FROM active_page WHERE edition LIKE 'bench-edition-%'").fetchone()[0] == 250
    assert len(list(app.assets_dir.iterdir())) == 252  # Unpublished retained input is preserved too.


def test_fixture_full_audit_refuses_actual_index_damage_before_ready_receipt(tmp_path, monkeypatch):
    root = tmp_path / 'invalid-fixture'
    root.mkdir()
    actual = Storage.check
    damaged = []
    def checked(storage):
        if not damaged and storage.db.execute('SELECT count(*) FROM page').fetchone()[0] == 7:
            storage.db.execute('DELETE FROM indexed WHERE id=(SELECT id FROM indexed ORDER BY id LIMIT 1)')
            damaged.append(True)
        return actual(storage)
    monkeypatch.setattr(Storage, 'check', checked)
    with pytest.raises(ValueError, match='active index content mismatch'):
        benchmark.build_retained_fixture(root, case())
    assert damaged == [True] and not (root / 'corpus.json').exists()
    assert len(list((root / 'library/originals').iterdir())) == 4


def selection():
    source = SCRIPT.parents[1] / 'src'
    return ['--source-root', str(source)] if Path(application.__file__).resolve().is_relative_to(source) else ['--installed']


def actual_cli(output, *extra):
    executable, env = benchmark.interpreter()
    return subprocess.run([executable, '-I', '-B', str(SCRIPT),
        '--output-dir', str(output), '--sizes', '2', '--segments-per-book', '2',
        '--segment-bytes', '128', '--result-page-size', '3', '--repeats', '1',
        '--phase-timeout', '300', '--max-disk-bytes', str(512 * 1024 ** 2),
        '--min-free-bytes', '0', '--construction', 'retained-fixture',
        '--expected-application-origin', str(Path(application.__file__).resolve()),
        *selection(), *extra], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=500, env=env, check=False)


def test_actual_capacity_cli_keeps_separate_setup_public_receipts_restart_and_backup(tmp_path):
    output = tmp_path / 'capacity'
    completed = actual_cli(output, '--public-samples', '3', '--complete-backup')
    if completed.returncode:
        # Preserve the failing exit assertion, but expose this synthetic child's
        # retained phase evidence instead of losing it with pytest's temp root.
        try:
            retained = benchmark.read_json(output / 'library-corpus-scale.json')
            diagnostic = {'status': retained.get('status'), 'cases': []}
            for item in retained.get('cases', [])[:1]:
                row = {'requested_books': item.get('requested_books'),
                       'status': item.get('status'), 'phases': []}
                for phase in item.get('phases', [])[:5]:
                    detail = {key: phase.get(key) for key in (
                        'phase', 'status', 'reason', 'diagnostic', 'progress',
                        'pid', 'exit_code', 'process_reaped', 'process_wall_seconds')}
                    if (phase.get('status') != 'PASS' and phase.get('phase') in
                            ('build', 'measure', 'restart', 'operations', 'backup')):
                        error_path = output / '2' / (phase['phase'] + '.stderr.txt')
                        try:
                            with error_path.open('rb') as stream:
                                detail['stderr_prefix'] = stream.read(8192).decode('utf-8', 'replace')
                        except OSError as error:
                            detail['stderr_error'] = type(error).__name__
                    row['phases'].append(detail)
                diagnostic['cases'].append(row)
        except Exception as error:
            # A diagnostic read must never replace the original nonzero exit.
            diagnostic = {'diagnostic_error': type(error).__name__,
                          'message': str(error)[:2000]}
        print('CAPACITY_FAILURE_DIAGNOSTIC ' +
              json.dumps(diagnostic, ensure_ascii=True)[:65536], flush=True)
    assert completed.returncode == 0, (completed.stdout.decode('utf-8'), completed.stderr.decode('utf-8'))
    report = benchmark.read_json(output / 'library-corpus-scale.json')
    assert report['status'] == 'PASS' and report['schema'] == benchmark.SCHEMA
    assert report['capacity'] == {'construction': 'retained-fixture', 'public_samples': 3, 'complete_backup': True}
    phases = report['cases'][0]['phases']
    assert [row['phase'] for row in phases] == ['build', 'measure', 'restart', 'operations', 'backup']
    assert len({row['pid'] for row in phases}) == 5
    assert all(row['status'] == 'PASS' and row['process_reaped'] and row['exit_code'] == 0
               and row['books'] == 2 and row['segments'] == 4 for row in phases)
    assert len({row['corpus_sha256'] for row in phases}) == 1
    assert phases[0]['imports']['count'] == 0
    assert phases[0]['imports']['status'] == 'NOT_MEASURED_BY_FIXTURE_CONSTRUCTION'
    measured = phases[3]
    for key in ('public_imports', 'exact_public_retries', 'immutable_edition_replacement_refusals'):
        assert measured[key]['count'] == 3 and measured[key]['p50_seconds'] >= 0
    recovery = measured['checkpoint_recovery']
    assert recovery['actual_owned_import_worker'] and recovery['observed_checkpoints'] >= 2
    assert recovery['checkpoint_prefix_preserved'] and recovery['external_original_removed']
    assert recovery['immutable_publication_receipt_unchanged'] and recovery['native_reservations_released']
    assert phases[4]['admission']['status'] == 'ADMITTED'
    assert phases[4]['all_manifest_files_verified'] and phases[4]['old_absolute_root_absent']
    assert phases[4]['retired_baseline_preserved'] and phases[4]['restored_root_differs']
    assert not (output / '2/library').exists()
    assert (output / '2/retained-baseline-library/library.sqlite').is_file()
    assert (output / '2/restored-library/library.sqlite').is_file()
    assert set(report['origins']) == set(benchmark.MODULES + benchmark.CAPACITY_MODULES)
    assert all(row['origins'] == report['origins'] for row in phases)
    assert report['claims']['sustained_declared_corpus_import_throughput'] == (
        'NOT_MEASURED_BY_CAPACITY_SETUP_OR_BOUNDED_SAMPLES')
    assert not list(output.rglob('*.pyc'))


def test_public_sample_rejectable_commit_failure_keeps_prior_corpus_and_no_false_receipt(tmp_path, monkeypatch):
    root = tmp_path / 'rollback'
    settings, built, metadata = setup(root)
    actual = application._CatalogPublisher.publish_revision
    seen = []
    def publish(publisher, workspace, edition, revision, asset, config, pages):
        before = publisher.before_commit
        def fail(scope, identity):
            before(scope, identity)
            if edition == 'capacity-sample-0000':
                seen.append(identity)
                raise RuntimeError('Reject the actual sampled publication before commit.')
        publisher.before_commit = fail
        return actual(publisher, workspace, edition, revision, asset, config, pages)
    monkeypatch.setattr(application._CatalogPublisher, 'publish_revision', publish)
    with pytest.raises(RuntimeError, match='sampled publication'):
        benchmark.measure_public_operations(root, settings, 3)
    assert len(seen) == 1
    app = LibraryApp(root / 'library')
    with app.read() as db:
        assert not db.execute('SELECT 1 FROM revision WHERE id=?', (seen[0],)).fetchone()
        assert not db.execute('SELECT 1 FROM catalog_edition WHERE id=?', ('capacity-sample-0000',)).fetchone()
    verified = benchmark.verify_corpus(app, metadata, settings, benchmark.Budget(root, settings))
    assert verified['corpus_sha256'] == built['corpus_sha256']
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
        assert db.execute('SELECT state FROM job WHERE id=?', (seen[0],)).fetchone()[0] == 'failed'
        assert db.execute('SELECT count(*) FROM checkpoint WHERE job=?', (seen[0],)).fetchone()[0] == 2
    assert not (root / 'operations.json').exists()


def test_public_samples_do_not_accept_a_forged_returned_source_receipt(tmp_path, monkeypatch):
    root = tmp_path / 'forged-return'
    settings, _, _ = setup(root)
    actual = LibraryApp.import_file
    completed = []
    def altered(app, workspace, source, **kwargs):
        result = actual(app, workspace, source, **kwargs)
        if kwargs.get('edition_id') == 'capacity-sample-0000':
            completed.append(result['id'])
            return dict(result, asset='0' * 64)
        return result
    monkeypatch.setattr(LibraryApp, 'import_file', altered)
    with pytest.raises(benchmark.BenchmarkFailure, match='sampled public import'):
        benchmark.measure_public_operations(root, settings, 3)
    assert len(completed) == 1
    app = LibraryApp(root / 'library')
    with app.read() as db:
        assert db.execute('SELECT count(*) FROM sealed WHERE revision=?', (completed[0],)).fetchone()[0] == 1
    assert not (root / 'operations.json').exists()


def test_retained_corruption_refuses_public_samples_before_new_workspace_or_input(tmp_path):
    root = tmp_path / 'changed-original'
    settings, _, metadata = setup(root)
    app = LibraryApp(root / 'library')
    source = app.assets.resolve(app.browse(metadata['workspace'])[0].asset)
    original = source.read_bytes()
    source.write_bytes(original.replace(b'CARRCorpusNeedle', b'XARRCorpusNeedle', 1))
    with pytest.raises(ValueError):
        benchmark.measure_public_operations(root, settings, 3)
    assert not (root / 'public-operation-inputs').exists()
    assert len(LibraryApp(root / 'library').workspaces()) == 2
    assert source.read_bytes() != original


@pytest.mark.parametrize('options', [
    {'construction': 'unknown', 'public_samples': 3, 'complete_backup': False},
    {'construction': 'retained-fixture', 'public_samples': True, 'complete_backup': False},
    {'construction': 'retained-fixture', 'public_samples': 2, 'complete_backup': False},
    {'construction': 'retained-fixture', 'public_samples': 21, 'complete_backup': False},
    {'construction': 'retained-fixture', 'public_samples': 3, 'complete_backup': 'yes'},
    {'construction': 'retained-fixture', 'public_samples': 3},
    {'construction': 'retained-fixture', 'public_samples': 3, 'complete_backup': False, 'skip_audit': True},
])
def test_invalid_capacity_options_refuse_before_any_output(tmp_path, options):
    settings = case()
    settings.pop('books')
    output = tmp_path / 'must-not-exist'
    with pytest.raises(benchmark.BenchmarkFailure):
        benchmark.run_benchmark(output, [3], settings, capacity=options)
    assert not output.exists()


def test_declared_backup_disk_refusal_is_not_a_success_or_destructive_restore(tmp_path):
    root = tmp_path / 'disk-limited'
    settings, _, _ = setup(root)
    current = benchmark.footprint(root)['bytes']
    settings['max_disk_bytes'] = current + 4096
    metadata = benchmark.read_json(root / 'corpus.json')
    metadata['spec'] = settings
    # This test's explicit smaller budget is persisted before invoking the phase.
    (root / 'corpus.json').unlink()
    benchmark.save_json(root / 'corpus.json', metadata)
    original = (root / 'library/library.sqlite').read_bytes()
    progress = {}
    with pytest.raises(benchmark.BudgetExceeded):
        benchmark.measure_complete_backup(root, settings, progress=progress)
    assert progress['backup_admission']['status'] == 'NOT_ADMITTED'
    assert (root / 'library/library.sqlite').read_bytes() == original
    assert not (root / 'complete-backup').exists()
    assert not (root / 'restored-library').exists()
    assert not (root / 'retained-baseline-library').exists()


def test_fixture_cli_without_public_samples_keeps_three_phases_and_truthful_empty_measurement(tmp_path):
    output = tmp_path / 'capacity-read-only'
    completed = actual_cli(output, '--public-samples', '0')
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    report = benchmark.read_json(output / 'library-corpus-scale.json')
    assert [row['phase'] for row in report['cases'][0]['phases']] == ['build', 'measure', 'restart']
    assert report['capacity']['public_samples'] == 0
    assert report['cases'][0]['phases'][0]['imports']['p50_seconds'] is None
    assert not (output / '2/public-operation-inputs').exists()


def test_two_million_profile_is_explicit_geometry_not_an_executed_capacity_claim():
    settings = benchmark.validate_case(case(books=50000, segments_per_book=40, segment_bytes=256))
    assert settings['books'] * settings['segments_per_book'] == 2_000_000
    with pytest.raises(benchmark.BenchmarkFailure, match='two million'):
        benchmark.validate_case(case(books=50000, segments_per_book=41, segment_bytes=256))


def test_fixture_entry_cancellation_creates_no_library_or_ready_receipt(tmp_path):
    with pytest.raises(InterruptedError):
        benchmark.build_retained_fixture(tmp_path, case(), cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('profile,complete', [
    (None, True), (True, True), ('auto', True), ('large-source-v1', False),
])
def test_backup_profile_requires_an_explicit_supported_name_and_backup_phase(tmp_path, profile, complete):
    settings = case()
    settings.pop('books')
    output = tmp_path / 'must-not-exist'
    options = dict(construction='retained-fixture', public_samples=0,
                   complete_backup=complete, backup_capacity_profile=profile)
    with pytest.raises(benchmark.BenchmarkFailure, match='backup capacity profile'):
        benchmark.run_benchmark(output, [3], settings, capacity=options)
    assert not output.exists()


def test_explicit_backup_profile_reaches_actual_isolated_phase_and_report(tmp_path):
    from carr_library.backup_capacity import LARGE_SOURCE_PROFILE, capacity
    output = tmp_path / 'explicit-profile'
    completed = actual_cli(output, '--public-samples', '0', '--complete-backup',
                           '--backup-capacity-profile', LARGE_SOURCE_PROFILE)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    report = benchmark.read_json(output / 'library-corpus-scale.json')
    assert report['status'] == 'PASS' and report['schema'] == benchmark.SCHEMA
    assert report['capacity'] == dict(construction='retained-fixture', public_samples=0,
        complete_backup=True, backup_capacity_profile=LARGE_SOURCE_PROFILE)
    phases = report['cases'][0]['phases']
    assert [row['phase'] for row in phases] == ['build', 'measure', 'restart', 'backup']
    assert len({row['pid'] for row in phases}) == 4
    assert all(row['status'] == 'PASS' and row['process_reaped'] and row['exit_code'] == 0
               and row['books'] == 2 and row['segments'] == 4 for row in phases)
    assert len({row['corpus_sha256'] for row in phases}) == 1
    actual = phases[-1]
    assert actual['admission']['status'] == 'ADMITTED'
    assert actual['admission']['capacity_profile'] == LARGE_SOURCE_PROFILE
    assert actual['admission']['allowances'] == capacity(LARGE_SOURCE_PROFILE)
    assert actual['all_manifest_files_verified'] and actual['old_absolute_root_absent']
    assert actual['retired_baseline_preserved'] and actual['restored_root_differs']
    assert 'carr_library.backup_capacity' in report['origins']
    assert set(report['origins']) == set(benchmark.MODULES + benchmark.CAPACITY_MODULES)
    assert all(row['origins'] == report['origins'] for row in phases)
    manifest = benchmark.read_json(output / '2/complete-backup/manifest.json')
    assert manifest['version'] == 4 and manifest['pending']['version'] == 1
    assert 'capacity_profile' not in manifest and 'capacity_profile' not in manifest['pending']
    assert not (output / '2/library').exists()
    assert (output / '2/restored-library/library.sqlite').is_file()
    assert not list(output.rglob('*.pyc'))


def test_selected_profile_is_forwarded_to_each_real_backup_api_without_wire_authority(tmp_path, monkeypatch):
    from carr_library import library_backup
    from carr_library.backup_capacity import LARGE_SOURCE_PROFILE, capacity
    root = tmp_path / 'forwarded-profile'
    settings, built, _ = setup(root)
    actual_create = library_backup.create_backup
    actual_verify = library_backup.verify_backup
    actual_restore = library_backup.restore_backup
    calls = []
    def recording(name, actual):
        def checked(*args, **kwargs):
            assert kwargs.get('capacity_profile') == LARGE_SOURCE_PROFILE
            calls.append(name)
            return actual(*args, **kwargs)
        return checked
    monkeypatch.setattr(library_backup, 'create_backup', recording('create', actual_create))
    monkeypatch.setattr(library_backup, 'verify_backup', recording('verify', actual_verify))
    monkeypatch.setattr(library_backup, 'restore_backup', recording('restore', actual_restore))
    result = benchmark.measure_complete_backup(root, settings, capacity_profile=LARGE_SOURCE_PROFILE)
    assert calls == ['create', 'verify', 'restore']
    assert result['status'] == 'PASS' and result['corpus_sha256'] == built['corpus_sha256']
    assert result['admission']['allowances'] == capacity(LARGE_SOURCE_PROFILE)
    assert result['source_root_retired'] and result['restored_root_differs']
    # A small backup made with a larger caller allowance has unchanged bytes
    # and remains verifiable under the default admission; no profile is trusted
    # from its manifest or needed to interpret its format.
    manifest = actual_verify(root / 'complete-backup', result['manifest_sha256'])
    assert manifest['version'] == 4 and manifest['pending']['version'] == 1
    assert 'capacity_profile' not in manifest and 'capacity_profile' not in manifest['pending']
    assert not (root / 'library').exists()
    assert (root / 'retained-baseline-library/library.sqlite').is_file()
    assert (root / 'restored-library/library.sqlite').is_file()
