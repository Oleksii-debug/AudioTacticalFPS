"""Canonical export boundaries exercised with actual Studio-rendered artifacts."""
import importlib.util
import json
from pathlib import Path
from threading import Event
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from carr_library.durable_state import read_json
from carr_library.research.export_file import capture_export_destination
from carr_library.studio import publishing
from carr_library.studio.publishing import PublicationService, inspect_epub

_spec = importlib.util.spec_from_file_location('publishing_boundary_fixtures',
    Path(__file__).with_name('test_publishing.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
actual_job = _fixtures.actual_job
source_job = _fixtures.source_job


def rewrite_member(source, destination, name, transform):
    with ZipFile(source) as original, ZipFile(destination, 'x') as updated:
        for info in original.infolist():
            content = original.read(info.filename)
            updated.writestr(info, transform(content) if info.filename == name else content)


def test_export_cannot_write_anywhere_inside_private_library(source_job):
    service, state = source_job
    output = service.root.parent.parent / 'outside-studio-but-private.epub'
    with pytest.raises(ValueError, match='private library'):
        PublicationService(service).export_epub(state['id'], output)
    assert not output.exists()


@pytest.mark.parametrize('mutation', ['overlap', 'past-master', 'disappearing-line', 'chapter-gap'])
def test_measured_speech_is_never_clamped_or_silently_omitted(source_job, tmp_path, mutation):
    service, state = source_job
    def change(timeline):
        if mutation == 'overlap':
            timeline['lines'][1]['start'] = timeline['lines'][0]['end'] - 1 / timeline['sample_rate']
        elif mutation == 'past-master':
            timeline['lines'][-1]['end'] = timeline['duration'] + 1 / timeline['sample_rate']
        elif mutation == 'disappearing-line':
            timeline['lines'][0]['end'] = timeline['lines'][0]['start'] + .0000002
        else:
            timeline['chapters'][0]['start'] = 1 / timeline['sample_rate']
    _fixtures.altered_timeline(service, state, change)
    output = tmp_path / 'invalid-measured-interval.epub'
    with pytest.raises(ValueError):
        PublicationService(service).export_epub(state['id'], output)
    assert not output.exists()


def test_original_measured_values_and_rounding_are_explicit(source_job, tmp_path):
    service, state = source_job
    output = tmp_path / 'measured.epub'
    PublicationService(service).export_epub(state['id'], output)
    value = inspect_epub(output)
    spoken = [row for row in value['intervals'] if row['kind'] == 'line']
    assert len(spoken) == len(state['result']['lines'])
    for row, measured in zip(spoken, state['result']['lines']):
        assert row['measured_start'] == measured['start']
        assert row['measured_end'] == measured['end']
        assert (row['start'], row['end']) == (round(measured['start'], 6), round(measured['end'], 6))
    assert 'not proof of sample, word or acoustic alignment' in value['timing_notice']
    assert all(row['measured_start'] is None and row['measured_end'] is None
               for row in value['intervals'] if row['kind'] == 'audio-interval')


@pytest.mark.parametrize('changed', ['new-file', 'parent-replaced'])
def test_chooser_identity_survives_queued_work(source_job, tmp_path, changed):
    service, state = source_job
    directory = tmp_path / 'chosen'; directory.mkdir()
    output = directory / 'chosen.epub'
    approval = capture_export_destination(output, private_root=service.root.parent.parent)
    if changed == 'new-file':
        output.write_bytes(b'Another application owns this destination.')
    else:
        directory.rename(tmp_path / 'previous-chosen')
        directory.mkdir()
    with pytest.raises(ValueError, match='destination changed'):
        PublicationService(service).export_epub(state['id'], output, approval=approval)
    if changed == 'new-file':
        assert output.read_bytes() == b'Another application owns this destination.'
    else:
        assert not output.exists() and not (tmp_path / 'previous-chosen/chosen.epub').exists()
    assert not list(directory.glob('.pending-epub-*'))


def test_atomic_publication_does_not_clobber_a_late_competing_create(source_job, tmp_path, monkeypatch):
    service, state = source_job
    output = tmp_path / 'atomic-race.epub'
    original = publishing.publish_file
    def competing(staged, destination, **kwargs):
        Path(destination).write_bytes(b'Late competing file')
        return original(staged, destination, **kwargs)
    monkeypatch.setattr(publishing, 'publish_file', competing)
    publisher = PublicationService(service)
    with pytest.raises(RuntimeError, match='Publication receipt:'):
        publisher.export_epub(state['id'], output)
    assert output.read_bytes() == b'Late competing file'
    assert not list(tmp_path.glob('.pending-epub-*'))
    pending, = publisher.publications()
    assert pending['status'] == 'prepared'
    with pytest.raises(ValueError, match='receipt'):
        publisher.verify(pending['id'])


def test_cancellation_after_atomic_publication_preserves_complete_result(source_job, tmp_path, monkeypatch):
    service, state = source_job
    output = tmp_path / 'already-published.epub'
    stopped = Event()
    original = publishing.publish_file
    def publish_then_cancel(staged, destination, **kwargs):
        result = original(staged, destination, **kwargs)
        stopped.set()
        return result
    monkeypatch.setattr(publishing, 'publish_file', publish_then_cancel)
    publisher = PublicationService(service)
    complete = publisher.export_epub(state['id'], output, cancelled=stopped.is_set)
    assert stopped.is_set() and complete['status'] == 'complete'
    assert read_json(publisher.receipts / (complete['id'] + '.json')) == complete
    assert publisher.verify(complete['id']) == complete


@pytest.mark.parametrize('change', ['utf16-dtd', 'manifest-alias', 'external-css', 'source-hash'])
def test_bounded_profile_rejects_hidden_entities_and_rebound_package_members(source_job, tmp_path, change):
    service, state = source_job
    source, altered = tmp_path / 'source.epub', tmp_path / 'altered.epub'
    PublicationService(service).export_epub(state['id'], source)
    if change == 'utf16-dtd':
        name = 'META-INF/container.xml'
        def transform(raw):
            return ('<?xml version="1.0" encoding="UTF-16"?>'
                '<!DOCTYPE container [<!ENTITY entry "EPUB/package.opf">]>'
                '<container xmlns="' + publishing.CONTAINER + '" version="1.0">'
                '<rootfiles><rootfile full-path="&entry;" media-type="application/oebps-package+xml"/>'
                '</rootfiles></container>').encode('utf-16')
    elif change == 'manifest-alias':
        name = 'EPUB/package.opf'
        def transform(raw):
            root = ET.fromstring(raw)
            node = root.find('{' + publishing.OPF + '}manifest/{' + publishing.OPF + '}item[@id="audio"]')
            node.set('href', 'content.xhtml')
            return ET.tostring(root, encoding='utf-8', xml_declaration=True)
    elif change == 'external-css':
        name = 'EPUB/styles.css'
        transform = lambda raw: b'@import url("https://example.invalid/unrequested.css");'
    else:
        name = 'META-INF/metadata.xml'
        def transform(raw):
            root = ET.fromstring(raw)
            node = root.find('{' + publishing.PROVENANCE + '}provenance')
            value = json.loads(node.text)
            value['source']['source_script_sha256'] = '0' * 64
            node.text = json.dumps(value, ensure_ascii=False)
            return ET.tostring(root, encoding='utf-8', xml_declaration=True)
    rewrite_member(source, altered, name, transform)
    with pytest.raises(ValueError):
        inspect_epub(altered)


def test_total_metadata_limit_is_checked_during_archive_reads(source_job, tmp_path, monkeypatch):
    service, state = source_job
    output = tmp_path / 'bounded.epub'
    PublicationService(service).export_epub(state['id'], output)
    monkeypatch.setattr(publishing, 'MAX_XML_TOTAL', 128)
    with pytest.raises(ValueError, match='metadata budget'):
        inspect_epub(output)
