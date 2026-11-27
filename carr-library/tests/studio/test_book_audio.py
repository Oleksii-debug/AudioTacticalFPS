"""Canonical source and Studio batch integration; synthetic tones are not speech quality evidence."""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import runpy
import shutil
import struct
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.durable_state import atomic_json, encode
from carr_library.library_backup import restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.speech.reading import ReadingDocument
from carr_library.studio.book_audio import (
    BookAudioService, BookPlan, SplitOptions, SOURCE_PREFIX, capture_plan, split_parts)
from carr_library.studio.project import Role
from carr_library.studio.render import ProductionService


class ToneVoices:
    """A deterministic local protocol fixture, never a voice-quality substitute."""
    def __init__(self):
        self.received, self.closed, self.after_write = [], 0, None

    def voices(self):
        return (SimpleNamespace(id='synthetic-tone', name='Synthetic fixture tone',
                                language='en', engine='test fixture'),)

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        assert voice_id == 'synthetic-tone' and not cancelled()
        self.received.append(text)
        with wave.open(str(destination), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(
                2 * math.pi * 440 * index / 24000))) for index in range(4800)))
        if self.after_write is not None:
            self.after_write()

    def close(self):
        self.closed += 1


@pytest.fixture
def book(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Book audio source')
    topic = app.create_topic(workspace, 'Selected book topic')
    original = '[Music: suspense]\nNarrator: Keep these literal words.\n\nSecond part!'
    imported = app.add_text(workspace, 'Original source title', original)
    voices = ToneVoices()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1))
    production = ProductionService(app.root / 'state' / 'studio', voices=voices, broker=broker,
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options))
    service = BookAudioService(app, workspace, production=production)
    output = tmp_path / 'explicit-output'
    output.mkdir()
    yield SimpleNamespace(app=app, workspace=workspace, topic=topic, original=original,
        imported=imported, voices=voices, production=production, service=service, output=output)
    service.close()
    production.close()
    assert voices.closed == 0  # Both services borrowed this registry.


def plan(e, mode='blank-lines', **options):
    return capture_plan(e.app, e.workspace, e.imported['edition'],
        topic=e.topic, options=SplitOptions(mode, **options),
        role=Role('narrator', 'Narrator', 'synthetic-tone', 'en'))


def prepared(e, candidate=None):
    candidate = candidate or plan(e)
    review = e.service.review(candidate, e.output)
    ready = e.service.prepare(candidate, e.output, approval=review.fingerprint)
    assert ready.prepared
    return candidate, ready


def require_audio():
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the reviewed FFmpeg/ffprobe test runtime.'


@pytest.mark.parametrize('mode,options', [
    ('document', {}), ('paragraphs', {}), ('blank-lines', {}), ('sentences', {}),
    ('characters', {'characters': 19}), ('regex', {'pattern': r'\n\n'}),
    ('regex', {'pattern': r'(?=Second)'}),
])
def test_literal_parts_cover_exact_reading_text_and_keep_original_metadata(book, mode, options):
    e = book
    candidate = plan(e, mode, **options)
    source = candidate.source
    assert ''.join(candidate.project(index).original_script
                   for index in range(1, len(candidate.parts) + 1)) == source['text']
    assert source['text'] == e.original
    assert source['source_metadata']['title'] == 'Original source title'
    assert source['source_anchor']['quotation_authority'] is False
    assert ReadingDocument(source['text'], source_anchor=source['source_anchor']).source_text == e.original
    for index in range(1, len(candidate.parts) + 1):
        project = candidate.project(index)
        assert project.mode == 'faithful' and project.original_script
        assert all(scene.music is None and not scene.cues for scene in project.scenes)
        assert ''.join(line.text for scene in project.scenes for line in scene.lines) == project.original_script
        assert all(line.source_ref.startswith(SOURCE_PREFIX + candidate.id + ':' + candidate.fingerprint)
                   for scene in project.scenes for line in scene.lines)
    assert e.app.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode()
    assert not e.production.jobs() and e.voices.received == []
    assert BookPlan.from_dict(candidate.to_dict()) == candidate


def test_real_epub_chapters_keep_spine_order_and_unverified_anchor(book, tmp_path):
    e = book
    factory = runpy.run_path(str(Path(__file__).parents[1] / 'reader' / 'test_ebook_reader.py'))['book']
    source = tmp_path / 'chapters.epub'
    factory(source)
    imported = e.app.import_file(e.workspace, source, title='Actual EPUB chapters')
    assert imported['state'] == 'complete', imported
    candidate = capture_plan(e.app, e.workspace, imported['edition'], topic=e.topic,
        options=SplitOptions('chapters'), role=Role('narrator', 'Narrator', 'synthetic-tone', 'en'))
    assert len(candidate.parts) == 2
    assert 'Railway research.' in candidate.project(1).original_script
    assert 'Archive context.' in candidate.project(2).original_script
    assert candidate.source['source_anchor']['quotation_authority'] is False
    assert all(not row['text_verified_at_capture'] for row in candidate.source['source_anchor']['positions'])
    expected_asset = sha256(source.read_bytes()).hexdigest()
    assert candidate.source['source_anchor']['asset_sha256'] == expected_asset
    assert e.app.assets.resolve(expected_asset).read_bytes() == source.read_bytes()


def test_physical_page_split_uses_actual_pdf_pages_and_refuses_txt_segments(book, tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    e = book
    source = tmp_path / 'pages.pdf'
    writer = Canvas(str(source))
    writer.drawString(72, 700, 'First physical source page.')
    writer.showPage()
    writer.drawString(72, 700, 'Second physical source page.')
    writer.save()
    imported = e.app.import_file(e.workspace, source, title='Physical source')
    assert imported['state'] == 'complete', imported
    candidate = capture_plan(e.app, e.workspace, imported['edition'], options=SplitOptions('pages'),
                             role=Role('narrator', 'Narrator', 'synthetic-tone', 'en'))
    assert [part.label for part in candidate.parts] == ['Page 1', 'Page 2']
    assert 'First physical' in candidate.project(1).original_script
    assert 'Second physical' in candidate.project(2).original_script
    with pytest.raises(ValueError, match='logical segments'):
        plan(e, 'pages')
    with pytest.raises(ValueError):
        plan(e, 'chapters')


@pytest.mark.parametrize('template', ['../{index}', '{book.__class__}-{index}', '{index!r}',
                                      '{index:010d}', '{book}', '{index}\\outside'])
def test_naming_cannot_become_path_or_object_access(template):
    with pytest.raises(ValueError):
        SplitOptions(name_template=template)


def test_split_boundaries_do_not_drop_blank_or_no_space_language_text(book):
    e = book
    source = plan(e).source
    value = '  第一段。\n\n第二段！  \n\n'
    source['text'] = value
    parts = split_parts(source, SplitOptions('sentences'))
    assert ''.join(value[part.start:part.end] for part in parts) == value
    assert len(parts) == 2
    assert all(value[part.start:part.end].strip() for part in parts)
    with pytest.raises(ValueError, match='256'):
        split_parts(dict(source, text='word ' * 300), SplitOptions('characters', characters=5))
    with pytest.raises(ValueError, match='duration'):
        SplitOptions('estimated-duration')


def test_pathological_user_regex_is_stopped_by_actual_existing_child(book):
    source = plan(book).source
    source['text'] = 'a' * 30000 + '!'
    with pytest.raises(TimeoutError):
        split_parts(source, SplitOptions('regex', pattern=r'(a+)+$'))
    assert book.voices.received == [] and not book.production.jobs()


def test_review_prepare_and_saved_plan_never_start_speech_and_names_are_exact(book):
    e = book
    candidate = plan(e)
    reviewed = e.service.review(candidate, e.output)
    assert not reviewed.prepared
    assert e.service.saved() == () and e.voices.received == []
    with pytest.raises(ValueError, match='Review'):
        e.service.prepare(candidate, e.output, approval='0' * 64)
    assert not e.production.jobs()
    ready = e.service.prepare(candidate, e.output, approval=reviewed.fingerprint)
    assert ready.prepared and ready.destinations == reviewed.destinations
    assert e.service.load(candidate.id) == candidate and e.service.saved() == (candidate,)
    assert len(e.production.jobs()) == 2
    assert [e.production.get(candidate.job_id(i))['destination'] for i in (1, 2)] == list(ready.destinations)
    assert all(Path(name).parent == e.output for name in ready.destinations)
    assert [Path(name).name for name in ready.destinations] == [
        f'{index:04d}---Original-source-title---Part-{index}-{candidate.job_id(index)[:12]}'
        for index in (1, 2)]
    assert not list(e.output.iterdir()) and e.voices.received == []
    again = e.service.prepare(candidate, e.output, approval=ready.fingerprint)
    assert again == ready and len(e.production.jobs()) == 2


@pytest.mark.parametrize('change', ['revision', 'workspace', 'topic', 'original'])
def test_stale_library_source_cannot_start_prepared_items(book, change):
    e = book
    candidate, ready = prepared(e)
    before = {state['id']: (e.production.jobs_root / state['id'] / 'job.json').read_bytes()
              for state in e.production.jobs()}
    if change == 'revision':
        e.app.update_metadata(e.workspace, e.imported['edition'], title='Changed metadata')
    elif change == 'workspace':
        e.app.archive_workspace(e.workspace)
    elif change == 'topic':
        with e.app.topic_transaction(e.workspace) as tree:
            tree.archive(e.topic)
    else:
        e.app.assets.resolve(e.imported['asset']).write_bytes(b'Changed original')
    with pytest.raises((ValueError, PermissionError)):
        e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert e.voices.received == [] and not list(e.output.iterdir())
    assert before == {identity: (e.production.jobs_root / identity / 'job.json').read_bytes()
                      for identity in before}


def test_standalone_studio_cannot_bypass_book_source_guard(book):
    e = book
    candidate, _ = prepared(e)
    with pytest.raises(ValueError, match='Book audio'):
        e.production.run(candidate.job_id(1))
    assert e.voices.received == [] and not list(e.output.iterdir())


def test_actual_render_resume_verifies_completed_items_without_new_synthesis(book):
    require_audio()
    e = book
    candidate, ready = prepared(e)
    result = e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert [row['status'] for row in result] == ['complete', 'complete']
    assert e.voices.received == [candidate.project(i).original_script for i in (1, 2)]
    before = {}
    for index, row in enumerate(result, 1):
        state = e.production.get(row['job'])
        source = json.loads(Path(row['destination'], 'book-source.json').read_bytes())
        assert source == candidate.provenance(index)
        assert source['source_metadata']['title'] == 'Original source title'
        assert state['result']['book_source_sha256'] == sha256(encode(source)).hexdigest()
        assert json.loads(Path(row['destination'], 'timeline.json').read_bytes())['book_source_sha256'] == state['result']['book_source_sha256']
        for artifact in state['artifacts']:
            path = Path(artifact['path'])
            before[str(path)] = (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    assert e.service.run(candidate.id, e.output, approval=ready.fingerprint) == result
    assert len(e.voices.received) == 2
    assert before == {name: (sha256(Path(name).read_bytes()).hexdigest(), Path(name).stat().st_mtime_ns)
                      for name in before}
    assert e.app.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode()


def test_actual_source_revocation_after_synthesis_never_publishes_and_explicit_resume_reuses_node(book):
    require_audio()
    e = book
    candidate, ready = prepared(e)
    def revoke_once():
        e.voices.after_write = None
        e.app.archive_workspace(e.workspace)
    e.voices.after_write = revoke_once
    with pytest.raises(ValueError, match='The selected library is missing or archived'):
        e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert len(e.voices.received) == 1 and not list(e.output.iterdir())
    first = e.production.get(candidate.job_id(1))
    assert first['status'] == 'failed' and any(row['kind'] == 'speech' for row in first['nodes'].values())
    e.app.restore_workspace(e.workspace)
    result = e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert [row['status'] for row in result] == ['complete', 'complete']
    assert len(e.voices.received) == 2  # Accepted first speech node was reused.


def test_source_revocation_after_copy_prevents_final_link_and_removes_pending_file(book, monkeypatch):
    require_audio()
    import carr_library.studio.render as render
    e = book
    candidate, ready = prepared(e)
    original, revoked = render.digest_file, []
    def digest(path, **options):
        value = original(path, **options)
        if Path(path).name.startswith('.pending-') and not revoked:
            revoked.append(True)
            e.app.archive_workspace(e.workspace)
        return value
    monkeypatch.setattr(render, 'digest_file', digest)
    with pytest.raises(ValueError, match='The selected library is missing or archived'):
        e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert revoked and not Path(ready.destinations[0], 'master.wav').exists()
    assert not list(e.output.rglob('.pending-*'))
    assert e.production.get(candidate.job_id(1))['status'] == 'failed'


def test_complete_item_still_rechecks_source_and_artifact_bytes(book):
    require_audio()
    e = book
    candidate, ready = prepared(e)
    e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    calls = list(e.voices.received)
    e.app.archive_workspace(e.workspace)
    with pytest.raises(ValueError, match='The selected library is missing or archived'):
        e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    e.app.restore_workspace(e.workspace)
    Path(ready.destinations[0], 'master.wav').write_bytes(b'Externally changed artifact')
    with pytest.raises(ValueError, match='checksum'):
        e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert e.voices.received == calls


def test_manifest_and_job_tamper_cannot_relabel_existing_review(book):
    e = book
    candidate, ready = prepared(e)
    path = e.production.jobs_root / candidate.job_id(1) / 'job.json'
    state = e.production.get(candidate.job_id(1))
    changed = replace(candidate.project(1), title='Another project')
    state.update(project=changed.to_dict(), fingerprint=changed.fingerprint)
    atomic_json(path, state, replace=True)
    with pytest.raises(ValueError, match='immutable'):
        e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert not e.voices.received and not list(e.output.iterdir())


def test_real_backup_restore_keeps_plan_source_and_uses_canonical_new_output_review(book, tmp_path):
    require_audio()
    e = book
    candidate, ready = prepared(e)
    backup = tmp_path / 'backup'
    receipt = e.app.backup(backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', expected_manifest_sha256=receipt))
    production = ProductionService(restored.root / 'state' / 'studio', voices=e.voices,
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options))
    service = BookAudioService(restored, e.workspace, production=production)
    destination = tmp_path / 'restored-output'
    destination.mkdir()
    try:
        reopened = service.load(candidate.id)
        assert reopened == candidate
        historical = {i: (production.jobs_root / candidate.job_id(i) / 'job.json').read_bytes() for i in (1, 2)}
        assert all(production.get(candidate.job_id(i))['restore_requires_new_output'] is True for i in (1, 2))
        initial = service.review(reopened, destination)
        with pytest.raises(ValueError, match='Prepare'):
            service.run(candidate.id, destination, approval=initial.fingerprint)
        with pytest.raises(ValueError, match='new output review'):
            service.prepare(reopened, destination, approval=initial.fingerprint)
        fresh = service.fresh(reopened)
        assert fresh.id != candidate.id and fresh.source == candidate.source
        preview = service.review(fresh, destination)
        relocated = service.prepare(fresh, destination, approval=preview.fingerprint)
        assert relocated.prepared and relocated.destinations != ready.destinations
        assert e.voices.received == []
        with pytest.raises(ValueError, match='Prepare'):
            service.run(fresh.id, destination, approval=preview.fingerprint)
        result = service.run(fresh.id, destination, approval=relocated.fingerprint)
        assert [row['status'] for row in result] == ['complete', 'complete']
        assert historical == {i: (production.jobs_root / candidate.job_id(i) / 'job.json').read_bytes() for i in (1, 2)}
        assert restored.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode()
        assert e.app.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode()
        assert not list(e.output.iterdir())
    finally:
        service.close()
        production.close()

@pytest.mark.parametrize('observer_phase', ['queued', 'rendering'])
def test_optional_observer_failure_cannot_fail_actual_published_audio(book, observer_phase):
    require_audio()
    e = book
    candidate, ready = prepared(e)
    failures = []
    def observer(message):
        if (observer_phase == 'queued' and message.startswith('Waiting')
                or observer_phase == 'rendering' and message.startswith('Scene')):
            failures.append(message)
            raise RuntimeError('Synthetic optional UI observer failed')
    result = e.service.run(candidate.id, e.output, approval=ready.fingerprint, progress=observer)
    assert failures and all(row['status'] == 'complete' for row in result)
    assert all(e.production.get(row['job'])['status'] == 'complete' for row in result)
    assert not e.production.broker.snapshot()['leases']


@pytest.mark.parametrize('stop', ['callback', 'close'])
def test_real_retained_source_cancellation_after_admission_is_paused_and_resumable(book, monkeypatch, stop):
    require_audio()
    e = book
    candidate, ready = prepared(e)
    cancel, armed, entered = threading.Event(), threading.Event(), []
    save, retained = e.production._save, e.app._retained_source
    def saved(state):
        save(state)
        if 'resource_plan' in state and state['status'] == 'queued':
            armed.set()
    def checked(*args, **kwargs):
        if armed.is_set() and not entered:
            entered.append(True)
            if stop == 'callback':
                cancel.set()
            else:
                e.service.close()
        # This is the actual retained-source implementation and its real
        # cancellation surface, not a manufactured validation exception.
        return retained(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(e.production, '_save', saved)
        patch.setattr(e.app, '_retained_source', checked)
        with pytest.raises(InterruptedError):
            e.service.run(candidate.id, e.output, approval=ready.fingerprint, cancelled=cancel.is_set)
    assert entered and e.voices.received == [] and not list(e.output.iterdir())
    assert e.production.get(candidate.job_id(1))['status'] == 'paused'
    assert not e.production.broker.snapshot()['leases']
    cancel.clear()
    reopened = BookAudioService(e.app, e.workspace, production=e.production)
    try:
        result = reopened.run(candidate.id, e.output, approval=ready.fingerprint)
        assert all(row['status'] == 'complete' for row in result)
        assert len(e.voices.received) == 2
    finally:
        reopened.close()


def test_cancel_between_items_retains_first_verified_result_and_resumes_only_pending_item(book, monkeypatch):
    require_audio()
    e = book
    candidate, ready = prepared(e)
    cancel = threading.Event()
    save = e.production._save
    def saved(state):
        save(state)
        if state['id'] == candidate.job_id(1) and state['status'] == 'complete':
            cancel.set()
    with monkeypatch.context() as patch:
        patch.setattr(e.production, '_save', saved)
        with pytest.raises(InterruptedError):
            e.service.run(candidate.id, e.output, approval=ready.fingerprint, cancelled=cancel.is_set)
    assert e.production.get(candidate.job_id(1))['status'] == 'complete'
    assert e.production.get(candidate.job_id(2))['status'] == 'pending'
    assert len(e.voices.received) == 1
    first = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
             for row in e.production.get(candidate.job_id(1))['artifacts']}
    cancel.clear()
    complete = e.service.run(candidate.id, e.output, approval=ready.fingerprint)
    assert all(row['status'] == 'complete' for row in complete) and len(e.voices.received) == 2
    assert first == {name: sha256(Path(name).read_bytes()).hexdigest() for name in first}

def test_completed_book_requires_its_retained_private_provenance_on_backup(book, tmp_path):
    require_audio()
    candidate, ready = prepared(book)
    result = book.service.run(candidate.id, book.output, approval=ready.fingerprint)
    assert all(row['status'] == 'complete' for row in result)
    source = book.production.jobs_root / candidate.job_id(1) / 'book-source.json'
    source.unlink()
    with pytest.raises(ValueError, match='exact retained source receipt'):
        book.app.backup(tmp_path / 'missing-provenance')
    assert not (tmp_path / 'missing-provenance').exists()


@pytest.mark.parametrize('change', ['index', 'hash', 'range', 'authority'])
def test_renderer_refuses_mismatched_book_provenance_before_audio(book, change):
    candidate, ready = prepared(book)
    receipt = candidate.provenance(1)
    if change == 'index':
        receipt['part']['index'] = 2
    elif change == 'hash':
        receipt['part']['text_sha256'] = '0' * 64
    elif change == 'range':
        receipt['part']['start'] += 1
    else:
        receipt['notice'] = 'This text grants quotation authority.'
    with pytest.raises(ValueError):
        book.production.run(candidate.job_id(1), source_guard=lambda: receipt)
    assert not book.voices.received and not list(book.output.iterdir())


# This helper models only delayed acknowledgement after a real ffprobe child
# has exited. It never substitutes PCM inspection, process execution or locks.
from contextlib import contextmanager as _contextmanager


@_contextmanager
def _uncertain_book_probe(e, monkeypatch):
    from carr_library.audio import cleanup, process
    owners, release, selected = [], threading.Event(), []
    stop = process._stop_owned

    def manual(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)

    def uncertain(child):
        assert stop(child) is True
        args = tuple(map(str, child.args))
        if (not selected and len(e.voices.received) == 2
                and '-show_entries' in args
                and Path(args[-1]).parent == e.production.cache):
            selected.append(child)
        if selected and child is selected[0] and not release.is_set():
            raise TimeoutError('Modeled delayed exit acknowledgement after actual ffprobe')
        return True

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, 'pending_cleanup', manual)
        patch.setattr(process, '_stop_owned', uncertain)
        try:
            yield owners, release, selected
        finally:
            release.set()
            for owner in owners:
                assert owner.close()


@pytest.mark.parametrize('masked_journal', [False, True], ids=['direct-owner', 'failed-status-context'])
def test_book_run_retains_same_plan_and_production_locks_until_native_owner_is_quiet(
        book, monkeypatch, masked_journal):
    from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners
    from carr_library.operations._locks import NativeLock

    require_audio()
    e = book
    candidate, ready = prepared(e)
    original = e.app.assets.resolve(e.imported['asset']).read_bytes()
    write = e.production._save
    if masked_journal:
        def failed_status(state):
            if state['status'] == 'failed':
                raise OSError('Modeled failed status save during native cleanup')
            return write(state)
        monkeypatch.setattr(e.production, '_save', failed_status)
    with _uncertain_book_probe(e, monkeypatch) as (owners, release, selected):
        with pytest.raises((AudioCleanupPending, OSError)) as caught:
            e.service.run(candidate.id, e.output, approval=ready.fingerprint)
        assert selected and selected[0].poll() == 0 and _pending_owners(caught.value)
        assert set(_pending_owners(caught.value)) == set(owners)
        staged = Path(selected[0].args[-1])
        with wave.open(str(staged), 'rb') as stream:
            assert stream.getnframes() == 4800
        first = e.production.get(candidate.job_id(1))
        assert first['status'] == 'complete'
        accepted = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
                    for row in first['artifacts']}
        assert not Path(ready.destinations[1]).exists()
        assert len(e.production.broker.snapshot()['leases']) == 1
        locks = (NativeLock(e.service.plans / ('.book-' + candidate.id + '.lock')),
                 NativeLock(e.production.jobs_root / candidate.job_id(2) / '.render.lock'))
        assert all(lock.acquire() is False for lock in locks)
        with pytest.raises(RuntimeError, match='already being changed'):
            e.service.run(candidate.id, e.output, approval=ready.fingerprint)
        assert len(e.voices.received) == 2 and staged.is_file()
        release.set()
        assert all(owner.close() for owner in owners)
        assert not e.production.broker.snapshot()['leases']
        for lock in locks:
            assert lock.acquire()
            lock.release()
        monkeypatch.setattr(e.production, '_save', write)
        done = e.service.run(candidate.id, e.output, approval=ready.fingerprint)
        assert all(row['status'] == 'complete' for row in done)
        assert len(e.voices.received) == 3  # Only the unaccepted second part reruns.
        assert accepted == {path: sha256(Path(path).read_bytes()).hexdigest() for path in accepted}
        assert e.app.assets.resolve(e.imported['asset']).read_bytes() == original
        assert not e.production.broker.snapshot()['leases']
