"""Real source/job/backup boundaries for an ordered selection of Book plans."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.durable_state import atomic_json, read_json
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
    stop = threading.Event()
    e.voices.after_write = stop.set
    e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint, cancelled=stop.is_set)
    e.voices.after_write = None
    backup = tmp_path / 'backup'
    receipt = e.app.backup(backup)
    before = [e.app.assets.resolve(item['asset']).read_bytes() for item in e.imported]
    shutil.rmtree(e.app.root)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', expected_manifest_sha256=receipt))
    production = ProductionService(restored.root / 'state' / 'studio', voices=e.voices,
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options))
    books = BookAudioService(restored, e.workspace, production=production)
    groups = BookBatchService(books)
    try:
        assert groups.load(e.selected.id) == e.selected
        assert all(row['output_parent'] is None for row in groups.status(e.selected.id)['books'])
        assert not (restored.root / 'runtime' / 'book-batches').exists()
        assert [restored.assets.resolve(item['asset']).read_bytes() for item in e.imported] == before
        historical = {path: path.read_bytes() for path in production.jobs_root.glob('*/job.json')}
        with pytest.raises(ValueError, match='output parent'):
            groups.review(e.selected, [])
        outputs = (tmp_path / 'restored-first', tmp_path / 'restored-second')
        for output in outputs:
            output.mkdir()
        reviewed = groups.review(e.selected, outputs)
        with pytest.raises(ValueError, match='Prepare'):
            groups.run(e.selected.id, outputs, approval=reviewed.fingerprint)
        with pytest.raises(ValueError, match='new output review'):
            groups.prepare(e.selected, outputs, approval=reviewed.fingerprint)
        fresh, plans = groups.fresh(e.selected.id)
        assert [plan.source for plan in plans] == [plan.source for plan in e.plans]
        assert {plan.id for plan in plans}.isdisjoint(plan.id for plan in e.plans)
        review = groups.review(fresh, outputs, plans=plans)
        prepared = groups.prepare(fresh, outputs, plans=plans, approval=review.fingerprint)
        assert prepared.prepared
        with pytest.raises(ValueError, match='Prepare'):
            groups.run(fresh.id, outputs, approval=review.fingerprint)
        assert groups.run(fresh.id, outputs, approval=prepared.fingerprint)['status'] == 'complete'
        assert historical == {path: path.read_bytes() for path in historical}
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



@pytest.mark.parametrize('masked_stop', [False, True], ids=['direct-owner', 'interrupted-after-cancel'])
def test_batch_keeps_batch_book_and_scene_locks_during_pending_native_cleanup(
        batch, monkeypatch, masked_stop):
    from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners
    from carr_library.operations._locks import NativeLock

    e = batch
    _, ready = prepare(e)
    cancel = threading.Event()
    run = e.production.run
    if masked_stop:
        def interrupted(*args, **kwargs):
            try:
                return run(*args, **kwargs)
            except AudioCleanupPending as exc:
                cancel.set()
                raise InterruptedError('Modeled adapter cancellation while cleanup is pending') from exc
        monkeypatch.setattr(e.production, 'run', interrupted)
    original = tuple(e.app.assets.resolve(item['asset']).read_bytes() for item in e.imported)
    with _fixtures._uncertain_book_probe(e, monkeypatch) as (owners, release, selected):
        with pytest.raises((AudioCleanupPending, InterruptedError)) as caught:
            e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint,
                         cancelled=cancel.is_set)
        assert selected and selected[0].poll() == 0 and _pending_owners(caught.value)
        assert set(_pending_owners(caught.value)) == set(owners)
        first = e.production.get(e.plans[0].job_id(1))
        assert first['status'] == 'complete'
        accepted = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
                    for row in first['artifacts']}
        assert e.production.get(e.plans[1].job_id(1))['status'] == 'pending'
        assert len(e.voices.calls) == 2
        assert len(e.production.broker.snapshot()['leases']) == 1
        locks = (NativeLock(e.groups.root / ('.batch-' + e.selected.id + '.lock')),
                 NativeLock(e.books.plans / ('.book-' + e.plans[0].id + '.lock')),
                 NativeLock(e.production.jobs_root / e.plans[0].job_id(2) / '.render.lock'))
        assert all(lock.acquire() is False for lock in locks)
        cancel.clear()
        with pytest.raises(RuntimeError, match='already being changed'):
            e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
        assert len(e.voices.calls) == 2
        release.set()
        assert all(owner.close() for owner in owners)
        assert not e.production.broker.snapshot()['leases']
        for lock in locks:
            assert lock.acquire()
            lock.release()
        monkeypatch.setattr(e.production, 'run', run)
        complete = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
        assert complete['status'] == 'complete'
        assert len(e.voices.calls) == 4  # First accepted part is not synthesized again.
        assert accepted == {path: sha256(Path(path).read_bytes()).hexdigest() for path in accepted}
        assert original == tuple(e.app.assets.resolve(item['asset']).read_bytes() for item in e.imported)
        assert not e.production.broker.snapshot()['leases']


def test_nested_batch_stop_checks_poll_after_completed_control_read(batch, monkeypatch):
    """Real rendering keeps source guards; nested polls do not reread per entry."""
    from carr_library.studio import book_batch as batch_module

    e = batch
    _, ready = prepare(e)
    clock, controls, observed = [100.0], [], []
    read = batch_module.read_json
    def charged_read(path, *args, **kwargs):
        value = read(path, *args, **kwargs)
        if Path(path).name.endswith('.active.json'):
            controls.append(value['token'])
            # Model the observed slow control-file operation without sleeping
            # or changing clocks used by the actual native process supervisor.
            clock[0] += 1.0
        return value
    monkeypatch.setattr(batch_module, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(batch_module, 'read_json', charged_read)
    synthesize = e.voices.synthesize
    def checked_synthesis(text, voice_id, destination, *, settings, cancelled):
        before = len(controls)
        for _ in range(1000):
            assert cancelled() is False
        assert len(controls) == before
        clock[0] += batch_module.CONTROL_POLL_SECONDS / 2
        assert cancelled() is False and len(controls) == before
        clock[0] += batch_module.CONTROL_POLL_SECONDS
        assert cancelled() is False and len(controls) == before + 1
        for _ in range(32):
            assert cancelled() is False
        assert len(controls) == before + 1
        observed.append(True)
        return synthesize(text, voice_id, destination, settings=settings, cancelled=cancelled)
    monkeypatch.setattr(e.voices, 'synthesize', checked_synthesis)
    result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert result['status'] == 'complete' and observed == [True] * 3
    assert len(e.voices.calls) == 3
    assert len(set(controls)) == 1
    assert not e.production.broker.snapshot()['leases']


def test_in_memory_batch_cancel_is_immediate_inside_control_poll_window(batch, monkeypatch):
    from carr_library.studio import book_batch as batch_module

    e = batch
    _, ready = prepare(e)
    monkeypatch.setattr(batch_module, 'monotonic', lambda: 100.0)
    read, controls = batch_module.read_json, []
    def observed_read(path, *args, **kwargs):
        if Path(path).name.endswith('.active.json'):
            controls.append(Path(path))
        return read(path, *args, **kwargs)
    monkeypatch.setattr(batch_module, 'read_json', observed_read)
    stop, actual_pcm = threading.Event(), []
    synthesize = e.voices.synthesize
    def checked_synthesis(text, voice_id, destination, *, settings, cancelled):
        def stop_after_pcm():
            assert Path(destination).is_file() and Path(destination).stat().st_size > 44
            actual_pcm.append(True)
            before = len(controls)
            stop.set()
            assert cancelled() is True
            assert len(controls) == before  # No filesystem poll delays local withdrawal.
        e.voices.after_write = stop_after_pcm
        return synthesize(text, voice_id, destination, settings=settings, cancelled=cancelled)
    monkeypatch.setattr(e.voices, 'synthesize', checked_synthesis)
    result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint,
                          cancelled=stop.is_set, cancel_action=lambda: 'cancel')
    assert result['status'] == 'cancelled' and actual_pcm == [True]
    assert len(e.voices.calls) == 1
    assert e.production.get(e.plans[0].job_id(1))['status'] == 'cancelled'
    assert all(not list(output.iterdir()) for output in e.outputs)
    assert not e.production.broker.snapshot()['leases']


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_external_batch_signal_is_forced_before_publication_inside_poll_window(batch, monkeypatch, action):
    from carr_library.studio import book_batch as batch_module

    e = batch
    _, ready = prepare(e)
    # Poll time cannot advance: the real source/publication boundary must force
    # validation, independently of the periodic cross-process stop check.
    monkeypatch.setattr(batch_module, 'monotonic', lambda: 100.0)
    requests = []
    def after_actual_pcm():
        requests.append(e.groups.request_stop(e.selected.id, action))
    e.voices.after_write = after_actual_pcm
    result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert result['status'] == ('paused' if action == 'pause' else 'cancelled')
    assert len(requests) == len(e.voices.calls) == 1
    assert all(not list(output.iterdir()) for output in e.outputs)
    assert e.production.get(e.plans[1].job_id(1))['status'] == 'pending'
    assert not e.production.broker.snapshot()['leases']


@pytest.mark.parametrize('field', ['token', 'fingerprint'])
def test_changed_batch_control_is_not_cached_through_source_publication(batch, monkeypatch, field):
    from carr_library.studio import book_batch as batch_module

    e = batch
    _, ready = prepare(e)
    monkeypatch.setattr(batch_module, 'monotonic', lambda: 100.0)
    changed = []
    def after_actual_pcm():
        root, path = e.groups._controls(e.selected)
        value = read_json(path)
        value[field] = ('f' if value[field][0] != 'f' else 'e') * len(value[field])
        atomic_json(path, value, replace=True)
        changed.append(True)
    e.voices.after_write = after_actual_pcm
    with pytest.raises(ValueError, match='active book batch control identity changed'):
        e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert changed == [True] and len(e.voices.calls) == 1
    assert all(not list(output.iterdir()) for output in e.outputs)
    assert e.production.get(e.plans[1].job_id(1))['status'] == 'pending'
    assert not e.production.broker.snapshot()['leases']


def test_slow_completed_disk_accounting_is_not_repeated_by_every_native_poll(batch, monkeypatch):
    from carr_library.studio import render

    e = batch
    _, ready = prepare(e)
    clock, scans, observed = [100.0], [], []
    actual_time, retained_bytes = render.time, render._retained_bytes
    monkeypatch.setattr(render, 'time', SimpleNamespace(time=actual_time.time,
                                                       monotonic=lambda: clock[0]))
    def measured_scan(roots, cancelled):
        value = retained_bytes(roots, cancelled)
        scans.append(value)
        clock[0] += .5  # An actual completed scan taking longer than its poll interval.
        return value
    monkeypatch.setattr(render, '_retained_bytes', measured_scan)
    synthesize = e.voices.synthesize
    def checked_synthesis(text, voice_id, destination, *, settings, cancelled):
        before = len(scans)
        assert before
        for _ in range(1000):
            assert cancelled() is False
        assert len(scans) == before
        observed.append(True)
        return synthesize(text, voice_id, destination, settings=settings, cancelled=cancelled)
    monkeypatch.setattr(e.voices, 'synthesize', checked_synthesis)
    result = e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert result['status'] == 'complete' and observed == [True] * 3
    assert len(scans) > len(observed)  # Mandatory force checks still run.
    assert len(e.voices.calls) == 3
    assert not e.production.broker.snapshot()['leases']


def test_forced_disk_accounting_still_rejects_growth_inside_poll_window(batch, monkeypatch):
    from carr_library.audio import AudioError
    from carr_library.studio import render

    e = batch
    _, ready = prepare(e)
    actual_time = render.time
    monkeypatch.setattr(render, 'time', SimpleNamespace(time=actual_time.time,
                                                       monotonic=lambda: 100.0))
    e.production.total_disk_bytes = 2 * 1024 * 1024
    ballast = e.production.root / 'actual-retained-growth.bin'
    def after_actual_pcm():
        ballast.write_bytes(b'x' * e.production.total_disk_bytes)
    e.voices.after_write = after_actual_pcm
    with pytest.raises(AudioError, match='total production disk budget'):
        e.groups.run(e.selected.id, e.outputs, approval=ready.fingerprint)
    assert ballast.stat().st_size == e.production.total_disk_bytes
    assert len(e.voices.calls) == 1
    assert all(not list(output.iterdir()) for output in e.outputs)
    assert e.production.get(e.plans[1].job_id(1))['status'] == 'pending'
    assert not e.production.broker.snapshot()['leases']
