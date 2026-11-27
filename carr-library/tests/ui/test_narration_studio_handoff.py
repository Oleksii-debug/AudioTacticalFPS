"""Actual reading/project persistence plus cancellable discovery dispatch.

Voices are explicit synthetic identities; these tests never render or play
audio, call a remote provider, or claim native screen-reader acceptance.
"""
from dataclasses import replace
from hashlib import sha256
from queue import Queue
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.narration import (
    NarrationStore, PronunciationRule, new_project, pronunciation_preview,
)
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech import Voice
from carr_library.studio.backup import backup_project, restore_project
from carr_library.ui.narration_editor import LocalVoiceDiscovery, NarrationController


VOICE = Voice('fixture:explicit-reader', 'fixture', 'Synthetic local voice', 'uk', ())


@pytest.fixture
def bound(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source-bound narration')
    imported = app.add_text(workspace, 'Original source', 'Original Є text.\r\nSecond line.',
                            authors=('Synthetic author',), language='uk')
    page = ReadingDesk(app, workspace, imported['edition']).page(1)
    project = new_project(page.raw_text, title='Reading derivative', source_location=page.location)
    controller = NarrationController(NarrationStore(app.root), project)
    return app, workspace, imported, controller


def test_handoff_saves_exact_pending_copy_and_provenance_without_render(bound, tmp_path):
    app, workspace, imported, controller = bound
    original = app.assets.resolve(imported['asset']).read_bytes()
    controller.set_text('Explicit derivative Є.\r\n  Keep whitespace.\n')
    controller.set_rules((PronunciationRule('derivative', 'spoken replacement'),))
    store = controller.prepare_audio_project(VOICE.id)
    audio = store.load()
    saved, digest = controller.store.load(controller.project.id)
    assert audio.scenes[0].lines[0].text == saved.current_text == controller.pending_text
    assert audio.authored_script == saved.current_text
    assert 'spoken replacement' not in audio.authored_script
    assert audio.roles[0].voice_id == VOICE.id
    assert audio.source_reference['reading_project_sha256'] == digest
    assert audio.source_reference['source_workspace'] == workspace
    assert audio.source_reference['source_edition'] == imported['edition']
    assert audio.source_reference['source_revision'] == saved.source_location.revision
    assert audio.source_reference['original_text_sha256'] == sha256(saved.original_text.encode()).hexdigest()
    assert audio.source_reference['handoff_text_sha256'] == sha256(saved.current_text.encode()).hexdigest()
    assert audio.source_reference['quotation_approval'].startswith('none;')
    assert not controller.dirty and not store.jobs()
    assert list(store.root.glob('*.wav')) == []
    assert app.assets.resolve(imported['asset']).read_bytes() == original
    assert app._selections == {}

    checksum = backup_project(store, tmp_path / 'audio-backup', include_audio=False)
    restored = restore_project(tmp_path / 'audio-backup', tmp_path / 'audio-restored',
                               expected_manifest_sha256=checksum)
    assert restored.load() == audio


def test_only_explicitly_applied_pronunciation_changes_transfer(bound):
    _, _, _, controller = bound
    controller.set_rules((PronunciationRule('Original', 'Pronounced'),))
    first = controller.prepare_audio_project(VOICE.id)
    preview, count = pronunciation_preview(controller.pending_text, controller.project.rules)
    assert count == 1
    controller.apply(preview, label='User-approved pronunciation preview')
    second = controller.prepare_audio_project(VOICE.id)
    assert first.root != second.root
    assert first.load().authored_script.startswith('Original')
    assert second.load().authored_script.startswith('Pronounced')
    assert controller.project.original_text.startswith('Original')


def test_handoff_keeps_exact_historical_source_after_metadata_revision(bound):
    app, workspace, imported, controller = bound
    historical = controller.project.source_location
    metadata = app.edition_metadata(workspace, imported['edition'])
    app.update_metadata(workspace, imported['edition'], title='Updated title',
                        authors=metadata.authors, language=metadata.language,
                        expected_metadata=metadata)
    current = ReadingDesk(app, workspace, imported['edition']).page(1).location
    assert current.revision != historical.revision
    audio = controller.prepare_audio_project(VOICE.id).load()
    assert audio.source_reference['source_revision'] == historical.revision


@pytest.mark.parametrize('damage', ('archive', 'original', 'forged-revision', 'forged-source', 'guard'))
def test_revoked_or_changed_source_cannot_create_audio_project(bound, damage):
    app, workspace, imported, controller = bound
    if damage == 'archive':
        app.archive_workspace(workspace)
    elif damage == 'original':
        app.assets.resolve(imported['asset']).write_bytes(b'Changed retained original')
    elif damage == 'forged-revision':
        controller.project = replace(controller.project,
            source_location=replace(controller.project.source_location, revision='unrelated'))
    elif damage == 'forged-source':
        controller.project = replace(controller.project,
            source_location=replace(controller.project.source_location, asset_sha256='0' * 64))
    else:
        controller.guard = lambda: (_ for _ in ()).throw(ValueError('Captured scope changed'))
    with pytest.raises((ValueError, OSError)):
        controller.prepare_audio_project(VOICE.id)
    assert not (controller.store.root / 'audio-projects').exists()
    assert controller.saved_digest is None


def test_failed_reading_save_cannot_create_audio_project(bound, monkeypatch):
    _, _, _, controller = bound
    controller.set_text('Unsaved reading changes')
    def fail(*args, **kwargs):
        raise OSError('Synthetic full disk')
    monkeypatch.setattr(controller.store, 'save', fail)
    with pytest.raises(OSError, match='full disk'):
        controller.prepare_audio_project(VOICE.id)
    assert controller.pending_text == 'Unsaved reading changes'
    assert not (controller.store.root / 'audio-projects').exists()


def test_independent_reading_copy_has_no_fabricated_library_source(tmp_path):
    root = tmp_path / 'independent'
    root.mkdir()
    controller = NarrationController(NarrationStore(root), new_project('Authored text.'))
    audio = controller.prepare_audio_project(VOICE.id).load()
    assert 'no authenticated library source' in audio.source_reference['source_status']
    assert 'source_workspace' not in audio.source_reference
    assert not (root / 'library.sqlite').exists()


@pytest.mark.parametrize('voice', ('', None))
def test_missing_voice_does_not_save_or_substitute_voice(bound, voice):
    _, _, _, controller = bound
    with pytest.raises(ValueError):
        controller.prepare_audio_project(voice)
    assert controller.saved_digest is None
    assert not (controller.store.root / 'audio-projects').exists()


def test_voice_discovery_runs_off_host_thread_and_dispatches_completion():
    callbacks = Queue()
    started, release = threading.Event(), threading.Event()
    threads, ready, failed = [], [], []
    main = threading.get_ident()
    def discover(*, cancelled):
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(3)
        assert not cancelled()
        return (VOICE,)
    job = LocalVoiceDiscovery(callbacks.put, discover=discover)
    try:
        job.start(ready.append, failed.append)
        assert started.wait(2) and job.busy and ready == failed == []
        assert threads == [threads[0]] and threads[0] != main
        with pytest.raises(ValueError, match='already running'):
            job.start(ready.append, failed.append)
        release.set()
        callback = callbacks.get(timeout=3)
        assert ready == []  # Worker never invokes GUI callbacks directly.
        callback()
        assert ready == [(VOICE,)] and failed == [] and not job.busy
    finally:
        release.set()
        job.close()


def test_cancelling_discovery_never_delivers_late_voice_selection():
    callbacks = Queue()
    started = threading.Event()
    ready, failed = [], []
    def discover(*, cancelled):
        started.set()
        while not cancelled():
            threading.Event().wait(.005)
        return (VOICE,)
    job = LocalVoiceDiscovery(callbacks.put, discover=discover)
    try:
        job.start(ready.append, failed.append)
        assert started.wait(2)
        job.cancel()
        callbacks.get(timeout=3)()
        assert ready == [] and len(failed) == 1
        assert isinstance(failed[0], InterruptedError) and not job.busy
    finally:
        job.close()


def test_closing_editor_before_queued_result_suppresses_all_ui_callbacks():
    callbacks = Queue()
    ready, failed = [], []
    job = LocalVoiceDiscovery(callbacks.put, discover=lambda **kwargs: (VOICE,))
    job.start(ready.append, failed.append)
    callback = callbacks.get(timeout=3)
    job.close()
    callback()
    assert ready == failed == []


def test_empty_voice_result_reports_absence_without_fake_fallback():
    callbacks = Queue()
    ready, failed = [], []
    job = LocalVoiceDiscovery(callbacks.put, discover=lambda **kwargs: ())
    try:
        job.start(ready.append, failed.append)
        callbacks.get(timeout=3)()
        assert ready == [] and len(failed) == 1
        assert 'No supported local voice' in str(failed[0])
    finally:
        job.close()


def test_public_voice_discovery_forwards_cancellation_to_existing_adapter(monkeypatch):
    import carr_library.speech.local as speech
    callback = lambda: True
    observed = []
    def entries(*, cancelled):
        observed.append(cancelled)
        raise speech.RenderCancelled('Synthetic cancellation')
    monkeypatch.setattr(speech, '_entries', entries)
    with pytest.raises(speech.RenderCancelled):
        speech.discover_voices(cancelled=callback)
    assert observed == [callback]


def test_unchanged_native_line_ending_display_preserves_exact_saved_copy(bound):
    _, _, _, controller = bound
    exact = controller.project.current_text
    assert '\r\n' in exact
    displayed = exact.replace('\r\n', '\n')
    controller.capture_text(displayed, rendered_value=displayed, rendered_text=exact)
    store = controller.prepare_audio_project(VOICE.id)
    assert controller.project.current_text == exact
    assert len(controller.project.revisions) == 1
    assert store.load().authored_script == exact


def test_actual_native_edit_is_saved_and_can_return_to_unchanged_display(bound):
    _, _, _, controller = bound
    exact = controller.project.current_text
    displayed = exact.replace('\r\n', '\n')
    controller.capture_text(displayed + '\nEdited sentence.',
                            rendered_value=displayed, rendered_text=exact)
    assert controller.pending_text == displayed + '\nEdited sentence.'
    controller.autosave()
    controller.capture_text(displayed, rendered_value=displayed, rendered_text=exact)
    store = controller.prepare_audio_project(VOICE.id)
    assert store.load().authored_script == exact
    assert len(controller.project.revisions) == 1
