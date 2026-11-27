"""Lifecycle/control policy; native events are exercised by the separate probe."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from carr_library.audio.legacy_playlist import QueueImportResult, QueueImportCommittedError
from carr_library.ui import legacy_playlist as module


class Modal:
    def __init__(self, owner, *, answer=1, mutate=None):
        self.owner, self.answer, self.mutate = owner, answer, mutate
        self.destroyed = False
    def ShowModal(self):
        if self.mutate:
            self.mutate()
        return self.answer
    def Destroy(self):
        self.destroyed = True


def owner(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    library = object()
    controller = SimpleNamespace(project_id='1' * 32, project_path=root / 'project.json',
                                 private_root=root, voice='sapi5:default', language='uk')
    opened = []
    controller.open = opened.append
    values = SimpleNamespace(controller=controller, parent=SimpleNamespace(library=library),
        closed=False, busy=False, cancelled=Event(), jobs=[], messages=[], opened=opened, dialog=object())
    values._run = lambda label, action, done, **options: values.jobs.append((label, action, done, options)) or True
    values._message = lambda text, **kwargs: values.messages.append(text)
    values._unsaved = lambda: True
    values._stop_for_project_change = lambda: True
    values._refresh_text = lambda: None
    values.plan_text = SimpleNamespace(ChangeValue=lambda value: None)
    values.wx = SimpleNamespace(ID_OK=1, OK=1, CANCEL=2, ICON_INFORMATION=4)
    return values


def test_review_confirmation_only_admits_worker_after_exact_native_choice(tmp_path, monkeypatch):
    ui = owner(tmp_path)
    action = module.LegacyQueueAction(ui)
    modal = Modal(ui)
    monkeypatch.setattr(module, 'review_dialog', lambda *args: modal)
    result = QueueImportResult('2' * 32, Path('/new/project.json'), 'a' * 64, 2)
    calls = []
    review = SimpleNamespace(description=lambda: 'Preserved recording review.')
    monkeypatch.setattr(module.service, 'import_reviewed',
                        lambda chosen, cancelled: calls.append(chosen) or result)
    token = action._token()
    action._confirm(token, review)
    assert modal.destroyed and len(ui.jobs) == 1 and not calls
    _, work, done, _ = ui.jobs.pop()
    done(work())
    assert calls == [review] and ui.opened == [result.project_path]
    assert action.last_result == result and 'explicit Play or Resume' in ui.messages[-1]


def test_cancel_or_changed_library_during_modal_never_imports(tmp_path, monkeypatch):
    for mutate in (False, True):
        directory = tmp_path / str(mutate)
        directory.mkdir()
        ui = owner(directory)
        action = module.LegacyQueueAction(ui)
        modal = Modal(ui, answer=1 if mutate else 2,
                      mutate=(lambda: setattr(ui.parent, 'library', object())) if mutate else None)
        monkeypatch.setattr(module, 'review_dialog', lambda *args: modal)
        action._confirm(action._token(), SimpleNamespace(description=lambda: 'Review'))
        assert modal.destroyed and not ui.jobs


def test_changed_project_cancels_queued_work_before_service_admission(tmp_path, monkeypatch):
    ui = owner(tmp_path)
    action = module.LegacyQueueAction(ui)
    seen = []
    def sources(root, cancelled):
        cancelled()
        seen.append(root)
        return ()
    monkeypatch.setattr(module.service, 'sources', sources)
    assert action.start()
    _, work, _, _ = ui.jobs[0]
    ui.controller.project_id = '9' * 32
    try:
        work()
    except InterruptedError:
        pass
    else:
        raise AssertionError('A stale selection reached source discovery.')
    assert not seen


def test_committed_project_identity_survives_stale_callbacks_and_is_not_replayed(tmp_path):
    ui = owner(tmp_path)
    action = module.LegacyQueueAction(ui)
    token = action._token()
    messages = []
    ui.parent.report = messages.append
    result = QueueImportResult('2' * 32, Path('/new/project.json'), 'a' * 64, 2, False)
    ui.parent.library = object()
    action._failed(token, QueueImportCommittedError(result))
    assert action.last_result is result and not ui.jobs and not ui.opened
    assert result.project_id in messages[0] and 'not opened here' in messages[0]


def test_declined_current_unsaved_replacement_still_reports_new_durable_project(tmp_path):
    ui = owner(tmp_path)
    ui._unsaved = lambda: False
    action = module.LegacyQueueAction(ui)
    result = QueueImportResult('2' * 32, Path('/new/project.json'), 'a' * 64, 2)
    action._complete(action._token(), result)
    assert action.last_result is result and not ui.opened
    assert result.project_id in ui.messages[-1]


def test_busy_or_closed_owner_does_not_start_another_import(tmp_path):
    ui = owner(tmp_path)
    action = module.LegacyQueueAction(ui)
    ui.busy = True
    assert action.start() is False and not ui.jobs
    ui.busy, ui.closed = False, True
    assert action.start() is False and not ui.jobs


def test_real_worker_result_retains_identity_when_stale_native_callback_is_suppressed(tmp_path, monkeypatch):
    ui = owner(tmp_path)
    action = module.LegacyQueueAction(ui)
    monkeypatch.setattr(module, 'review_dialog', lambda *args: Modal(ui))
    result = QueueImportResult('2' * 32, Path('/new/project.json'), 'a' * 64, 2)
    def committed(review, cancelled):
        assert not cancelled()
        ui.parent.library = object()
        return result
    monkeypatch.setattr(module.service, 'import_reviewed', committed)
    action._confirm(action._token(), SimpleNamespace(description=lambda: 'Review'))
    _, work, _suppressed_done, _ = ui.jobs.pop()
    assert work() is result
    assert action.last_result is result and not ui.opened and not ui.messages
