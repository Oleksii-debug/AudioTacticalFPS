"""Accepted frame close stops its real timer owner before native destruction.

These tests execute production install/close callbacks with explicit controls.
The Windows smoke still exercises the actual wx event loop and timer.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def lifetime():
    ui = Path(__file__).resolve().parents[2] / 'src/carr_library/ui'
    events, bindings = [], []
    class BaseFrame:
        def on_close(self, event):
            if event.CanVeto() and self.unsaved:
                event.Veto()
                return
            event.Skip()
    tree = ast.parse((ui / 'desktop_app.py').read_text(encoding='utf-8'))
    node = next(item for item in tree.body
                if isinstance(item, ast.ClassDef) and item.name == 'DesktopFrame')
    namespace = {'__name__': 'carr_library.ui._trust_lifetime_probe',
                 '__package__': 'carr_library.ui', 'WorkspaceFrame': BaseFrame}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ui / 'desktop_app.py'), 'exec'), namespace)
    frame = object.__new__(namespace['DesktopFrame'])
    frame.working = frame.unsaved = frame._closing = False
    frame.alive = True
    frame.cancel_event = SimpleNamespace(set=lambda: events.append('cancel'))
    frame.executor = SimpleNamespace(shutdown=lambda **options: events.append(('shutdown', options)))
    frame.report = Mock()
    frame.Bind = lambda kind, callback, source=None: bindings.append((kind, callback, source))
    menu = SimpleNamespace(Append=lambda *args: object())
    bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabelText=lambda index: 'Help',
                          GetMenu=lambda index: menu)
    frame.GetMenuBar = lambda: bar
    class Timer:
        def __init__(self, owner):
            assert owner is frame
            self.running = False
        def Start(self, interval):
            assert interval == 1000
            self.running = True
        def Stop(self):
            assert frame.alive, 'The native timer must stop while its owner is alive.'
            events.append('timer stopped')
            self.running = False
        def IsRunning(self):
            return self.running
    def host(name):
        return SimpleNamespace(
            enabled=True,
            start=Mock(side_effect=lambda: events.append(name + ' started')),
            set_idle=Mock(),
            close=Mock(side_effect=lambda: events.append(name + ' closed')))
    renewal, polling = host('renewal'), host('polling')
    frame.trust_controller = SimpleNamespace(refresh=object(), automatic_refresh=True,
                                            update_poll=polling)
    wx = SimpleNamespace(Timer=Timer, EVT_MENU='menu', EVT_TIMER='timer',
                         EVT_WINDOW_DESTROY='destroy', ID_ANY=-1)
    install_tree = ast.parse((ui / 'trust_status.py').read_text(encoding='utf-8'))
    install = next(item for item in install_tree.body
                   if isinstance(item, ast.FunctionDef) and item.name == 'install_trust_actions')
    namespace = {'wx': wx, 'IdleRenewalHost': lambda *args, **kwargs: renewal,
                 '__name__': 'carr_library.ui._trust_install_probe',
                 '__package__': 'carr_library.ui'}
    exec(compile(ast.Module(body=[install], type_ignores=[]), str(ui / 'trust_status.py'), 'exec'), namespace)
    namespace['install_trust_actions'](frame)
    class CloseEvent:
        def __init__(self):
            self.vetoed = self.skipped = False
        def CanVeto(self):
            return True
        def Veto(self):
            self.vetoed = True
        def GetVeto(self):
            return self.vetoed
        def Skip(self):
            self.skipped = True
    idle = next(callback for kind, callback, _ in bindings if kind == 'timer')
    destroy = next(callback for kind, callback, _ in bindings if kind == 'destroy')
    return SimpleNamespace(frame=frame, event=CloseEvent(), events=events,
                           renewal=renewal, polling=polling, idle=idle, destroy=destroy)


def test_accepted_close_stops_native_timer_and_hosts_before_owner_destruction():
    probe = lifetime()
    timer = probe.frame.trust_client_timer
    assert timer.IsRunning()
    probe.frame.on_close(probe.event)
    assert probe.event.skipped and not probe.event.vetoed
    assert not timer.IsRunning()
    probe.renewal.close.assert_called_once_with()
    probe.polling.close.assert_called_once_with()
    assert probe.events.index('timer stopped') < probe.events.index('renewal closed')
    assert probe.events.index('polling closed') < next(
        index for index, event in enumerate(probe.events) if isinstance(event, tuple))
    probe.frame.alive = False
    # The later native destroy event is a redundant fallback, not a second
    # access to the already destroyed timer owner.
    event = SimpleNamespace(GetEventObject=lambda: probe.frame, Skip=Mock())
    probe.destroy(event)
    event.Skip.assert_called_once_with()
    probe.renewal.close.assert_called_once_with()
    probe.polling.close.assert_called_once_with()


@pytest.mark.parametrize('reason', ('working', 'unsaved'))
def test_vetoed_close_preserves_polling_and_the_existing_frame(reason):
    probe = lifetime()
    setattr(probe.frame, reason, True)
    probe.frame.on_close(probe.event)
    assert probe.event.vetoed and not probe.frame._closing
    assert probe.frame.trust_client_timer.IsRunning()
    probe.renewal.close.assert_not_called()
    probe.polling.close.assert_not_called()
    assert not any(isinstance(event, tuple) for event in probe.events)


def test_already_queued_idle_callback_has_no_admission_after_accepted_close():
    probe = lifetime()
    probe.frame.on_close(probe.event)
    before = (probe.renewal.set_idle.call_count, probe.polling.set_idle.call_count)
    probe.idle()
    assert (probe.renewal.set_idle.call_count, probe.polling.set_idle.call_count) == before


def test_direct_destroy_fallback_ignores_children_and_stops_only_its_owner():
    probe = lifetime()
    child = SimpleNamespace(GetEventObject=lambda: object(), Skip=Mock())
    probe.destroy(child)
    assert probe.frame.trust_client_timer.IsRunning()
    probe.polling.close.assert_not_called()
    owner = SimpleNamespace(GetEventObject=lambda: probe.frame, Skip=Mock())
    probe.destroy(owner)
    assert not probe.frame.trust_client_timer.IsRunning()
    probe.polling.close.assert_called_once_with()
