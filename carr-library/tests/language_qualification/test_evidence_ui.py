"""Real library I/O and exact native callback methods with inert dialog controls."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.durable_state import encode
from carr_library.language_qualification.evidence import EvidenceStore, import_observation
from carr_library.ui import language_matrix as module

_spec = importlib.util.spec_from_file_location('_qualification_fixture', Path(__file__).with_name('evidence_fixture.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def controller(root):
    store, value, snapshot, token = fixture.saved(root)
    snapshot, token = fixture.assess(store, value, token)
    current = module.LanguageMatrixController(root)
    current.accept_evidence(current.load_evidence())
    return current, value, snapshot, token


def test_controller_requires_explicit_configuration_and_retains_exact_review_details(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    forbidden = lambda *a, **k: pytest.fail('Evidence view discovered or executed a runtime.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    current, value, snapshot, token = controller(tmp_path)
    assert current.filter(status='verified') == ()
    current.choose_configuration(value.context.fingerprint)
    row = current.filter(capability='tts', status='verified')
    assert len(row) == 1 and row[0].language.id == value.language_id
    details = current.evidence_details(value.fingerprint)
    assert value.fingerprint in details and value.context.fingerprint in details
    assert value.engine.sha256 in details and value.pack.sha256 in details
    assert value.test.input_text in details and fixture.reviewer().source in details
    assert 'not authenticated identities' in details and 'no automatic execution' in details
    current.set_inventory(module.InstalledVoiceInventory((), (), 'synthetic metadata only'))
    assert current.filter(capability='tts', status='verified') == row
    assert 'all 22 capabilities in the selected recorded configuration only' in current.summary()


@pytest.mark.parametrize('change', [
    'queued-object', 'queued-root', 'queued-directory', 'late-object', 'late-root', 'late-close',
])
def test_worker_and_result_callbacks_reject_current_library_changes(tmp_path, change):
    root = tmp_path / 'library'
    root.mkdir()
    current, value, before, token = controller(root)
    jobs, accepted, failed = [], [], []
    frame = SimpleNamespace(library=SimpleNamespace(root=root), cancel_event=Event(),
        working=False, busy=False, _closing=False,
        run_job=lambda title, work, done, error: jobs.append((work, done, error)))
    next_value = fixture.observation('unicode_text')
    module.request_evidence_operation(frame, current,
        lambda cancelled: current.store.add(next_value, expected_token=token, cancelled=cancelled),
        completed=lambda result: (current.accept_evidence(result), accepted.append(result)),
        failed=failed.append)
    assert len(jobs) == 1 and current.evidence == before
    work, done, _ = jobs[0]
    result = work() if change.startswith('late') else None
    if change.endswith('object'):
        frame.library = SimpleNamespace(root=root)
    elif change.endswith('root'):
        frame.library.root = tmp_path / 'other'
        frame.library.root.mkdir()
    elif change.endswith('directory'):
        root.rename(tmp_path / 'held')
        shutil.copytree(tmp_path / 'held', root)
    else:
        frame._closing = True
    with pytest.raises(ValueError, match='changed'):
        done(result) if result is not None else work()
    assert not accepted and not failed and current.evidence == before
    saved = EvidenceStore(root).load()[0]
    assert saved.observations == before.observations + ((next_value,) if result is not None else ())
    if change.endswith('root'):
        assert not list(frame.library.root.iterdir())


def test_controller_snapshot_cannot_overwrite_a_concurrent_actual_review(tmp_path):
    current, value, before, token = controller(tmp_path)
    other = EvidenceStore(tmp_path)
    rejected, new_token = fixture.assess(other, value, token, 'reject')
    raw = other.path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        fixture.assess(current.store, value, token)
    assert other.load() == (rejected, new_token) and other.path.read_bytes() == raw
    assert current.evidence == before


def test_report_is_create_only_passive_and_never_roundtrips_as_approval(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    current, value, before, token = controller(root)
    current.choose_configuration(value.context.fingerprint)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Report executed an input.'))
    raw = current.store.path.read_bytes()
    target = tmp_path / 'report.json'
    assert current.export_report(target, expected_token=token) == target
    exported = json.loads(target.read_bytes())
    assert exported['schema'] == module.REPORT_SCHEMA
    assert exported['evidence'] == before.to_dict()
    assert exported['matrix']['recorded_context'] == value.context.fingerprint
    assert 'no automatic execution' in exported['qualification']
    with pytest.raises(ValueError):
        import_observation(target)
    with pytest.raises(FileExistsError):
        current.export_report(target, expected_token=token)
    with pytest.raises(ValueError, match='outside'):
        current.export_report(root / 'state' / 'another.json', expected_token=token)
    assert current.store.path.read_bytes() == raw


@pytest.mark.parametrize('original', [False, True])
def test_cancelled_report_preserves_original_exception_and_creates_nothing(tmp_path, original):
    root = tmp_path / 'library'
    root.mkdir()
    current, value, before, token = controller(root)
    stop = InterruptedError('Original report stop')

    def cancelled():
        if original:
            raise stop
        return True

    target = tmp_path / 'report.json'
    with pytest.raises(InterruptedError) as caught:
        current.export_report(target, expected_token=token, cancelled=cancelled)
    if original:
        assert caught.value is stop
    assert not target.exists() and current.store.load() == (before, token)


def test_report_rechecks_actual_state_after_the_last_callback(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    current, value, before, token = controller(root)
    target = tmp_path / 'report.json'
    actual_encode = module.encode
    armed = False
    changed = False

    def encode_report(value):
        nonlocal armed
        raw = actual_encode(value)
        if isinstance(value, dict) and value.get('schema') == module.REPORT_SCHEMA:
            armed = True
        return raw

    def cancelled():
        nonlocal changed
        if armed and not changed:
            changed = True
            value = before.to_dict()
            value['revision'] += 1
            current.store.path.write_bytes(actual_encode(value))
        return False

    monkeypatch.setattr(module, 'encode', encode_report)
    with pytest.raises(ValueError, match='changed'):
        current.export_report(target, expected_token=token, cancelled=cancelled)
    assert changed and not target.exists()


def native_callback_fixture(monkeypatch, current, value, *, decision=0, confirmed=True,
                            independent=True, reviewer=None, question=True, selected_path=None):
    controls, dialogs, jobs = {}, [], []

    class Control:
        def __init__(self, parent=None, value='', label='', choices=(), **kwargs):
            self.value, self.selection, self.name = value, -1, label
            self.choices = choices
            if label:
                controls[label] = self
        def SetName(self, name):
            self.name = name
            controls[name] = self
        def GetValue(self):
            return self.value
        def ChangeValue(self, value):
            self.value = value
        def SetSelection(self, selection):
            self.selection = selection
        def GetSelection(self):
            return self.selection

    class Sizer:
        def __init__(self, *args):
            pass
        def Add(self, *args, **kwargs):
            pass

    class Dialog:
        def __init__(self, parent, *args, **kwargs):
            self.parent, self.destroyed = parent, False
            dialogs.append(self)
        def SetSizer(self, *args):
            pass
        def SetEscapeId(self, *args):
            pass
        def CreateButtonSizer(self, *args):
            return object()
        def ShowModal(self):
            for name, text in {
                'Reviewer identity': reviewer or fixture.reviewer().identity,
                'Reviewer provenance reference': fixture.reviewer().source,
                'Reviewer role': fixture.reviewer().role,
                'Assessment and limitations': 'Synthetic native callback assessment only.',
            }.items():
                if name in controls:
                    controls[name].value = text
            if 'Recorded decision' in controls:
                controls['Recorded decision'].selection = decision
            for name, item in controls.items():
                if name.startswith('This decision concerns') or name == 'Confirm the exact displayed observation':
                    item.value = confirmed
                if name.startswith('I record an independent') or name == 'Recorded independent assessment acknowledgement':
                    item.value = independent
            return 1
        def Destroy(self):
            self.destroyed = True

    class Picker(Dialog):
        def GetPath(self):
            return str(selected_path)

    class Question(Dialog):
        def ShowModal(self):
            return 2 if question else 3

    wx = SimpleNamespace(Dialog=Dialog, BoxSizer=Sizer, TextCtrl=Control, StaticText=Control,
        Choice=Control, CheckBox=Control, FileDialog=Picker, MessageDialog=Question,
        ID_OK=1, ID_YES=2, ID_NO=3, ID_CANCEL=4)
    for name in ('VERTICAL', 'HORIZONTAL', 'LEFT', 'RIGHT', 'TOP', 'BOTTOM', 'EXPAND',
                 'ALL', 'ALIGN_RIGHT', 'TE_MULTILINE', 'TE_READONLY', 'OK', 'CANCEL',
                 'DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'FD_OPEN', 'FD_FILE_MUST_EXIST',
                 'FD_SAVE', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION'):
        setattr(wx, name, 1)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    native = object.__new__(module._native_dialog_type())
    native.controller, native.pending = current, False
    native.evidence_rows = (value,)
    native.evidence_list = Control()
    native.evidence_list.SetSelection(0)
    native.status = Control()
    native._run_evidence = lambda operation, done: jobs.append((operation, done))
    native._accepted_evidence = current.accept_evidence
    return native, jobs, dialogs


@pytest.mark.parametrize('decision,expected', [(0, 'accept'), (1, 'reject'), (2, 'revoke')])
def test_actual_native_review_callback_uses_live_fields_and_bound_selected_record(tmp_path, monkeypatch, decision, expected):
    current, value, before, token = controller(tmp_path)
    native, jobs, dialogs = native_callback_fixture(monkeypatch, current, value, decision=decision)
    raw = current.store.path.read_bytes()
    native.record_review()
    assert len(jobs) == 1 and current.store.path.read_bytes() == raw
    operation, done = jobs[0]
    result = operation(lambda: False)
    assert current.evidence == before
    done(result)
    assert current.evidence.reviews[-1].decision == expected
    assert current.evidence.reviews[-1].observation_sha256 == value.fingerprint
    assert current.evidence.reviews[-1].reviewer == fixture.reviewer()
    assert all(dialog.destroyed for dialog in dialogs)


@pytest.mark.parametrize('refusal', ['unconfirmed', 'self', 'not-independent', 'stale-token'])
def test_native_callback_never_publishes_unconfirmed_or_stale_review(tmp_path, monkeypatch, refusal):
    current, value, before, token = controller(tmp_path)
    native, jobs, dialogs = native_callback_fixture(monkeypatch, current, value,
        confirmed=refusal != 'unconfirmed', independent=refusal != 'not-independent',
        reviewer=value.test.executor.identity if refusal == 'self' else None)
    if refusal == 'stale-token':
        fixture.assess(EvidenceStore(tmp_path), value, token, 'revoke')
    raw = current.store.path.read_bytes()
    native.record_review()
    if refusal == 'unconfirmed':
        assert not jobs and 'Confirm' in native.status.value
    else:
        assert len(jobs) == 1
        with pytest.raises(ValueError):
            jobs[0][0](lambda: False)
    assert current.store.path.read_bytes() == raw
    assert all(dialog.destroyed for dialog in dialogs)


@pytest.mark.parametrize('accept', [False, True])
def test_native_import_requires_explicit_yes_and_stays_passive(tmp_path, monkeypatch, accept):
    root = tmp_path / 'library'
    root.mkdir()
    current = module.LanguageMatrixController(root)
    current.accept_evidence(current.load_evidence())
    value = fixture.observation()
    selected = tmp_path / 'observation.json'
    raw = encode(value.to_dict())
    selected.write_bytes(raw)
    native, jobs, dialogs = native_callback_fixture(monkeypatch, current, value,
                                                    question=accept, selected_path=selected)
    native.import_evidence()
    assert len(jobs) == 1 and not current.store.path.exists()
    operation, done = jobs.pop(0)
    parsed = operation(lambda: False)
    done(parsed)
    assert len(jobs) == int(accept) and not current.store.path.exists()
    if accept:
        operation, done = jobs.pop()
        result = operation(lambda: False)
        done(result)
        assert current.evidence.observations == (value,) and current.evidence.reviews == ()
        current.choose_configuration(value.context.fingerprint)
        assert current.filter(status='verified') == ()
    assert selected.read_bytes() == raw and all(dialog.destroyed for dialog in dialogs)



def _outcome_dialog(monkeypatch, current, value, root):
    native, _, _ = native_callback_fixture(monkeypatch, current, value)
    jobs, failures, refreshed = [], [], []
    frame = SimpleNamespace(library=SimpleNamespace(root=root), cancel_event=Event(),
        working=False, busy=False, _closing=False,
        run_job=lambda title, work, done, failed: jobs.append((work, done, failed)))
    native._alive = True
    native.GetParent = lambda: frame
    native._set_pending = lambda value: setattr(native, 'pending', value)
    native.update_evidence = lambda: refreshed.append('evidence')
    native.filter = lambda: refreshed.append('matrix')
    del native._run_evidence
    actual_request = module.request_evidence_operation

    def observe_request(frame, controller, operation, *, completed, failed):
        def observed_failure(error):
            failures.append(error)
            failed(error)
        return actual_request(frame, controller, operation,
                              completed=completed, failed=observed_failure)

    monkeypatch.setattr(module, 'request_evidence_operation', observe_request)
    return native, frame, jobs, failures, refreshed


@pytest.mark.parametrize('change', ['object', 'root', 'directory', 'close'])
def test_late_committed_failure_retains_receipt_without_applying_old_library(tmp_path, monkeypatch, change):
    root = tmp_path / 'library'
    root.mkdir()
    current, value, before, token = controller(root)
    native, frame, jobs, failures, refreshed = _outcome_dialog(monkeypatch, current, value, root)
    next_value = fixture.observation('unicode_text')
    receipts = []

    def operation(cancelled):
        result = current.store.add(next_value, expected_token=token, cancelled=cancelled)
        error = module.EvidenceSaveCommittedError(*result)
        receipts.append(error)
        raise error

    native._run_evidence(operation, lambda result: pytest.fail('Failed work reported success.'))
    work, _, failed = jobs[0]
    with pytest.raises(module.EvidenceSaveCommittedError) as caught:
        work()
    original = caught.value
    raw = current.store.path.read_bytes()
    if change == 'object':
        frame.library = SimpleNamespace(root=root)
    elif change == 'root':
        frame.library.root = tmp_path / 'other'
        frame.library.root.mkdir()
    elif change == 'directory':
        root.rename(tmp_path / 'held')
        shutil.copytree(tmp_path / 'held', root)
    else:
        frame._closing = True
    failed(original)
    assert len(failures) == 1
    error = failures[0]
    assert type(error) is module.EvidenceDeliveryScopeError
    assert error.original_error is original is receipts[0]
    assert error.__cause__ is original and isinstance(error.scope_error, ValueError)
    assert error.completed_result is None
    assert original.snapshot.observations == before.observations + (next_value,)
    assert current.evidence == before and current.evidence_token == token
    assert not native.pending and not refreshed
    assert 'saved in the original library' in native.status.value
    assert EvidenceStore(root).load() == (original.snapshot, original.token)
    assert (root / 'state' / 'language-qualification' / 'evidence.json').read_bytes() == raw
    if change == 'root':
        assert not list(frame.library.root.iterdir())


@pytest.mark.parametrize('committed', [False, True])
def test_current_failure_preserves_original_error_and_only_applies_actual_commit(tmp_path, monkeypatch, committed):
    current, value, before, token = controller(tmp_path)
    native, frame, jobs, failures, refreshed = _outcome_dialog(monkeypatch, current, value, tmp_path)
    originals = []

    def operation(cancelled):
        result = current.store.add(fixture.observation('unicode_text'),
                                   expected_token=token, cancelled=cancelled) if committed else None
        original = module.EvidenceSaveCommittedError(*result) if committed else InterruptedError('Original stop')
        originals.append(original)
        raise original

    native._run_evidence(operation, lambda result: pytest.fail('Failed work reported success.'))
    work, _, failed = jobs[0]
    with pytest.raises((module.EvidenceSaveCommittedError, InterruptedError)) as caught:
        work()
    failed(caught.value)
    assert len(failures) == 1 and failures[0] is caught.value is originals[0]
    assert not native.pending
    if committed:
        assert (current.evidence, current.evidence_token) == (caught.value.snapshot, caught.value.token)
        assert refreshed == ['evidence', 'matrix']
    else:
        assert (current.evidence, current.evidence_token) == (before, token)
        assert not refreshed
    assert current.store.load() == (current.evidence, current.evidence_token)


def test_late_success_retains_exact_result_without_delivering_it_to_changed_library(tmp_path):
    current, value, before, token = controller(tmp_path)
    jobs, accepted, failures = [], [], []
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), cancel_event=Event(),
        working=False, busy=False, _closing=False,
        run_job=lambda title, work, done, failed: jobs.append((work, done, failed)))
    module.request_evidence_operation(frame, current,
        lambda cancelled: current.store.add(fixture.observation('unicode_text'),
                                            expected_token=token, cancelled=cancelled),
        completed=accepted.append, failed=failures.append)
    work, done, failed = jobs[0]
    result = work()
    frame.library = SimpleNamespace(root=tmp_path)
    with pytest.raises(module.EvidenceDeliveryScopeError) as caught:
        done(result)
    failed(caught.value)
    assert len(failures) == 1 and failures[0] is caught.value
    assert caught.value.completed_result is result and caught.value.original_error is None
    assert isinstance(caught.value.scope_error, ValueError) and not accepted
    assert (current.evidence, current.evidence_token) == (before, token)
    assert EvidenceStore(tmp_path).load() == result
