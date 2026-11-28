"""Controller and owned preview lifetimes; native widget acceptance is a separate probe."""
import importlib.util
from pathlib import Path
import queue
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.ui.narration_editor import NarrationController, PronunciationPreviewTask
from carr_library.ui import narration_preparation as subject

_spec = importlib.util.spec_from_file_location('_ui_preparation_fixture',
    Path(__file__).parents[1] / 'reader/preparation_fixture.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


class Control:
    def __init__(self, value='', selection=0):
        self.value, self.selection, self.enabled, self.items = value, selection, True, []
    def GetValue(self):
        return self.value
    def ChangeValue(self, value):
        self.value = value
    def GetSelection(self):
        return self.selection
    def SetSelection(self, value):
        self.selection = value
    def Enable(self, value):
        self.enabled = bool(value)
    def Set(self, items):
        self.items = list(items)
        self.selection = -1
    def SetInsertionPoint(self, _value):
        pass


def window():
    item = object.__new__(subject.PreparationDialog)
    item.project = new_project('42')
    item.start, item.end = 0, 2
    item.result = item.rule = item.preview = item.report = None
    item.closed = item.close_pending = False
    item.guard = lambda: None
    item.task = SimpleNamespace(busy=False, cancel=Event())
    item.language, item.kind, item.number, item.date_order = (Control() for _ in range(4))
    item.unit, item.unit_symbol, item.gender, item.case = Control(), Control('kg'), Control(), Control()
    item.voice = Control()
    item.normal_manifest, item.spell_manifest = Control(), Control()
    item.issues, item.suggestions, item.details = Control(selection=-1), Control(selection=-1), Control()
    item.fields = [item.language, item.kind, item.number, item.date_order, item.unit, item.unit_symbol,
                   item.gender, item.case, item.voice, item.normal_manifest, item.spell_manifest, item.suggestions]
    item.buttons = {key: Control() for key in ('normal_browse', 'spell_browse', 'normalize', 'spell',
                   'spelling_preview', 'accept_word', 'apply', 'cancel', 'close')}
    outcomes = []
    item.dialog = SimpleNamespace(EndModal=outcomes.append)
    item.wx = SimpleNamespace(ID_CANCEL=1, ID_OK=2)
    return item, outcomes


def test_empty_optional_selection_disables_work_without_automatic_discovery():
    item, _ = window()
    item.refresh()
    assert not item.buttons['normalize'].enabled and not item.buttons['spell'].enabled
    assert not item.buttons['apply'].enabled and item.buttons['close'].enabled


@pytest.mark.parametrize('language,kind,number', [(1, 3, 0), (2, 3, 0), (2, 0, 1)])
def test_unsupported_symbol_language_and_slovak_ordinals_have_named_explanations(language, kind, number):
    item, _ = window()
    item.language.selection, item.kind.selection, item.number.selection = language, kind, number
    item.normal_manifest.ChangeValue('explicit-manifest.json')
    item.refresh()
    assert not item.buttons['normalize'].enabled
    assert 'English only' in item.details.GetValue() or 'does not implement ordinals' in item.details.GetValue()


def test_preview_snapshots_selected_context_before_controls_can_change(monkeypatch):
    item, _ = window()
    item.normal_manifest.ChangeValue('/selected/manifest.json')
    calls, operations = [], []
    item._start = operations.append
    monkeypatch.setattr(subject, 'prepare_selection',
                        lambda project, first, last, context, path, **kwargs:
                        calls.append((project, first, last, context, path)))
    item.normalize()
    item.language.selection = 1
    item.number.selection = 2
    item.normal_manifest.ChangeValue('/another/manifest.json')
    operations[0](lambda: False)
    assert calls[0][3].language == 'en' and calls[0][3].number_mode == 'cardinal'
    assert calls[0][4] == '/selected/manifest.json'


def test_accepted_preparation_is_one_saved_source_bound_revision(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = NarrationController(NarrationStore(root), new_project('42'))
    controller.save()
    original_digest = controller.saved_digest
    proposal = _fixture.preview(controller.project, 0, 2, 'forty-two')
    controller.apply_preparation(proposal)
    assert controller.dirty and controller.saved_digest == original_digest
    controller.save()
    reopened, _ = controller.store.load(controller.project.id)
    assert reopened.current_text == 'forty-two' and reopened.original_text == '42'
    assert reopened.revisions[-1].preparation == proposal.record
    controller.undo()
    assert controller.pending_text == '42'


def test_revoked_source_guard_cannot_apply_a_prepared_result(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = NarrationController(NarrationStore(root), new_project('42'))
    proposal = _fixture.preview(controller.project, 0, 2, 'forty-two')
    before = controller.project
    controller.guard = lambda: (_ for _ in ()).throw(ValueError('source closed'))
    with pytest.raises(ValueError, match='source closed'):
        controller.apply_preparation(proposal)
    assert controller.project is before and controller.pending_text == '42'


def test_cancel_after_success_before_dispatch_does_not_offer_an_applicable_result():
    callbacks, outcomes = queue.Queue(), []
    task = PronunciationPreviewTask(dispatch=lambda fn, *args: callbacks.put((fn, args)),
                                   completed=lambda result, error: outcomes.append((result, error)))
    proposal = _fixture.preview(new_project('42'), 0, 2, 'forty-two')
    task.start_operation(lambda cancelled: proposal)
    callback, args = callbacks.get(timeout=5)
    task.stop()
    callback(*args)
    assert outcomes[0][0] is None and isinstance(outcomes[0][1], InterruptedError)


def test_closing_waits_for_owned_worker_then_discards_all_results():
    item, outcomes = window()
    item.task.busy = True
    item.task.stop = item.task.cancel.set
    veto = []
    item.close(SimpleNamespace(CanVeto=lambda: True, Veto=lambda: veto.append(True)))
    assert item.close_pending and not item.closed and item.task.cancel.is_set()
    assert outcomes == [] and veto == [True]
    item.task.busy = False
    item.completed(_fixture.preview(item.project, 0, 2, 'forty-two'), None)
    assert item.closed and item.result is None and outcomes == [item.wx.ID_CANCEL]


def test_stale_or_unreviewed_preview_cannot_close_editor_as_success():
    item, outcomes = window()
    item.apply()
    assert outcomes == []
    item.preview = _fixture.preview(item.project, 0, 2, 'forty-two')
    item.project = revise(item.project, '43')
    item.apply()
    assert outcomes == [] and item.result is None and 'provenance' in item.details.GetValue()


def test_context_change_invalidates_a_previous_proposal_before_apply():
    item, _ = window()
    item.preview = _fixture.preview(item.project, 0, 2, 'forty-two')
    item.changed()
    assert item.preview is None and not item.buttons['apply'].enabled


def test_owned_preview_rejects_overlapping_work_and_late_closed_callbacks():
    callbacks, outcomes = queue.Queue(), []
    task = PronunciationPreviewTask(dispatch=lambda fn, *args: callbacks.put((fn, args)),
                                   completed=lambda result, error: outcomes.append((result, error)))
    task.start_operation(lambda cancelled: 'first result')
    with pytest.raises(ValueError, match='active'):
        task.start_operation(lambda cancelled: 'second result')
    callback, args = callbacks.get(timeout=5)
    task.dispose()
    callback(*args)
    assert outcomes == []
