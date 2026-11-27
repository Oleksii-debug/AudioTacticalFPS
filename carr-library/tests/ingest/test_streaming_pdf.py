"""Strict supported-PDF streaming boundaries; no large-original guard bypass."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.catalog.intake import MAX_BYTES, IntakeError, snapshot
from carr_library.catalog.streaming_pdf import (CHUNK_BYTES, MAX_PDF_BYTES, MAX_READ_BYTES,
    FileAsset, _BoundedReader, pdf_budget, snapshot_original, valid_original_size)
from carr_library.audio.engines import _start_process, _stop
def _fixture():
    import importlib.util
    path = Path(__file__).with_name('streaming_pdf_fixture.py')
    spec = importlib.util.spec_from_file_location('_carr_streaming_pdf_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

MIB, write_pdf = _fixture().MIB, _fixture().write_pdf


@pytest.mark.parametrize('megabytes', [72, 144])
def test_real_large_pdf_has_bounded_fresh_process_resident_and_python_allocations(tmp_path, record_property, megabytes):
    probe = Path(__file__).with_name('streaming_pdf_probe.py').resolve(strict=True)
    flags = ['-I'] if sys.flags.isolated else []
    process = _start_process([sys.executable, '-B', *flags, str(probe), str(tmp_path), str(megabytes)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = process.communicate(timeout=180)
        assert process.returncode == 0, errors[-4000:].decode('utf-8', 'replace')
        assert len(output) <= 16_384
        report = json.loads(output)
    finally:
        _stop(process)
    assert report['input_bytes'] > megabytes * MIB > MAX_BYTES
    assert report['native_text'] is report['render_fingerprint'] is report['retained_readback'] is True
    assert report['real_corpus'] is report['multi_gigabyte_execution'] is False
    assert report['max_single_parser_read'] == MAX_READ_BYTES
    assert report['traced_peak_bytes'] < 32 * MIB
    assert report['peak_rss_bytes'] - report['baseline_peak_rss_bytes'] < 64 * MIB
    assert report['profile']['machine'] and report['profile']['cpu_count'] > 0
    record_property('large_pdf_measurement', json.dumps(report, sort_keys=True))


@pytest.mark.parametrize('limit', [0, -1, True, MAX_PDF_BYTES + 1])
def test_pdf_budget_is_finite_and_typed(limit):
    with pytest.raises(IntakeError):
        pdf_budget(limit)


def test_non_pdf_and_legacy_bytes_limits_remain(tmp_path):
    large = tmp_path / 'too-large.txt'
    with large.open('wb') as stream:
        block = b'a' * MIB
        for _ in range(65):
            stream.write(block)
    with pytest.raises(IntakeError, match='large'):
        snapshot_original(tmp_path, large.name)
    assert not valid_original_size('text/plain', MAX_BYTES + 1)
    assert not valid_original_size('application/epub+zip', MAX_BYTES + 1)
    assert valid_original_size('application/pdf', MAX_BYTES + 1)
    with pytest.raises(IntakeError):
        snapshot(tmp_path, large.name, max_bytes=MAX_BYTES + 1)


def test_bound_snapshot_rejects_replacement_and_rehashed_metadata(tmp_path):
    path = write_pdf(tmp_path / 'source.pdf', 32)
    asset = FileAsset.capture(path)
    path.rename(tmp_path / 'old.pdf')
    write_pdf(path, 32)
    with pytest.raises((ValueError, RuntimeError), match='identity|changed'):
        with asset.open():
            pass
    current = FileAsset.capture(path)
    with pytest.raises(IntakeError, match='manifest'):
        FileAsset.capture(path, expected_manifest=dict(current.manifest(), size=float(current.size)))
    with pytest.raises(IntakeError, match='identity'):
        FileAsset.capture(path, expected_identity=[float(value) for value in current.identity])
    with pytest.raises(IntakeError, match='checksum'):
        with replace(current, digest='0' * 64).open():
            pass


def test_original_hardlinks_and_foreign_relative_paths_are_rejected(tmp_path):
    source = write_pdf(tmp_path / 'source.pdf', 32)
    linked = tmp_path / 'linked.pdf'
    os.link(source, linked)
    assert source.stat().st_nlink == 2
    with pytest.raises(IntakeError, match='link'):
        FileAsset.capture(source)
    for name in ('../source.pdf', 'nested/../source.pdf', 'source.pdf:stream', 'nested\\source.pdf'):
        with pytest.raises(IntakeError):
            snapshot_original(tmp_path, name)


def test_parser_cannot_request_whole_source_or_oversized_read(tmp_path):
    path = write_pdf(tmp_path / 'source.pdf', 32)
    asset = FileAsset.capture(path)
    with asset.open() as stream:
        assert stream.read(5) == b'%PDF-'
        for size in (-1, MAX_READ_BYTES + 1):
            with pytest.raises(IntakeError, match='bounded'):
                stream.read(size)
        with pytest.raises(IntakeError, match='outside'):
            stream.seek(asset.size + 1)


def test_copy_cancellation_after_actual_bytes_removes_only_its_partial(tmp_path):
    source = write_pdf(tmp_path / 'source.pdf', 3 * MIB)
    asset = FileAsset.capture(source)
    destination = tmp_path / 'cancelled.pdf'
    observed = []
    def cancelled():
        if destination.exists() and destination.stat().st_size >= MIB:
            observed.append(destination.stat().st_size)
            return True
        return False
    with pytest.raises(InterruptedError):
        asset.copy_to(destination, cancelled=cancelled)
    assert observed and not destination.exists()
    assert FileAsset.capture(source).manifest() == asset.manifest()
    occupied = tmp_path / 'occupied.pdf'
    occupied.write_bytes(b'keep original')
    with pytest.raises(FileExistsError):
        asset.copy_to(occupied)
    assert occupied.read_bytes() == b'keep original'


def test_native_block_read_failure_cannot_produce_render_proof(tmp_path, monkeypatch):
    from carr_library.citations.pdf_review_attestation import render_fingerprint
    source = write_pdf(tmp_path / 'source.pdf', 32)
    asset = FileAsset.capture(source)
    observed = []
    def failed_read(self, buffer):
        observed.append(len(buffer))
        raise IntakeError('deliberate native block failure')
    monkeypatch.setattr(_BoundedReader, 'readinto', failed_read)
    with pytest.raises((IntakeError, RuntimeError), match='native block|streamed'):
        render_fingerprint(asset, 1)
    assert observed


def test_selected_byte_budget_is_bound_to_request_and_receipt_identity(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request
    from carr_library.ingest.publication_identity import request_identity
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('PDF request')
    source = write_pdf(tmp_path / 'source.pdf', 32)
    metadata = dict(title='PDF', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id='work', edition_id='edition')
    request = prepare_request(app.root, workspace, source, metadata, Config(), pdf_max_bytes=96 * MIB)
    assert json.loads(request_identity(request))['pdf_max_bytes'] == 96 * MIB
    with pytest.raises(ValueError, match='identity'):
        prepare_request(app.root, workspace, source, metadata, Config(),
                        request_id=request['request_id'], pdf_max_bytes=97 * MIB)
    legacy = dict(request)
    legacy.pop('pdf_max_bytes')
    assert 'pdf_max_bytes' not in json.loads(request_identity(legacy))
    assert request_identity(legacy) != request_identity(request)
    too_large = dict(request, expected_manifest=dict(sha256='a' * 64, mime='application/pdf', size=97 * MIB))
    with pytest.raises(ValueError, match='budget'):
        request_identity(too_large)


def test_streaming_parser_source_is_in_the_existing_runtime_identity():
    from carr_library.catalog import streaming_pdf
    from carr_library.extract.runtime_identity import _source_hashes
    expected = sha256(Path(streaming_pdf.__file__).read_bytes()).hexdigest()
    assert _source_hashes()['streaming_pdf.py'] == expected


def test_pdf_attestation_rechecks_file_even_with_an_injected_renderer(tmp_path):
    from carr_library.citations.anchors import Identity, Locator
    from carr_library.citations.pdf_review_attestation import ReviewedPage, pdf_review_attestor
    source = write_pdf(tmp_path / 'source.pdf', 32)
    asset = FileAsset.capture(source)
    identity = Identity('workspace', 'edition', asset.digest, 'revision')
    raw_hash = sha256(b'Large railway original').hexdigest()
    review = ReviewedPage(identity, 1, raw_hash, 'a' * 64, 'renderer', 'Independent reviewer')
    called = []
    attest = pdf_review_attestor('workspace', lambda _: asset, lambda *_: review,
        renderer=lambda *_: (called.append(True) or 'renderer', 'a' * 64))
    source.write_bytes(b'changed original')
    assert attest(identity, Locator('physical_page', 1), raw_hash) is False
    assert called == []


@pytest.mark.parametrize('replacement', ['different-file', 'new-hardlink'])
def test_copy_failure_preserves_a_replacement_after_owned_handles_close(tmp_path, monkeypatch, replacement):
    from contextlib import contextmanager
    from carr_library.audio import jobs
    source = write_pdf(tmp_path / 'source.pdf', MIB)
    asset = FileAsset.capture(source)
    destination = tmp_path / 'copy.pdf'
    foreign = tmp_path / 'foreign.pdf'
    foreign.write_bytes(b'user-owned replacement')
    original_reader = jobs.verified_reader
    observed = []

    @contextmanager
    def fail_after_close(path, **options):
        with original_reader(path, **options) as stream:
            yield stream
        # Both copy contexts have closed. Windows can perform real replacement here.
        assert destination.exists()
        if replacement == 'different-file':
            destination.unlink()
            foreign.rename(destination)
            observed.append(destination.stat().st_ino)
        else:
            os.link(destination, tmp_path / 'linked-copy.pdf')
            observed.append(destination.stat().st_nlink)
        raise OSError('controlled failure after owned handles close')

    monkeypatch.setattr(jobs, 'verified_reader', fail_after_close)
    with pytest.raises(OSError, match='controlled failure'):
        asset.copy_to(destination)
    assert observed and destination.exists()
    if replacement == 'different-file':
        assert destination.read_bytes() == b'user-owned replacement'
        assert destination.stat().st_ino == observed[0]
    else:
        assert destination.stat().st_nlink == 2
        assert (tmp_path / 'linked-copy.pdf').stat().st_ino == destination.stat().st_ino
    monkeypatch.setattr(jobs, 'verified_reader', original_reader)
    assert FileAsset.capture(source).manifest() == asset.manifest()
