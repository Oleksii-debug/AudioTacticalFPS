"""Actual library, CSL, broker and child protocol checks with synthetic Word.

The real child and cleanup paths execute. SyntheticNative is not Microsoft Word
and these tests cannot qualify Office compatibility or document appearance.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.citations.bibliography_workbench import BibliographyWorkbench, StylePreview
from carr_library.academic_integration import word_bridge as bridge
from carr_library.academic_integration import word_worker
from carr_library.operations import ResourceBroker
from carr_library.durable_state import atomic_json, read_json_snapshot
from carr_library.audio.cleanup import pending_cleanup
from carr_library.audio.models import AudioCleanupPending

_spec = importlib.util.spec_from_file_location('word_bridge_fixtures', Path(__file__).with_name('word_fixtures.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    library = LibraryApp(root)
    workspace = library.create_workspace('Current references')
    controller = AcademicController(library, workspace)
    row = controller.create(dict(title='Source methods', authors=('Smith, Jane',), year='2020',
                                 publisher='Example Press', notes='NEVER_SEND_PERSONAL_NOTE'))
    workbench = BibliographyWorkbench(controller)
    snapshot = controller.export(row.id, format='csl-json', include_notes=False)
    preview = StylePreview(workspace, row.id, sha256(snapshot.encode('utf-8')).hexdigest(),
                           'apa', 'en-US', 'family-given', ('Source methods',), None)
    monkeypatch.setattr(bridge, 'IS_WINDOWS', True)  # Protocol fixture; never an Office availability assertion.
    return library, controller, row, workbench, preview


def intent(row):
    return {'action': 'insert_citation', 'citation_id': None, 'items': [
        dict(reference_id=row.id, locator=None, label='page', locator_verified=False,
             prefix='', suffix='', suppress_author=False)]}


def transport(monkeypatch, tmp_path, *, mode='complete', on_poll=None, after_run=None):
    observation = tmp_path / 'observed'
    observation.mkdir()
    stages, children = [], []
    real_run = bridge.run_process
    if os.name == 'nt':
        from carr_library.audio import windows_job
        actual = windows_job.JobProcess
        class Tracked(actual):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                children.append(self)
        monkeypatch.setattr(windows_job, 'JobProcess', Tracked)
    else:
        actual = subprocess.Popen
        def tracked(*args, **kwargs):
            result = actual(*args, **kwargs)
            if args and isinstance(args[0], (tuple, list)) and str(Path(fixture.__file__)) in args[0]:
                children.append(result)
            return result
        monkeypatch.setattr(subprocess, 'Popen', tracked)

    def run(argv, **options):
        assert argv[1:3] == ['-I', '-B'] and Path(argv[3]).resolve() == Path(word_worker.__file__).resolve()
        assert len(argv) == 5 and options['owned_windows_job'] is (os.name == 'nt')
        assert options['env']['PYTHONDONTWRITEBYTECODE'] == '1'
        stages.append(Path(options['cwd']))
        poll = options['cancelled']
        def observed_poll():
            if on_poll is not None:
                on_poll(stages[-1], observation)
            return poll()
        options['cancelled'] = observed_poll
        import carr_library
        root = str(Path(carr_library.__file__).resolve().parent.parent)
        actual_argv = [argv[0], '-I', '-B', str(Path(fixture.__file__)), root,
                       argv[3], argv[4], mode, str(observation)]
        result = real_run(actual_argv, **options)
        if after_run is not None:
            after_run(stages[-1], observation)
        return result

    monkeypatch.setattr(bridge, 'run_process', run)
    return observation, stages, children


def assert_quiescent(source, stages, children):
    assert stages and all(not stage.exists() for stage in stages)
    assert children and all(child.poll() is not None for child in children)
    if os.name == 'nt':
        assert all(child._job is None for child in children)
    assert ResourceBroker.for_app(source[0].root).snapshot()['leases'] == []


def test_real_child_inspection_does_not_offer_approval_or_write(source, monkeypatch, tmp_path):
    observed, stages, children = transport(monkeypatch, tmp_path)
    service = bridge.WordBridge(source[3], source[4])
    result = service.run(timeout=30)
    assert result.status == 'INSPECTED' and result.capture['metadata'] is None
    assert not result.may_have_changed_word and not (observed / 'mutation.json').exists()
    assert_quiescent(source, stages, children)


@pytest.mark.parametrize('mode', ['complete', 'close-error'])
def test_real_child_receipt_and_csl_win_over_late_close_error(source, monkeypatch, tmp_path, mode):
    observed, stages, children = transport(monkeypatch, tmp_path, mode=mode)
    decision, views = bridge.WordDecision(), []
    def approve(view):
        views.append(view)
        assert 'NEVER_SEND_PERSONAL_NOTE' not in view.explanation + view.references_json
        decision.approve(view.token)
    service = bridge.WordBridge(source[3], source[4])
    result = service.run(intent(source[2]), decision=decision, on_preview=approve, timeout=45)
    assert result.status == 'COMPLETED' and result.receipt['saved_by_bridge'] is False
    assert len(views) == 1 and '(Smith, 2020)' in views[0].explanation
    assert result.receipt['proposal_sha256'] == views[0].token
    actual, _ = read_json_snapshot(observed / 'mutation.json')
    assert actual['count'] == 1 and actual['actual_word'] is False
    assert actual['proposal']['interchange']['processor']['name'] == 'citeproc-js'
    assert service.last_outcome == result
    if mode == 'close-error':
        assert result.warnings
    assert_quiescent(source, stages, children)


def test_no_approval_stops_before_any_native_mutation(source, monkeypatch, tmp_path):
    observed, stages, children = transport(monkeypatch, tmp_path)
    decision = bridge.WordDecision()
    with pytest.raises(InterruptedError):
        bridge.WordBridge(source[3], source[4]).run(intent(source[2]), decision=decision,
            on_preview=lambda view: decision.close(), timeout=45)
    assert not (observed / 'mutation.json').exists()
    assert_quiescent(source, stages, children)


def test_changed_source_after_preview_is_rejected_before_grant(source, monkeypatch, tmp_path):
    observed, stages, children = transport(monkeypatch, tmp_path)
    decision = bridge.WordDecision()
    def change_and_approve(view):
        source[1].update(source[2], {'title': 'Changed after review'})
        decision.approve(view.token)
    with pytest.raises(ValueError, match='changed'):
        bridge.WordBridge(source[3], source[4]).run(intent(source[2]), decision=decision,
            on_preview=change_and_approve, timeout=45)
    assert not (observed / 'mutation.json').exists()
    assert_quiescent(source, stages, children)


def test_word_changed_during_preview_cannot_use_earlier_document_approval(source, monkeypatch, tmp_path):
    observed, stages, children = transport(monkeypatch, tmp_path, mode='changed-word')
    decision = bridge.WordDecision()
    with pytest.raises(RuntimeError):
        bridge.WordBridge(source[3], source[4]).run(intent(source[2]), decision=decision,
            on_preview=lambda view: decision.approve(view.token), timeout=45)
    assert not (observed / 'mutation.json').exists()
    assert_quiescent(source, stages, children)


@pytest.mark.parametrize('mode', ['after-mutation-hang', 'bad-receipt'])
def test_interruption_after_grant_is_uncertain_never_retryable_success(source, monkeypatch, tmp_path, mode):
    stop = threading.Event()
    def observed(stage, root):
        if mode == 'after-mutation-hang' and (root / 'mutation.json').exists():
            stop.set()
    observed_root, stages, children = transport(monkeypatch, tmp_path, mode=mode, on_poll=observed)
    decision = bridge.WordDecision()
    result = bridge.WordBridge(source[3], source[4]).run(intent(source[2]), decision=decision,
        on_preview=lambda view: decision.approve(view.token), cancelled=stop.is_set, timeout=45)
    assert result.status == 'UNCERTAIN' and result.receipt is None
    assert 'do not retry' in result.message
    assert (observed_root / 'mutation.json').exists()
    assert_quiescent(source, stages, children)


@pytest.mark.parametrize('late', ['source', 'cancel'])
def test_source_change_or_cancellation_after_completed_receipt_cannot_relabel_success(source, monkeypatch, tmp_path, late):
    changed, stopped = [], threading.Event()
    def after_receipt(stage, root):
        assert (stage / 'receipt.json').is_file()
        if late == 'source':
            changed.append(source[1].update(source[2], {'title': 'Changed after native completion'}))
        else:
            stopped.set()
        changed.append(True)
    observed, stages, children = transport(monkeypatch, tmp_path, after_run=after_receipt)
    decision = bridge.WordDecision()
    service = bridge.WordBridge(source[3], source[4])
    result = service.run(intent(source[2]), decision=decision,
        on_preview=lambda view: decision.approve(view.token), cancelled=stopped.is_set, timeout=45)
    assert changed and result.status == 'COMPLETED' and (observed / 'mutation.json').is_file()
    assert_quiescent(source, stages, children)


def test_preview_observer_failure_prevents_approval_and_preserves_native_document(source, monkeypatch, tmp_path):
    observed, stages, children = transport(monkeypatch, tmp_path)
    decision = bridge.WordDecision()
    def broken(view):
        raise RuntimeError('Synthetic GUI delivery failure.')
    with pytest.raises(RuntimeError, match='delivery'):
        bridge.WordBridge(source[3], source[4]).run(intent(source[2]), decision=decision, on_preview=broken, timeout=45)
    assert decision.closed and not (observed / 'mutation.json').exists()
    assert_quiescent(source, stages, children)


@pytest.mark.parametrize('change', ['workspace', 'object', 'root'])
def test_stale_owner_is_refused_before_launch_or_admission(source, monkeypatch, tmp_path, change):
    library, controller, row, workbench, preview = source
    service = bridge.WordBridge(workbench, preview)
    monkeypatch.setattr(bridge, 'run_process', lambda *a, **k: pytest.fail('Stale scope launched a child.'))
    if change == 'workspace':
        library.archive_workspace(controller.workspace)
    elif change == 'object':
        controller.library = LibraryApp(tmp_path / 'different')
    else:
        library.root.rename(tmp_path / 'old-library')
        library.root.mkdir()
    monkeypatch.setattr(bridge.ResourceBroker, 'for_app',
        lambda *a, **k: pytest.fail('Stale scope attempted resource initialization.'))
    with pytest.raises((ValueError, OSError)):
        service.run(timeout=10)


def test_visible_grant_sync_failure_is_uncertain_even_if_begun_marker_is_missing(source, monkeypatch, tmp_path):
    observed, stages, children = transport(monkeypatch, tmp_path, mode='slow-first-write')
    real = bridge.atomic_json
    def write(path, value, **kwargs):
        result = real(path, value, **kwargs)
        if path.name == 'grant.json':
            raise OSError('Synthetic directory fsync failure after visible final grant.')
        return result
    monkeypatch.setattr(bridge, 'atomic_json', write)
    decision = bridge.WordDecision()
    result = bridge.WordBridge(source[3], source[4]).run(intent(source[2]), decision=decision,
        on_preview=lambda view: decision.approve(view.token), timeout=45)
    assert result.status == 'UNCERTAIN' and result.receipt is None
    assert not (observed / 'mutation.json').exists()
    assert_quiescent(source, stages, children)


def test_postcommit_lease_release_notice_preserves_verified_outcome(source, monkeypatch, tmp_path):
    observed, stages, children = transport(monkeypatch, tmp_path)
    from carr_library.operations import broker as broker_module
    real = broker_module.ResourceLease.release
    def release(lease):
        real(lease)
        raise OSError('Synthetic record sync notice after native lock release.')
    monkeypatch.setattr(broker_module.ResourceLease, 'release', release)
    decision = bridge.WordDecision()
    service = bridge.WordBridge(source[3], source[4])
    result = service.run(intent(source[2]), decision=decision,
        on_preview=lambda view: decision.approve(view.token), timeout=45)
    assert result.status == 'COMPLETED' and result.warnings
    assert service.last_outcome == result
    assert_quiescent(source, stages, children)


def test_pending_real_child_retains_staging_and_native_resource_lease_until_death(source, monkeypatch, tmp_path):
    retained, errors = {}, []
    entered, release = threading.Event(), threading.Event()
    def interrupted(*args, **kwargs):
        environment = os.environ.copy()
        environment.pop('__PYVENV_LAUNCHER__', None); environment.pop('PYTHONEXECUTABLE', None)
        executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True))
        child = subprocess.Popen([executable, '-I', '-B', '-c', 'import time; time.sleep(45)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, env=environment)
        retained.update(child=child, stage=Path(kwargs['cwd']))
        def settle():
            if not release.is_set():
                return False
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            return True
        pending = pending_cleanup(settle, message='Synthetic retained Word child.')
        retained['owner'] = pending.cleanup_owner
        entered.set()
        raise pending
    monkeypatch.setattr(bridge, 'run_process', interrupted)
    def work():
        try:
            bridge.WordBridge(source[3], source[4]).run(timeout=30)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=work, name='word-owned-cleanup', daemon=True)
    try:
        worker.start()
        assert entered.wait(15)
        assert retained['child'].poll() is None and worker.is_alive()
        assert (retained['stage'] / 'request.json').is_file()
        leases = ResourceBroker.for_app(source[0].root).snapshot()['leases']
        assert len(leases) == 1 and leases[0]['operation_id'].startswith('word:')
    finally:
        release.set(); worker.join(15)
        child = retained.get('child')
        died_before_fallback = child is not None and child.poll() is not None
        worker_ended_before_fallback = not worker.is_alive()
        if child is not None and child.poll() is None:
            child.kill(); child.wait(timeout=5); worker.join(5)
    assert died_before_fallback and worker_ended_before_fallback
    assert len(errors) == 1 and isinstance(errors[0], AudioCleanupPending)
    assert retained['owner'].quiescent and not retained['stage'].exists()
    assert ResourceBroker.for_app(source[0].root).snapshot()['leases'] == []


def test_pending_stage_cleanup_remains_owned_after_a_confirmed_result(source, monkeypatch, tmp_path):
    transport(monkeypatch, tmp_path)
    actual = bridge.TemporaryDirectory
    retained, results, errors = {}, [], []
    entered, release = threading.Event(), threading.Event()
    class HeldTemporary:
        def __init__(self, **kwargs):
            self.inner = actual(**kwargs); self.name = self.inner.name
            retained['stage'] = Path(self.name)
        def cleanup(self):
            entered.set()
            if not release.is_set():
                raise PermissionError('Synthetic held staging handle.')
            self.inner.cleanup()
    monkeypatch.setattr(bridge, 'TemporaryDirectory', HeldTemporary)
    decision = bridge.WordDecision()
    def work():
        try:
            results.append(bridge.WordBridge(source[3], source[4]).run(intent(source[2]),
                decision=decision, on_preview=lambda view: decision.approve(view.token), timeout=45))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=work, name='word-stage-cleanup', daemon=True)
    try:
        worker.start()
        assert entered.wait(30) and worker.is_alive()
        assert (retained['stage'] / 'receipt.json').is_file()
        assert len(ResourceBroker.for_app(source[0].root).snapshot()['leases']) == 1
    finally:
        release.set(); worker.join(15)
    assert not worker.is_alive() and not errors
    assert results[0].status == 'COMPLETED' and results[0].warnings
    assert not retained['stage'].exists()
    assert ResourceBroker.for_app(source[0].root).snapshot()['leases'] == []


@pytest.mark.parametrize('deferred_type', [KeyboardInterrupt, SystemExit])
def test_deferred_interruption_after_native_quiescence_cannot_delete_a_complete_receipt(source, monkeypatch, deferred_type):
    from carr_library.operations import broker as broker_module
    from carr_library.academic_integration.word_records import fingerprint
    entered, release = threading.Event(), threading.Event()
    retained, results, errors = {}, [], []
    original_settle = broker_module.ResourceLease.settle_cleanup
    def deferred(lease, error):
        original_settle(lease, error)
        assert error.cleanup_owner.quiescent_event.is_set()
        raise deferred_type('Synthetic interruption deferred until actual native cleanup.')
    monkeypatch.setattr(broker_module.ResourceLease, 'settle_cleanup', deferred)
    def interrupted(argv, **options):
        stage = Path(options['cwd'])
        request = read_json_snapshot(stage / 'request.json')[0]
        current = fixture.capture(request['session'])
        atomic_json(stage / 'capture.json', current)
        options['cancelled']()
        proposal = read_json_snapshot(stage / 'proposal.json')[0]
        assert (stage / 'approved.json').is_file()
        ready = {'schema': 'resonance-word-ready/1', 'session': request['session'],
            'proposal_sha256': fingerprint(proposal), 'capture_sha256': fingerprint(current)}
        atomic_json(stage / 'ready.json', ready)
        options['cancelled']()
        assert (stage / 'grant.json').is_file()
        atomic_json(stage / 'receipt.json', fixture.receipt(proposal))
        environment = os.environ.copy()
        environment.pop('__PYVENV_LAUNCHER__', None); environment.pop('PYTHONEXECUTABLE', None)
        executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True))
        child = subprocess.Popen([executable, '-I', '-B', '-c', 'import time; time.sleep(45)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, env=environment)
        retained.update(child=child, stage=stage)
        def settle():
            if not release.is_set():
                return False
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            return True
        pending = pending_cleanup(settle, message='Synthetic pending process after receipt.')
        retained['owner'] = pending.cleanup_owner
        entered.set()
        raise pending
    monkeypatch.setattr(bridge, 'run_process', interrupted)
    decision = bridge.WordDecision()
    def work():
        try:
            results.append(bridge.WordBridge(source[3], source[4]).run(intent(source[2]),
                decision=decision, on_preview=lambda view: decision.approve(view.token), timeout=45))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=work, name='word-receipt-before-deferred-interrupt', daemon=True)
    try:
        worker.start()
        assert entered.wait(35)
        assert retained['child'].poll() is None and worker.is_alive()
        assert (retained['stage'] / 'receipt.json').is_file()
        assert len(ResourceBroker.for_app(source[0].root).snapshot()['leases']) == 1
    finally:
        release.set(); worker.join(15)
        child = retained.get('child')
        died_before_fallback = child is not None and child.poll() is not None
        ended_before_fallback = not worker.is_alive()
        if child is not None and child.poll() is None:
            child.kill(); child.wait(timeout=5); worker.join(5)
    assert died_before_fallback and ended_before_fallback and not errors
    assert len(results) == 1 and results[0].status == 'COMPLETED' and results[0].warnings
    assert retained['owner'].quiescent and not retained['stage'].exists()
    assert ResourceBroker.for_app(source[0].root).snapshot()['leases'] == []
