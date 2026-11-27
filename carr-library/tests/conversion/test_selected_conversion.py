"""Actual byte/child/lease/publication contracts; no native format qualification."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.conversion import (ConversionBudget, ConversionError,
    ConversionService, ConversionUnavailable)
from carr_library.conversion import selected
from carr_library.operations import ResourceBroker

TEXT = 'Читальна копія.\nExact synthetic contract text.'
RAW = br'{\rtf1\ansi Visible original.}'


@pytest.fixture
def selected_case(tmp_path, monkeypatch):
    library, inputs, outputs = (tmp_path / name for name in ('library', 'inputs', 'outputs'))
    for path in (library / 'state', inputs, outputs):
        path.mkdir(parents=True)
    source = inputs / 'Оригінал.rtf'
    source.write_bytes(RAW)
    runtime = tmp_path / 'selected-pandoc'
    runtime.write_bytes(b'Synthetic selection only; the actual child replaces just selected_records.')
    current = {'alive': True}
    def guard():
        if not current['alive']:
            raise ValueError('The current conversion owner changed.')
    service = ConversionService(outputs, input_roots=(source,), pandoc=runtime,
        state_root=library / 'state', guard=guard, budget=ConversionBudget(timeout=30))
    command = selected._worker_command
    commands = []
    def synthetic_request(request, digest):
        args, environment = command(request, digest)
        args = list(args)
        position = args.index('-c') + 1
        seam = 'sys.path[:0]=sys.argv[3:]; '
        assert args[position].count(seam) == 1
        record = {'raw_text': TEXT, 'physical_index': None, 'status': 'review_required',
                  'logical_locator': {'synthetic_transport_fixture': True}}
        patch = ("import carr_library.conversion.selected_adapters as adapter; "
                 "adapter.selected_records=lambda *a: " + repr(
                     ('synthetic-owned-worker', '\n\n', [record],
                      ['synthetic_fixture_does_not_qualify_native_formats'])) + "; ")
        args[position] = args[position].replace(seam, seam + patch)
        commands.append(tuple(args))
        return args, environment
    monkeypatch.setattr(selected, '_worker_command', synthetic_request)
    return SimpleNamespace(library=library, inputs=inputs, outputs=outputs, source=source,
        runtime=runtime, current=current, service=service, commands=commands)


def clean(case):
    assert case.source.read_bytes() == RAW
    assert not list(case.outputs.glob('.pending-conversion-*'))
    assert ResourceBroker.for_app(case.library).snapshot()['leases'] == []


def test_actual_owned_worker_publishes_one_exact_triplet_and_read_is_passive(selected_case, monkeypatch):
    case = selected_case
    result = case.service.extract(case.source, 'reading.txt')
    assert len(case.commands) == 1
    assert result.path == case.outputs / 'reading-files' / 'reading.txt'
    assert result.original_path.read_bytes() == RAW
    assert set(path.name for path in result.path.parent.iterdir()) == {
        'reading.txt', 'original.rtf', 'provenance.json'}
    assert result.path.read_bytes() == TEXT.encode('utf-8')
    assert result.report['source_sha256'] == sha256(RAW).hexdigest()
    assert result.report['retained_original_sha256'] == sha256(RAW).hexdigest()
    assert result.report['native_catalog_source'] is False
    assert result.report['verified_for_quotation'] is False
    assert result.report['sections'][0]['physical_index'] is None
    assert result.report['sections'][0]['printed_label'] is None
    assert result.receipt_sha256 == sha256(result.report_path.read_bytes()).hexdigest()
    case.source.unlink()  # The complete external folder retains the exact source.
    case.runtime.unlink()
    monkeypatch.setattr(selected, 'run_process', lambda *a, **k: pytest.fail('Reading launched a converter.'))
    assert selected.read_result(result) == TEXT
    assert ResourceBroker.for_app(case.library).snapshot()['leases'] == []


@pytest.mark.parametrize('kind', ['original', 'reading', 'report', 'extra-file', 'replace-folder'])
def test_read_refuses_changed_complete_folder_before_use(selected_case, kind):
    case = selected_case
    result = case.service.extract(case.source, 'reading.txt')
    if kind == 'original':
        result.original_path.write_bytes(b'changed original')
    elif kind == 'reading':
        result.path.write_bytes(b'changed reading')
    elif kind == 'report':
        result.report_path.write_bytes(result.report_path.read_bytes() + b' ')
    elif kind == 'extra-file':
        (result.path.parent / 'unreviewed.txt').write_bytes(b'extra')
    else:
        retired = result.path.parent.with_name('retired')
        result.path.parent.rename(retired)
        result.path.parent.mkdir()
        for path in retired.iterdir():
            (result.path.parent / path.name).write_bytes(path.read_bytes())
    with pytest.raises((ValueError, OSError)):
        selected.read_result(result)
    clean(case)


def test_initial_cancel_does_not_create_broker_or_admit_worker(selected_case):
    case = selected_case
    with pytest.raises(InterruptedError):
        case.service.extract(case.source, 'reading.txt', cancelled=lambda: True)
    assert case.commands == []
    assert list(case.outputs.iterdir()) == []
    assert list((case.library / 'state').iterdir()) == []


@pytest.mark.parametrize('kind', ['source', 'selected-executable', 'library', 'input-directory', 'output-directory'])
def test_queued_source_runtime_and_directory_identity_refuse_before_child(selected_case, kind):
    case = selected_case
    if kind == 'source':
        case.source.write_bytes(RAW + b'changed')
    elif kind == 'selected-executable':
        case.runtime.write_bytes(b'changed selected executable')
    else:
        path = {'library': case.library, 'input-directory': case.inputs,
                'output-directory': case.outputs}[kind]
        retired = path.with_name(path.name + '-retired')
        path.rename(retired)
        path.mkdir()
        if kind == 'library':
            (path / 'state').mkdir()
        elif kind == 'input-directory':
            case.source.write_bytes(RAW)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        case.service.extract(case.source, 'reading.txt')
    assert case.commands == []
    assert not (case.outputs / 'reading-files').exists()
    assert not list(case.outputs.glob('.pending-conversion-*'))


def test_source_mutation_before_final_publication_is_refused(selected_case, monkeypatch):
    case = selected_case
    actual = selected.sync_directory
    attempted = []
    def sync(path):
        result = actual(path)
        if Path(path).name == 'result':
            attempted.append(True)
            # Windows' retained source handle itself refuses this write. POSIX
            # allows it, then the exact prepublication source hash refuses it.
            case.source.write_bytes(RAW + b' changed after native output')
        return result
    monkeypatch.setattr(selected, 'sync_directory', sync)
    with pytest.raises((ValueError, OSError)):
        case.service.extract(case.source, 'reading.txt')
    assert attempted == [True]
    assert not (case.outputs / 'reading-files').exists()
    assert not list(case.outputs.glob('.pending-conversion-*'))
    assert ResourceBroker.for_app(case.library).snapshot()['leases'] == []


def test_late_cancel_before_atomic_rename_leaves_no_partial_triplet(selected_case, monkeypatch):
    case = selected_case
    cancel = threading.Event()
    actual = selected.sync_directory
    def sync(path):
        result = actual(path)
        if Path(path).name == 'result':
            cancel.set()
        return result
    monkeypatch.setattr(selected, 'sync_directory', sync)
    with pytest.raises(InterruptedError):
        case.service.extract(case.source, 'reading.txt', cancelled=cancel.is_set)
    assert len(case.commands) == 1 and list(case.outputs.iterdir()) == []
    clean(case)


def test_competing_folder_is_never_adopted_removed_or_overwritten(selected_case, monkeypatch):
    case = selected_case
    publish = selected.publish_directory_no_replace
    competitor = b'Other owner exact bytes.'
    def compete(stage, destination):
        destination.mkdir()
        (destination / 'owner.txt').write_bytes(competitor)
        return publish(stage, destination)
    monkeypatch.setattr(selected, 'publish_directory_no_replace', compete)
    with pytest.raises(FileExistsError):
        case.service.extract(case.source, 'reading.txt')
    assert (case.outputs / 'reading-files' / 'owner.txt').read_bytes() == competitor
    assert set(path.name for path in (case.outputs / 'reading-files').iterdir()) == {'owner.txt'}
    clean(case)


def test_visible_triplet_survives_directory_durability_warning(selected_case, monkeypatch):
    case = selected_case
    actual = selected.sync_directory
    def sync(path):
        if Path(path) == case.outputs:
            raise OSError('Synthetic directory durability failure after visible rename.')
        return actual(path)
    monkeypatch.setattr(selected, 'sync_directory', sync)
    result = case.service.extract(case.source, 'reading.txt')
    assert result.notices and 'saved' in result.notices[0]
    assert selected.read_result(result) == TEXT
    with pytest.raises(FileExistsError):
        case.service.extract(case.source, 'reading.txt')
    clean(case)


def test_actual_committed_result_survives_late_reservation_release_error(selected_case, monkeypatch):
    from carr_library.operations.broker import ResourceLease
    case = selected_case
    release = ResourceLease.release
    def release_then_fail(self):
        release(self)
        raise ValueError('Synthetic observer failure after actual native lock release.')
    monkeypatch.setattr(ResourceLease, 'release', release_then_fail)
    with pytest.raises(ValueError) as caught:
        case.service.extract(case.source, 'reading.txt')
    result = caught.value.completed_conversion
    assert selected.read_result(result) == TEXT
    assert result.original_path.read_bytes() == RAW
    clean(case)


def test_staging_cleanup_attention_keeps_actual_committed_result(selected_case, monkeypatch):
    case = selected_case
    factory = selected.TemporaryDirectory
    attempts = []
    class Temporary:
        def __init__(self, **options):
            self.owner = factory(**options)
            self.name = self.owner.name
        def cleanup(self):
            attempts.append(self.name)
            if len(attempts) == 1:
                raise PermissionError('Synthetic first cleanup refusal.')
            self.owner.cleanup()
    monkeypatch.setattr(selected, 'TemporaryDirectory', Temporary)
    from carr_library.audio.models import AudioCleanupPending
    with pytest.raises(AudioCleanupPending) as caught:
        case.service.extract(case.source, 'reading.txt')
    assert caught.value.cleanup_owner.quiescent
    assert len(attempts) >= 2 and not Path(attempts[0]).exists()
    assert selected.read_result(caught.value.completed_conversion) == TEXT
    clean(case)


def test_pending_actual_child_keeps_core_stage_and_broker_until_quiescence(selected_case, monkeypatch):
    from carr_library.audio.cleanup import pending_cleanup
    from carr_library.audio.models import AudioCleanupPending
    case = selected_case
    entered, release = threading.Event(), threading.Event()
    retained, failures, held = {}, [], set()
    actual_reader = selected.verified_reader
    @contextmanager
    def observed_reader(path, **options):
        with actual_reader(path, **options) as stream:
            held.add(Path(path))
            try:
                yield stream
            finally:
                held.remove(Path(path))
    monkeypatch.setattr(selected, 'verified_reader', observed_reader)
    def owned_child(command, **options):
        environment = os.environ.copy()
        environment.pop('__PYVENV_LAUNCHER__', None)
        environment.pop('PYTHONEXECUTABLE', None)
        executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True))
        child = subprocess.Popen([executable, '-I', '-B', '-c', 'import time; time.sleep(45)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, env=environment)
        retained.update(child=child, stage=Path(options['cwd']))
        def close():
            assert case.runtime in held and case.source in held
            if not release.is_set():
                return False
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            return True
        error = pending_cleanup(close, message='Synthetic actual-child converter cleanup.')
        retained['owner'] = error.cleanup_owner
        entered.set()
        raise error
    monkeypatch.setattr(selected, 'run_process', owned_child)
    def work():
        try:
            case.service.extract(case.source, 'reading.txt')
        except BaseException as error:
            failures.append(error)
    worker = threading.Thread(target=work)
    worker.start()
    stopped_without_fallback = False
    try:
        assert entered.wait(10)
        assert retained['child'].poll() is None and worker.is_alive()
        assert not retained['owner'].quiescent and retained['stage'].is_dir()
        assert case.runtime in held and case.source in held
        if os.name == 'nt':
            with pytest.raises(OSError):
                case.runtime.write_bytes(b'A live native core must not be replaced.')
        leases = ResourceBroker.for_app(case.library).snapshot()['leases']
        assert len(leases) == 1 and leases[0]['operation_id'].startswith('document-conversion:')
        release.set()
        worker.join(10)
        stopped_without_fallback = not worker.is_alive() and retained['child'].poll() is not None
        assert stopped_without_fallback
        assert retained['owner'].quiescent
        assert len(failures) == 1 and isinstance(failures[0], AudioCleanupPending)
        assert not retained['stage'].exists() and held == set()
        assert list(case.outputs.iterdir()) == []
        clean(case)
    finally:
        release.set()
        if 'child' in retained and retained['child'].poll() is None:
            retained['child'].kill()
            retained['child'].wait(timeout=5)
        worker.join(10)
    assert stopped_without_fallback


@pytest.mark.parametrize('kind', ['calibre', 'office', 'pandoc'])
def test_optional_selection_never_uses_path_discovery_or_writes_private_library(tmp_path, monkeypatch, kind):
    library = tmp_path / 'library'
    (library / 'state').mkdir(parents=True)
    runtime = tmp_path / 'tool'
    runtime.write_bytes(b'not executed')
    from carr_library.conversion import service
    monkeypatch.setattr(service.shutil, 'which', lambda *a, **k: pytest.fail('Implicit runtime discovery.'))
    with pytest.raises(PermissionError):
        ConversionService(library / 'private-output', state_root=library / 'state', **{kind: runtime})
    assert not (library / 'private-output').exists()


def test_cancel_after_real_atomic_commit_keeps_returned_receipt(selected_case, monkeypatch):
    case = selected_case
    cancel = threading.Event()
    actual = selected.publish_directory_no_replace
    def commit(stage, destination):
        result = actual(stage, destination)
        cancel.set()
        return result
    monkeypatch.setattr(selected, 'publish_directory_no_replace', commit)
    result = case.service.extract(case.source, 'reading.txt', cancelled=cancel.is_set)
    assert cancel.is_set()
    assert selected.read_result(result) == TEXT
    clean(case)


def test_actual_import_retains_only_reading_text_and_survives_external_folder_removal(selected_case):
    import shutil
    from carr_library.application import LibraryApp
    case = selected_case
    result = case.service.extract(case.source, 'reading.txt')
    app = LibraryApp(case.library)
    workspace = app.create_workspace('Reading copies')
    rows = selected.import_result(result, app, workspace)
    assert len(rows) == 1 and rows[0]['state'] == 'complete'
    retained = [path.read_bytes() for path in app.assets_dir.rglob('*') if path.is_file()]
    assert TEXT.encode('utf-8') in retained and RAW not in retained
    assert result.original_path.read_bytes() == RAW
    assert not result.report_path.is_relative_to(app.root)
    shutil.rmtree(result.path.parent)
    case.source.unlink()
    hits = app.search(workspace, 'Exact synthetic contract text.')
    assert len(hits) == 1 and hits[0].quote == 'Exact synthetic contract text.'


def test_reading_folder_swap_at_actual_import_boundary_refuses_before_ingest(selected_case, monkeypatch):
    from carr_library.application import LibraryApp
    case = selected_case
    result = case.service.extract(case.source, 'reading.txt')
    app = LibraryApp(case.library)
    workspace = app.create_workspace('Reading copies')
    original = app.import_files
    entered = []
    def replace_folder(workspace, paths, **options):
        entered.append(True)
        retired = result.path.parent.with_name('retired-folder')
        # POSIX can rename a directory containing held readers; Windows may
        # refuse it directly. Neither outcome may admit different source bytes.
        result.path.parent.rename(retired)
        result.path.parent.mkdir()
        for path in retired.iterdir():
            (result.path.parent / path.name).write_bytes(path.read_bytes())
        return original(workspace, paths, **options)
    monkeypatch.setattr(app, 'import_files', replace_folder)
    try:
        rows = selected.import_result(result, app, workspace)
    except (ValueError, OSError):
        rows = []
    assert entered == [True]
    assert not any(row.get('state') == 'complete' for row in rows)
    assert app.browse(workspace) == ()
    assert not any(path.is_file() for path in app.assets_dir.rglob('*'))


def test_import_returned_receipt_survives_late_reader_cleanup_error(selected_case, monkeypatch):
    from carr_library.application import LibraryApp
    case = selected_case
    result = case.service.extract(case.source, 'reading.txt')
    app = LibraryApp(case.library)
    workspace = app.create_workspace('Reading copies')
    actual_reader = selected.verified_reader
    @contextmanager
    def late_error(path, **options):
        with actual_reader(path, **options) as stream:
            yield stream
        if Path(path) == result.report_path:
            raise OSError('Synthetic retained reader cleanup error after returned import.')
    monkeypatch.setattr(selected, 'verified_reader', late_error)
    with pytest.raises(OSError) as caught:
        selected.import_result(result, app, workspace)
    assert caught.value.completed_import[0]['state'] == 'complete'
    assert len(app.browse(workspace)) == 1
    assert result.original_path.read_bytes() == RAW
