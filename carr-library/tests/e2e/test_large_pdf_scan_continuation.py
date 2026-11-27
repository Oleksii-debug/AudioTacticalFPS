"""Composition-only checks for bf603 scan continuation plus large-PDF streaming."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from carr_library.catalog.streaming_pdf import FileAsset, CHUNK_BYTES
from carr_library.ingest.batch_scan import scan_paths_page


def fixture_module():
    path = Path(__file__).parents[1] / 'ingest' / 'streaming_pdf_fixture.py'
    spec = importlib.util.spec_from_file_location('_carr_scan_large_pdf_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_more_than_5000_scanned_files_keep_the_large_pdf_once_across_pages(tmp_path):
    with TemporaryDirectory(prefix='scan-large-', dir=tmp_path) as temporary:
        root = Path(temporary).resolve()
        fixture = fixture_module()
        fixture.write_pdf(root / 'large.pdf')
        for number in range(5000):
            (root / f'source-{number:05d}.txt').write_text(f'Unique document {number}.', encoding='utf-8')
        cursor, rows, pages = None, {}, 0
        while True:
            page = scan_paths_page([root], cursor=cursor, max_files=1000)
            pages += 1
            assert pages <= 8
            for row in page.rows:
                assert row['state'] == 'queued', row
                assert row['source'] not in rows
                rows[row['source']] = row['manifest']
            cursor = page.cursor
            if cursor is None:
                break
        assert len(rows) == 5001 and pages >= 6
        original = FileAsset.capture(root / 'large.pdf')
        assert original.size > 64 * fixture.MIB
        assert rows[str(root / 'large.pdf')] == original.manifest()


def test_scan_cancel_during_large_pdf_hash_keeps_the_prior_cursor(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from carr_library.audio import jobs
    with TemporaryDirectory(prefix='scan-cancel-', dir=tmp_path) as temporary:
        root = Path(temporary).resolve()
        fixture = fixture_module()
        first = root / 'a-first.txt'
        first.write_text('First retained scan page.', encoding='utf-8')
        large = fixture.write_pdf(root / 'b-large.pdf')
        initial = scan_paths_page([first, large], max_files=1)
        assert initial.cursor is not None and len(initial.rows) == 1
        before = repr(initial.cursor)
        read_bytes = []
        actual_reader = jobs.verified_reader

        @contextmanager
        def observed_reader(path, **options):
            with actual_reader(path, **options) as stream:
                class Observed:
                    def __getattr__(self, name):
                        return getattr(stream, name)
                    def read(self, size=-1):
                        block = stream.read(size)
                        if Path(path) == large:
                            read_bytes.append(len(block))
                        return block
                yield Observed()

        monkeypatch.setattr(jobs, 'verified_reader', observed_reader)
        with pytest.raises(InterruptedError):
            scan_paths_page([first, large], cursor=initial.cursor,
                cancelled=lambda: sum(read_bytes) >= CHUNK_BYTES)
        assert sum(read_bytes) >= CHUNK_BYTES
        assert repr(initial.cursor) == before
        monkeypatch.setattr(jobs, 'verified_reader', actual_reader)
        continued = scan_paths_page([first, large], cursor=initial.cursor)
        assert [row['source'] for row in continued.rows] == [str(large)]
        assert continued.rows[0]['state'] == 'queued' and continued.cursor is None
