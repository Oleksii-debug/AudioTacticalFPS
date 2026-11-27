"""Actual current Studio -> reused EPUB/DAISY publication boundaries.

ToneEngine is a public synthetic PCM source. The production renderer, FFmpeg,
ffprobe, file publication and archive validators are real; speech quality,
external readers, physical playback and NVDA are not exercised here.
"""
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import struct
from threading import Event
import wave
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pytest

from carr_library.audio import publishing, publication_io
from carr_library.audio.daisy import inspect_daisy
from carr_library.audio.jobs import RenderCancelled
from carr_library.audio.models import digest
from carr_library.audio.production import parse_script, plan_narration, render_project
from carr_library.audio.publishing import PublicationService, validate_publication_receipt
from carr_library.audio.publication_io import PublicationBudget
from carr_library.research.export_file import capture_export_destination


class ToneEngine:
    def cache_identity(self, voice):
        return {'fixture': 'publication-synthetic-tone-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        sample_rate, frames = 8000, 1600
        frequency = 330 + sum(map(ord, text)) % 200
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, sample_rate, frames, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', int(7000 * math.sin(i * 2 * math.pi * frequency / sample_rate)))
                                       for i in range(frames)))
        return output


@pytest.fixture(scope='module')
def actual_production(tmp_path_factory):
    selected = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    if selected is None:
        pytest.skip('Actual local FFmpeg is required; no publication result is claimed.')
    ffmpeg = Path(selected).resolve()
    if not ffmpeg.with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe').is_file():
        pytest.skip('Actual sibling ffprobe is required for codec/duration evidence.')
    root = tmp_path_factory.mktemp('actual-publication-producer')
    (root / 'assets').mkdir()
    ToneEngine().synthesize('Synthetic retained sound', root / 'assets/signal.wav')
    script = ('[output sample_rate=16000 stems=false]\r\n'
              '# First chapter\r\nNarrator: First exact line.\r\n[pause 0.001]\r\n'
              '[sound signal.wav]\r\n# Second chapter\r\nNarrator: Другий точний рядок.')
    plan = parse_script(script, title='Public synthetic production')
    result = render_project(plan, root, engine=ToneEngine(), ffmpeg=ffmpeg)
    assert result['status'] == 'complete'
    assert result['plan_sha256'] == digest(plan.to_dict())
    # Exercise the actual renderer's retained chapter hardlinks before copies
    # used by independent mutation cases sever those links.
    chapter = Path(result['manifest']).parent / 'chapter-000001.wav'
    assert chapter.stat().st_nlink > 1
    return root, plan, result, ffmpeg


@pytest.fixture
def source_job(tmp_path, actual_production):
    root, plan, result, ffmpeg = actual_production
    copied = tmp_path / 'audio-project'
    shutil.copytree(root, copied, ignore=shutil.ignore_patterns('publications'))
    private = tmp_path / 'private-library'
    private.mkdir()
    service = PublicationService(copied, private_root=private, ffmpeg=ffmpeg)
    return service, result['run_key'], plan


def export(service, identity, output, kind, **options):
    if kind == 'epub':
        return service.export_epub(identity, output, language='uk', creator='Synthetic creator', **options)
    return service.export_daisy(identity, output, language='uk', creator='Synthetic creator',
                                publisher='Synthetic publisher metadata', **options)


def inspector(kind):
    return publishing.inspect_epub if kind == 'epub' else inspect_daisy


def output_path(tmp_path, kind, stem='book'):
    return tmp_path / (stem + ('.epub' if kind == 'epub' else '.zip'))


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and not p.name.endswith('.lock') and p.parts[-2] != 'publications'}


def rewrite(source, target, changed):
    with ZipFile(source) as original, ZipFile(target, 'x') as result:
        for member in original.infolist():
            raw = original.read(member.filename)
            result.writestr(member, changed[member.filename](raw) if member.filename in changed else raw)


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_actual_renderer_to_publication_preserves_source_frames_and_verified_audio(source_job, tmp_path, kind):
    service, identity, plan = source_job
    before = tree_bytes(service.root)
    output = output_path(tmp_path, kind)
    record = export(service, identity, output, kind)
    assert record['status'] == 'complete'
    assert validate_publication_receipt(record) is record
    proof = inspector(kind)(output)
    assert proof['original_script'] == plan.original_script
    assert proof['source']['source_script_sha256'] == plan.source_sha256
    assert proof['source']['project_sha256'] == digest(plan.to_dict())
    assert proof['source']['approved_library_evidence'] is False
    assert proof['source']['frame_rate'] == 16000
    assert proof['source']['frame_intervals'] == record['source']['frame_intervals']
    spoken = [row for row in proof['intervals'] if row['kind'] == 'line']
    assert len(spoken) == 2
    assert len([row for row in proof['intervals'] if row['kind'] == 'audio-interval']) >= 1
    assert all(row['measured_end'] > row['measured_start'] for row in spoken)
    assert proof['audio']['codec'] == 'mp3' and proof['audio']['duration'] >= proof['duration']
    assert tree_bytes(service.root) == before
    assert service.verify(record['id']) == record
    assert service.jobs()[0]['id'] == identity


def test_actual_sealed_chapter_hardlinks_are_read_only_supported_sources(actual_production, tmp_path):
    root, plan, result, ffmpeg = actual_production
    private = tmp_path / 'private'
    private.mkdir()
    service = PublicationService(root, private_root=private, ffmpeg=ffmpeg)
    before = tree_bytes(root)
    record = service.export_epub(result['run_key'], tmp_path / 'hardlinks.epub')
    assert record['status'] == 'complete' and tree_bytes(root) == before


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_literal_formfeeds_crlf_and_directive_text_remain_exact_derivatives(actual_production, tmp_path, kind):
    _, _, _, ffmpeg = actual_production
    root, private = tmp_path / 'literal-project', tmp_path / 'private'
    private.mkdir()
    text = '  [pause 2]\r\nRead these brackets literally.\fУкраїнський другий сегмент.\r\n'
    plan = replace(plan_narration(text, title='Literal source'), sample_rate=16000, export_stems=False)
    result = render_project(plan, root, engine=ToneEngine(), ffmpeg=ffmpeg)
    service = PublicationService(root, private_root=private, ffmpeg=ffmpeg)
    output = output_path(tmp_path, kind)
    export(service, result['run_key'], output, kind)
    proof = inspector(kind)(output)
    assert proof['original_script'] == text and proof['source_display_encoding'] == 'json-string'
    assert proof['source']['input_kind'] == 'narration'
    assert all(row['literal_narration'] for row in proof['source']['frame_intervals'])
    with ZipFile(output) as package:
        name = 'EPUB/source.xhtml' if kind == 'epub' else 'source.html'
        raw = package.read(name)
        if kind == 'daisy':
            from carr_library.audio.daisy import HTML_DTD
            raw = raw.replace(HTML_DTD, b'')
        root_xml = ET.fromstring(raw)
        displayed = root_xml.find('.//{http://www.w3.org/1999/xhtml}pre').text
        assert json.loads(displayed) == text
    assert (root / ('plan-' + result['run_key'] + '.json')).read_text('utf-8')


def test_existing_job_mp3_is_reused_without_encoding(actual_production, tmp_path, monkeypatch):
    _, original_plan, _, ffmpeg = actual_production
    root, private = tmp_path / 'mp3-project', tmp_path / 'private'
    (root / 'assets').mkdir(parents=True)
    ToneEngine().synthesize('Synthetic retained sound', root / 'assets/signal.wav')
    private.mkdir()
    plan = replace(original_plan, output_format='mp3')
    result = render_project(plan, root, engine=ToneEngine(), ffmpeg=ffmpeg)
    selected = Path(result['output']).read_bytes()
    from carr_library.audio import ffmpeg as tools
    monkeypatch.setattr(tools, 'transcode', lambda *args, **kwargs: pytest.fail('A completed MP3 must not be re-encoded.'))
    service = PublicationService(root, private_root=private, ffmpeg=ffmpeg)
    output = tmp_path / 'reused.epub'
    record = service.export_epub(result['run_key'], output)
    with ZipFile(output) as package:
        assert package.read('EPUB/audio/master.mp3') == selected
    assert publishing.inspect_epub(output)['audio']['reused'] is True
    assert record['status'] == 'complete'


@pytest.mark.parametrize('change', ['title', 'role'])
def test_changed_saved_plan_cannot_acquire_existing_render_identity(source_job, tmp_path, change):
    service, identity, _ = source_job
    path = service.root / ('plan-' + identity + '.json')
    value = json.loads(path.read_bytes())
    if change == 'title':
        value['title'] = 'Changed title'
    else:
        value['roles'][0]['volume'] = .75
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='plan differs'):
        service.export_epub(identity, tmp_path / 'refused.epub')
    assert not (tmp_path / 'refused.epub').exists()


def test_exact_legacy_render_key_remains_readable_but_changed_plan_is_refused(source_job, tmp_path):
    service, identity, _ = source_job
    marker = service.root / 'renders' / identity / 'complete.json'
    value = json.loads(marker.read_bytes())
    value.pop('plan_sha256')
    marker.write_text(json.dumps(value), encoding='utf-8')
    assert service.export_epub(identity, tmp_path / 'legacy.epub')['status'] == 'complete'
    path = service.root / ('plan-' + identity + '.json')
    plan = json.loads(path.read_bytes())
    plan['title'] = 'Rebound historical title'
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='historical render'):
        service.export_epub(identity, tmp_path / 'refused.epub')


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_unknown_job_existing_output_and_private_destinations_do_not_launch_native_tools(source_job, tmp_path, monkeypatch, kind):
    service, identity, _ = source_job
    monkeypatch.setattr(publication_io, 'run_process', lambda *a, **k: pytest.fail('No native probe should run.'))
    existing = output_path(tmp_path, kind)
    existing.write_bytes(b'Existing user file')
    with pytest.raises(FileExistsError):
        export(service, identity, existing, kind)
    assert existing.read_bytes() == b'Existing user file'
    with pytest.raises(ValueError, match='private'):
        export(service, identity, output_path(service.private_root, kind), kind)
    with pytest.raises(ValueError, match='audio project'):
        export(service, identity, output_path(service.root, kind), kind)
    with pytest.raises((ValueError, FileNotFoundError)):
        export(service, '0' * 64, output_path(tmp_path, kind, 'unknown'), kind)
    assert not (service.root / 'renders' / ('0' * 64)).exists()


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
@pytest.mark.parametrize('phase', ['Copying', 'Packaging', 'Prepared publication'])
def test_explicit_cancel_at_prepublication_boundaries_leaves_no_final_file(source_job, tmp_path, kind, phase):
    service, identity, _ = source_job
    stop = Event()
    def progress(message):
        if message.startswith(phase):
            stop.set()
            raise RuntimeError('Observer failure must not hide explicit cancellation.')
    output = output_path(tmp_path, kind)
    with pytest.raises(RenderCancelled):
        export(service, identity, output, kind, cancelled=stop.is_set, progress=progress)
    assert not output.exists()
    assert not list(tmp_path.glob('.pending-epub-*')) and not list(tmp_path.glob('.pending-daisy-*'))


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_progress_observer_exception_cannot_replace_complete_publication(source_job, tmp_path, kind):
    service, identity, _ = source_job
    def progress(_message):
        raise RuntimeError('Synthetic observer failure')
    result = export(service, identity, output_path(tmp_path, kind), kind, progress=progress)
    assert result['status'] == 'complete'


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_late_cancel_after_atomic_publication_preserves_complete_result(source_job, tmp_path, monkeypatch, kind):
    service, identity, _ = source_job
    stop = Event()
    publish = publishing.publish_noreplace
    def completed(stage, target):
        publish(stage, target)
        stop.set()
    monkeypatch.setattr(publishing, 'publish_noreplace', completed)
    output = output_path(tmp_path, kind)
    result = export(service, identity, output, kind, cancelled=stop.is_set)
    assert stop.is_set() and result['status'] == 'complete'
    assert inspector(kind)(output)['publication_id'] == result['id']


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_late_competing_destination_is_never_overwritten(source_job, tmp_path, monkeypatch, kind):
    service, identity, _ = source_job
    publish = publishing.publish_noreplace
    def competing(stage, target):
        Path(target).write_bytes(b'Competing user file')
        publish(stage, target)
    monkeypatch.setattr(publishing, 'publish_noreplace', competing)
    output = output_path(tmp_path, kind)
    with pytest.raises(RuntimeError, match='did not confirm'):
        export(service, identity, output, kind)
    assert output.read_bytes() == b'Competing user file'
    record, = service.publications()
    assert record['status'] == 'prepared'
    with pytest.raises(ValueError, match='receipt'):
        service.verify(record['id'], output=output)


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_prepared_receipt_recovers_published_file_after_finalization_failure(source_job, tmp_path, monkeypatch, kind):
    service, identity, _ = source_job
    original = publishing.atomic_json
    def fail_final(path, value, **options):
        if value.get('schema') == 'carr-audio-publication/1' and value.get('status') == 'complete':
            raise OSError('Synthetic receipt finalization failure')
        return original(path, value, **options)
    output = output_path(tmp_path, kind)
    with monkeypatch.context() as patch:
        patch.setattr(publishing, 'atomic_json', fail_final)
        with pytest.raises(RuntimeError, match='saved'):
            export(service, identity, output, kind)
    before = output.read_bytes()
    record, = service.publications()
    assert record['status'] == 'prepared'
    assert service.verify(record['id'])['status'] == 'complete'
    assert output.read_bytes() == before


def test_source_changed_after_packaging_is_rejected_before_publication(source_job, tmp_path):
    service, identity, _ = source_job
    source = service.root / 'renders' / identity / 'timeline.json'
    def progress(message):
        if message.startswith('Rechecking source'):
            value = json.loads(source.read_bytes())
            value['lines'][0]['text'] += ' changed'
            source.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='size|checksum'):
        service.export_epub(identity, tmp_path / 'refused.epub', progress=progress)
    assert not (tmp_path / 'refused.epub').exists()


def test_changed_parent_after_destination_selection_is_refused(source_job, tmp_path):
    service, identity, _ = source_job
    parent = tmp_path / 'chosen'
    parent.mkdir()
    output = parent / 'book.epub'
    approval = capture_export_destination(output, private_root=service.private_root)
    parent.rename(tmp_path / 'moved')
    parent.mkdir()
    with pytest.raises(ValueError, match='destination changed'):
        service.export_epub(identity, output, approval=approval)
    assert not output.exists()


def test_explicit_byte_budget_rejects_before_native_probe(source_job, tmp_path, monkeypatch):
    service, identity, _ = source_job
    service.budget = PublicationBudget(max_output_bytes=64)
    monkeypatch.setattr(publication_io, 'run_process', lambda *a, **k: pytest.fail('Budget refusal precedes probing.'))
    with pytest.raises(ValueError, match='byte budget'):
        service.export_epub(identity, tmp_path / 'small.epub')
    assert not (tmp_path / 'small.epub').exists()


@pytest.mark.parametrize('kind', ['epub', 'daisy'])
def test_xml_source_and_total_budget_checks_remain_active(source_job, tmp_path, monkeypatch, kind):
    service, identity, _ = source_job
    output = output_path(tmp_path, kind)
    export(service, identity, output, kind)
    if kind == 'daisy':
        from carr_library.audio import daisy
        monkeypatch.setattr(daisy, 'MAX_XML_TOTAL', 20)
    else:
        monkeypatch.setattr(publishing, 'MAX_XML_TOTAL', 20)
    with pytest.raises(ValueError, match='metadata budget'):
        inspector(kind)(output)


def test_epub_refuses_active_xhtml_without_external_resource_access(source_job, tmp_path):
    service, identity, _ = source_job
    original, altered = tmp_path / 'valid.epub', tmp_path / 'active.epub'
    service.export_epub(identity, original)
    rewrite(original, altered, {'EPUB/content.xhtml': lambda raw: raw.replace(
        b'</body>', b'<script>window.location="https://invalid.example"</script></body>')})
    with pytest.raises(ValueError, match='active|unsupported'):
        publishing.inspect_epub(altered)


def test_zip_directory_budget_is_checked_before_zipfile_allocation(tmp_path, monkeypatch):
    path = tmp_path / 'small-false-directory.epub'
    path.write_bytes(struct.pack('<4s4H2LH', b'PK\x05\x06', 0, 0, 10, 10, 65537, 0, 0))
    monkeypatch.setattr(publication_io, 'ZipFile', lambda *a, **k: pytest.fail('Oversized directory reached ZipFile.'))
    with pytest.raises(ValueError, match='directory'):
        publishing.inspect_epub(path)


def test_minimum_valid_pause_retains_its_integer_frames(source_job):
    service, identity, _ = source_job
    from carr_library.audio.publication_source import load_source
    source = load_source(service.root, identity, lambda: None, service.budget)
    raw = json.loads((service.root / 'renders' / identity / 'timeline.json').read_bytes())
    pause = next(event for scene in raw['scenes'] for event in scene['events'] if event['kind'] == 'pause')
    assert pause['frames'] == 16
    assert source.timeline['duration'] == source.marker['frames'] / 16000


@pytest.mark.parametrize('include_audio', [False, True])
def test_project_snapshot_retains_inert_receipts_and_explicit_verification(source_job, tmp_path, include_audio):
    service, identity, plan = source_job
    from carr_library.audio.backup import backup_project, restore_project
    from carr_library.ui.audio_studio import StudioController
    controller = StudioController(private_root=service.private_root)
    controller.new(plan.original_script, title=plan.title, input_kind='script')
    controller.configure(output_dir=service.root)
    project_file = tmp_path / 'saved-project.json'
    controller.save(project_file)
    output = tmp_path / 'saved.epub'
    record = service.export_epub(identity, output)
    expected = output.read_bytes()
    archive = tmp_path / 'snapshot.zip'
    backup_project(project_file, archive, project_dir=service.root, include_cache=False, include_audio=include_audio)
    restored = restore_project(archive, tmp_path / 'restored')
    restored_service = PublicationService(restored['project_dir'], private_root=service.private_root, ffmpeg=service.ffmpeg)
    restored_record, = restored_service.publications()
    assert restored_record == record
    if not include_audio:
        assert not (Path(restored['project_dir']) / 'renders').exists()
    with pytest.raises(ValueError, match='restored elsewhere'):
        restored_service.verify(record['id'])
    shutil.rmtree(service.root)
    assert restored_service.verify(record['id'], output=output)['status'] == 'complete'
    assert output.read_bytes() == expected


def test_typed_receipt_cannot_grant_library_evidence_authority(source_job, tmp_path):
    service, identity, _ = source_job
    record = service.export_epub(identity, tmp_path / 'book.epub')
    record['source']['approved_library_evidence'] = True
    with pytest.raises(ValueError, match='source provenance'):
        validate_publication_receipt(record)
    record['source']['approved_library_evidence'] = 0
    with pytest.raises(ValueError, match='source provenance'):
        validate_publication_receipt(record)
