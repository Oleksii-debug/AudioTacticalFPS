"""Durable one-time native dispatch through the actual assistant job service.

The bridge here is explicitly synthetic. Native SAPI/window qualification is
performed separately by windows_assistant_interactive_smoke.py.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
from threading import Event
from uuid import NAMESPACE_URL, uuid5

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.state import atomic_json
from carr_library.assistant.validation import validate_job
from carr_library.assistant.interactive_tools import validate_saved_scope

_spec = importlib.util.spec_from_file_location(
    '_service_interactive_fixture', Path(__file__).with_name('interactive_fixture.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


class ObservedBridge:
    def __init__(self):
        self.calls = []
        self.validations = []
        self.service = self.identity = None
        self.failure = None
        self.stop = None

    def validate_step(self, step, scope):
        validate_saved_scope(step, scope.to_dict(), None)
        self.validations.append(step.to_dict())

    def execute_step(self, step, scope, *, operation_id, cancelled, progress):
        # Execution can only start after the real JobStore has durably recorded
        # this exact dispatch ID and the reviewed plan has remained unchanged.
        record = self.service.store.load(self.identity)
        index = len(self.calls)
        saved = record['steps'][index]
        assert saved['status'] == 'running' and saved['result'] is None
        assert saved['intent'] == {'interactive_dispatch': operation_id}
        assert operation_id == uuid5(NAMESPACE_URL,
            'carr-assistant:' + self.identity + ':' + str(index)).hex
        self.calls.append((step.to_dict(), operation_id))
        progress('Synthetic bridge observed the selected command.')
        if self.failure == 'interrupted':
            raise InterruptedError('Synthetic interruption after durable dispatch.')
        if self.failure == 'runtime' or self.failure == 'second' and len(self.calls) == 2:
            raise RuntimeError('Synthetic owner failure after durable dispatch.')
        result = _fixture.result(step, operation_id)
        if self.failure == 'receipt':
            result['interactive_receipt']['effect'] = 'whole_book_finished'
        if self.stop is not None:
            self.stop.set()
        return result


@pytest.fixture
def env(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact selected workspace')
    bridge = ObservedBridge()
    service = AssistantService(app, interactive=bridge)
    bridge.service = service
    return app, workspace, bridge, service


def reviewed(env, tool='reader.control', action='prepare'):
    _, workspace, _, _ = env
    fixture = _fixture.step(tool, action)
    fixture.arguments['target']['location']['workspace'] = workspace
    return Step(tool, fixture.arguments)


def submit(env, commands=None):
    _, workspace, bridge, service = env
    identity = service.submit(Plan(Scope(workspace), tuple(commands or (reviewed(env),))))
    bridge.identity = identity
    return identity


@pytest.mark.parametrize('tool,action', [
    ('reader.control', 'select'), ('reader.control', 'prepare'),
    ('reader.control', 'read'), ('reader.control', 'stop'),
    ('player.control', 'play'), ('player.control', 'pause'),
    ('player.control', 'resume'), ('player.control', 'seek'), ('player.control', 'stop'),
])
def test_each_typed_command_persists_one_dispatch_before_observed_execution(env, tool, action):
    app, _, bridge, service = env
    step = reviewed(env, tool, action)
    identity = submit(env, (step,))
    report = service.run(identity)
    assert report.status == 'complete' and report.completed == 1, report
    assert len(bridge.calls) == 1
    result = report.results[0]
    assert result['artifacts'] == []
    assert result['interactive_receipt']['target'] == step.arguments['target']
    assert result['interactive_receipt']['action'] == action
    assert 'not completion of a book' in report.message
    # Reopening with no connected native window can inspect/reverify completed
    # inert receipts. It must not dispatch a second command.
    assert AssistantService(app).resume(identity).status == 'complete'
    assert len(bridge.calls) == 1


def test_unconnected_assistant_cannot_approve_native_commands(env):
    app, workspace, bridge, _ = env
    with pytest.raises(PermissionError, match='selected native reader'):
        AssistantService(app).submit(Plan(Scope(workspace), (reviewed(env),)))
    assert not bridge.calls


def test_native_target_must_match_explicit_scope_even_with_a_permissive_bridge(env):
    _, _, bridge, service = env
    step = reviewed(env)
    step.arguments['target']['location']['workspace'] = 'another-workspace'
    bridge.validate_step = lambda *args: None
    with pytest.raises(ValueError, match='workspace'):
        submit(env, (step,))
    assert not service.jobs() and not bridge.calls


@pytest.mark.parametrize('failure', ['interrupted', 'runtime', 'receipt'])
def test_unfinished_durable_dispatch_requires_new_review_and_is_never_replayed(env, failure):
    _, _, bridge, service = env
    identity = submit(env)
    bridge.failure = failure
    first = service.run(identity)
    assert first.status in ('paused', 'failed') and first.completed == 0
    assert len(bridge.calls) == 1
    bridge.failure = None
    second = service.resume(identity)
    assert second.status == 'attention' and second.completed == 0
    assert 'will not be replayed' in second.message
    assert len(bridge.calls) == 1


def test_crash_after_durable_intent_before_native_call_still_refuses_replay(env, monkeypatch):
    _, _, bridge, service = env
    identity = submit(env)
    save = service.store.save
    def crash(record):
        result = save(record)
        if record['steps'][0]['intent']:
            raise SystemExit('Synthetic process loss immediately after durable intent.')
        return result
    monkeypatch.setattr(service.store, 'save', crash)
    with pytest.raises(SystemExit):
        service.run(identity)
    assert not bridge.calls
    report = AssistantService(env[0], interactive=bridge).resume(identity)
    assert report.status == 'attention' and not bridge.calls


def test_failed_intent_storage_cannot_dispatch_but_unstarted_job_can_be_retried(env, monkeypatch):
    _, _, bridge, service = env
    identity = submit(env)
    save = service.store.save
    def fail(record):
        if record['steps'][0]['intent']:
            raise OSError('Synthetic unavailable checkpoint storage.')
        return save(record)
    monkeypatch.setattr(service.store, 'save', fail)
    with pytest.raises(OSError):
        service.run(identity)
    assert not bridge.calls and service.store.load(identity)['steps'][0]['intent'] == {}
    monkeypatch.setattr(service.store, 'save', save)
    assert service.resume(identity).status == 'complete'
    assert len(bridge.calls) == 1


def test_completed_first_command_remains_complete_when_second_dispatch_needs_review(env):
    _, _, bridge, service = env
    identity = submit(env, (reviewed(env, action='select'), reviewed(env)))
    bridge.failure = 'second'
    report = service.run(identity)
    assert report.status == 'failed' and report.completed == 1 and len(bridge.calls) == 2
    before = deepcopy(report.results[0])
    bridge.failure = None
    report = service.resume(identity)
    assert report.status == 'attention' and report.completed == 1 and report.results[0] == before
    assert len(bridge.calls) == 2


def test_stop_before_dispatch_leaves_no_intent_and_can_resume_after_explicit_restart(env):
    _, _, bridge, service = env
    identity = submit(env)
    assert service.run(identity, cancelled=lambda: True).status == 'paused'
    assert not bridge.calls and service.store.load(identity)['steps'][0]['intent'] == {}
    assert service.resume(identity).status == 'complete'
    assert len(bridge.calls) == 1


def test_stop_after_observed_command_does_not_erase_its_completion_receipt(env):
    _, _, bridge, service = env
    identity = submit(env)
    stop = bridge.stop = Event()
    report = service.run(identity, cancelled=stop.is_set)
    assert stop.is_set() and report.status == 'complete' and report.completed == 1
    assert len(bridge.calls) == 1


@pytest.mark.parametrize('completed', [False, True])
def test_restored_native_job_keeps_original_scope_but_cannot_reconnect_or_dispatch(env, tmp_path, completed):
    from carr_library.library_backup import create_backup, restore_backup
    app, _, bridge, service = env
    identity = submit(env)
    bridge.failure = None if completed else 'runtime'
    report = service.run(identity)
    assert report.completed == int(completed)
    checksum = create_backup(app, tmp_path / 'backup')
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum))
    review = AssistantService(restored, interactive=bridge)
    assert review.get(identity).status == 'attention'
    with pytest.raises(PermissionError, match='review'):
        review.resume(identity)
    assert len(bridge.calls) == 1


@pytest.mark.parametrize('damage', ['dispatch', 'receipt'])
def test_saved_dispatch_or_receipt_cannot_be_assigned_another_operation(env, damage):
    _, _, _, service = env
    identity = submit(env)
    assert service.run(identity).status == 'complete'
    record = service.store.load(identity)
    if damage == 'dispatch':
        record['steps'][0]['intent']['interactive_dispatch'] = '0' * 32
    else:
        record['steps'][0]['result']['interactive_receipt']['operation_id'] = '0' * 32
    with pytest.raises(ValueError):
        validate_job(record)
    atomic_json(service.store.path(identity), record)
    with pytest.raises(ValueError):
        service.resume(identity)


def test_saved_native_validation_never_resolves_paths_or_opens_windows(env, monkeypatch):
    _, _, bridge, service = env
    identity = submit(env)
    assert service.run(identity).status == 'complete'
    record = service.store.load(identity)
    def forbidden(*args, **kwargs):
        pytest.fail('Pure checkpoint validation must not touch files or a native session.')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    bridge.execute_step = bridge.validate_step = forbidden
    assert validate_job(record) is record
