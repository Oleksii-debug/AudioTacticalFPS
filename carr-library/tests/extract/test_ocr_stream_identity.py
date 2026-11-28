"""Exact OCR wire compatibility and real streamed PDF source authority."""
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import threading

import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library.catalog import intake
from carr_library.extract import page_ocr
from carr_library.extract.page_ocr import Config, ExtendedConfig, config_from_dict

LEGACY = {'languages': 'eng', 'dpi': 300, 'timeout': 30,
          'max_pixels': 20_000_000, 'orientation': False,
          'model_policy': 'default', 'model_profile': 'installed'}


def document(path, *, pages=1):
    canvas = Canvas(str(path), pagesize=(200, 200))
    for index in range(1, pages + 1):
        canvas.drawString(20, 150, f'Exact physical page {index}.')
        canvas.showPage()
    canvas.save()
    return path


def test_legacy_wire_identity_remains_exact_and_extended_round_trips_without_dropping_policy():
    assert asdict(Config()) == LEGACY
    assert vars(Config()) == LEGACY
    assert not Config().automatic_languages
    encoded = json.dumps(LEGACY, sort_keys=True, separators=(',', ':')).encode()
    assert json.dumps(asdict(config_from_dict(LEGACY)),
                      sort_keys=True, separators=(',', ':')).encode() == encoded
    for automatic in (False, True):
        values = {**LEGACY, 'automatic_languages': automatic}
        result = config_from_dict(values)
        assert type(result) is ExtendedConfig
        assert asdict(result) == values and result.automatic_languages is automatic


@pytest.mark.parametrize('values', [
    {}, {**LEGACY, 'unexpected': True}, {**LEGACY, 'automatic_languages': 1},
    {**LEGACY, 'automatic_languages': None}, list(LEGACY),
])
def test_frozen_config_never_silently_discards_or_supplies_missing_fields(values):
    with pytest.raises(ValueError):
        config_from_dict(values)


def test_exact_actual_pdf_agrees_in_legacy_and_file_backed_paths(tmp_path):
    path = document(tmp_path / 'actual.pdf')
    original = intake.file_snapshot(tmp_path, path.name)
    legacy = page_ocr.extract_page(path, 1)
    streamed = page_ocr.extract_page(path, 1, original=original)
    assert streamed == legacy
    assert streamed['raw_text'] == 'Exact physical page 1.\n'
    assert streamed['provenance']['config'] == LEGACY
    assert 'source_format' not in streamed['provenance']
    assert 'source_unit' not in streamed['provenance']
    assert streamed['provenance']['asset_sha256'] == sha256(path.read_bytes()).hexdigest()


def test_real_128_mib_pdf_is_streamed_and_legacy_default_still_refuses(tmp_path):
    fixture_path = Path(__file__).resolve().parents[1] / 'ingest' / 'large_source_fixtures.py'
    spec = importlib.util.spec_from_file_location('carr_reviewed_large_pdf_fixture', fixture_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.write_large_pdf(tmp_path / 'large.pdf')
    path = fixture['path']
    assert path.stat().st_size == 128 * 1024**2
    with pytest.raises(ValueError):
        page_ocr.extract_page(path, 1)
    original = intake.file_snapshot(tmp_path, path.name, max_bytes=fixture['size'])
    result = page_ocr.extract_page(path, 1, Config(timeout=120), original=original)
    assert result['raw_text'].strip() == 'Large PDF railway evidence.'
    assert result['provenance']['method'] == 'native'
    assert result['provenance']['asset_sha256'] == fixture['sha256']
    assert path.stat().st_size == fixture['size']


def test_actual_1001_page_pdf_requires_explicit_reviewed_page_allowance(tmp_path):
    path = document(tmp_path / 'many-pages.pdf', pages=1001)
    original = intake.file_snapshot(tmp_path, path.name)
    with pytest.raises(ValueError, match='oversized'):
        page_ocr.extract_page(path, 1001, Config(timeout=120), original=original)
    result = page_ocr.extract_page(path, 1001, Config(timeout=120),
                                  original=original, max_pages=1001)
    assert result['physical_index'] == 1001
    assert result['raw_text'] == 'Exact physical page 1001.\n'
    assert result['printed_label'] is None and not result['printed_label_verified']


def test_file_snapshot_from_another_actual_path_never_authorizes_this_pdf(tmp_path):
    selected = document(tmp_path / 'selected.pdf')
    other = tmp_path / 'other.pdf'
    other.write_bytes(selected.read_bytes())
    original = intake.file_snapshot(tmp_path, other.name)
    with pytest.raises(ValueError, match='captured original'):
        page_ocr.extract_page(selected, 1, original=original)


def test_real_mutation_after_parsing_is_rehashed_even_when_observed_stamps_are_held(tmp_path, monkeypatch):
    path = document(tmp_path / 'mutable.pdf')
    original = intake.file_snapshot(tmp_path, path.name)
    initial = path.read_bytes()
    captured = path.stat()
    real_extract = page_ocr._extract_page_stream
    observed = []
    def mutate_after_real_parsers(*args, **kwargs):
        result = real_extract(*args, **kwargs)
        observed.append(result['raw_text'])
        assert initial.endswith(b'\n')
        with path.open('r+b') as stream:
            stream.seek(len(initial) - 1)
            stream.write(b' ')
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(path, ns=(captured.st_atime_ns, captured.st_mtime_ns))
        return result
    # This holds only observed file stamps, including Windows deferred metadata.
    # The bytes, descriptor, parsers and complete SHA256 verifier remain real.
    monkeypatch.setattr(intake, '_stamp', lambda info: original.identity)
    monkeypatch.setattr(page_ocr, '_extract_page_stream', mutate_after_real_parsers)
    with pytest.raises(ValueError, match='digest changed'):
        page_ocr.extract_page(path, 1, original=original)
    assert observed == ['Exact physical page 1.\n']
    assert path.stat().st_ino == captured.st_ino
    assert path.stat().st_size == captured.st_size
    assert sha256(path.read_bytes()).hexdigest() != original.digest


def test_cancellation_after_actual_parser_work_never_returns_a_candidate(tmp_path, monkeypatch):
    path = document(tmp_path / 'cancelled.pdf')
    original = intake.file_snapshot(tmp_path, path.name)
    stopped = threading.Event()
    real_extract = page_ocr._extract_page_stream
    observed = []
    def cancel_after_parsers(*args, **kwargs):
        result = real_extract(*args, **kwargs)
        observed.append(result['raw_text'])
        stopped.set()
        return result
    monkeypatch.setattr(page_ocr, '_extract_page_stream', cancel_after_parsers)
    with pytest.raises(InterruptedError):
        page_ocr.extract_page(path, 1, original=original, cancelled=stopped.is_set)
    assert observed == ['Exact physical page 1.\n']
    assert sha256(path.read_bytes()).hexdigest() == original.digest


@pytest.mark.parametrize('maximum', [True, 0, -1, 1.5, 2**31])
def test_page_allowance_rejects_nonintegral_or_unbounded_values(tmp_path, maximum):
    path = document(tmp_path / 'limits.pdf')
    with pytest.raises(ValueError, match='allowance'):
        page_ocr.extract_page(path, 1, max_pages=maximum)
