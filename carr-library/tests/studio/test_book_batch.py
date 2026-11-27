"""Real source/job/backup boundaries for an ordered selection of Book plans."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.durable_state import atomic_json, read_json, state_lock
from carr_library.library_backup import restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.studio.book_audio import BookAudioService, SplitOptions, capture_plan
from carr_library.studio.book_batch import BookBatch, BookBatchItem, BookBatchService, select_books, validate_plans
from carr_library.studio.project import Role
from carr_library.studio.render import ProductionService

_spec = importlib.util.spec_from_file_location('book_batch_tone_fixture', Path(__file__).with_name('test_book_audio.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


class TwoToneVoices(_fixtures.ToneVoices):
    """Two declared local fixture identities; these tones make no speech claim."""
    def __init__(self):
        super().__init__()
        self.calls = []

    def voices(self):
        return tuple(SimpleNamespace(id=identity, name=identity, language=language,
                                     engine='synthetic fixture', capabilities=('rate', 'volume'))
                     for identity, language in (('synthetic-one', 'en'), ('synthetic-two', 'zh')))

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        assert voice_id in ('synthetic-one', 'synthetic-two')
        self.calls.append((text, voice_id, settings.rate, settings.volume))
        super().synthesize(text, 'synthetic-tone', destination, settings=settings, cancelled=cancelled)


@pytest.fixture
def batch(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Ordered books')
    texts = ('First literal book.\n\nAnother exact paragraph.', '第二本。沒有空格！')
    imported = tuple(app.add_text(workspace, title, text) for title, text in
                     zip(('Original first book', 'Original second book'), texts))
    voices = TwoToneVoices()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1))
    production = ProductionService(app.root / 'state' / 'studio', voices=voices, broker=broker,
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options))
    books = BookAudioService(app, workspace, production=production)
    groups = BookBatchService(books)
    plans = tuple(capture_plan(app, workspace, source['edition'], options=SplitOptions('blank-lines'),
        role=Role('narrator', 'Narrator', voice_id, language, rate=rate, volume=volume), format=format)
        for source, voice_id, language, rate, volume, format in zip(imported,
            ('synthetic-one', 'synthetic-two'), ('en', 'zh'), (1, -1), (75, 55), ('wav', 'flac')))
    outputs = (tmp_path / 'first-output', tmp_path / 'second-output')
    for output in outputs:
        output.mkdir()
    selected = select_books(plans, title='Explicit two-book selection')
    yield SimpleNamespace(app=app, workspace=workspace, imported=imported, texts=texts,
        voices=voices, production=production, books=books, groups=groups, plans=plans,
        selected=selected, outputs=outputs)
    books.close()
    production.close()
    assert voices.closed == 0


def prepare(e):
    preview = e.groups.review(e.selected, e.outputs, plans=e.plans)
    ready = e.groups.prepare(e.selected, e.outputs, plans=e.plans, approval=preview.fingerprint)
    return preview, ready


def test_batch_preview_prepare_and_reopen_reuse_existing_jobs_without_speech(batch):
    e = batch
    preview = e.groups.review(e.selected, e.outputs, plans=e.plans)
    assert not preview.prepared and not e.production.jobs() and not e.groups.saved()
    assert e.voices.calls == []
    with pytest.raises(ValueError, match='Review'):
        e.groups.prepare(e.selected, e.outputs, plans=e.plans, approval='0' * 64)
    assert not e.production.jobs() and not e.books.saved()
    ready = e.groups.prepare(e.selected, e.outputs, plans=e.plans, approval=preview.fingerprint)
    assert ready.prepared and len(e.production.jobs()) == 3
    assert ready.books[0].destinations == preview.books[0].destinations
    assert ready.books[1].destinations == preview.books[1].destinations
    assert all(not list(output.iterdir()) for output in e.outputs) and e.voices.calls == []
    assert e.groups.load(e.selected.id) == e.selected
    raw = (e.groups.root / (e.selected.id + '.json')).read_text()
    assert 'output_parent' not in raw and 'status' not in raw
    assert all(str(output) not in raw for output in e.outputs)
    status = BookBatchService(e.books).status(e.selected.id)
    assert [row['voice']['voice_id'] for row in status['books']] == ['synthetic-one', 'synthetic-two']
    assert [row['format'] for row in status['books']] == ['wav', 'flac']
    assert [row['output_parent'] for row in status['books']] == list(map(str, e.outputs))
    assert {row['status'] for book in status['books'] for row in book['items']} == {'pending'}


def test_actual_two_book_render_and_reopened_resume_verify_original_outputs(batch):
    e = batch
    _, ready = prepare(e)
    progress = []
    result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint, progress=progress.append)
    assert result['status'] == 'complete'
    assert len(e.voices.calls) == 3
    assert [(row[1], row[2], row[3]) for row in e.voices.calls] == [
        ('synthetic-one', 1, 75), ('synthetic-one', 1, 75), ('synthetic-two', -1, 55)]
    assert ''.join(row[0] for row in e.voices.calls[:2]) == e.texts[0]
    assert e.voices.calls[2][0] == e.texts[1]
    before = {}
    for plan, reviewed in zip(e.plans, ready.books):
        for index, output in enumerate(reviewed.destinations, 1):
            state = e.production.get(plan.job_id(index))
            assert state['status'] == 'complete'
            assert read_json(Path(output) / 'book-source.json') == plan.provenance(index)
            for artifact in state['artifacts']:
                path = Path(artifact['path'])
                before[str(path)] = (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    assert any('Verified 3/3 saved parts' in message for message in progress)
    again = BookBatchService(e.books).run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert again == result and len(e.voices.calls) == 3
    assert before == {path: (sha256(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns)
                      for path in before}
    assert all(e.app.assets.resolve(item['asset']).read_bytes() == text.encode()
               for item, text in zip(e.imported, e.texts))


@pytest.mark.parametrize('change', ['order', 'voice', 'format', 'destination'])
def test_changed_review_cannot_reuse_preparation_approval(batch, change):
    e = batch
    old = e.groups.review(e.selected, e.outputs, plans=e.plans)
    plans, outputs = e.plans, e.outputs
    if change == 'order':
        plans = tuple(reversed(plans))
    elif change == 'voice':
        plans = (replace(plans[0], role=replace(plans[0].role, voice_id='synthetic-two')), plans[1])
    elif change == 'format':
        plans = (replace(plans[0], format='flac'), plans[1])
    else:
        outputs = tuple(reversed(outputs))
    selected = select_books(plans, title=e.selected.title) if change != 'destination' else e.selected
    with pytest.raises(ValueError, match='Review'):
        e.groups.prepare(selected, outputs, plans=plans, approval=old.fingerprint)
    assert e.voices.calls == [] and not e.production.jobs()


def test_scope_duplicate_and_missing_plan_bindings_are_rejected(batch):
    e = batch
    with pytest.raises(ValueError, match='distinct'):
        select_books((e.plans[0], e.plans[0]))
    wrong = replace(e.selected, workspace='another-library')
    with pytest.raises(ValueError, match='scope'):
        validate_plans(wrong, e.plans)
    wrong = replace(e.selected, items=(BookBatchItem(e.plans[0].id, '0' * 64), e.selected.items[1]))
    with pytest.raises(ValueError, match='changed'):
        validate_plans(wrong, e.plans)
    with pytest.raises(ValueError, match='changed'):
        validate_plans(e.selected, (e.plans[0], None))


def test_source_change_in_later_book_keeps_first_outputs_and_stops_before_second(batch):
    e = batch
    _, ready = prepare(e)
    changed = []
    def after_write():
        if not changed:
            changed.append(True)
            e.app.update_metadata(e.workspace, e.imported[1]['edition'], title='Changed after review')
    e.voices.after_write = after_write
    with pytest.raises(ValueError, match='saved reading revision is no longer active'):
        e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert len(e.voices.calls) == 2
    assert all(e.production.get(e.plans[0].job_id(i))['status'] == 'complete' for i in (1, 2))
    assert e.production.get(e.plans[1].job_id(1))['status'] == 'pending'
    assert not list(e.outputs[1].iterdir())


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_active_control_stops_current_item_and_never_starts_later_books(batch, action):
    e = batch
    _, ready = prepare(e)
    requests = []
    def stop_after_actual_pcm():
        if not requests:
            other = BookBatchService(BookAudioService(e.app, e.workspace, production=e.production))
            requests.append(other.request_stop(e.selected.id, action))
    e.voices.after_write = stop_after_actual_pcm
    stopped = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert stopped['status'] == ('paused' if action == 'pause' else 'cancelled')
    assert len(e.voices.calls) == 1 and not list(e.outputs[1].iterdir())
    assert e.production.get(e.plans[0].job_id(1))['status'] == stopped['status']
    assert not list((e.app.root / 'runtime' / 'book-batches').rglob('*.active.json'))
    e.voices.after_write = None
    resumed = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert resumed['status'] == 'complete'
    assert e.production.get(e.plans[1].job_id(1))['status'] == 'complete'


def test_pause_between_books_retains_verified_first_book_and_resume_does_not_duplicate(batch):
    e = batch
    _, ready = prepare(e)
    paused = threading.Event()
    def progress(message):
        if message.startswith('Verified 2/3'):
            paused.set()
    result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint,
        cancelled=paused.is_set, progress=progress)
    assert result['status'] == 'paused' and len(e.voices.calls) == 2
    assert not list(e.outputs[1].iterdir())
    first = [e.production.get(e.plans[0].job_id(index)) for index in (1, 2)]
    resumed = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert resumed['status'] == 'complete' and len(e.voices.calls) == 3
    assert [e.production.get(e.plans[0].job_id(index)) for index in (1, 2)] == first


def test_old_invocation_signal_cannot_stop_a_new_explicit_run(batch):
    e = batch
    _, ready = prepare(e)
    previous = []
    def pause():
        root, path = e.groups._controls(e.selected)
        previous.append(read_json(path))
        e.groups.request_stop(e.selected.id, 'pause')
    e.voices.after_write = pause
    assert e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)['status'] == 'paused'
    old = previous[0]
    def late_old_writer():
        root, path = e.groups._controls(e.selected)
        assert read_json(path)['token'] != old['token']
        atomic_json(root / (e.selected.id + '.' + old['token'] + '.control.json'),
                    dict(old, action='cancel'), replace=True)
    e.voices.after_write = late_old_writer
    assert e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)['status'] == 'complete'
    assert e.production.get(e.plans[1].job_id(1))['status'] == 'complete'


def test_observer_failure_does_not_relabel_or_repeat_completed_books(batch):
    e = batch
    _, ready = prepare(e)
    def broken(message):
        raise RuntimeError('Detached optional observer')
    result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint, progress=broken)
    assert result['status'] == 'complete' and len(e.voices.calls) == 3
    assert e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)['status'] == 'complete'
    assert len(e.voices.calls) == 3


def test_backup_restore_preserves_order_and_requires_new_explicit_destination_review(batch, tmp_path):
    e = batch
    _, ready = prepare(e)
    # Actual execution creates runtime control files; they grant no restored authority.
    stop = threading.Event()
    e.voices.after_write = stop.set
    e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint, cancelled=stop.is_set)
    e.voices.after_write = None
    backup = tmp_path / 'backup'
    receipt = e.app.backup(backup)
    original = e.app.root
    before = [e.app.assets.resolve(item['asset']).read_bytes() for item in e.imported]
    shutil.rmtree(original)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', receipt))
    production = ProductionService(restored.root / 'state' / 'studio', voices=e.voices,
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options))
    books = BookAudioService(restored, e.workspace, production=production)
    groups = BookBatchService(books)
    try:
        assert groups.load(e.selected.id) == e.selected
        assert all(row['output_parent'] is None for row in groups.status(e.selected.id)['books'])
        assert not (restored.root / 'runtime' / 'book-batches').exists()
        assert [restored.assets.resolve(item['asset']).read_bytes() for item in e.imported] == before
        with pytest.raises(ValueError, match='output parent'):
            groups.review(e.selected, [])
        fresh = (tmp_path / 'restored-first', tmp_path / 'restored-second')
        for output in fresh:
            output.mkdir()
        reviewed = groups.review(e.selected, fresh)
        with pytest.raises(ValueError, match='Prepare'):
            groups.run(e.selected.id, fresh, approval=reviewed.fingerprint)
        prepared = groups.prepare(e.selected, fresh, approval=reviewed.fingerprint)
        assert prepared.prepared and all('-restored-' in Path(path).name
            for review in prepared.books for path in review.destinations)
        with pytest.raises(ValueError, match='Prepare'):
            groups.run(e.selected.id, fresh, approval=reviewed.fingerprint)
        assert groups.run(e.selected.id, fresh, approval=prepared.fingerprint)['status'] == 'complete'
    finally:
        books.close()
        production.close()


@pytest.mark.parametrize('change', ['missing', 'fingerprint', 'scope'])
def test_passive_backup_refuses_unbound_batch_membership(batch, tmp_path, change):
    e = batch
    prepare(e)
    path = e.groups.root / (e.selected.id + '.json')
    if change == 'missing':
        (e.books.plans / (e.plans[1].id + '.json')).unlink()
    else:
        selected = (replace(e.selected, items=(replace(e.selected.items[0], plan_sha256='0' * 64),
                                             e.selected.items[1]))
                    if change == 'fingerprint' else replace(e.selected, workspace='unrelated'))
        atomic_json(path, {'batch': selected.to_dict(), 'fingerprint': selected.fingerprint}, replace=True)
    with pytest.raises(ValueError):
        e.app.backup(tmp_path / 'invalid-backup')
    assert not (tmp_path / 'invalid-backup' / 'manifest.json').exists()
    assert e.voices.calls == []



def test_plan_save_then_prepare_uses_one_lock_at_a_time(batch):
    e = batch
    # The real non-reentrant FileLock would fail immediately if Prepare called
    # save() while already holding the same book lock.
    assert e.books.save(e.plans[0]) == e.plans[0]
    before = (e.books.plans / (e.plans[0].id + '.json')).read_bytes()
    review = e.books.review(e.plans[0], e.outputs[0])
    assert e.books.prepare(e.plans[0], e.outputs[0], approval=review.fingerprint).prepared
    assert (e.books.plans / (e.plans[0].id + '.json')).read_bytes() == before
    assert not e.voices.calls


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_stop_between_books_does_not_signal_a_foreign_locked_job(batch, action):
    e = batch
    _, ready = prepare(e)
    foreign_id = e.plans[1].job_id(1)
    foreign_root = e.production.jobs_root / foreign_id
    before = (foreign_root / 'job.json').read_bytes()
    requested = []
    def progress(message):
        if message.startswith('Verified 2/3') and not requested:
            requested.append(e.groups.request_stop(e.selected.id, action))
    # This is the real native lock another standalone/batch owner would hold.
    with state_lock(foreign_root, 'render'):
        result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint, progress=progress)
        assert result['status'] == ('paused' if action == 'pause' else 'cancelled')
        assert not (foreign_root / 'control.json').exists()
        assert (foreign_root / 'job.json').read_bytes() == before
        with pytest.raises(RuntimeError, match='already being changed'):
            with state_lock(foreign_root, 'render'):
                pytest.fail('Foreign native lease was released.')
    assert len(e.voices.calls) == 2 and not tuple(e.outputs[1].iterdir())


def test_failure_in_second_part_preserves_actual_first_part_receipt_and_resume(batch, monkeypatch):
    e = batch
    _, ready = prepare(e)
    actual = e.production.run
    def fail_second(identity, **kwargs):
        if identity == e.plans[0].job_id(2):
            raise ValueError('Concrete next-part fixture failure.')
        return actual(identity, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(e.production, 'run', fail_second)
        with pytest.raises(ValueError, match='next-part fixture') as failed:
            e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    partial = failed.value.book_batch_result
    assert partial['status'] == 'failed' and len(partial['books']) == 1
    first = partial['books'][0]['items']
    assert len(first) == 1 and first[0]['job'] == e.plans[0].job_id(1) and first[0]['status'] == 'complete'
    assert failed.value.book_audio_result['items'] == tuple(first)
    master = Path(first[0]['destination']) / 'master.wav'
    before = (sha256(master.read_bytes()).hexdigest(), master.stat().st_mtime_ns)
    assert len(e.voices.calls) == 1 and not tuple(e.outputs[1].iterdir())
    assert e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)['status'] == 'complete'
    assert len(e.voices.calls) == 3
    assert (sha256(master.read_bytes()).hexdigest(), master.stat().st_mtime_ns) == before


def test_postcommit_control_cleanup_failure_retains_results_and_does_not_repeat_audio(batch, monkeypatch):
    e = batch
    _, ready = prepare(e)
    actual = e.groups._cleanup_control
    def failed_cleanup(*args):
        raise OSError('Control removal denied.')
    with monkeypatch.context() as patch:
        patch.setattr(e.groups, '_cleanup_control', failed_cleanup)
        result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert result['status'] == 'complete' and result['control_cleanup_pending'] is True
    assert all(item['status'] == 'complete' for row in result['books'] for item in row['items'])
    assert len(e.voices.calls) == 3
    root, active = e.groups._controls(e.selected)
    assert active.exists()
    again = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert again['status'] == 'complete' and len(e.voices.calls) == 3 and not active.exists()


def test_primary_failure_is_not_masked_by_control_cleanup_failure(batch, monkeypatch):
    e = batch
    _, ready = prepare(e)
    failure = ValueError('Actual render boundary error.')
    def fail_render(*args, **kwargs):
        raise failure
    def fail_cleanup(*args):
        raise OSError('Control cleanup error.')
    monkeypatch.setattr(e.production, 'run', fail_render)
    monkeypatch.setattr(e.groups, '_cleanup_control', fail_cleanup)
    with pytest.raises(ValueError, match='render boundary') as observed:
        e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert observed.value is failure
    assert observed.value.book_batch_result['control_cleanup_pending'] is True
    assert observed.value.book_batch_result['books'][0]['items'] == []
    assert not e.voices.calls


def test_selection_postcommit_sync_warning_requires_real_confirmation_before_jobs(batch, monkeypatch):
    from carr_library import durable_state
    from carr_library.studio.book_batch import BookBatchSelectionCommitted
    e = batch
    preview = e.groups.review(e.selected, e.outputs, plans=e.plans)
    real_sync = durable_state.sync_directory
    visible = e.groups.root / (e.selected.id + '.json')
    def fail_visible(directory):
        if Path(directory) == e.groups.root and visible.exists():
            raise OSError('Actual directory fsync failure after visible selection.')
        return real_sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(durable_state, 'sync_directory', fail_visible)
        with pytest.raises(BookBatchSelectionCommitted) as warning:
            e.groups.prepare(e.selected, e.outputs, plans=e.plans, approval=preview.fingerprint)
    assert warning.value.selection_saved and warning.value.durable is False
    before = visible.read_bytes()
    assert e.groups.load(e.selected.id) == e.selected and not e.production.jobs()
    syncs = []
    from carr_library.studio import book_batch as module
    def confirm(directory):
        syncs.append(Path(directory))
        return real_sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'sync_directory', confirm)
        assert e.groups.prepare(e.selected, e.outputs, plans=e.plans, approval=preview.fingerprint).prepared
    assert e.groups.root in syncs and visible.read_bytes() == before and not e.voices.calls


def test_competing_same_bytes_selection_is_not_adopted_as_our_commit(batch, monkeypatch):
    from carr_library.studio import book_batch as module
    e = batch
    preview = e.groups.review(e.selected, e.outputs, plans=e.plans)
    real = module.atomic_json
    competed = []
    def racing(path, value, **kwargs):
        if Path(path).name == e.selected.id + '.json' and not competed:
            competed.append(real(path, value, **kwargs))
        return real(path, value, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'atomic_json', racing)
        with pytest.raises(FileExistsError):
            e.groups.prepare(e.selected, e.outputs, plans=e.plans, approval=preview.fingerprint)
    assert e.groups.load(e.selected.id) == e.selected
    assert not e.production.jobs() and not e.voices.calls


def test_same_path_library_replacement_invalidates_existing_batch_service_before_writes(batch):
    e = batch
    preview = e.groups.review(e.selected, e.outputs, plans=e.plans)
    moved = e.app.root.with_name('original-library-moved')
    e.app.root.rename(moved)
    e.app.root.mkdir()
    marker = e.app.root / 'sentinel'
    marker.write_bytes(b'replacement owner')
    try:
        with pytest.raises(ValueError, match='replaced'):
            e.groups.prepare(e.selected, e.outputs, plans=e.plans, approval=preview.fingerprint)
        assert tuple(e.app.root.iterdir()) == (marker,) and marker.read_bytes() == b'replacement owner'
        assert not e.voices.calls
    finally:
        shutil.rmtree(e.app.root)
        moved.rename(e.app.root)


def test_saved_batch_inventory_never_silently_omits_unreadable_directory(batch, monkeypatch):
    from carr_library.studio import book_batch as module
    e = batch
    prepare(e)
    real = os.scandir
    def denied(path):
        if Path(path) == e.groups.root:
            raise PermissionError('Selected batch folder cannot be read.')
        return real(path)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'scandir', denied)
        with pytest.raises(PermissionError):
            e.groups.saved()
    assert e.groups.saved() == (e.selected,)
