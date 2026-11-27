"""Actual isolated document conversion, persisted receipts and canonical Studio.

The synthetic PCM fixture exercises real FFmpeg composition, not voice quality.
No original document, private runtime or downloaded model is used.
"""
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.conversion.batches import BatchBudget, BatchPreview, DocumentBatches
from carr_library.conversion import batches as batch_module
from carr_library.conversion.planning import SplitOptions
from carr_library.durable_state import atomic_json, read_json
from carr_library.ingest.writer_guard import WriterBusy
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech.reading import ReadingDocument
from carr_library.studio.render import ProductionService


TEXT = '😀 First exact line.\r\n\r\nДругий текст.\r\n\r\n[Scene: literal source]\r\nNarrator: still prose.\n'


@pytest.fixture
def batch(tmp_path):
    library, output = tmp_path / 'бібліотека', tmp_path / 'output'
    library.mkdir(); output.mkdir()
    source = tmp_path / 'джерело.txt'
    source.write_bytes(TEXT.encode())
    service = DocumentBatches(library)
    identity = service.create([source])
    return service, identity, source, output


def reviewed(batch, options=None, **kwargs):
    service, identity, source, output = batch
    service.prepare(identity)
    preview = service.preview(identity, output, options or SplitOptions('blank-lines'), **kwargs)
    service.approve(preview)
    return preview


def receipts(service, identity, output):
    state = service.get(identity)
    data = []
    for row in state['outputs']:
        target = output / row['name']
        raw = target.read_bytes()
        assert len(raw) == row['bytes'] and sha256(raw).hexdigest() == row['sha256']
        sidecar = output / row['provenance']['name']
        raw_receipt = sidecar.read_bytes()
        assert sha256(raw_receipt).hexdigest() == row['provenance']['sha256']
        assert len(raw_receipt) == row['provenance']['bytes']
        value = json.loads(raw_receipt)
        assert value['batch_id'] == identity and value['quotation_authority'] is False
        assert value['plan_sha256'] == state['review_sha256']
        data.append(raw)
    return b''.join(data)


def test_actual_conversion_retains_originals_and_exact_unicode_split_outputs(batch):
    service, identity, source, output = batch
    source.unlink()  # every later operation uses the explicitly retained copy
    service.prepare(identity)
    preview = service.preview(identity, output, SplitOptions('blank-lines'))
    assert len(preview.outputs) == 3 and not list(output.iterdir())
    assert service.approve(preview)['status'] == 'reviewed'
    assert not list(output.iterdir())
    state = service.run(identity)
    assert state['status'] == 'complete'
    assert receipts(service, identity, output) == TEXT.encode()
    assert not source.exists()
    assert all(row['prepared'] for row in state['sources'])


def test_folder_selection_is_explicit_recursive_deduplicated_and_bounded(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    folder = tmp_path / 'inputs'; folder.mkdir()
    sub = folder / 'sub'; sub.mkdir()
    (folder / 'a.TXT').write_text('One', encoding='utf-8')
    (sub / 'b.md').write_text('Two', encoding='utf-8')
    (sub / 'ignored.exe').write_bytes(b'not executed')
    service = DocumentBatches(root)
    identity = service.create([folder, folder / 'a.TXT'])
    state = service.get(identity)
    assert [row['name'] for row in state['sources']] == ['a.TXT', 'b.md']
    assert state['skipped'] == ['ignored.exe']
    shutil.rmtree(folder)
    assert service.prepare(identity)['status'] == 'prepared'


@pytest.mark.parametrize('budget', [BatchBudget(max_files=1), BatchBudget(max_scan_entries=1),
                                   BatchBudget(max_source_bytes=2)])
def test_selection_budget_fails_without_creating_a_job(tmp_path, budget):
    root = tmp_path / 'library'; root.mkdir()
    source = tmp_path / 'inputs'; source.mkdir()
    (source / 'a.txt').write_text('one'); (source / 'b.txt').write_text('two')
    service = DocumentBatches(root, budget=budget)
    with pytest.raises(ValueError, match='budget'):
        service.create([source])
    assert service.jobs() == () and not list(service.store.directory.glob('.pending-*'))


def test_folder_link_or_junction_is_rejected_before_reading_target(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    folder = tmp_path / 'inputs'; folder.mkdir()
    target = tmp_path / 'elsewhere'; target.mkdir()
    (target / 'private.txt').write_text('outside selection')
    link = folder / 'redirect'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    service = DocumentBatches(root)
    with pytest.raises(ValueError, match='links'):
        service.create([folder])
    assert service.jobs() == ()


@pytest.mark.parametrize('mutation', ['add', 'remove', 'rename'])
def test_folder_membership_rechecked_after_traversal_even_with_same_native_stamp(tmp_path, monkeypatch, mutation):
    library = tmp_path / 'library'; library.mkdir()
    folder = tmp_path / 'folder'; folder.mkdir()
    first, last = folder / 'a.txt', folder / 'b.txt'
    first.write_text('first'); last.write_text('last')
    original_regular, original_stamp = batch_module.regular, batch_module._stamp
    before = original_regular(folder, directory=True)
    def same_directory_stamp(info):
        return original_stamp(before) if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino) else original_stamp(info)
    changed = []
    def mutate_after_last_classification(path, **kwargs):
        info = original_regular(path, **kwargs)
        if Path(path) == last and not changed:
            changed.append(mutation)
            if mutation == 'add':
                (folder / 'late.txt').write_text('late')
            elif mutation == 'remove':
                first.unlink()
            else:
                first.rename(folder / 'renamed.txt')
        return info
    monkeypatch.setattr(batch_module, '_stamp', same_directory_stamp)
    monkeypatch.setattr(batch_module, 'regular', mutate_after_last_classification)
    service = DocumentBatches(library)
    with pytest.raises(ValueError, match='folder changed'):
        service.create([folder])
    assert changed == [mutation] and service.jobs() == ()


@pytest.mark.parametrize('control,expected', [('pause', 'paused'), ('cancel', 'cancelled')])
def test_pause_and_cancel_preserve_completed_receipts_and_explicit_resume(batch, control, expected):
    service, identity, source, output = batch
    reviewed(batch)
    def after_first(message):
        if message.startswith('Verified output 1 '):
            getattr(service, control)(identity)
    with pytest.raises(InterruptedError):
        service.run(identity, progress=after_first)
    state = service.get(identity)
    assert state['status'] == expected and len(state['outputs']) == 1
    first = output / state['outputs'][0]['name']
    before = first.stat()
    reopened = DocumentBatches(service.library)
    assert reopened.run(identity)['status'] == 'complete'
    after = first.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert receipts(reopened, identity, output) == TEXT.encode()


def test_final_observer_exception_cannot_demote_completed_output_or_repeat_conversion(batch):
    service, identity, source, output = batch
    reviewed(batch)
    def observer(message):
        raise RuntimeError('observer failed')
    assert service.run(identity, progress=observer)['status'] == 'complete'
    before = {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in output.iterdir()}
    reopened = DocumentBatches(service.library, conversion_factory=lambda *a, **k: pytest.fail('reconversion'))
    assert reopened.run(identity)['status'] == 'complete'
    assert {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in output.iterdir()} == before


def test_real_process_crash_after_output_pair_before_checkpoint_recovers_same_job(batch):
    service, identity, source, output = batch
    preview = reviewed(batch)
    code = '''import os, sys
from carr_library.conversion.batches import DocumentBatches
service = DocumentBatches(sys.argv[1])
saved = service._save
def crash(record):
    if record['outputs']:
        os._exit(74)
    saved(record)
service._save = crash
service.run(sys.argv[2])
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code,
                             str(service.library), identity], capture_output=True, text=True,
                            encoding='utf-8', timeout=30)
    assert result.returncode == 74, result.stderr
    assert service.get(identity)['outputs'] == []
    first = output / preview.outputs[0]
    assert first.exists() and first.with_name(first.name + '.provenance.json').exists()
    before = first.stat()
    assert DocumentBatches(service.library).run(identity)['status'] == 'complete'
    assert first.stat().st_ino == before.st_ino and first.stat().st_mtime_ns == before.st_mtime_ns
    assert receipts(service, identity, output) == TEXT.encode()


@pytest.mark.parametrize('sidecar', [False, True])
def test_foreign_identical_bytes_are_not_claimed_without_own_completion_receipt(batch, sidecar):
    service, identity, source, output = batch
    preview = reviewed(batch, SplitOptions())
    target = output / preview.outputs[0]
    target.write_bytes(TEXT.encode())
    if sidecar:
        target.with_name(target.name + '.provenance.json').write_text('{}')
    with pytest.raises((ValueError, FileExistsError)):
        service.run(identity)
    assert service.get(identity)['outputs'] == [] and target.read_bytes() == TEXT.encode()


def test_preview_approval_is_bound_to_unoccupied_names_and_destination_identity(batch):
    service, identity, source, output = batch
    service.prepare(identity)
    preview = service.preview(identity, output)
    (output / preview.outputs[0]).write_text('foreign')
    with pytest.raises(ValueError, match='changed|occupied'):
        service.approve(preview)
    assert service.get(identity)['review'] is None
    (output / preview.outputs[0]).unlink()
    service.approve(service.preview(identity, output))
    moved = output.with_name('old-output'); output.rename(moved); output.mkdir()
    with pytest.raises(ValueError, match='folder changed'):
        service.run(identity)
    assert not list(output.iterdir())


def test_reviewed_preview_roundtrip_is_exact_and_rejects_malformed_shapes(batch):
    service, identity, source, output = batch
    service.prepare(identity)
    preview = service.preview(identity, output)
    assert BatchPreview.from_dict(preview.to_dict()) == preview
    for key, value in [('job_id', '../bad'), ('plans', []), ('outputs', ['same.txt', 'SAME.txt']),
                       ('destination_identity', [True, 1]), ('combined', 1), ('voice_id', 'v' * 513)]:
        with pytest.raises(ValueError):
            BatchPreview.from_dict({**preview.to_dict(), key: value})


def test_saved_job_listing_has_aggregate_budget_and_cancellation_before_loading(batch, monkeypatch):
    service, identity, source, output = batch
    monkeypatch.setattr(service, 'get', lambda identity: pytest.fail('Budget/cancel must stop before loading the record.'))
    with pytest.raises(InterruptedError):
        service.jobs(cancelled=lambda: True)
    monkeypatch.setattr(batch_module, 'MAX_LIST_BYTES', 1)
    with pytest.raises(ValueError, match='aggregate metadata byte budget'):
        service.jobs()


@pytest.mark.parametrize('stage', ['first', 'last'])
def test_actual_directory_replacement_between_outputs_never_claims_completion(batch, stage):
    service, identity, source, output = batch
    preview = reviewed(batch)
    replaced = output.with_name('reviewed-directory-moved')
    trigger = 1 if stage == 'first' else len(preview.outputs)
    def replace_parent(message):
        if message.startswith(f'Verified output {trigger} '):
            output.rename(replaced)
            output.mkdir()
    with pytest.raises(ValueError, match='folder changed'):
        service.run(identity, progress=replace_parent)
    assert service.get(identity)['status'] == 'failed'
    assert not list(output.iterdir())
    assert len(list(replaced.glob('*.txt'))) == trigger


def test_publication_uses_reviewed_parent_approval_not_a_fresh_capture(batch, monkeypatch):
    service, identity, source, output = batch
    reviewed(batch)
    original = batch_module.write_export
    moved = output.with_name('moved-before-write')
    def swap(path, content, **kwargs):
        output.rename(moved); output.mkdir()
        return original(path, content, **kwargs)
    monkeypatch.setattr(batch_module, 'write_export', swap)
    with pytest.raises(ValueError, match='destination changed'):
        service.run(identity)
    assert not list(output.iterdir()) and not list(moved.iterdir())


def test_final_batch_validation_rehashes_earlier_completed_artifacts(batch):
    service, identity, source, output = batch
    preview = reviewed(batch)
    def damage_earlier(message):
        if message.startswith(f'Verified output {len(preview.outputs)} '):
            first = output / preview.outputs[0]
            first.write_bytes(first.read_bytes().replace(b'First', b'Worse'))
    with pytest.raises(ValueError, match='changed'):
        service.run(identity, progress=damage_earlier)
    assert service.get(identity)['status'] == 'failed'


@pytest.mark.parametrize('damaged', ['source', 'reading', 'provenance', 'text'])
def test_tampered_managed_graph_never_publishes_an_output(batch, damaged):
    service, identity, source, output = batch
    reviewed(batch)
    row = service.get(identity)['sources'][0]
    if damaged == 'source':
        relative = row['path']
    elif damaged == 'reading':
        relative = row['prepared']['reading']
    else:
        relative = next(x['path'] for x in row['prepared']['artifacts']
                        if x['path'].endswith('.provenance.json' if damaged == 'provenance' else '.txt'))
    path = service.store.directory / relative
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='changed'):
        service.run(identity)
    assert not list(output.iterdir()) and service.get(identity)['status'] == 'failed'


def test_reading_derivative_keeps_original_text_map_and_undo_history(tmp_path):
    library, output = tmp_path / 'library', tmp_path / 'output'
    library.mkdir(); output.mkdir()
    document = ReadingDocument('Original exact text.\nSecond.', title='Reading derivative')
    document.replace(0, 8, 'Pronounced')
    before = document.to_dict()
    path = tmp_path / 'reading.json'; document.save(path)
    service = DocumentBatches(library)
    identity = service.create_reading_copies([path]); path.unlink()
    service.prepare(identity)
    preview = service.preview(identity, output, SplitOptions('paragraphs'))
    assert preview.plans[0]['parts'][0]['source_exact'] is False
    service.approve(preview); service.run(identity)
    saved = service.get(identity)['sources'][0]['prepared']['reading']
    assert read_json(service.store.directory / saved) == before
    assert receipts(service, identity, output).decode() == document.text


def test_actual_pdf_sections_bind_codepoint_offsets_and_physical_pages(tmp_path):
    from reportlab.pdfgen import canvas
    library, output = tmp_path / 'library', tmp_path / 'output'
    library.mkdir(); output.mkdir()
    source = tmp_path / 'pages.pdf'
    pdf = canvas.Canvas(str(source)); pdf.drawString(70, 700, 'First physical page')
    pdf.showPage(); pdf.drawString(70, 700, 'Second physical page'); pdf.save()
    service = DocumentBatches(library)
    identity = service.create([source]); service.prepare(identity)
    preview = service.preview(identity, output, SplitOptions('pages'))
    assert len(preview.outputs) == 2
    service.approve(preview); service.run(identity)
    texts = [(output / name).read_text(encoding='utf-8') for name in preview.outputs]
    assert 'First physical page' in texts[0] and 'Second physical page' in texts[1]
    assert 'Second physical page' not in texts[0]


def test_combined_documents_have_explicit_separators_and_complete_source_lineage(batch, tmp_path):
    service, _, source, output = batch
    other = tmp_path / 'second.txt'; other.write_bytes('Другий документ'.encode())
    identity = service.create([source, other]); service.prepare(identity)
    preview = service.preview(identity, output, SplitOptions('sections'), combined=True)
    service.approve(preview); service.run(identity)
    assert len(preview.outputs) == 2
    assert receipts(service, identity, output).decode() == TEXT + '\n\nДругий документ'
    for row in service.get(identity)['outputs']:
        report = read_json(output / row['provenance']['name'])
        assert len(report['sources']) == 2 and report['combined_separators'] == '\n\n'


def test_actual_backup_delete_restore_requires_explicit_output_rebinding(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'; output.mkdir()
    source = tmp_path / 'source.txt'; source.write_bytes(TEXT.encode())
    service = DocumentBatches(app.root)
    identity = service.create([source]); service.prepare(identity)
    service.approve(service.preview(identity, output, SplitOptions('blank-lines')))
    service.run(identity)
    originals = {path.name: path.read_bytes() for path in output.iterdir()}
    backup = tmp_path / 'backup'; digest = create_backup(app, backup)
    source.unlink(); shutil.rmtree(service.library)
    restored_root = restore_backup(backup, tmp_path / 'restored', digest)
    restored = DocumentBatches(restored_root)
    assert restored.get(identity)['status'] == 'waiting-input'
    with pytest.raises(ValueError, match='Restored outputs'):
        restored.run(identity)
    replacement = tmp_path / 'replacement'; replacement.mkdir()
    preview = restored.preview(identity, replacement, SplitOptions('blank-lines'))
    restored.approve(preview)
    assert restored.run(identity)['status'] == 'complete'
    assert receipts(restored, identity, replacement) == TEXT.encode()
    assert {path.name: path.read_bytes() for path in output.iterdir()} == originals
    assert len(restored.get(identity)['history']) == 1


def test_active_batch_uses_existing_library_backup_exclusion(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    source = tmp_path / 'source.txt'; source.write_text('Source')
    service = DocumentBatches(app.root); identity = service.create([source])
    seen = []
    def progress(message):
        with pytest.raises(WriterBusy):
            create_backup(app, tmp_path / 'backup')
        seen.append(message)
    service.prepare(identity, progress=progress)
    assert seen and not (tmp_path / 'backup').exists()
    assert create_backup(app, tmp_path / 'backup')


class ToneVoices:
    def __init__(self):
        self.spoken = []

    def voices(self):
        return (SimpleNamespace(id='synthetic-tone', name='Synthetic protocol tone',
                                language='und', engine='test fixture'),)

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        assert voice_id == 'synthetic-tone' and not cancelled()
        self.spoken.append(text)
        with wave.open(str(destination), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * 440 * i / 24000)))
                                       for i in range(4800)))


def test_actual_studio_uses_literal_scenes_measured_audio_and_same_job_receipts(batch):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Reviewed FFmpeg/ffprobe are required.'
    service, identity, source, output = batch
    voices = ToneVoices()
    production = ProductionService(service.library / 'state' / 'studio', voices=voices)
    service.production = production
    try:
        preview = reviewed(batch, action='studio', voice_id='synthetic-tone')
        state = service.run(identity)
        assert state['status'] == 'complete' and len(state['outputs']) == 1
        child = production.get(state['outputs'][0]['studio_job'])
        assert child['status'] == 'complete'
        assert child['project']['original_script'] == TEXT
        assert len(child['project']['scenes']) == 3
        assert all(not scene['cues'] for scene in child['project']['scenes'])
        assert '[Scene: literal source]' in ''.join(voices.spoken)
        assert 'Narrator: still prose.' in ''.join(voices.spoken)
        assert Path(child['destination']).name == preview.outputs[0]
        assert (Path(child['destination']) / 'master.wav').is_file()
        before = tuple(voices.spoken)
        discover = voices.voices
        voices.voices = lambda: pytest.fail('Completed child must not discover or execute an engine.')
        assert service.run(identity)['status'] == 'complete'
        assert tuple(voices.spoken) == before
        voices.voices = discover
        earlier = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest() for row in child['artifacts']}
        replacement = output.with_name('new-reviewed-plan'); replacement.mkdir()
        next_preview = service.preview(identity, replacement, SplitOptions(), action='studio', voice_id='synthetic-tone')
        service.approve(next_preview)
        next_state = service.run(identity)
        second = production.get(next_state['outputs'][0]['studio_job'])
        assert second['project']['id'] != child['project']['id'] and len(second['project']['scenes']) == 1
        assert second['project']['original_script'] == child['project']['original_script'] == TEXT
        assert production.projects.load(child['project']['id']).fingerprint == child['fingerprint']
        assert {path: sha256(Path(path).read_bytes()).hexdigest() for path in earlier} == earlier
        assert len(service.get(identity)['history']) == 1
    finally:
        production.close()


@pytest.mark.parametrize('command,status', [('pause', 'paused'), ('cancel', 'cancelled')])
def test_studio_typed_provider_interruption_remains_resumable_through_same_batch(batch, command, status):
    from carr_library.audio.models import AudioCancelled
    service, identity, source, output = batch
    class InterruptedTone(ToneVoices):
        first = True
        def synthesize(self, *args, **kwargs):
            if self.first:
                self.first = False
                getattr(service, command)(identity)
                raise AudioCancelled('Explicit synthetic provider interruption.')
            return super().synthesize(*args, **kwargs)
    production = ProductionService(service.library / 'state' / 'studio', voices=InterruptedTone())
    service.production = production
    try:
        reviewed(batch, SplitOptions(), action='studio', voice_id='synthetic-tone')
        with pytest.raises(InterruptedError, match='receipts remain available'):
            service.run(identity)
        assert service.get(identity)['status'] == status
        child, = production.jobs()
        assert child['status'] == 'paused'
        assert service.run(identity)['status'] == 'complete'
        assert production.jobs()[0]['id'] == child['id']
    finally:
        production.close()
