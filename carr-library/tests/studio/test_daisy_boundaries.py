"""Independent canonical DAISY evidence, approval and post-commit regressions."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from threading import Event
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.research.export_file import capture_export_destination
from carr_library.studio import daisy, publishing
from carr_library.studio.publishing import PublicationService

_spec = importlib.util.spec_from_file_location('daisy_boundary_fixtures',
    Path(__file__).with_name('test_publishing.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
actual_job, source_job = _fixtures.actual_job, _fixtures.source_job


def export(source_job, output, **kwargs):
    service, state = source_job
    return PublicationService(service).export_daisy(state['id'], output,
        language='uk', publisher='Explicit synthetic boundary fixture', **kwargs)


def rewritten(source, destination, changes):
    with ZipFile(source) as original, ZipFile(destination, 'x') as changed:
        for info in original.infolist():
            raw = original.read(info.filename)
            changed.writestr(info, changes.get(info.filename, lambda data: data)(raw))


def test_first_smil_reference_is_a_non_narrated_heading(source_job, tmp_path):
    output = tmp_path / 'heading.zip'
    export(source_job, output)
    with ZipFile(output) as archive:
        smil = ET.fromstring(archive.read('book.smil'))
        content = ET.fromstring(archive.read('content.html'))
        first = smil.find('body/seq/par')
        target = first.find('text').get('src').split('#', 1)[1]
        node = next(row for row in content.iter() if row.get('id') == target)
        assert node.tag in {'{' + publishing.XHTML + '}h' + str(i) for i in range(1, 7)}
        assert first.get('dur') == '0s' and first.find('audio') is None
        intervals = smil.findall('body/seq/par')[1:]
        proof = daisy.inspect_daisy(output)
        assert len(intervals) == len(proof['intervals'])
        assert [row.find('text').get('src').split('#', 1)[1] for row in intervals] == [
            row['id'] for row in proof['intervals']]


def test_rebound_readable_script_still_requires_its_recorded_source_hash(source_job, tmp_path):
    original, altered = tmp_path / 'original.zip', tmp_path / 'changed.zip'
    export(source_job, original)
    appended = '\nChanged derivative text without changing the recorded source identity.'
    def provenance(raw):
        value = json.loads(raw)
        value['original_script'] += appended
        return encode(value)
    def readable(raw):
        root = ET.fromstring(raw)
        node = root.find('.//{' + publishing.XHTML + '}pre')
        node.text += appended
        return daisy._serialize(root, daisy.HTML_DTD)
    rewritten(original, altered, {'provenance.json': provenance, 'source.html': readable})
    with pytest.raises(ValueError, match='source hash'):
        daisy.inspect_daisy(altered)


def test_daisy_keeps_original_measured_values_and_explicit_rounding(source_job, tmp_path):
    service, state = source_job
    output = tmp_path / 'evidence.zip'
    export(source_job, output)
    proof = daisy.inspect_daisy(output)
    spoken = [row for row in proof['intervals'] if row['kind'] == 'line']
    assert len(spoken) == len(state['result']['lines'])
    for row, source in zip(spoken, state['result']['lines']):
        assert (row['measured_start'], row['measured_end']) == (source['start'], source['end'])
        assert (row['start'], row['end']) == (round(source['start'], 6), round(source['end'], 6))
        assert row['source_ref'] == source['source_ref']
    assert 'not proof of sample, word or acoustic alignment' in proof['timing_notice']


@pytest.mark.parametrize('change', ['new-file','parent-replaced'])
def test_daisy_preserves_captured_destination_through_queue(source_job, tmp_path, change):
    service, _ = source_job
    directory = tmp_path / 'chosen'; directory.mkdir()
    output = directory / 'chosen.zip'
    approval = capture_export_destination(output, private_root=service.root.parent.parent)
    if change == 'new-file': output.write_bytes(b'Other application bytes')
    else:
        directory.rename(tmp_path / 'previous-chosen')
        directory.mkdir()
    with pytest.raises(ValueError, match='destination changed'):
        export(source_job, output, approval=approval)
    if change == 'new-file': assert output.read_bytes() == b'Other application bytes'
    else: assert not output.exists()
    assert not list(directory.glob('.pending-daisy-*'))


def test_daisy_never_exports_into_private_library(source_job):
    service, _ = source_job
    output = service.root.parent.parent / 'private.zip'
    with pytest.raises(ValueError, match='private library'): export(source_job, output)
    assert not output.exists()


def test_daisy_atomic_late_competing_create_is_not_overwritten(source_job, tmp_path, monkeypatch):
    service, _ = source_job
    output = tmp_path / 'competing.zip'
    original = publishing.publish_file
    def competing(staged, destination, **kwargs):
        Path(destination).write_bytes(b'Late competing bytes')
        return original(staged, destination, **kwargs)
    monkeypatch.setattr(publishing, 'publish_file', competing)
    with pytest.raises(RuntimeError, match='Publication receipt:'): export(source_job, output)
    assert output.read_bytes() == b'Late competing bytes'
    record, = PublicationService(service).publications()
    assert record['status'] == 'prepared' and record['format'] == 'daisy-2.02'
    with pytest.raises(ValueError, match='receipt'): PublicationService(service).verify(record['id'])


def test_daisy_cancellation_after_atomic_commit_preserves_success(source_job, tmp_path, monkeypatch):
    service, _ = source_job
    stopped = Event()
    original = publishing.publish_file
    def completed(staged, destination, **kwargs):
        result = original(staged, destination, **kwargs)
        stopped.set()
        return result
    monkeypatch.setattr(publishing, 'publish_file', completed)
    record = export(source_job, tmp_path / 'complete.zip', cancelled=stopped.is_set)
    assert stopped.is_set() and record['status'] == 'complete'
    publisher = PublicationService(service)
    assert read_json(publisher.receipts / (record['id'] + '.json')) == record
    assert publisher.verify(record['id']) == record


def test_daisy_metadata_reads_share_a_total_budget(source_job, tmp_path, monkeypatch):
    output = tmp_path / 'bounded.zip'; export(source_job, output)
    monkeypatch.setattr(daisy, 'MAX_XML_TOTAL', 128)
    with pytest.raises(ValueError, match='metadata budget'): daisy.inspect_daisy(output)


@pytest.mark.parametrize('change', ['utf16-entity','internal-entity','unsupported-rate','non-audio-heading'])
def test_daisy_rejects_nonlocal_xml_and_false_audio_evidence(source_job, tmp_path, change):
    original, altered = tmp_path / 'original.zip', tmp_path / 'altered.zip'
    export(source_job, original)
    if change == 'utf16-entity':
        name = 'content.html'
        def mutate(raw):
            return ('<?xml version="1.0" encoding="UTF-16"?>'
                '<!DOCTYPE html [<!ENTITY text SYSTEM "file:///unrelated">]>'
                '<html xmlns="' + publishing.XHTML + '"><body>&text;</body></html>').encode('utf-16')
    elif change == 'internal-entity':
        name = 'content.html'
        mutate = lambda raw: raw.replace(b'<head>', b'<!ENTITY expand "unapproved"><head>', 1)
    elif change == 'unsupported-rate':
        name = 'provenance.json'
        def mutate(raw):
            value = json.loads(raw); value['audio']['sample_rate'] = 8000
            return encode(value)
    else:
        name = 'book.smil'
        mutate = lambda raw: raw.replace(b'dur="0s"', b'dur="1s"', 1)
    rewritten(original, altered, {name: mutate})
    with pytest.raises(ValueError): daisy.inspect_daisy(altered)


def test_existing_epub_receipts_still_default_to_epub(source_job, tmp_path):
    service, state = source_job
    publisher = PublicationService(service)
    record = publisher.export_epub(state['id'], tmp_path / 'legacy.epub')
    assert 'format' not in record
    assert publisher.verify(record['id']) == record
    daisy_record = export(source_job, tmp_path / 'daisy.zip')
    daisy_record['format'] = 'unrecognized'
    atomic_json(publisher.receipts / (daisy_record['id'] + '.json'), daisy_record, replace=True)
    with pytest.raises(ValueError, match='receipt'): publisher.verify(daisy_record['id'])
    with pytest.raises(ValueError, match='receipt'): publisher.publications()


def test_daisy_queued_ui_preserves_destination_approval(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.format_choice.SetSelection(1)
    output = tmp_path / 'queued.zip'; dialog.output.ChangeValue(str(output))
    dialog.publisher.ChangeValue('Explicit queued publisher')
    pending = []
    frame.run_job = _ui_fixtures.queue_job(frame, pending)
    dialog.on_export()
    assert dialog.busy
    output.write_bytes(b'File appeared after explicit export approval')
    operation, done, failed = pending[0]
    try: done(operation())
    except Exception as exc: failed(exc)
    assert output.read_bytes() == b'File appeared after explicit export approval'
    assert 'destination changed' in dialog.status.GetValue() and not dialog.receipts


# Exercise the real existing handlers with widget stand-ins, retaining the shared
# operation lifecycle. The separate Windows smoke uses actual native wx widgets.
_ui_spec = importlib.util.spec_from_file_location('daisy_boundary_handlers',
    Path(__file__).with_name('test_publishing_handlers.py'))
_ui_fixtures = importlib.util.module_from_spec(_ui_spec)
_ui_spec.loader.exec_module(_ui_fixtures)
desktop = _ui_fixtures.desktop
