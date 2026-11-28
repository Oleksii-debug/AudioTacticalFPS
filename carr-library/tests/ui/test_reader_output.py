"""Deterministic UI admission/lifecycle; real native controls have their own probe."""
from types import SimpleNamespace

import pytest

from carr_library.speech.output_devices import OutputDevice
from carr_library.ui import reader_output as module


class Control:
    def __init__(self, parent=None, *, label='', choices=(), style=0):
        self.label, self.name, self.items = label, label, list(choices)
        self.selection, self.enabled, self.value = -1, True, ''
        self.handlers = {}
    def SetName(self, value): self.name = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def SetItems(self, values): self.items = list(values); self.selection = -1
    def ChangeValue(self, value): self.value = value
    def Enable(self, value): self.enabled = bool(value)
    def Bind(self, event, handler): self.handlers[event] = handler


class Layout:
    def __init__(self, *args): self.items = []
    def Add(self, value, *args): self.items.append(value)


class Player:
    output_selection_supported = True
    def __init__(self):
        self.session_generation, self.output_device, self.applied = 0, None, []
    def set_output_device(self, selected):
        self.applied.append(selected)
        self.output_device = selected
        self.session_generation += 1


@pytest.fixture
def controls(monkeypatch):
    wx = SimpleNamespace(BoxSizer=Layout, StaticText=Control, Choice=Control,
        Button=Control, TextCtrl=Control, HORIZONTAL=1, ALL=2, ALIGN_CENTER_VERTICAL=4,
        EXPAND=8, TE_READONLY=16, EVT_CHOICE='choice', EVT_BUTTON='button', NOT_FOUND=-1)
    state = SimpleNamespace(alive=True, busy=False, cancelled=False, current=True)
    jobs, reports, invalidations = [], [], []
    player = Player()
    devices = (OutputDevice(0, 'Speakers', 'interface-A'),
               OutputDevice(1, 'Headphones', 'interface-B'))
    observed = []
    def enumerate_outputs(*, cancelled):
        observed.append('inventory')
        if cancelled(): raise InterruptedError('cancelled inventory')
        return devices
    def resolve_output(selected, *, cancelled):
        observed.append(('resolve', selected))
        if cancelled(): raise InterruptedError('cancelled resolution')
        return selected
    monkeypatch.setattr(module, 'output_devices', enumerate_outputs)
    monkeypatch.setattr(module, 'resolve_output_device', resolve_output)
    def guard():
        if not state.current:
            raise ValueError('Changed reader source')
    def submit(work, done):
        assert not state.busy
        # Same explicit operation policy as the existing reader.run().
        state.cancelled, state.busy = False, True
        jobs.append((work, done))
    view = module.ReaderOutputControls(object(), Layout(), player, submit=submit,
        guard=guard, alive=lambda: state.alive, busy=lambda: state.busy,
        cancelled=lambda: state.cancelled,
        report=lambda message, **kwargs: reports.append((message, kwargs)),
        invalidate=lambda: invalidations.append('invalidate'), wx=wx)
    def finish():
        work, done = jobs.pop(0)
        result = work()
        state.busy = False
        done(result)
    return SimpleNamespace(view=view, state=state, player=player, devices=devices,
        jobs=jobs, reports=reports, invalidations=invalidations,
        observed=observed, finish=finish)


def test_opening_controller_does_not_enumerate_select_or_play(controls):
    c = controls
    assert not c.jobs and not c.observed and not c.player.applied
    assert c.view.choice.items == ['System default output']
    assert c.view.choice.GetSelection() == 0
    assert 'Session only' in c.view.status.value
    assert 'screen-reader settings are unchanged' in c.view.status.value


def test_refresh_runs_in_existing_worker_and_requires_explicit_selection(controls):
    c = controls
    c.view.refresh_devices()
    assert len(c.jobs) == 1 and not c.observed
    assert not c.view.refresh.enabled
    c.finish()
    assert c.observed == ['inventory']
    assert c.view.choice.items == ['System default output', 'Speakers', 'Headphones']
    assert c.view.choice.GetSelection() == 0
    assert not c.player.applied and not c.invalidations


def test_apply_revalidates_then_invalidates_quiet_generation_without_play(controls):
    c = controls
    c.view.refresh_devices(); c.finish()
    c.view.choice.SetSelection(2)
    c.view.apply_device()
    assert len(c.jobs) == 1 and not c.player.applied
    c.finish()
    assert c.observed[-1] == ('resolve', c.devices[1])
    assert c.invalidations == ['invalidate']
    assert c.player.applied == [c.devices[1]]
    assert 'no audio started' in c.view.status.value
    assert 'Resume or read explicitly' in c.reports[-1][0]


def test_explicit_default_requires_apply_and_does_not_rewrite_another_reader(controls):
    c = controls
    c.player.output_device = c.devices[1]
    c.view.choice.SetSelection(0)
    assert c.player.output_device == c.devices[1]
    c.view.apply_device(); c.finish()
    assert c.player.applied == [None]
    assert not c.observed
    assert 'System default output' in c.view.status.value


@pytest.mark.parametrize('edge', ['cancel', 'scope', 'close', 'new-audio'])
def test_stale_worker_completion_never_changes_output(controls, edge):
    c = controls
    c.view.refresh_devices(); c.finish()
    c.view.choice.SetSelection(1)
    c.view.apply_device()
    work, done = c.jobs.pop()
    result = work()
    if edge == 'cancel': c.state.cancelled = True
    elif edge == 'scope': c.state.current = False
    elif edge == 'close': c.view.close(); c.state.alive = False
    else: c.player.session_generation += 1
    c.state.busy = False
    done(result)
    assert not c.player.applied and not c.invalidations


def test_removed_selection_remains_unselected_until_user_chooses_default(controls):
    c = controls
    c.player.output_device = OutputDevice(2, 'Removed headset', 'removed-interface')
    c.view.refresh_devices(); c.finish()
    assert c.view.choice.GetSelection() == -1 and not c.view.apply.enabled
    assert c.player.output_device.name == 'Removed headset'
    assert 'unavailable' in c.view.status.value
    c.view.apply_device()
    assert not c.jobs and not c.player.applied
    c.view.choice.SetSelection(0)
    c.view.apply_device(); c.finish()
    assert c.player.applied == [None]


def test_worker_error_is_readable_and_reenables_controls_without_changing_route(controls, monkeypatch):
    c = controls
    def broken(*, cancelled):
        raise RuntimeError('Actual inventory failed')
    monkeypatch.setattr(module, 'output_devices', broken)
    c.view.refresh_devices(); c.finish()
    assert c.view.refresh.enabled and c.view.apply.enabled
    assert not c.player.applied
    assert 'Actual inventory failed' in c.reports[-1][0]
    assert c.reports[-1][1]['focus']


def test_removed_device_during_apply_is_not_changed_to_default(controls, monkeypatch):
    c = controls
    c.view.refresh_devices(); c.finish()
    c.view.choice.SetSelection(2)
    def broken(*args, **kwargs):
        raise RuntimeError('Selected endpoint was removed')
    monkeypatch.setattr(module, 'resolve_output_device', broken)
    c.view.apply_device(); c.finish()
    assert not c.player.applied and not c.invalidations
    assert 'removed' in c.reports[-1][0]


def test_busy_reader_does_not_submit_another_executor_or_mutate_route(controls):
    c = controls
    c.state.busy = True
    c.view.sync()
    assert not c.view.refresh.enabled and not c.view.apply.enabled
    c.view.refresh_devices()
    assert not c.jobs and not c.player.applied
    assert 'running' in c.reports[-1][0]


def test_explicit_refresh_after_stop_never_resumes_audio(controls):
    c = controls
    c.state.cancelled = True
    c.view.refresh_devices(); c.finish()
    assert c.view.choice.items[1:] == ['Speakers', 'Headphones']
    assert not c.player.applied and not c.invalidations


def test_unsupported_backend_explains_disabled_controls(controls):
    c = controls
    c.player.output_selection_supported = False
    c.view._describe(); c.view.sync()
    assert not c.view.choice.enabled and not c.view.refresh.enabled and not c.view.apply.enabled
    assert 'requires Windows MCI' in c.view.status.value


def test_close_never_uses_destroyed_native_widgets(controls, monkeypatch):
    c = controls
    for widget in (c.view.choice, c.view.refresh, c.view.apply, c.view.status):
        monkeypatch.setattr(widget, 'Enable', lambda *args: pytest.fail('Destroyed wx object'))
        if hasattr(widget, 'ChangeValue'):
            monkeypatch.setattr(widget, 'ChangeValue', lambda *args: pytest.fail('Destroyed wx object'))
    c.state.alive = False
    c.view.close()
    c.view.refresh_devices()
    c.view.apply_device()
    c.view.sync()
    assert not c.jobs and not c.player.applied
