"""Synthetic-only PDF/FTS5 runtime preflight. Not an application installer."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

LOCK = Path(__file__).resolve().with_name('requirements-pdf-smoke.lock')


def offline_install(wheelhouse: Path) -> None:
    """Use pip's own hash enforcement in a fresh, caller-created venv."""
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Refusing to install outside a virtual environment')
    if not wheelhouse.is_dir():
        raise ValueError('Local wheelhouse directory required')
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('PIP_')}
    env['PIP_CONFIG_FILE'] = os.devnull
    subprocess.run([
        sys.executable, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check',
        'install', '--no-index', '--no-deps', '--require-hashes', '--only-binary=:all:',
        '--no-cache-dir', '--force-reinstall', '--find-links', str(wheelhouse.resolve()),
        '-r', str(LOCK),
    ], env=env, check=True, timeout=120)


def probe() -> dict:
    """Use real upstream pypdf and SQLite against generated, nonprivate fixtures."""
    if sys.version_info < (3, 11):
        raise RuntimeError('Python 3.11 or newer required')
    import pypdf
    if importlib.metadata.version('pypdf') != '5.9.0':
        raise RuntimeError('Unexpected pypdf version for this smoke lock')
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    with tempfile.TemporaryDirectory(prefix='Research library ') as tmp:
        folder = Path(tmp) / 'Дослідження Ž'
        folder.mkdir()
        pdf = folder / 'synthetic book.pdf'
        writer = pypdf.PdfWriter()
        for text in ('First fixture', 'Second railway fixture'):
            page = writer.add_blank_page(width=200, height=200)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 20 100 Td ({text}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = stream
        with pdf.open('wb') as handle:
            writer.write(handle)
        reader = pypdf.PdfReader(pdf)
        pages = [p.extract_text() for p in reader.pages]
        if len(pages) != 2 or 'Second railway fixture' not in pages[1]:
            raise RuntimeError('PDF extraction mismatch')
        db_path = folder / 'local index.db'
        db = sqlite3.connect(db_path)
        try:
            db.execute('CREATE VIRTUAL TABLE pages USING fts5(raw)')
            db.executemany('INSERT INTO pages(rowid,raw) VALUES (?,?)', enumerate(pages, 1))
            db.commit()
        finally:
            db.close()
        db = sqlite3.connect(db_path)
        try:
            rows = db.execute('SELECT rowid, raw FROM pages WHERE pages MATCH ?', ('railway',)).fetchall()
            if rows != [(2, pages[1])]:
                raise RuntimeError('FTS5 page persistence mismatch')
        finally:
            db.close()
    return {'python': sys.version.split()[0], 'pypdf': pypdf.__version__,
            'sqlite': sqlite3.sqlite_version, 'optional_tesseract_found': shutil.which('tesseract') is not None,
            'synthetic_pdf_pages': 2, 'fts_page': 2, 'status': 'PASS'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-from', type=Path, help='Install locked smoke dependency from local wheelhouse first')
    args = parser.parse_args()
    if args.install_from is not None:
        offline_install(args.install_from)
    print(json.dumps(probe(), sort_keys=True))
