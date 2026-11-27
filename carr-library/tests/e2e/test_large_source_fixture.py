"""Exercise the actual streaming acceptance-fixture producers at small size."""
from contextlib import closing
from hashlib import sha256
import importlib.util
from pathlib import Path

from carr_library.ingest.file_pdf import open_pdf
from carr_library.ingest.source_profiles import LargeTextProfile, PdfProfile
from carr_library.ingest.streamed_text import inspect_text


def producers():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'large_source_acceptance.py'
    spec = importlib.util.spec_from_file_location('carr_large_source_acceptance_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_streamed_fixture_produces_real_late_source_positions_and_digest(tmp_path):
    path = tmp_path / 'fixture.txt'
    expected = producers().make_text(path, 1)
    plan = inspect_text(path, LargeTextProfile(path.stat().st_size))
    assert plan.digest == expected == sha256(path.read_bytes()).hexdigest()
    assert 10000 < len(plan.fragments) < 65536
    assert plan.fragments[-1].sha256 == sha256('Tailproofneedle exact source Україна\r\n'.encode()).hexdigest()


def test_real_incremental_pdf_fixture_keeps_current_parser_and_render_semantics(tmp_path):
    path = tmp_path / 'fixture.pdf'
    expected = producers().make_pdf(path, 1)
    assert path.stat().st_size > 1024**2 and expected == sha256(path.read_bytes()).hexdigest()
    with open_pdf(path, PdfProfile(path.stat().st_size), expected_digest=expected) as (reader, document, _):
        assert len(reader.pages) == len(document) == 1
        assert 'Pdfprofileproof exact painted source' in reader.pages[0].extract_text()
        with closing(document[0]) as page:
            with closing(page.get_textpage()) as text:
                assert 'Pdfprofileproof exact painted source' in text.get_text_range()


def test_independent_large_pdf_allowance_is_admitted_before_any_fixture(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import carr_library
    import pytest

    module = producers()
    # Enough for the ordinary 257 MiB TXT phase, but insufficient for an
    # independently selected 3 GiB PDF original and its retained backups.
    available = 12 * 257 * module.MIB + 2 * 1024**3 + module.MIB
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=available))
    calls = []
    monkeypatch.setattr(module, 'text_case', lambda *args: calls.append('txt'))
    monkeypatch.setattr(module, 'pdf_case', lambda *args: calls.append('pdf'))
    report = tmp_path / 'measured.json'
    with pytest.raises(ValueError, match='Insufficient free space'):
        module.main(['--workdir', str(tmp_path), '--output', str(report),
                     '--expected-package', str(Path(carr_library.__file__).resolve(strict=True)),
                     '--txt-mib', '257', '--pdf-mib', '3072'])
    assert calls == []
    assert not report.exists()
