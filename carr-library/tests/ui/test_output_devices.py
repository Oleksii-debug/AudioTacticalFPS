"""Chooser behavior with explicit native-peer fixtures; real wx is a separate gate."""
from types import SimpleNamespace

import pytest

from carr_library.audio.output_devices import OutputDevice
from carr_library.ui import output_devices as module


@pytest.fixture
def chooser(monkeypatch):
    rows = [OutputDevice(0, 'headphone-id', 'Навушники'), OutputDevice(1, 'speaker-id', 'Speakers')]
    fixture = SimpleNamespace(rows=rows, dialogs=[], action=None, answer=1, selection=1, closed=False, guards=0)
    class Dialog:
        def __init__(self, parent, message, title, labels):
            self.message, self.title, self.labels = message, title, labels
            self.dead = False
            self.handlers, self.reads, self.destroy_calls, self.id_reads = {}, 0, 0, 0
            fixture.dialogs.append(self)
        def __bool__(self):
            return not self.dead
        def GetId(self):
            assert not self.dead
            self.id_reads += 1
            return 719
        def Bind(self, kind, callback):
            self.handlers[kind] = callback
        def SetName(self, value):
            self.name = value
        def SetSelection(self, value):
            self.initial = value
        def ShowModal(self):
            if fixture.action:
                fixture.action(self)
            return fixture.answer
        def GetSelection(self):
            assert not self.dead and not self.disposed
            self.reads += 1
            return fixture.selection
        def Destroy(self):
            assert not self.dead
            self.destroy_calls += 1
            self.dead = True
            return True
    fixture.wx = SimpleNamespace(SingleChoiceDialog=Dialog, EVT_WINDOW_DESTROY='destroy', ID_OK=1)
    def guard():
        fixture.guards += 1
        if fixture.closed:
            raise ValueError('owner changed')
    fixture.guard = guard
    monkeypatch.setattr(module, 'output_devices', lambda: tuple(fixture.rows))
    def resolve(value):
        matches = [item for item in fixture.rows if item.identity == value.identity]
        if len(matches) != 1:
            raise ValueError('selected device changed')
        return matches[0]
    monkeypatch.setattr(module, 'resolve_output_device', resolve)
    return fixture


def run(fixture, current=None):
    return module.choose_output_device(object(), current=current, guard=fixture.guard, wx_module=fixture.wx)


def test_explicit_selection_uses_the_reviewed_endpoint_and_preserves_default_distinction(chooser):
    result = run(chooser)
    assert result.device is chooser.rows[0]
    assert chooser.dialogs[-1].destroy_calls == 1
    assert chooser.dialogs[-1].initial == 0
    chooser.selection = 0
    result = run(chooser, chooser.rows[0])
    assert result is not None and result.device is None
    assert chooser.dialogs[-1].initial == 1


def test_cancel_never_reads_or_applies_native_selection(chooser):
    chooser.answer = 2
    assert run(chooser) is None
    assert chooser.dialogs[-1].reads == 0 and chooser.dialogs[-1].destroy_calls == 1


@pytest.mark.parametrize('boundary', ['direct', 'parent-event', 'owner'])
def test_native_owner_disposal_never_reads_a_dead_peer(chooser, boundary):
    def action(dialog):
        if boundary == 'direct':
            dialog.Destroy()
        elif boundary == 'parent-event':
            dialog.dead = True
            dialog.handlers['destroy'](SimpleNamespace(GetId=lambda: 719, Skip=lambda: None))
        else:
            chooser.closed = True
    chooser.action = action
    if boundary == 'owner':
        with pytest.raises(ValueError, match='owner changed'):
            run(chooser)
    else:
        assert run(chooser) is None
    dialog = chooser.dialogs[-1]
    assert dialog.reads == 0 and dialog.id_reads == 1
    assert dialog.destroy_calls == (0 if boundary == 'parent-event' else 1)


def test_endpoint_disappearance_after_selection_cannot_choose_a_same_named_replacement(chooser):
    selected = chooser.rows[0]
    def changed(dialog):
        chooser.rows[:] = [OutputDevice(0, 'other-endpoint', selected.name)]
    chooser.action = changed
    with pytest.raises(ValueError, match='device changed'):
        run(chooser, selected)
    assert chooser.dialogs[-1].destroy_calls == 1


def test_unavailable_previous_selection_is_visible_and_never_preselects_default(chooser):
    missing = OutputDevice(4, 'missing-endpoint', 'Removed USB audio')
    chooser.selection = 3
    with pytest.raises(ValueError, match='device changed'):
        run(chooser, missing)
    assert chooser.dialogs[-1].initial == 3
    assert 'Unavailable previous choice' in chooser.dialogs[-1].labels[3]


def test_owner_change_during_enumeration_never_constructs_a_dialog(chooser, monkeypatch):
    def enumerate_changed():
        chooser.closed = True
        return tuple(chooser.rows)
    monkeypatch.setattr(module, 'output_devices', enumerate_changed)
    with pytest.raises(ValueError, match='owner changed'):
        run(chooser)
    assert chooser.dialogs == []


@pytest.mark.parametrize('index', [-1, 9, True])
def test_unlisted_or_ambiguous_native_selection_is_rejected(chooser, index):
    chooser.selection = index
    with pytest.raises(ValueError, match='displayed device list'):
        run(chooser)
    assert chooser.dialogs[-1].destroy_calls == 1
