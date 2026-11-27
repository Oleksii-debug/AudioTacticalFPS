"""Actual FFmpeg publications from the current renderer, never speech-quality proof."""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import struct
from threading import Event
import wave
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import publishing
from carr_library.audio.daisy import inspect_daisy
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.jobs import atomic_json, sha256_file
from carr_library.audio.models import digest
from carr_library.audio.production import parse_script, render_project
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.ui.audio_studio import StudioController


class Tone:
    """Synthetic local input with measured duration; no installed voice is needed."""
    def __init__(self):
        self.calls = []
    def cache_identity(self, voice, **kwargs):
        return {'fixture': 'publication-tone-v1', 'voice': voice}
    def synthesize(self, text, output, **kwargs):
        self.calls.append(text)
        rate, frames = 8000, 1600
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(5500 * math.sin(i * 2 * math.pi * 330 / rate)))
                                       for i in range(frames)))
        return output


@pytest.fixture
def rendered(tmp_path):
    ffmpeg = resolve_ffmpeg()
    assert ffmpeg is not None and ffmpeg.is_file(), 'This strict suite requires CARR_FFMPEG or installed FFmpeg.'
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Publication preservation')
    studio = StudioController(private_root=app.root, ffmpeg=ffmpeg)
    script = ('[output sample_rate=16000 stems=false]\r\n# Перша\r\n'
              'Alice: Оригінал & <слово>.\r\n[pause 0.1]\r\n# Second\r\nBob: Last line.')
    studio.new(script, title='Книжка 🙂', input_kind='script')
    studio.configure(voice='fixture:tone', language='uk')
    studio.save_in_library()
    tone = Tone()
    result = render_project(studio.plan(), studio.output_dir, engine=tone, ffmpeg=ffmpeg)
    studio.result = result
    service = publishing.PublicationService(studio.output_dir, private_root=app.root, ffmpeg=ffmpeg,
                                           max_bytes=512 * 1024**2)
    return app, studio, tone, service, result


def _export(service, result, path, kind='epub', **kwargs):
    options = {'language': 'uk', 'creator': 'Synthetic author', **kwargs}
    return (service.export_epub(result['run_key'], path, **options) if kind == 'epub'
            else service.export_daisy(result['run_key'], path, publisher='Test publisher', **options))


@pytest.mark.parametrize('kind', ['epub', 'daisy'], ids=['epub3', 'daisy202'])
def test_completed_plan_pcm_and_measured_lines_become_a_verified_portable_book(rendered, tmp_path, kind):
    _, studio, tone, service, result = rendered
    output = tmp_path / ('Книга.epub' if kind == 'epub' else 'Книга.zip')
    before = {path: path.read_bytes() for path in (
        studio.project_path, Path(result['manifest']), Path(result['output']))}
    record = _export(service, result, output, kind)
    assert record['status'] == 'complete' and record['artifact']['sha256'] == sha256_file(output)
    assert len(tone.calls) == 2
    provenance = (publishing.inspect_epub if kind == 'epub' else inspect_daisy)(output)
    assert provenance['source']['project_sha256'] == digest(studio.plan().to_dict())
    assert provenance['original_script'] == studio.script
    assert provenance['audio']['codec'] == 'mp3'
    assert provenance['audio']['duration'] >= result['duration_seconds']
    assert [row['kind'] for row in provenance['intervals']] == ['line', 'audio-interval', 'line']
    assert provenance['intervals'][-1]['end_frame'] == result['frames']
    assert provenance['validation']['reader_interoperability'] == 'NOT_RUN'
    assert all(path.read_bytes() == contents for path, contents in before.items())
    with ZipFile(output) as archive:
        if kind == 'epub':
            assert archive.infolist()[0].filename == 'mimetype'
            assert archive.read('mimetype') == b'application/epub+zip'
            xml = archive.read('EPUB/overlay.smil')
        else:
            xml = archive.read('book.smil')
        assert b'master.mp3' in xml and b'http://' not in xml.replace(
            b'http://www.w3.org/ns/SMIL', b'').replace(
            b'http://www.idpf.org/2007/ops', b'').replace(
            b'http://www.w3.org/TR/REC-SMIL/SMIL10.dtd', b'')
    assert service.verify(record['id'])['status'] == 'complete'


def test_legacy_complete_marker_is_upgraded_only_by_verified_exact_resume(rendered, tmp_path):
    _, studio, tone, service, result = rendered
    marker = Path(result['manifest'])
    saved = json.loads(marker.read_text(encoding='utf-8'))
    del saved['plan_sha256']
    atomic_json(marker, saved)
    original_audio = Path(result['output']).read_bytes()
    with pytest.raises(ValueError, match='typed-plan'):
        _export(service, result, tmp_path / 'unbound.epub')
    assert not (tmp_path / 'unbound.epub').exists()
    assert 'plan_sha256' not in json.loads(marker.read_text(encoding='utf-8'))
    resumed = render_project(studio.plan(), studio.output_dir, engine=tone, ffmpeg=service.ffmpeg)
    assert len(tone.calls) == 2 and Path(resumed['output']).read_bytes() == original_audio
    assert resumed['plan_sha256'] == digest(studio.plan().to_dict())
    assert json.loads(marker.read_text(encoding='utf-8'))['plan_sha256'] == resumed['plan_sha256']


def test_conflicting_complete_plan_binding_is_never_silently_repaired(rendered):
    _, studio, tone, service, result = rendered
    marker = Path(result['manifest'])
    saved = json.loads(marker.read_text(encoding='utf-8'))
    saved['plan_sha256'] = '0' * 64
    atomic_json(marker, saved)
    before = marker.read_bytes()
    with pytest.raises(ValueError, match='different typed-plan'):
        render_project(studio.plan(), studio.output_dir, engine=tone, ffmpeg=service.ffmpeg)
    assert marker.read_bytes() == before and len(tone.calls) == 2


@pytest.mark.parametrize('change', ['plan', 'line', 'sample', 'audio'], ids=[
    'same-source-other-plan', 'rehash-forged-line', 'rehash-forged-samples', 'same-size-pcm-edit'])
def test_source_changes_cannot_be_hidden_by_metadata_or_recomputed_artifact_receipts(rendered, tmp_path, change):
    _, studio, _, service, result = rendered
    root = Path(result['output']).parent
    marker = json.loads((root / 'complete.json').read_text(encoding='utf-8'))
    if change == 'plan':
        path = studio.output_dir / ('plan-' + result['run_key'] + '.json')
        value = json.loads(path.read_text(encoding='utf-8'))
        value['roles'][0]['volume'] = 0.5
        atomic_json(path, value)
    elif change == 'audio':
        path = root / 'master.wav'
        info = wav_info(path)
        raw = bytearray(path.read_bytes())
        raw[info.data_offset + 20] ^= 1
        path.write_bytes(raw)
    else:
        path = root / 'timeline.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        if change == 'line':
            value['lines'][0]['text'] += ' NOT IN SOURCE'
        else:
            value['lines'][0]['end_frame'] -= 1
        atomic_json(path, value)
        for artifact in marker['files']:
            if artifact['name'] == path.name:
                artifact.update(size=path.stat().st_size, sha256=sha256_file(path))
        atomic_json(root / 'complete.json', marker)
    with pytest.raises((ValueError, RuntimeError)):
        _export(service, result, tmp_path / 'changed.epub')
    assert not (tmp_path / 'changed.epub').exists()


def test_captured_empty_destination_and_private_library_remain_protected(rendered, tmp_path):
    app, _, _, service, result = rendered
    selected = tmp_path / 'raced.epub'
    approval = publishing.capture_export_destination(selected, private_root=app.root)
    selected.write_bytes(b'Another completed user book')
    with pytest.raises(ValueError, match='destination changed'):
        _export(service, result, selected, approval=approval)
    assert selected.read_bytes() == b'Another completed user book'
    with pytest.raises(ValueError, match='outside the private'):
        _export(service, result, app.root / 'cannot.epub')
    assert not (app.root / 'cannot.epub').exists()


def test_cancel_after_prepared_receipt_preserves_source_and_does_not_publish(rendered, tmp_path):
    _, _, _, service, result = rendered
    stop, output = Event(), tmp_path / 'cancelled.epub'
    original = Path(result['output']).read_bytes()
    def progress(message):
        if message.startswith('Publication prepared;'):
            stop.set()
    with pytest.raises(publishing.PublicationCancelled):
        _export(service, result, output, cancelled=stop.is_set, progress=progress)
    assert not output.exists() and Path(result['output']).read_bytes() == original
    records = service.publications()
    assert len(records) == 1 and records[0]['status'] == 'prepared'


def test_move_then_sync_failure_is_recovered_without_reencoding_or_overwrite(rendered, tmp_path, monkeypatch):
    _, _, _, service, result = rendered
    output = tmp_path / 'recover.epub'
    original_publish = publishing.publish_noreplace
    def failed_sync(source, destination, **options):
        original_publish(source, destination, **options)
        raise OSError('Injected post-publication directory flush failure')
    monkeypatch.setattr(publishing, 'publish_noreplace', failed_sync)
    with pytest.raises(RuntimeError, match='explicit verification'):
        _export(service, result, output)
    assert output.is_file()
    record, = service.publications()
    assert record['status'] == 'prepared'
    before = output.read_bytes()
    monkeypatch.setattr(publishing.audio, 'run_ffmpeg', lambda *a, **k: pytest.fail('Recovery re-encoded audio'))
    assert service.verify(record['id'])['status'] == 'complete'
    assert output.read_bytes() == before
    with pytest.raises(FileExistsError):
        _export(service, result, output)


def test_late_cancel_after_new_file_move_does_not_turn_publication_into_a_retry(rendered, tmp_path, monkeypatch):
    _, _, _, service, result = rendered
    stop, output = Event(), tmp_path / 'complete.epub'
    original = publishing.publish_noreplace
    def moved(source, destination, **options):
        original(source, destination, **options)
        stop.set()
    monkeypatch.setattr(publishing, 'publish_noreplace', moved)
    record = _export(service, result, output, cancelled=stop.is_set)
    assert stop.is_set() and output.is_file() and record['status'] == 'complete'


def test_complete_backup_restores_receipt_and_bound_audio_without_reactivating_external_path(rendered, tmp_path, monkeypatch):
    app, studio, _, service, result = rendered
    output = tmp_path / 'retained.epub'
    record = _export(service, result, output)
    snapshot = tmp_path / 'snapshot'
    manifest_sha = create_backup(app, snapshot)
    verify_backup(snapshot, manifest_sha)
    restored = tmp_path / 'restored-library'
    restore_backup(snapshot, restored, manifest_sha)
    restored_audio = restored / 'audio-projects' / studio.project_id / 'audio'
    reopened = publishing.PublicationService(restored_audio, private_root=restored, ffmpeg=None)
    assert json.loads((restored_audio / 'renders' / result['run_key'] / 'complete.json').read_text(
        encoding='utf-8'))['plan_sha256'] == digest(studio.plan().to_dict())
    receipt = restored_audio / ('publication-' + record['id'] + '.json')
    assert receipt.is_file()
    monkeypatch.setattr(publishing.audio, 'run_ffmpeg', lambda *a, **k: pytest.fail('Offline receipt verification used FFmpeg'))
    with pytest.raises(ValueError, match='restored elsewhere'):
        reopened.verify(record['id'])
    assert reopened.verify(record['id'], output=output)['status'] == 'complete'
    again = tmp_path / 'again'
    again_sha = create_backup(LibraryApp(restored), again)
    verify_backup(again, again_sha)


def test_assistant_existing_renderer_bridge_accepts_the_additive_plan_receipt(rendered, tmp_path):
    from carr_library.assistant.production import LocalProduction
    _, studio, tone, service, _ = rendered
    outside = tmp_path / 'assistant-output'
    outside.mkdir()
    def renderer(plan, root, **kwargs):
        return render_project(plan, root, engine=tone, ffmpeg=service.ffmpeg)
    bridge = LocalProduction(studio.private_root, registry=tone, ffmpeg=service.ffmpeg, renderer=renderer)
    arguments = {'operation_id': '1' * 32, '_artifact_budget_bytes': 512 * 1024**2,
                 'script': studio.plan().to_dict()}
    summary = bridge(arguments, outside, lambda: False, lambda message: None)
    master = Path(summary['output'])
    marker = json.loads((master.parent / 'complete.json').read_text(encoding='utf-8'))
    assert marker['plan_sha256'] == digest(bridge.plan(arguments, max_bytes=512 * 1024**2).to_dict())
    assert str(master) in summary['artifacts'] and summary['source_sha256'] == studio.plan().source_sha256


def test_current_studio_rejects_an_old_render_after_same_source_settings_change(rendered, tmp_path):
    _, studio, _, service, result = rendered
    changed = replace(studio.plan(), title='A different reviewed title')
    bound = publishing.PublicationService(service.root, private_root=service.private_root,
        ffmpeg=service.ffmpeg, expected_plan_sha256=digest(changed.to_dict()))
    with pytest.raises(ValueError, match='current Studio plan differs'):
        _export(bound, result, tmp_path / 'wrong-version.epub')


@pytest.mark.parametrize('bad', ['nan', 'bool', 'negative', 'wrong-rate'])
def test_sample_provenance_rejects_forged_clock_authority(bad):
    row = {'sample_rate': 48000, 'start_frame': 0, 'end_frame': 48000, 'start': 0., 'end': 1.}
    if bad == 'nan':
        row['end'] = float('nan')
    elif bad == 'bool':
        row['end_frame'] = True
    elif bad == 'negative':
        row['start_frame'] = -1
    else:
        row['sample_rate'] = 16000
    with pytest.raises(ValueError):
        publishing._sample_provenance({'schema': 'resonance-epub/2', 'intervals': [row], 'duration': 1.})


def test_literal_page_boundaries_keep_exact_source_in_provenance_without_invalid_xml(rendered, tmp_path):
    _, studio, tone, service, _ = rendered
    studio.new('First [literal] & <tag>.\r\n\fДруга сторінка.', title='Literal pages', input_kind='narration')
    studio.configure(voice='fixture:tone', language='uk')
    studio.save_in_library()
    result = render_project(studio.plan(), studio.output_dir, engine=tone, ffmpeg=service.ffmpeg)
    service = publishing.PublicationService(studio.output_dir, private_root=studio.private_root,
                                           ffmpeg=service.ffmpeg)
    output = tmp_path / 'literal.epub'
    _export(service, result, output)
    proof = publishing.inspect_epub(output)
    assert proof['original_script'] == studio.script and '\f' in proof['original_script']
    with ZipFile(output) as archive:
        assert b'[Source page boundary]' in archive.read('EPUB/source.xhtml')
        assert b'\x0c' not in archive.read('EPUB/source.xhtml')



@pytest.mark.parametrize('book_format', ['epub', 'daisy'])
def test_cancellation_during_final_book_flush_keeps_prepared_receipt_without_output(
        rendered, tmp_path, monkeypatch, book_format):
    import os
    from carr_library.audio import jobs
    _, _, _, service, result = rendered
    suffix = '.epub' if book_format == 'epub' else '.zip'
    target = tmp_path / ('flush-cancelled' + suffix)
    stop, flushed = Event(), []
    actual_fsync = os.fsync

    def cancel_book_flush(descriptor):
        actual_fsync(descriptor)
        current = os.fstat(descriptor)
        for staged in tmp_path.glob('.publication-*/publication' + suffix):
            saved = staged.stat()
            if (current.st_dev, current.st_ino) == (saved.st_dev, saved.st_ino):
                flushed.append(staged.name)
                stop.set()

    monkeypatch.setattr(jobs.os, 'fsync', cancel_book_flush)
    with pytest.raises(publishing.PublicationCancelled):
        _export(service, result, target, kind=book_format, cancelled=stop.is_set)
    assert flushed == ['publication' + suffix]
    assert not target.exists()
    record, = service.publications()
    assert record['status'] == 'prepared'
    assert Path(result['output']).is_file()
