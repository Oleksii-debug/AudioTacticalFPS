"""Execute production voice/playback callbacks without claiming native UI input."""

import ast
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.progress_speech import activate_progress_voice, play_when_quiet
from carr_library.speech.runtime import Voice, VoiceRegistry


def callbacks(filename, window_name, names, values, *, globals_=None, cleanup=False):
    """Retain the real nested callback bodies and their mutable closure cells."""
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui' / filename
    module = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in module.body
                  if isinstance(node, ast.FunctionDef) and node.name == window_name)
    definitions = [node for node in window.body
                   if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == set(names)
    if cleanup:
        body = next(node.finalbody for node in reversed(window.body)
                    if isinstance(node, ast.Try) and node.finalbody)
        wrapper = ast.parse('def cleanup():\n    nonlocal alive\n').body[0]
        wrapper.body.extend(body)
        definitions.append(wrapper)
    result_names = [node.name for node in definitions]
    result = ', '.join(repr(name) + ': ' + name for name in result_names)
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result + '}\n')
    factory.body[0].body[:0] = definitions
    namespace = {'Path': Path, 'VoiceRegistry': VoiceRegistry,
                 'play_when_quiet': play_when_quiet, '__package__': 'carr_library.ui'}
    namespace.update(globals_ or {})
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), namespace)
    return SimpleNamespace(**namespace['build'](**values))


class Hold:
    def __init__(self, owner):
        self.owner, self.closed = owner, False

    @property
    def ready(self):
        return self.owner.ready

    def close(self):
        if not self.closed:
            self.closed = True
            self.owner.events.append(('release', self.owner.name))


class Progress:
    def __init__(self, name='main', events=None):
        self.name, self.events = name, events if events is not None else []
        self.ready, self.closed = False, False
        self.holds, self.players, self.selections = [], [], []
        self.preferences = SimpleNamespace(mode='off')
        self.provider = None
        self.reject_selection = False

    def hold_audio(self):
        hold = Hold(self)
        self.holds.append(hold)
        return hold

    def refresh_audio_state(self): self.events.append(('refresh', self.name))
    def register_player(self, player): self.players.append(player)
    def unregister_player(self, player, *, silence_confirmed=True):
        self.events.append(('unregister', self.name, silence_confirmed))
    def close(self): self.closed = True

    def set_speech_provider(self, provider, *, root):
        if self.reject_selection:
            raise ValueError('Library folder changed')
        self.provider = provider
        self.selections.append((provider.voice.id, Path(root)))
        self.events.append(('select', self.name))

    def settings_dialog(self):
        assert self.provider is not None, 'Settings opened before explicit selection'
        self.events.append(('settings', self.name))

    def reload_preferences(self): self.events.append(('reload', self.name))


class Scheduler:
    def __init__(self): self.pending = []
    def __call__(self, milliseconds, callback):
        assert milliseconds == 25
        self.pending.append(callback)
    def tick(self): self.pending.pop(0)()


class WorkQueue:
    def __init__(self): self.pending = []
    def __call__(self, work, done, **options): self.pending.append((work, done, options))
    def finish(self):
        work, done, _ = self.pending.pop(0)
        done(work())


class Choice:
    def __init__(self): self.selection, self.items = 0, []
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def Set(self, values): self.items = list(values)


class ListedVoices:
    name = 'purpose-written local test voices'

    def voices(self):
        return (Voice('fixture:first', 'First', 'fixture', 'en'),
                Voice('fixture:selected', 'Selected', 'fixture', 'uk'))


@pytest.fixture
def reader(tmp_path, monkeypatch):
    from carr_library import runtime_settings

    progress, scheduler, work = Progress(), Scheduler(), WorkQueue()
    registry, roots, activations, created = VoiceRegistry([ListedVoices()]), [], [], []

    def registry_factory(state_root, *, cancelled=lambda: False):
        assert callable(cancelled) and not cancelled()
        roots.append(Path(state_root))
        return registry

    def activate(root, voice_id):
        activations.append((Path(root), voice_id))
        provider = activate_progress_voice(root, voice_id, _registry_factory=registry_factory)
        created.append(provider)
        return provider

    monkeypatch.setattr(runtime_settings, 'voice_registry', registry_factory)
    reports, choice = [], Choice()
    report = lambda message, focus=False: reports.append(str(message))
    parent = SimpleNamespace(library=SimpleNamespace(root=tmp_path), report=report)
    values = dict(parent=parent, progress=progress, voice_choice=choice,
        voices=registry.voices(), registry=registry, run=work, report=report,
        alive=True, playback_generation=0, cancel=threading.Event(), player=Mock(),
        wx=SimpleNamespace(CallLater=scheduler, ID_CANCEL=1),
        document=SimpleNamespace(dirty=False, text=''), source_loaded=True,
        source_guard=lambda: None, draft_store=None, recovered=None, recovery_notice='', editor=Mock(),
        source_status=Mock(), sync_document=lambda: None, save_project=lambda: True,
        dialog=Mock(), autosave_timer=Mock(), executor=Mock(), audio_files=Mock(),
        unregister=Mock(), owned_registries=set())
    handlers = callbacks('speech_dialog.py', 'show_speech_reader',
        ['reload', 'select_progress_voice', 'start_playback', 'stop_audio', 'closing'],
        values, globals_={'activate_progress_voice': activate}, cleanup=True)
    yield SimpleNamespace(handlers=handlers, progress=progress, scheduler=scheduler,
        work=work, roots=roots, activations=activations, created=created, values=values,
        parent=parent, choice=choice, reports=reports, registry=registry)
    for provider in created:
        provider.close()


def test_reader_discovery_does_not_activate_progress_and_explicit_choice_uses_current_root(reader, tmp_path):
    reader.handlers.reload()
    reader.work.finish()
    assert reader.choice.items and reader.choice.selection == 0
    assert reader.activations == []
    assert reader.progress.provider is None and reader.progress.events == []

    current = tmp_path / 'current-library'
    current.mkdir()
    reader.parent.library.root = current
    reader.choice.selection = 1
    reader.handlers.select_progress_voice()
    assert reader.activations == [] and reader.progress.events == []
    reader.work.finish()
    assert reader.activations == [(current, 'fixture:selected')]
    assert reader.roots[-1] == current / 'state'
    assert reader.progress.selections == [('fixture:selected', current)]
    assert reader.progress.events == [('select', 'main'), ('settings', 'main')]
    assert reader.progress.preferences.mode == 'off'


@pytest.mark.parametrize('selection', [-1, 2])
def test_reader_invalid_voice_selection_never_runs_activation(reader, selection):
    reader.choice.selection = selection
    reader.handlers.select_progress_voice()
    assert reader.work.pending == [] and reader.activations == []
    assert reader.progress.provider is None


def test_reader_rejected_activation_closes_provider_without_opening_settings(reader):
    reader.progress.reject_selection = True
    reader.handlers.select_progress_voice()
    with pytest.raises(ValueError, match='Library folder changed'):
        reader.work.finish()
    assert len(reader.created) == 1 and reader.created[0]._closed.is_set()
    assert reader.progress.provider is None and reader.progress.events == []


@pytest.mark.parametrize('termination', ['stop', 'close'])
def test_reader_stop_or_modal_cleanup_invalidates_already_deferred_playback(reader, termination):
    started = Mock()
    reader.handlers.start_playback(started)
    assert reader.scheduler.pending and not reader.progress.holds[0].closed
    if termination == 'stop':
        reader.handlers.stop_audio()
        reader.values['player'].stop.assert_called_once()
    else:
        reader.handlers.closing(None)
        reader.values['dialog'].EndModal.assert_called_once_with(1)
        # Execute the real outer finally block when the simulated modal unwinds.
        reader.handlers.cleanup()
        reader.values['player'].close.assert_called_once()
    reader.progress.ready = True
    reader.scheduler.tick()
    started.assert_not_called()
    assert reader.scheduler.pending == []
    assert all(hold.closed for hold in reader.progress.holds)
    assert not any('Playback did not start' in report for report in reader.reports)


def test_studio_explicit_voice_selection_creates_independent_parent_selection(reader):
    events = []
    local, parent = Progress('studio', events), Progress('parent', events)
    reader.choice.selection = 1
    values = dict(voice_choice=reader.choice, voices=reader.registry.voices(),
        dialog=reader.parent, progress=local, parent_progress=parent, run=reader.work,
        report=lambda message, focus=False: reader.reports.append(message))
    handler = callbacks('studio_dialog.py', 'show_studio', ['select_progress_voice'],
        values, globals_={'activate_progress_voice': lambda root, voice_id:
            activate_progress_voice(root, voice_id, _registry_factory=lambda state: reader.registry)})
    assert events == []
    handler.select_progress_voice()
    assert events == []
    reader.work.finish()
    try:
        assert local.provider is not parent.provider
        assert local.selections == parent.selections == [('fixture:selected', reader.parent.library.root)]
        assert events == [('select', 'studio'), ('select', 'parent'),
                          ('settings', 'studio'), ('reload', 'parent')]
        assert local.preferences.mode == parent.preferences.mode == 'off'
    finally:
        local.provider.close()
        parent.provider.close()


def test_studio_preview_waits_for_both_controllers_and_close_invalidates_start(tmp_path):
    master = tmp_path / 'master.wav'
    master.write_bytes(b'Synthetic placeholder; playback is deliberately never admitted')
    scheduler, reports = Scheduler(), []
    local, parent, player = Progress('studio'), Progress('parent'), Mock()
    dialog = Mock()
    dialog._audio_player = player
    values = dict(current_job='completed-test-job', service=SimpleNamespace(
        get=lambda job: {'destination': str(tmp_path)}, close=Mock()), dialog=dialog,
        progress=local, parent_progress=parent, wx=SimpleNamespace(CallLater=scheduler, ID_CLOSE=3),
        report=lambda message, focus=False: reports.append(message), alive=True,
        autosave=lambda: None, cancel=threading.Event(), busy=False,
        timer=Mock(), executor=Mock())
    handlers = callbacks('studio_dialog.py', 'show_studio', ['preview', 'close'], values)
    handlers.preview()
    assert local.players == parent.players == [player]
    player.play.assert_not_called()
    local.ready = True
    scheduler.tick()
    player.play.assert_not_called()
    handlers.close()
    values['executor'].submit.assert_called_once_with(values['service'].close)
    values['service'].close.assert_not_called()
    parent.ready = True
    scheduler.tick()
    player.play.assert_not_called()
    player.close.assert_called_once()
    assert all(hold.closed for owner in (local, parent) for hold in owner.holds)
    assert scheduler.pending == []
    assert reports == []


def test_quiet_helper_deduplicates_controllers_then_refreshes_transport_before_release():
    events, scheduler = [], Scheduler()
    first, second = Progress('first', events), Progress('second', events)
    errors = []
    play_when_quiet((None, first, first, second), lambda: events.append(('start',)),
        schedule=scheduler, alive=lambda: True, on_error=errors.append)
    assert len(first.holds) == len(second.holds) == 1
    assert events == [] and len(scheduler.pending) == 1
    first.ready = True
    scheduler.tick()
    assert events == [] and len(scheduler.pending) == 1
    second.ready = True
    scheduler.tick()
    assert events == [('start',), ('refresh', 'first'), ('refresh', 'second'),
                      ('release', 'first'), ('release', 'second')]
    assert scheduler.pending == [] and errors == []


@pytest.mark.parametrize('reason', ['timeout', 'not_alive'])
def test_quiet_helper_releases_pending_holds_without_starting(reason):
    progress, scheduler, errors, started = Progress(), Scheduler(), [], Mock()
    now, alive = [0.0], [True]
    play_when_quiet((progress,), started, schedule=scheduler,
        alive=lambda: alive[0], on_error=errors.append, _clock=lambda: now[0])
    if reason == 'timeout':
        now[0] = 3.0
    else:
        alive[0] = False
    scheduler.tick()
    started.assert_not_called()
    assert progress.holds[0].closed and scheduler.pending == []
    assert len(errors) == (1 if reason == 'timeout' else 0)
    if errors:
        assert 'silence' in str(errors[0])


@pytest.mark.parametrize('stage', ['schedule', 'start'])
def test_quiet_helper_releases_every_hold_on_callback_failure(stage):
    first, second, errors = Progress('first'), Progress('second'), []
    started = Mock()

    def broken(*args):
        raise RuntimeError('Synthetic ' + stage + ' failure')

    scheduler = broken if stage == 'schedule' else Scheduler()
    if stage != 'schedule':
        first.ready = second.ready = True
    if stage == 'start':
        started.side_effect = broken
    play_when_quiet((first, second), started, schedule=scheduler,
        alive=lambda: True, on_error=errors.append)
    assert all(hold.closed for owner in (first, second) for hold in owner.holds)
    assert len(errors) == 1 and str(errors[0]) == 'Synthetic ' + stage + ' failure'
    assert started.call_count == (0 if stage == 'schedule' else 1)


def test_quiet_helper_rolls_back_an_earlier_hold_if_later_acquisition_fails():
    first, second, started = Progress('first'), Mock(), Mock()
    second.hold_audio.side_effect = RuntimeError('Second controller is closed')
    scheduler, errors = Scheduler(), []
    with pytest.raises(RuntimeError, match='Second controller is closed'):
        play_when_quiet((first, second), started, schedule=scheduler,
            alive=lambda: True, on_error=errors.append)
    assert first.holds[0].closed and scheduler.pending == []
    started.assert_not_called()
    assert errors == []


def test_refresh_failure_after_playback_starts_is_only_a_progress_diagnostic():
    first, second, errors, started = Progress('first'), Progress('second'), [], Mock()
    first.ready = second.ready = True

    def broken():
        raise RuntimeError('Detached progress label')

    first.refresh_audio_state = broken
    play_when_quiet((first, second), started, schedule=Scheduler(),
        alive=lambda: True, on_error=errors.append)
    started.assert_called_once()
    assert errors == []
    assert 'Detached progress label' in first.reporting_error
    assert ('refresh', 'second') in second.events
    assert all(hold.closed for owner in (first, second) for hold in owner.holds)


def test_release_failure_after_playback_starts_does_not_abandon_other_holds():
    first, second, errors, started = Progress('first'), Progress('second'), [], Mock()
    first.ready = second.ready = True
    failed_hold = Mock()
    failed_hold.ready = True
    failed_hold.close.side_effect = RuntimeError('Detached first hold')
    first.hold_audio = lambda: failed_hold
    play_when_quiet((first, second), started, schedule=Scheduler(),
        alive=lambda: True, on_error=errors.append)
    started.assert_called_once()
    assert errors == []
    assert 'Detached first hold' in first.reporting_error
    failed_hold.close.assert_called_once()
    assert second.holds[0].closed


def test_hold_acquisition_failure_preserves_original_error_and_releases_every_prior_hold():
    first, second, third = Progress('first'), Progress('second'), Mock()
    failed_hold = Mock()
    failed_hold.close.side_effect = RuntimeError('Earlier hold cleanup failed')
    first.hold_audio = lambda: failed_hold
    third.hold_audio.side_effect = RuntimeError('Third controller is closed')
    started, errors = Mock(), []
    with pytest.raises(RuntimeError, match='Third controller is closed'):
        play_when_quiet((first, second, third), started, schedule=Scheduler(),
            alive=lambda: True, on_error=errors.append)
    started.assert_not_called()
    assert errors == []
    failed_hold.close.assert_called_once()
    assert second.holds[0].closed
    assert 'Earlier hold cleanup failed' in first.reporting_error


def test_reader_failed_close_retains_its_pcm_and_still_destroys_dialog(reader):
    player, temporary = reader.values['player'], reader.values['audio_files']
    player.close.side_effect = RuntimeError('Native stop and close failed')
    reader.handlers.cleanup()
    temporary.cleanup.assert_not_called()
    reader.values['dialog'].Destroy.assert_called_once()
    assert ('unregister', 'main', False) in reader.progress.events
    assert (player, temporary) in reader.parent._reader_playback_retained
    assert any('Native stop and close failed' in message for message in reader.reports)


@pytest.mark.parametrize('stage', ['worker_shutdown', 'temporary_cleanup'])
def test_reader_dialog_is_destroyed_even_when_final_cleanup_raises(reader, stage):
    player, temporary = reader.values['player'], reader.values['audio_files']
    if stage == 'worker_shutdown':
        message = 'Worker shutdown unavailable'
        reader.values['executor'].shutdown.side_effect = RuntimeError(message)
    else:
        message = 'Temporary file cleanup denied'
        temporary.cleanup.side_effect = PermissionError(message)
    reader.handlers.cleanup()
    reader.values['dialog'].Destroy.assert_called_once()
    assert (player, temporary) in reader.parent._reader_playback_retained
    assert any(message in report for report in reader.reports)
    if stage == 'worker_shutdown':
        temporary.cleanup.assert_not_called()
        assert reader.parent._reader_engines_retained == list(reader.values['owned_registries'])
    else:
        temporary.cleanup.assert_called_once()


def test_reader_detached_parent_reporter_cannot_prevent_dialog_destruction(reader):
    player, temporary = reader.values['player'], reader.values['audio_files']
    player.close.side_effect = RuntimeError('Native close failed')
    reader.parent.report = Mock(side_effect=RuntimeError('Parent reporter detached'))
    reader.handlers.cleanup()
    reader.parent.report.assert_called_once()
    reader.values['dialog'].Destroy.assert_called_once()
    temporary.cleanup.assert_not_called()
    assert (player, temporary) in reader.parent._reader_playback_retained


def test_reader_detached_progress_observer_does_not_prevent_actual_resource_cleanup(reader):
    reader.progress.unregister_player = Mock(side_effect=RuntimeError('Progress observer detached'))
    reader.handlers.cleanup()
    reader.values['player'].close.assert_called_once()
    reader.values['executor'].shutdown.assert_called_once_with(wait=True, cancel_futures=True)
    reader.values['audio_files'].cleanup.assert_called_once()
    reader.values['dialog'].Destroy.assert_called_once()
    assert any('Progress observer detached' in message for message in reader.reports)
    assert not hasattr(reader.parent, '_reader_playback_retained')
