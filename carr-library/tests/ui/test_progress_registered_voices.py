"""Registered progress voices, real manifest admission and native-owner boundaries.

Synthetic model bytes are never executed. Deterministic UI/player boundaries
qualify control flow only; native GUI and speech quality have separate gates.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import threading
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library import progress, progress_audio as audio, runtime_settings
from carr_library.audio import engines, runtime
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui import operation_progress as ui
from tests.audio.test_engine_profiles import installation
from tests.ui.test_operation_progress import Control, NativeBoundary, Notifications
from tests.ui.test_progress_concurrency import ModalBoundary


@pytest.fixture
def registrations(tmp_path, monkeypatch):
    first, second = tmp_path / 'first-library', tmp_path / 'second-library'
    first.mkdir()
    second.mkdir()
    one, two = installation(tmp_path, 'first-voice'), installation(tmp_path, 'second-voice')
    first_store, second_store = runtime_settings.EngineProfileStore(first), runtime_settings.EngineProfileStore(second)
    a = first_store.register(one, name='A registered Piper')
    b = second_store.register(two, name='B registered Piper')
    monkeypatch.setattr(runtime, 'resolve_ffmpeg', lambda: None)
    monkeypatch.setattr(engines, 'default_registry', lambda **options: engines.VoiceRegistry())
    process = Mock(side_effect=AssertionError('Synthetic model assets must never execute.'))
    monkeypatch.setattr(engines.subprocess, 'Popen', process)
    return SimpleNamespace(first=first, second=second, one=one, two=two,
                           first_store=first_store, second_store=second_store,
                           a=a, b=b, process=process)


def selected(root):
    registry = audio.configured_registry(root)
    voice, = registry.voices()
    return registry, voice, audio.voice_fingerprint(registry, voice.id, 'uk')


def test_progress_discovers_only_this_librarys_enabled_registered_voice(registrations):
    item = registrations
    a, first, first_hash = selected(item.first)
    b, second, second_hash = selected(item.second)
    assert first.engine == 'local-' + item.a.id and first.name.startswith('A registered Piper')
    assert second.engine == 'local-' + item.b.id and second.name.startswith('B registered Piper')
    assert first.id != second.id and first_hash != second_hash
    assert a.cache_identity(first.id)['local_profile_id'] == item.a.id
    assert b.cache_identity(second.id)['local_profile_id'] == item.b.id
    item.process.assert_not_called()


def test_another_library_cannot_resolve_a_saved_registered_progress_selection(registrations):
    item = registrations
    _, voice, _ = selected(item.first)
    other = audio.configured_registry(item.second)
    with pytest.raises(ValueError, match='unavailable'):
        audio.voice_fingerprint(other, voice.id, 'uk')
    item.process.assert_not_called()


@pytest.mark.parametrize('change', ['disabled', 'removed', 'model-bytes', 'manifest-bytes', 'missing-installation'])
def test_progress_fingerprint_readmits_the_registration_after_inventory(registrations, change):
    item = registrations
    registry, voice, _ = selected(item.first)
    if change == 'disabled':
        item.first_store.set_enabled(item.a.id, False)
    elif change == 'removed':
        item.first_store.remove(item.a.id)
    elif change == 'model-bytes':
        path = item.one.parent / 'voice.onnx'
        path.write_bytes(b'x' * path.stat().st_size)
    elif change == 'manifest-bytes':
        item.one.write_bytes(item.one.read_bytes() + b'\n')
    else:
        item.one.rename(item.one.with_suffix('.disconnected'))
    with pytest.raises((ValueError, OSError)):
        audio.voice_fingerprint(registry, voice.id, 'uk')
    item.process.assert_not_called()


def test_cancelled_progress_admission_never_executes_a_registered_helper(registrations):
    item = registrations
    registry = audio.configured_registry(item.first)
    voice, = registry.voices()
    with pytest.raises(InterruptedError):
        audio.voice_fingerprint(registry, voice.id, 'uk', cancelled=lambda: True)
    item.process.assert_not_called()


def test_progress_legacy_engine_paths_remain_passive_and_unchanged(registrations, tmp_path):
    root = tmp_path / 'legacy-library'
    root.mkdir()
    store = runtime_settings.EngineProfileStore(root)
    store.path.parent.mkdir(parents=True)
    raw = (json.dumps(dict(schema=1, ffmpeg='/unreviewed/ffmpeg', ffprobe=None,
        piper=[{'executable': '/unreviewed/private-helper'}], voice_helpers=[],
        music_models=[], fluidsynth=None), indent=3) + '\n').encode()
    store.path.write_bytes(raw)
    registry = audio.configured_registry(root)
    assert registry.voices() == ()
    assert any(row['engine'] == 'local-profiles' for row in registry.notices)
    assert store.path.read_bytes() == raw
    registrations.process.assert_not_called()


def test_progress_missing_library_discovery_does_not_create_configuration(registrations, tmp_path):
    absent = tmp_path / 'absent-library'
    registry = audio.configured_registry(absent)
    assert registry.voices() == () and not absent.exists()
    registrations.process.assert_not_called()


def synthetic_speech(monkeypatch):
    calls = []
    def synthesize(self, text, output, *, voice, language, volume, cancelled, **options):
        assert not cancelled()
        calls.append((voice, language, text, volume))
        with wave.open(str(output), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(22050)
            stream.writeframes(b'\0\0' * 32)
        return output
    monkeypatch.setattr(engines.PiperEngine, 'synthesize', synthesize)
    return calls


def test_private_notification_uses_the_current_registered_adapter_without_changing_assets(registrations, monkeypatch):
    item = registrations
    _, voice, fingerprint = selected(item.first)
    before = {path.name: path.read_bytes() for path in item.one.parent.iterdir()}
    calls = synthetic_speech(monkeypatch)
    played = []
    output = audio.LocalNotificationOutput(library_root=item.first)
    monkeypatch.setattr(output, '_play', lambda path, cancelled: played.append(
        (path.read_bytes(), cancelled())))
    output.speak('Приватне сповіщення', volume=.2, cancelled=lambda: False,
                 selection=(voice.id, 'uk', fingerprint))
    assert len(calls) == len(played) == 1
    assert calls[0][0].startswith('piper:') and calls[0][1:] == ('uk', 'Приватне сповіщення', .2)
    assert played[0][0].startswith(b'RIFF') and played[0][1] is False and output.quiescent
    assert before == {path.name: path.read_bytes() for path in item.one.parent.iterdir()}
    item.process.assert_not_called()


@pytest.mark.parametrize('change', ['disabled', 'removed'])
def test_cached_progress_output_cannot_reuse_revoked_registration(registrations, monkeypatch, change):
    item = registrations
    _, voice, fingerprint = selected(item.first)
    calls = synthetic_speech(monkeypatch)
    output = audio.LocalNotificationOutput(library_root=item.first)
    played = []
    monkeypatch.setattr(output, '_play', lambda path, cancelled: played.append(path.read_bytes()))
    selection = (voice.id, 'uk', fingerprint)
    output.speak('First', volume=.2, cancelled=lambda: False, selection=selection)
    if change == 'disabled':
        item.first_store.set_enabled(item.a.id, False)
    else:
        item.first_store.remove(item.a.id)
    with pytest.raises((ValueError, OSError)):
        output.speak('Must not run', volume=.2, cancelled=lambda: False, selection=selection)
    assert len(calls) == len(played) == 1
    item.process.assert_not_called()


@pytest.mark.parametrize('change', ['root', 'selection'])
def test_actual_late_discovery_cannot_overwrite_a_new_root_or_selection(registrations, monkeypatch, change):
    item = registrations
    old, voice, fingerprint = selected(item.first)
    newer = audio.configured_registry(item.second)
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []
    def discover(root):
        calls.append(root)
        entered.set()
        assert release.wait(5)
        return old
    monkeypatch.setattr(audio, 'configured_registry', discover)
    output = audio.LocalNotificationOutput(library_root=item.first)
    def speak():
        try:
            output.speak('Never played', volume=.2, cancelled=lambda: False,
                         selection=(voice.id, 'uk', fingerprint))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=speak, name='progress-discovery-boundary')
    thread.start()
    try:
        assert entered.wait(3)
        if change == 'root':
            output.set_library_root(item.second)
        else:
            output.set_registry(newer)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], InterruptedError)
    assert calls == [item.first]
    assert output._registry is (None if change == 'root' else newer)
    assert output.quiescent
    item.process.assert_not_called()


def test_root_change_cancels_an_owned_registered_speech_request_before_playback(registrations, monkeypatch):
    item = registrations
    _, voice, fingerprint = selected(item.first)
    entered, release = threading.Event(), threading.Event()
    played, errors = [], []
    def synthesize(self, text, output, *, cancelled, **options):
        entered.set()
        assert release.wait(5)
        assert cancelled()
        raise InterruptedError('Owned request observed its changed library.')
    monkeypatch.setattr(engines.PiperEngine, 'synthesize', synthesize)
    output = audio.LocalNotificationOutput(library_root=item.first)
    monkeypatch.setattr(output, '_play', lambda *args: played.append(True))
    def speak():
        try:
            output.speak('Old root', volume=.2, cancelled=lambda: False,
                         selection=(voice.id, 'uk', fingerprint))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=speak, name='progress-render-boundary')
    thread.start()
    try:
        assert entered.wait(3)
        output.set_library_root(item.second)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], InterruptedError)
    assert played == [] and output.quiescent and output._registry is None
    item.process.assert_not_called()


def test_root_binding_never_discards_failed_close_ownership(registrations):
    output = audio.LocalNotificationOutput(library_root=registrations.first)
    retained = object()
    output._retained.append(retained)
    output._possibly_audible = True
    output.set_library_root(registrations.second)
    assert not output.quiescent and output._retained == [retained] and output._possibly_audible


def test_off_worker_binds_library_without_discovering_or_starting_audio(registrations, monkeypatch):
    discovery = Mock(side_effect=AssertionError('Off must remain passive.'))
    monkeypatch.setattr(audio, 'configured_registry', discovery)
    worker = audio.NotificationWorker(library_root=registrations.first)
    try:
        worker.offer((progress.ProgressHub().start('Passive fixture').event,))
        assert worker._thread is None and worker._output._library_root == registrations.first
        worker.set_library_root(registrations.second)
        assert worker._output._library_root == registrations.second and worker._thread is None
    finally:
        worker.close()
    discovery.assert_not_called()


@contextmanager
def presenter(root, *, real_output=False):
    frame = SimpleNamespace(library=SimpleNamespace(root=root), messages=[])
    frame.report = frame.messages.append
    native = NativeBoundary()
    native.active = frame
    value = ui.OperationProgress(frame, Control(), Control(), wx_module=native,
        notifications=None if real_output else Notifications(), start_timer=False)
    try:
        yield value, native
    finally:
        value.close()


def test_presenter_rebinds_existing_output_to_new_library_without_releasing_owned_handles(registrations):
    with presenter(registrations.first, real_output=True) as (view, native):
        output = view.notifications._output
        retained = object()
        output._retained.append(retained)
        view.frame.library = SimpleNamespace(root=registrations.second)
        view.change_root()
        assert view.notifications._output is output and output._library_root == registrations.second
        assert output._registry is None and output._retained == [retained]
        assert not view.notifications.quiescent and view.library is view.frame.library


class VoiceModal(ModalBoundary):
    """Exercise the real settings callbacks with deterministic modal decisions."""
    def __init__(self, native, action, *, after_choice=None):
        super().__init__(native, action)
        self.choices = []
        self.after_choice = after_choice

    def control(self, parent, **kwargs):
        field = super().control(parent, **kwargs)
        field.Bind = lambda event, callback, **options: setattr(field, 'handler', callback)
        return field

    StaticText = TextCtrl = Choice = CheckBox = Button = control

    def SingleChoiceDialog(self, parent, message, title, choices):
        owner = self
        class Choice:
            def SetName(self, name): self.name = name
            def GetSelection(self): return 0
            def ShowModal(self):
                owner.choices.append((title, tuple(choices)))
                if owner.after_choice is not None:
                    owner.after_choice(len(owner.choices))
                return owner.ID_OK
            def Destroy(self): pass
        return Choice()


def pump(native, condition):
    deadline = monotonic() + 5
    while not condition():
        while native.posted:
            native.posted.pop(0)()
        if condition():
            break
        if monotonic() >= deadline:
            raise AssertionError('The actual background settings callback did not settle.')
        threading.Event().wait(.005)


def test_settings_discovery_uses_bound_library_and_saves_only_explicit_selected_voice(registrations, monkeypatch):
    item = registrations
    calls = []
    original = ui.configured_registry
    owner = threading.get_ident()
    def discover(root):
        calls.append((root, threading.get_ident()))
        return original(root)
    monkeypatch.setattr(ui, 'configured_registry', discover)
    with presenter(item.first) as (view, native):
        def choose(dialog):
            dialog.controls['Choose local progress voice'].handler(None)
            pump(native, lambda: 'Voice checked.' in dialog.controls['Progress settings status'].label)
            dialog.save(None)
        modal = view.wx = VoiceModal(native, choose)
        assert ui.show_progress_settings(view)
        assert len(calls) == 1 and calls[0][0] == item.first and calls[0][1] != owner
        assert len(modal.choices) == 2
        assert all('B registered Piper' not in row for row in modal.choices[0][1])
        assert view.preferences.voice_id.startswith('local-' + item.a.id + ':')
        assert view.preferences.voice_language == 'uk' and view.preferences.mode == 'off'
        assert progress.load_preferences(item.first) == view.preferences
        assert not (item.second / progress.FILENAME).exists()
    item.process.assert_not_called()


@pytest.mark.parametrize('after_choice', [1, 2])
def test_changed_library_during_native_voice_choices_cannot_publish_old_selection(registrations, after_choice):
    item = registrations
    with presenter(item.first) as (view, native):
        initial = view.preferences, view._preferences_sha256, view.notifications.registry
        def change(count):
            if count == after_choice:
                view.frame.library = SimpleNamespace(root=item.second)
        def choose(dialog):
            dialog.controls['Choose local progress voice'].handler(None)
            pump(native, lambda: len(modal.choices) == after_choice)
            dialog.save(None)
            assert 'library changed' in dialog.controls['Progress settings status'].label.lower()
        modal = view.wx = VoiceModal(native, choose, after_choice=change)
        assert not ui.show_progress_settings(view)
        assert len(modal.choices) == after_choice
        assert (view.preferences, view._preferences_sha256, view.notifications.registry) == initial
        assert not (item.first / progress.FILENAME).exists() and not (item.second / progress.FILENAME).exists()
    item.process.assert_not_called()


def test_closed_settings_discards_actual_delayed_discovery_without_saving(registrations, monkeypatch):
    item = registrations
    entered, release = threading.Event(), threading.Event()
    original = ui.configured_registry
    def discover(root):
        entered.set()
        assert release.wait(5)
        return original(root)
    monkeypatch.setattr(ui, 'configured_registry', discover)
    with presenter(item.first) as (view, native):
        initial = view.preferences, view._preferences_sha256
        def cancel(dialog):
            dialog.controls['Choose local progress voice'].handler(None)
            assert entered.wait(3)
        modal = view.wx = VoiceModal(native, cancel)
        try:
            assert not ui.show_progress_settings(view)
        finally:
            release.set()
            view._voice_executor.shutdown(wait=True, cancel_futures=True)
        while native.posted:
            native.posted.pop(0)()
        assert modal.choices == [] and (view.preferences, view._preferences_sha256) == initial
        assert not (item.first / progress.FILENAME).exists()
    item.process.assert_not_called()


def test_restored_progress_selection_cannot_activate_a_missing_machine_registration(registrations, tmp_path):
    item = registrations
    app = LibraryApp(item.first)
    app.create_workspace('Retained preferences')
    _, voice, fingerprint = selected(item.first)
    saved = progress.ProgressPreferences(mode='speech', voice_id=voice.id,
        voice_language='uk', voice_fingerprint=fingerprint)
    progress.save_preferences(app.root, saved)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    assert progress.load_preferences(restored) == saved
    assert runtime_settings.EngineProfileStore(restored).snapshot().profiles == ()
    output = audio.LocalNotificationOutput(library_root=restored)
    with pytest.raises(ValueError, match='unavailable'):
        output.speak('Do not infer an engine', volume=.2, cancelled=lambda: False,
                     selection=(voice.id, 'uk', fingerprint))
    assert output.quiescent
    item.process.assert_not_called()
