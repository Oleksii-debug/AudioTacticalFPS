"""Execute production voice/playback callbacks without claiming native UI input."""

import ast
from concurrent.futures import Future
from contextlib import contextmanager
from importlib.util import find_spec
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.progress_speech import activate_progress_voice, play_when_quiet
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.runtime import Voice, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume


def callbacks(filename, window_name, names, values, *, globals_=None, cleanup=False):
    """Retain the real nested callback bodies and their mutable closure cells."""
    source = Path(find_spec('carr_library.ui.' + Path(filename).stem).origin)
    module = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in module.body
                  if isinstance(node, ast.FunctionDef) and node.name == window_name)
    names = set(names)
    if filename == 'speech_dialog.py':
        names.update(('detach_reader', 'transfer_owner', 'release_owner'))
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

    def registry_factory(state_root, **_ignored):
        roots.append(Path(state_root))
        return registry

    def activate(root, voice_id, *, cancelled):
        activations.append((Path(root), voice_id))
        provider = activate_progress_voice(root, voice_id, cancelled=cancelled,
                                           _registry_factory=registry_factory)
        created.append(provider)
        return provider

    monkeypatch.setattr(runtime_settings, 'voice_registry', registry_factory)
    reports, choice = [], Choice()
    report = lambda message, focus=False: reports.append(str(message))
    parent = SimpleNamespace(library=SimpleNamespace(root=tmp_path), report=report)
    editor = Mock(value='')
    editor.GetValue.side_effect = lambda: editor.value
    editor.ChangeValue.side_effect = lambda value: setattr(editor, 'value', value)
    editor.GetLastPosition.side_effect = lambda: len(editor.value)
    editor.GetRange.side_effect = lambda start, end: editor.value[start:end]
    live = SimpleNamespace(voice=choice, _voices=registry.voices(), core=Mock(busy=False),
                           close=Mock(), stop=Mock(), rate=Mock(), volume=Mock())
    values = dict(parent=parent, progress=progress, voice_choice=choice,
        owned_registries=set(), ownership_lock=threading.RLock(),
        voices=registry.voices(), registry=registry, run=work, report=report,
        alive=True, busy=False, playback_generation=0, cancel=threading.Event(), player=Mock(),
        wx=SimpleNamespace(CallLater=scheduler, ID_CANCEL=1, NOT_FOUND=-1),
        document=SimpleNamespace(dirty=False, text=''), source_loaded=True,
        source_guard=lambda: None, recovered=None, recovery_notice='', editor=editor,
        source_loader=None, draft_store=None, save_path=None, last_audio=None,
        last_recovery_signature=None, rate=Mock(), volume=Mock(),
        source_status=Mock(), sync_document=lambda: None, save_project=lambda: True,
        dialog=Mock(), autosave_timer=Mock(), executor=Mock(), audio_files=Mock(),
        live=live, resources_closed=False, displayed={}, source_locations=(), native_id=4100)
    handlers = callbacks('speech_dialog.py', 'show_speech_reader',
        ['select_progress_voice', 'close_resources', 'closing', 'open_project',
         'recover_saved_draft', 'with_recovery_warning', 'display_document', 'restore_cursor'],
        values, globals_={'activate_progress_voice': activate, 'ReadingDocument': ReadingDocument,
                         'ReaderDraftStore': ReaderDraftStore,
                         'native_cursor_position': __import__('carr_library.ui.live_reading',
                             fromlist=['native_cursor_position']).native_cursor_position}, cleanup=True)
    yield SimpleNamespace(handlers=handlers, progress=progress, scheduler=scheduler,
        work=work, roots=roots, activations=activations, created=created, values=values,
        parent=parent, choice=choice, reports=reports, registry=registry)
    for provider in created:
        provider.close()


def test_reader_discovery_does_not_activate_progress_and_explicit_choice_uses_current_root(reader, tmp_path):
    from carr_library.ui.live_reading import LiveReadingControls
    live = reader.values['live']
    live.closed, live._report = False, Mock()
    LiveReadingControls.discover(live)
    live.core.discover_voices.assert_called_once_with()
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
def test_reader_stop_or_modal_cleanup_invalidates_already_deferred_playback(tmp_path, termination):
    import time
    from carr_library.reader.live_reading import DocumentSequence, LiveReader
    from carr_library.speech.reading import ReadingDocument
    voice = Voice('fixture:explicit', 'Explicit fixture', 'fixture', 'en')
    render, player = Mock(), Mock()
    player.available.return_value = True
    sequence = DocumentSequence(tmp_path, ReadingDocument('A deferred source sentence.'))
    core = LiveReader(sequence, discover=lambda **kwargs: (voice,), render=render,
                      player_factory=lambda: player, require_display_ack=True)
    try:
        core.discover_voices().result(5)
        future = core.start(1, voice.id)
        deadline = time.monotonic() + 5
        while core.status().section is None:
            assert time.monotonic() < deadline
            time.sleep(.01)
        section = core.status().section
        assert not future.done()
        (core.stop if termination == 'stop' else core.close)()
        core.acknowledge(section)  # A late native/display acknowledgement cannot start it.
        future.result(5)
        render.assert_not_called()
        player.play.assert_not_called()
        assert core.status().state in ('closed', 'stopped') and core.quiescent
    finally:
        completion = core.close()
        if completion is not None:
            completion.result(5)


@contextmanager
def pending_live(reader, tmp_path):
    """Real live worker waiting for the exact section's native acknowledgement."""
    import time
    from carr_library.reader.live_reading import DocumentSequence, LiveReader
    tmp_path.mkdir()
    voice = Voice('fixture:deferred', 'Deferred fixture', 'fixture', 'en')
    render = Mock(side_effect=RuntimeError('Synthetic render boundary reached.'))
    player = Mock()
    player.available.return_value = True
    player.status.return_value = {'state': 'closed'}
    core = LiveReader(DocumentSequence(tmp_path, ReadingDocument('Previous pending text.')),
        discover=lambda **kwargs: (voice,), render=render, player_factory=lambda: player,
        require_display_ack=True)
    live = reader.values['live']
    previous = live.core, live.stop
    live.core, live.stop = core, Mock(side_effect=core.stop)
    try:
        core.discover_voices().result(5)
        future = core.start(1, voice.id)
        deadline = time.monotonic() + 5
        while core.status().section is None:
            assert not future.done() and time.monotonic() < deadline, core.status()
            time.sleep(.01)
        section = core.status().section
        assert not future.done()
        render.assert_not_called()
        player.play.assert_not_called()
        yield SimpleNamespace(core=core, section=section, future=future,
                              render=render, player=player)
    finally:
        completion = core.close()
        if completion is not None:
            completion.result(5)
        live.core, live.stop = previous


@pytest.mark.parametrize('replacement', ['project', 'saved_draft'])
@pytest.mark.parametrize('accepted', [False, True])
def test_reader_document_replacement_invalidates_deferred_previous_audio(
        reader, tmp_path, monkeypatch, replacement, accepted):
    """Execute real handlers, durable drafts and the pending live-worker boundary."""
    document = ReadingDocument('Replacement original text.', title='Replacement reading')
    document.set_text('Saved replacement edits.')
    original = ReaderDraftStore(tmp_path, slot='saved-replacement')
    original.save(ReaderResume(document))
    before = original.path.read_bytes()
    with pending_live(reader, tmp_path / 'live') as pending:
        _replace_document(reader, tmp_path, monkeypatch, replacement, accepted, document, original, before, pending)


def _replace_document(reader, tmp_path, monkeypatch, replacement, accepted, document, original, before, pending):

    if replacement == 'project':
        path = tmp_path / 'replacement.reading.json'
        document.save(path)
        selected = reader.values['wx']
        selected.ID_OK, selected.FD_OPEN, selected.FD_FILE_MUST_EXIST = 2, 0, 0

        class SelectedProject:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def Destroy(self): pass
            def ShowModal(self): return selected.ID_OK if accepted else selected.ID_CANCEL
            def GetPath(self): return str(path)

        selected.FileDialog = SelectedProject
        reader.handlers.open_project()
    else:
        from carr_library.ui import research_actions
        chooser = Mock(return_value=0 if accepted else None)
        monkeypatch.setattr(research_actions, 'NativeDialogs',
                            lambda: SimpleNamespace(choose=chooser))
        reader.handlers.recover_saved_draft()
        assert len(reader.work.pending) == 1
        reader.work.finish()  # Real bounded discovery, then the explicit choice.
        if accepted:
            assert len(reader.work.pending) == 1
            reader.work.finish()  # Real independent recovery and replacement.
        chooser.assert_called_once()

    if accepted:
        reader.values['live'].stop.assert_called_once()
        reader.values['editor'].ChangeValue.assert_called_with('Saved replacement edits.')
    else:
        reader.values['live'].stop.assert_not_called()
        reader.values['editor'].ChangeValue.assert_not_called()
    pending.core.acknowledge(pending.section)
    pending.future.result(5)
    if accepted:
        pending.render.assert_not_called()
    else:
        pending.render.assert_called_once()
    pending.player.play.assert_not_called()
    assert pending.core.quiescent
    assert original.path.read_bytes() == before
    assert not any('Playback did not start' in report for report in reader.reports)


@pytest.mark.parametrize('binding', ['same', 'legacy_same', 'pre_release_same', 'unbound', 'different', 'pending'])
def test_source_bound_reader_open_project_preserves_lineage_before_replacing_playback(
        reader, tmp_path, binding):
    from carr_library.application import LibraryApp
    from carr_library.durable_state import encode
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range

    app = LibraryApp(tmp_path / 'sources')
    workspace = app.create_workspace('Current retained source')
    imported = app.add_text(workspace, 'Same title', 'Identical retained source text.')
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 1)
    current = source.reading_document()
    current.save(tmp_path / 'current.reading.json')
    if binding == 'unbound':
        replacement = ReadingDocument(current.source_text)
    elif binding == 'different':
        other_workspace = app.create_workspace('Different retained source')
        other = app.add_text(other_workspace, 'Same title', current.source_text)
        replacement = read_source_range(ReadingDesk(app, other_workspace, other['edition']), 1, 1).reading_document()
    else:
        replacement = source.reading_document()
    assert replacement.source_text == current.source_text
    replacement.set_text('Portable edited narration.')
    path = tmp_path / 'portable.reading.json'
    if binding == 'legacy_same':
        record = replacement.to_dict()
        record.pop('source_anchor')
        record['source_origin'] = replacement.source_origin
        path.write_bytes(encode(record))
    elif binding == 'pre_release_same':
        record = replacement.to_dict()
        anchor = record['source_anchor']
        anchor['schema'] = 'carr.narration-source.v1'
        anchor['derived_separators'] = []
        for position in anchor['positions']:
            position['text_verified_at_read'] = position.pop('text_verified_at_capture')
        path.write_bytes(encode(record))
    else:
        replacement.save(path)
    before = path.read_bytes()
    values = {**reader.values, 'document': current,
              'source_loader': lambda cancelled: source, 'source_loaded': binding != 'pending'}
    selected = values['wx']
    selected.ID_OK, selected.FD_OPEN, selected.FD_FILE_MUST_EXIST = 2, 0, 0

    class SelectedProject:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def Destroy(self): pass
        def ShowModal(self): return selected.ID_OK
        def GetPath(self): return str(path)

    selected.FileDialog = SelectedProject
    handlers = callbacks('speech_dialog.py', 'show_speech_reader',
        ['open_project', 'display_document'], values, globals_={'ReadingDocument': ReadingDocument})
    with pending_live(reader, tmp_path / 'live') as pending:
        _check_bound_replacement(reader, handlers, values, binding, current, path, before, pending)


def _check_bound_replacement(reader, handlers, values, binding, current, path, before, pending):
    handlers.open_project()
    accepted = binding in ('same', 'legacy_same', 'pre_release_same')
    if accepted:
        values['live'].stop.assert_called_once()
        values['editor'].ChangeValue.assert_called_with('Portable edited narration.')
        assert not any('standalone voice reader' in message for message in reader.reports)
    else:
        values['live'].stop.assert_not_called()
        values['editor'].ChangeValue.assert_not_called()
        assert any('standalone voice reader' in message for message in reader.reports)
    pending.core.acknowledge(pending.section)
    pending.future.result(5)
    if accepted:
        pending.render.assert_not_called()
    else:
        pending.render.assert_called_once()
    pending.player.play.assert_not_called()
    assert pending.core.quiescent
    assert current.text == current.source_text and path.read_bytes() == before
    if binding == 'pre_release_same':
        assert any('pre-release reading-project format' in message and 'Save As' in message
                   for message in reader.reports)


def test_studio_explicit_voice_selection_creates_independent_parent_selection(reader):
    events = []
    local, parent = Progress('studio', events), Progress('parent', events)
    reader.choice.selection = 1
    values = dict(voice_choice=reader.choice, voices=reader.registry.voices(),
        alive=True, cancel=threading.Event(),
        dialog=reader.parent, progress=local, parent_progress=parent, run=reader.work,
        report=lambda message, focus=False: reader.reports.append(message))
    handler = callbacks('studio_dialog.py', 'show_studio', ['select_progress_voice'],
        values, globals_={'activate_progress_voice': lambda root, voice_id, **options:
            activate_progress_voice(root, voice_id, **options,
                _registry_factory=lambda state, **_ignored: reader.registry)})
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
    dialog._playback_generation = 0
    dialog._playback_navigation = Mock()
    values = dict(current_job='completed-test-job', service=SimpleNamespace(
        get=lambda job: {'destination': str(tmp_path)}, close=Mock()), dialog=dialog,
        progress=local, parent_progress=parent, wx=SimpleNamespace(CallLater=scheduler, ID_CLOSE=3),
        report=lambda message, focus=False: reports.append(message), alive=True,
        autosave=lambda: None, cancel=threading.Event(), busy=False,
        timer=Mock(), executor=Mock(), cleanup_started=False, frame=Mock())
    handlers = callbacks('studio_dialog.py', 'show_studio', ['preview', 'close', 'dispose', 'cleanup', 'release_service'], values)
    handlers.preview()
    assert local.players == parent.players == [player]
    player.play.assert_not_called()
    local.ready = True
    scheduler.tick()
    player.play.assert_not_called()
    handlers.close()
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
    # PCM/device retention is covered against the real LiveReader in
    # test_live_reading; this host must still finish after a failed handoff.
    live = reader.values['live']
    live.close.side_effect = RuntimeError('Native stop and close failed')
    reader.handlers.cleanup()
    live.close.assert_called_once()
    reader.values['dialog'].Destroy.assert_called_once()
    reader.values['executor'].shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    assert any('Native stop and close failed' in message for message in reader.reports)


@pytest.mark.parametrize('stage', ['worker_shutdown', 'live_cleanup'])
def test_reader_dialog_is_destroyed_even_when_final_cleanup_raises(reader, stage):
    if stage == 'worker_shutdown':
        message = 'Worker shutdown unavailable'
        reader.values['executor'].shutdown.side_effect = RuntimeError(message)
    else:
        message = 'Live worker cleanup unavailable'
        reader.values['live'].close.side_effect = PermissionError(message)
    reader.handlers.cleanup()
    reader.values['dialog'].Destroy.assert_called_once()
    assert any(message in report for report in reader.reports)
    reader.values['live'].close.assert_called_once()


def test_reader_detached_parent_reporter_cannot_prevent_dialog_destruction(reader):
    reader.values['live'].close.side_effect = RuntimeError('Native close failed')
    reader.parent.report = Mock(side_effect=RuntimeError('Parent reporter detached'))
    reader.handlers.cleanup()
    reader.parent.report.assert_called_once()
    reader.values['dialog'].Destroy.assert_called_once()


def test_reader_detached_progress_observer_does_not_prevent_actual_resource_cleanup(reader):
    reader.progress.unregister_player = Mock(side_effect=RuntimeError('Progress observer detached'))
    reader.handlers.cleanup()
    reader.values['live'].close.assert_called_once()
    reader.values['executor'].shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    reader.values['dialog'].Destroy.assert_called_once()
    reader.progress.unregister_player.assert_not_called()  # Plain worker-completion lease, no late native observer.
    assert not hasattr(reader.parent, '_reader_playback_retained')


def test_reader_late_progress_voice_activation_is_closed_without_touching_destroyed_ui(reader):
    future, queued, completed = Future(), [], Mock()
    reader.values['executor'].submit.return_value = future
    reader.values['wx'] = SimpleNamespace(CallAfter=queued.append)
    handlers = callbacks('speech_dialog.py', 'show_speech_reader', ['run', 'close_resources'], reader.values)
    handlers.run(lambda: None, completed, discard=lambda provider: provider.close())
    handlers.close_resources()
    provider = activate_progress_voice(reader.parent.library.root, 'fixture:selected',
        _registry_factory=lambda state, **_ignored: reader.registry)
    future.set_result(provider)
    assert provider._closed.is_set() and queued == []
    completed.assert_not_called()


def test_reader_rewrapped_native_destruction_ignores_children_and_discards_late_activation(reader):
    future, queued, completed = Future(), [], Mock()
    reader.values['executor'].submit.return_value = future
    reader.values['wx'] = SimpleNamespace(CallAfter=queued.append)
    reader.values['dialog'].GetId.side_effect = AssertionError('The original native wrapper expired.')
    handlers = callbacks('speech_dialog.py', 'show_speech_reader',
                         ['run', 'close_resources', 'destroyed'], reader.values)
    handlers.run(lambda: None, completed, discard=lambda provider: provider.close())
    child = SimpleNamespace(GetId=lambda: 4101, GetEventObject=lambda: object(), Skip=Mock())
    handlers.destroyed(child)
    child.Skip.assert_called_once_with()
    reader.values['live'].close.assert_not_called()
    assert not reader.values['cancel'].is_set()
    owner = SimpleNamespace(GetId=lambda: 4100, GetEventObject=lambda: object(), Skip=Mock())
    handlers.destroyed(owner)
    owner.Skip.assert_called_once_with()
    assert reader.values['cancel'].is_set()
    reader.values['live'].close.assert_called_once_with()
    reader.values['autosave_timer'].Stop.assert_called_once_with()
    reader.values['executor'].shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    reader.values['dialog'].GetId.assert_not_called()
    provider = activate_progress_voice(reader.parent.library.root, 'fixture:selected',
        _registry_factory=lambda state, **_ignored: reader.registry)
    future.set_result(provider)
    assert provider._closed.is_set() and queued == []
    completed.assert_not_called()


def test_reader_pending_progress_voice_activation_uses_reader_cancellation(reader):
    reader.choice.selection = 1
    reader.handlers.select_progress_voice()
    assert reader.work.pending
    reader.values['cancel'].set()
    with pytest.raises(InterruptedError):
        reader.work.finish()
    assert not reader.created and reader.progress.provider is None
    assert not reader.roots


def test_reader_final_cleanup_releases_unapplied_progress_selection_even_without_gui_delivery(reader):
    selection = activate_progress_voice(reader.parent.library.root, 'fixture:selected',
        _registry_factory=lambda state_root, **options: reader.registry)
    reader.values['owned_registries'].add(selection)
    try:
        reader.handlers.cleanup()
        assert selection._closed.is_set()
        assert selection._ownership.finished.wait(5)
        assert not reader.registry._providers
        assert not reader.values['owned_registries']
        assert reader.progress.provider is None
    finally:
        selection.close()


@pytest.mark.parametrize('cleanup_failure', ['returns-false', 'raises', 'interrupted'])
def test_reader_rejected_progress_handoff_retains_actual_selection_until_retry(reader, cleanup_failure):
    reader.progress.reject_selection = True
    reader.handlers.select_progress_voice()
    work, accepted, _ = reader.work.pending.pop(0)
    selection = work()
    # The real run() completion registers this owned result before UI delivery;
    # the separate real-future cases exercise that registration boundary.
    reader.values['owned_registries'].add(selection)
    actual_close = selection.close
    attempts = []
    def failed_close():
        attempts.append(True)
        if cleanup_failure == 'raises':
            raise OSError('Selected progress worker cleanup is unconfirmed.')
        if cleanup_failure == 'interrupted':
            raise KeyboardInterrupt('Selected progress cleanup interrupted.')
        return False
    selection.close = failed_close
    try:
        with pytest.raises(ValueError, match='Library folder changed'):
            accepted(selection)
        assert attempts == [True]
        assert reader.values['owned_registries'] == {selection}
        assert reader.progress.provider is None and reader.progress.events == []
        assert not selection._closed.is_set() and reader.registry._providers
        selection.close = actual_close
        reader.handlers.cleanup()
        assert selection._closed.is_set() and selection._ownership.finished.wait(5)
        assert not reader.values['owned_registries'] and not reader.registry._providers
    finally:
        selection.close = actual_close
        selection.close()


def test_reader_accepted_progress_owner_survives_nested_settings_destruction(reader):
    reader.handlers.select_progress_voice()
    work, accepted, _ = reader.work.pending.pop(0)
    selection = work()
    reader.values['owned_registries'].add(selection)
    def settings_destroy_reader():
        reader.handlers.cleanup()
        raise RuntimeError('The settings modal lost its parent.')
    reader.progress.settings_dialog = settings_destroy_reader
    try:
        with pytest.raises(RuntimeError, match='settings modal'):
            accepted(selection)
        assert reader.progress.provider is selection
        assert not reader.values['owned_registries']
        assert not selection._closed.is_set()
        assert reader.registry.get('fixture:selected').id == 'fixture:selected'
    finally:
        selection.close()
        assert selection._ownership.finished.wait(5)
