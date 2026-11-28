"""Typed resource review; choosing fields creates no job or new authority."""
from dataclasses import replace
import pytest

from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.ui.import_resources import profile_values, profile_from_values, profile_summary


def test_explicit_ui_values_reproduce_frozen_budget_and_legacy_defaults():
    original = ImportBudget()
    assert profile_from_values(profile_values()) == original
    assert '64 MiB' in profile_summary() and '120-second' in profile_summary()
    selected = replace(original, source_bytes=129*MIB, segment_bytes=4*MIB,
                       segments=9000, steps=2, seconds=600)
    assert profile_from_values(profile_values(selected)) == selected
    text = profile_summary(selected)
    assert '129 MiB' in text and '9000' in text and '2 steps' in text and '600 seconds' in text
    assert 'memory' in text and 'library growth' in text and 'temporary' in text


@pytest.mark.parametrize('field,value', [
    ('source_mib', '0'), ('source_mib', '-1'), ('segment_mib', '65'),
    ('segments', '2147483648'), ('steps', '1002'), ('seconds', 'nan'),
    ('seconds', 'inf'), ('seconds', '0.01'), ('seconds', '86401'),
    ('memory_mib', '1'), ('library_mib', '1'), ('temporary_mib', '1'),
    ('source_mib', '2.5'), ('seconds', ''), ('segments', '9'*33),
])
def test_invalid_ui_budget_is_rejected_before_any_plan(field, value):
    values = profile_values()
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        profile_from_values(values)


@pytest.mark.parametrize('change', ['missing', 'unknown', 'not-dict', 'not-text'])
def test_resource_dialog_input_requires_exact_known_fields(change):
    values = profile_values()
    if change == 'missing':
        values.pop('seconds')
    elif change == 'unknown':
        values['authority'] = 'old approval'
    elif change == 'not-dict':
        values = tuple(values.items())
    else:
        values['seconds'] = 120
    with pytest.raises((TypeError, ValueError)):
        profile_from_values(values)



@pytest.fixture
def resource_dialog(monkeypatch):
    import sys
    from types import SimpleNamespace
    from threading import get_ident
    owner = get_ident()
    controls, dialogs, actions, destroyed = [], [], [], []
    current = [True]
    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent, self.value = parent, kwargs.get('value', '')
            self.handlers, self.name = {}, ''
            self.dead, self.focused = False, False
            self.result = 2
            controls.append(self)
        def native(self):
            assert get_ident() == owner
            assert not self.dead, 'Destroyed native resource control was accessed.'
            current_parent = self.parent
            while isinstance(current_parent, Control):
                assert not current_parent.dead, 'Resource control outlived its native parent.'
                current_parent = current_parent.parent
        def GetId(self): self.native(); return id(self)
        def SetName(self, value): self.native(); self.name = value
        def Bind(self, kind, callback): self.native(); self.handlers[kind] = callback
        def SetScrollRate(self, *args): self.native()
        def Add(self, *args): self.native()
        def AddGrowableCol(self, *args): self.native()
        def Wrap(self, *args): self.native()
        def SetMaxLength(self, *args): self.native()
        def SetSizer(self, *args): self.native()
        def SetMinSize(self, *args): self.native()
        def SetEscapeId(self, *args): self.native()
        def SetFocus(self): self.native(); self.focused = True
        def GetValue(self): self.native(); return self.value
        def SetValue(self, value): self.native(); self.value = value
        def EndModal(self, value): self.native(); self.result = value
        def IsBeingDeleted(self): self.native(); return False
        def Destroy(self):
            self.native()
            destroyed.append(self)
            self.dead = True
            callback = self.handlers.get('destroy')
            if callback:
                callback(SimpleNamespace(GetId=lambda: id(self), Skip=lambda: None))
    class Dialog(Control):
        def ShowModal(self):
            dialogs.append(self)
            if actions:
                actions[0](self)
            return self.result
    wx = SimpleNamespace(Dialog=Dialog, ScrolledWindow=Control, BoxSizer=Control,
        StaticText=Control, FlexGridSizer=Control, TextCtrl=Control, Button=Control,
        EVT_WINDOW_DESTROY='destroy', EVT_BUTTON='button', ID_OK=1, ID_CANCEL=2)
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'TAB_TRAVERSAL',
                 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND', 'ALIGN_CENTER_VERTICAL',
                 'ALIGN_RIGHT', 'TE_MULTILINE', 'TE_READONLY'):
        setattr(wx, name, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return SimpleNamespace(wx=wx, controls=controls, dialogs=dialogs, actions=actions,
                           destroyed=destroyed, current=current)


@pytest.mark.parametrize('route', ['apply', 'defaults', 'cancel'])
def test_actual_resource_callbacks_keep_previous_policy_until_explicit_choice(resource_dialog, route):
    from carr_library.ui.import_resources import choose_import_resources
    f = resource_dialog
    old = replace(ImportBudget(), steps=2)
    def action(dialog):
        assert len(dialog.fields) == 8
        assert all(field.name for field in dialog.fields.values())
        if route == 'apply':
            dialog.fields['steps'].SetValue('3')
            dialog.accept()
        elif route == 'defaults':
            dialog.defaults()
    f.actions[:] = [action]
    accepted, chosen = choose_import_resources(None, old, current=lambda: f.current[0])
    assert accepted is (route != 'cancel')
    assert chosen == (replace(old, steps=3) if route == 'apply' else None if route == 'defaults' else old)
    assert f.destroyed == f.dialogs and len(f.destroyed) == 1


def test_bad_resource_value_keeps_modal_review_and_prior_profile(resource_dialog):
    from carr_library.ui.import_resources import choose_import_resources
    f = resource_dialog
    old = ImportBudget()
    def action(dialog):
        dialog.fields['seconds'].SetValue('nan')
        dialog.accept()
        assert dialog.result == f.wx.ID_CANCEL
        assert dialog.selected is old
        assert dialog.status.focused and 'finite' in dialog.status.GetValue()
    f.actions[:] = [action]
    assert choose_import_resources(None, old) == (False, old)
    assert len(f.destroyed) == 1


@pytest.mark.parametrize('boundary', ['destroy', 'scope-change'])
def test_stale_modal_return_cannot_read_dead_controls_or_adopt_resource_policy(resource_dialog, boundary):
    from carr_library.ui.import_resources import choose_import_resources
    f = resource_dialog
    old = ImportBudget()
    def action(dialog):
        dialog.result = f.wx.ID_OK
        if boundary == 'destroy':
            dialog.Destroy()
        else:
            f.current[0] = False
        dialog.accept()
        dialog.defaults()
        assert dialog.selected is old
    f.actions[:] = [action]
    with pytest.raises(InterruptedError, match='changed during'):
        choose_import_resources(None, old, current=lambda: f.current[0])
    assert len(f.destroyed) == 1 and f.destroyed == f.dialogs


def test_child_destroy_event_does_not_revoke_live_resource_dialog(resource_dialog):
    from types import SimpleNamespace
    from carr_library.ui.import_resources import choose_import_resources
    f = resource_dialog
    skipped = []
    def action(dialog):
        dialog.destroyed(SimpleNamespace(GetId=lambda: id(dialog) + 1, Skip=lambda: skipped.append(True)))
        assert dialog.alive
        dialog.defaults()
    f.actions[:] = [action]
    assert choose_import_resources(None, ImportBudget()) == (True, None)
    assert skipped == [True] and len(f.destroyed) == 1
