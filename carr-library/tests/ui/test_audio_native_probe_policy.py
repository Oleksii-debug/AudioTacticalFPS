"""Probe event-turn and failure policy; this is not native Windows acceptance."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.audio.production import parse_script
from carr_library.ui.audio_clips import clip_values


def _probe(name):
    path = Path(__file__).resolve().parents[2] / 'scripts' / name
    spec = spec_from_file_location(name.removesuffix('.py'), path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Timer:
    def __init__(self, callback):
        self.callback, self.stopped = callback, False

    def Stop(self):
        self.stopped = True


class Control:
    def __init__(self, harness, *, name='', value='', selection=0):
        self.harness, self.name, self.value, self.selection = harness, name, value, selection

    def live(self):
        assert not self.harness.destroyed, 'A late probe callback accessed a destroyed native control.'

    def GetName(self):
        self.live()
        return self.name

    def GetValue(self):
        self.live()
        return self.value

    def SetValue(self, value):
        self.live()
        assert self.harness.shown, 'The probe edited controls before the owned modal was shown.'
        self.value = value

    def GetSelection(self):
        self.live()
        return self.selection

    def SetSelection(self, value):
        self.live()
        self.selection = value

    def SetFocus(self):
        self.live()
        # Focus becomes observable only on a subsequent native event turn.
        self.harness.CallLater(1, lambda: setattr(self.harness, 'focus', self))


class Button(Control):
    def __init__(self, harness, action):
        super().__init__(harness)
        self.action = action

    def IsEnabled(self):
        self.live()
        return True

    def GetId(self):
        return self.action

    def GetEventHandler(self):
        return self

    def ProcessEvent(self, event):
        self.live()
        assert self.harness.shown
        assert event.event_object is self
        if self.harness.reject_dispatch:
            return False
        self.harness.accept(self.action)
        return True


class Event:
    def __init__(self, event_type, identifier):
        self.identifier = identifier

    def SetEventObject(self, item):
        self.event_object = item


class ModalHarness:
    """Only the native-boundary contract, with an intentionally early ready hook."""
    def __init__(self, *, kind='clip', action='edit', book_format='epub', reject_dispatch=False,
                 reject_acceptance=False):
        self.kind, self.action, self.book_format = kind, action, book_format
        self.reject_dispatch, self.reject_acceptance = reject_dispatch, reject_acceptance
        self.frame, self.queue, self.timers = object(), [], []
        self.shown = self.modal = self.destroyed = False
        self.focus = self.result = None
        self.end_code = None
        self.status = Control(self)
        self.fields = {}
        self.buttons = {}
        self.wx = SimpleNamespace(Button=Button, CallLater=self.CallLater, CommandEvent=Event,
            EVT_BUTTON=SimpleNamespace(typeId=1), ID_OK='ok', ID_CANCEL='cancel',
            Window=SimpleNamespace(FindFocus=lambda: self.focus))
        if kind == 'clip':
            for key, value in dict(start='0', trim_start='0', duration='.5', gain_db='0',
                                   pan='0', fade_in='0', fade_out='0').items():
                self.fields[key] = Control(self, value=value)
            for key, name in dict(scene='Chapter', track='Track role', clip='Clip', title='Clip name',
                                   destination='Move to chapter', minimum='Minimum duration').items():
                self.fields[key] = Control(self, name=name)
            self.buttons = {key: Button(self, key) for key in ('edit', 'move', 'duration', 'remove', 'preview')}
        else:
            for key, name in dict(language='Publication language code', creator='Creator name (optional)',
                                  publisher='Publisher (required for DAISY)').items():
                self.fields[key] = Control(self, name=name)
        self.standard = {key: Button(self, key) for key in ('ok', 'cancel')}

    def CallLater(self, delay, callback):
        timer = Timer(callback)
        self.queue.append(timer)
        self.timers.append(timer)
        return timer

    def IsModal(self):
        return self.modal

    def IsShown(self):
        return self.shown

    def GetParent(self):
        return self.frame

    def FindWindowById(self, identifier, parent=None):
        # wx exposes this as a static lookup: omitting parent may select a
        # preceding modal whose deferred destruction has not run yet.
        assert parent is self, 'Standard button lookup escaped the owning modal.'
        return self.standard[identifier]

    def EndModal(self, code):
        self.shown, self.end_code = False, code

    def accept(self, action):
        if action == 'cancel':
            self.EndModal('cancel')
            return
        if self.kind == 'clip':
            result = dict(action=action, scene_id='scene-000001')
            if action in ('edit', 'remove', 'preview', 'move'):
                result['clip_id'] = 'clip-one'
            if action == 'edit':
                try:
                    result['values'] = clip_values({key: self.fields[key].value for key in
                        ('start', 'trim_start', 'duration', 'gain_db', 'pan', 'fade_in', 'fade_out')},
                        title=self.fields['title'].value,
                        track=('sfx', 'ambience', 'music', 'microphone')[self.fields['track'].selection])
                except ValueError as error:
                    self.status.value = str(error)
                    return
            elif action == 'move':
                assert self.fields['destination'].selection == 1
                result['scene_id'] = 'scene-000002'
            elif action == 'duration':
                result['minimum_duration'] = float(self.fields['minimum'].value)
        else:
            from carr_library.audio.publishing import language_tag, _text
            try:
                result = dict(language=language_tag(self.fields['language'].value),
                              creator=_text(self.fields['creator'].value, 'Creator', empty=True))
                if self.book_format == 'daisy-2.02':
                    result['publisher'] = _text(self.fields['publisher'].value, 'Publisher')
            except ValueError as error:
                self.status.value = str(error)
                return
        if self.reject_acceptance:
            self.status.value = 'Synthetic native validation refused the final request.'
            return
        self.result = result
        self.EndModal('ok')

    def show(self, frame, value, *, on_ready, **kwargs):
        assert frame is self.frame
        if self.kind == 'clip':
            on_ready(self, self.fields, self.buttons, self.status)
        else:
            on_ready(self, self.fields, self.status)
        # Process one queued turn while the dialog is still initializing. The
        # probe must wait, rather than dispatch a button from the ready hook.
        self.queue.pop(0).callback()
        assert self.result is None
        self.shown = self.modal = True
        for _ in range(20):
            if not self.shown:
                break
            assert self.queue, 'The native probe left an open modal without a queued next step.'
            timer = self.queue.pop(0)
            if not timer.stopped:
                timer.callback()
        assert not self.shown, 'The native probe did not settle the bounded modal.'
        self.modal, self.destroyed = False, True
        return self.result if self.end_code == 'ok' else None

    def replay_late_callbacks(self):
        # A callback already delivered by the native queue may run after Stop.
        for timer in self.timers:
            timer.callback()


PLAN = '# First\n[clip retained.wav id=clip-one duration=.5 title="Голос"]\n# Second\n[duration 1]'


@pytest.mark.parametrize('action', ('edit', 'move', 'duration', 'remove', 'preview', 'cancel'))
def test_clip_probe_waits_for_modal_and_focus_without_reentrant_yield(action):
    probe = _probe('windows_clip_editor_smoke.py')
    harness, checks = ModalHarness(action=action), []
    probe.drive_editor(harness.wx, harness.show, harness.frame, parse_script(PLAN), action, checks.append)
    assert checks
    harness.replay_late_callbacks()


@pytest.mark.parametrize(('book_format', 'cancel'), [('epub', False), ('daisy-2.02', False), ('epub', True)])
def test_metadata_probe_waits_for_modal_and_focus_without_reentrant_yield(book_format, cancel):
    probe = _probe('windows_publication_smoke.py')
    harness, checks = ModalHarness(kind='metadata', book_format=book_format), []
    probe.drive_metadata(harness.wx, harness.show, harness.frame, book_format, checks.append, cancel=cancel)
    assert checks
    harness.replay_late_callbacks()


@pytest.mark.parametrize('kind', ['clip', 'metadata'])
@pytest.mark.parametrize(('refusal', 'message'), [('reject_dispatch', 'event was not handled'),
                                                 ('reject_acceptance', 'Synthetic native validation')])
def test_native_probe_rejects_unhandled_or_refused_action_and_stops_late_callbacks(kind, refusal, message):
    harness = ModalHarness(kind=kind, **{refusal: True})
    with pytest.raises(AssertionError, match=message):
        if kind == 'clip':
            probe = _probe('windows_clip_editor_smoke.py')
            probe.drive_editor(harness.wx, harness.show, harness.frame, parse_script(PLAN), 'edit', lambda x: None)
        else:
            probe = _probe('windows_publication_smoke.py')
            probe.drive_metadata(harness.wx, harness.show, harness.frame, 'epub', lambda x: None)
    harness.replay_late_callbacks()
