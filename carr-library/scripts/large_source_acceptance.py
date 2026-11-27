"""Actual, finite large-original qualification; never forge sizes or skip work.

The TXT fixture really crosses the old 128 MiB source and 512 MiB journal/DB
limits. The PDF fixture is a real painted page plus an unused streamed PDF
object. It qualifies original-byte handling, not large scanned-page OCR.
"""
from contextlib import closing
from hashlib import sha256
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import monotonic

MIB = 1024**2


def make_text(path, mebibytes):
    hashed = sha256()
    with path.open('xb') as output:
        def write(block):
            output.write(block)
            hashed.update(block)
        # Actual short fields also place the last selection beyond position
        # 10,000; no database insertion or synthetic ordinal bypass is used.
        write(b'Largeproofneedle exact original\r\n\f' + b'x\f' * 10001)
        block = b'\\' * MIB
        remaining = mebibytes * MIB
        while remaining:
            current = block[:min(len(block), remaining)]
            write(current)
            remaining -= len(current)
        write('\fTailproofneedle exact source Україна\r\n'.encode('utf-8'))
        output.flush()
        os.fsync(output.fileno())
    return hashed.hexdigest()


def make_pdf(path, mebibytes):
    """Use pypdf for the real page; append one valid unused incremental stream."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b'BT /F1 16 Tf 40 700 Td (Pdfprofileproof exact painted source) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(content)
    with path.open('xb') as output:
        writer.write(output)
    writer.close()
    with path.open('rb') as incoming:
        reader = PdfReader(incoming)
        root = reader.trailer['/Root'].indirect_reference
        root_id, root_generation = root.idnum, root.generation
        new_id = int(reader.trailer['/Size'])
        incoming.seek(max(0, path.stat().st_size - 8192))
        endings = re.findall(rb'startxref\s+(\d+)\s+%%EOF', incoming.read(8192))
        if not endings:
            raise ValueError('The fixture producer did not emit a conventional final cross-reference.')
        previous = int(endings[-1])
        reader.close()
    with path.open('ab') as output:
        output.write(b'\n')
        offset = output.tell()
        length = mebibytes * MIB
        output.write(f'{new_id} 0 obj\n<< /Length {length} >>\nstream\n'.encode('ascii'))
        block, remaining = b'A' * MIB, length
        while remaining:
            amount = min(len(block), remaining)
            output.write(block[:amount])
            remaining -= amount
        output.write(b'\nendstream\nendobj\n')
        cross_reference = output.tell()
        output.write((f'xref\n{new_id} 1\n{offset:010d} 00000 n \n'
            f'trailer\n<< /Size {new_id + 1} /Root {root_id} {root_generation} R /Prev {previous} >>\n'
            f'startxref\n{cross_reference}\n%%EOF\n').encode('ascii'))
        output.flush()
        os.fsync(output.fileno())
    hashed = sha256()
    with path.open('rb') as incoming:
        for block in iter(lambda: incoming.read(MIB), b''):
            hashed.update(block)
    return hashed.hexdigest()


def timed(report, name, operation):
    started = monotonic()
    value = operation()
    report[name + '_seconds'] = monotonic() - started
    return value


def text_case(root, mebibytes, timeout):
    from carr_library.application import LibraryApp, open_existing_library
    from carr_library.backup_capacity import LARGE_SOURCE_PROFILE
    from carr_library.ingest.source_profiles import LargeTextProfile
    from carr_library.ingest.streamed_text import TextProfile, inspect_text
    from carr_library.ingest.worker import load_request
    from carr_library.library_backup import restore_backup, verify_backup
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.book_source import capture_source
    report = {}
    source = root / 'Large original.txt'
    original_digest = timed(report, 'fixture', lambda: make_text(source, mebibytes))
    original_bytes = source.stat().st_size
    assert original_bytes > 128 * MIB
    try:
        inspect_text(source, TextProfile())
    except ValueError:
        pass
    else:
        raise AssertionError('The legacy profile silently admitted a larger source.')
    profile = LargeTextProfile(original_bytes)
    app = LibraryApp(root / 'original library')
    workspace = app.create_workspace('Measured large original')
    observed = 0
    def cancelled():
        nonlocal observed
        journal = app.root / 'imports.sqlite'
        try:
            with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
                observed = db.execute('SELECT count(*) FROM checkpoint').fetchone()[0]
        except sqlite3.Error:
            return False
        return observed >= 2
    stopped = timed(report, 'owned_cancel', lambda: app.import_file(workspace, source,
        title='Large original', source_profile=profile, timeout=timeout, cancelled=cancelled))
    assert stopped['state'] == 'cancelled' and observed >= 2, stopped
    request = load_request(app.root, stopped['request_id'])
    assert request['version'] == 3 and request['asset_sha256'] == original_digest
    assert app.browse(workspace) == ()
    pending = root / 'pending backup'
    pending_digest = timed(report, 'pending_backup', lambda: app.backup(
        pending, capacity_profile=LARGE_SOURCE_PROFILE))
    timed(report, 'pending_verify', lambda: verify_backup(pending, pending_digest))
    source.unlink()
    restored_root = timed(report, 'pending_restore', lambda: restore_backup(
        pending, root / 'resumed library', pending_digest))
    resumed = open_existing_library(restored_root)
    # These are exclusively this script's completed private fixture directories.
    # Remove duplicate phases before allocating the next full-size snapshot.
    shutil.rmtree(app.root)
    shutil.rmtree(pending)
    completed = timed(report, 'owned_resume', lambda: resumed.resume_import(
        workspace, stopped['request_id'], timeout=timeout))
    assert completed['state'] == 'complete' and completed['asset'] == original_digest, completed
    journal_bytes = (resumed.root / 'imports.sqlite').stat().st_size
    database_bytes = resumed.database.stat().st_size
    assert journal_bytes > 512 * MIB, journal_bytes
    assert database_bytes > 512 * MIB, database_bytes
    desk = ReadingDesk(resumed, workspace, completed['edition'], timeout=60)
    assert 10000 < len(desk.pages) <= 65536
    tail = desk.page(len(desk.pages))
    assert tail.verified and tail.raw_text == 'Tailproofneedle exact source Україна\r\n'
    capture = capture_source(resumed, workspace, completed['edition'],
        first=tail.ordinal, last=tail.ordinal, timeout=min(3600, timeout))
    assert capture['text'] == tail.raw_text
    assert capture['source_anchor']['positions'][0]['ordinal'] == tail.ordinal
    hit = resumed.search_page(workspace, 'Largeproofneedle exact original', limit=1,
                              timeout=60).results[0]
    assert hit.verified and resumed.copy(workspace, hit.ref, hit.revision, 'quote') == 'Largeproofneedle exact original'
    complete = root / 'complete backup'
    complete_digest = timed(report, 'complete_backup', lambda: resumed.backup(
        complete, capacity_profile=LARGE_SOURCE_PROFILE))
    timed(report, 'complete_verify', lambda: verify_backup(complete, complete_digest))
    shutil.rmtree(resumed.root)
    final_root = timed(report, 'complete_restore', lambda: restore_backup(
        complete, root / 'final library', complete_digest))
    final = open_existing_library(final_root)
    final_desk = ReadingDesk(final, workspace, completed['edition'], timeout=60)
    assert final_desk.page(tail.ordinal).raw_text == tail.raw_text
    report.update(status='PASS', original_bytes=original_bytes, original_sha256=original_digest,
        database_bytes=database_bytes, journal_bytes=journal_bytes, actual_fragments=len(desk.pages),
        final_ordinal=tail.ordinal, backup_sha256=complete_digest,
        checks=['real source above 128 MiB', 'real journal and catalog above 512 MiB',
                'owned running-child cancellation', 'retained input after external source deletion',
                'pending v4 / complete v6 restore', 'fresh isolated resume', 'late reader and source anchor',
                'independent exact TXT quotation', 'reopened restored reader'],
        scale_claim='Only these measured bytes/fragments and this runtime; the 4 GiB code cap is not a measured claim.')
    return report


def pdf_case(root, mebibytes, timeout):
    from carr_library.application import LibraryApp, open_existing_library
    from carr_library.backup_capacity import LARGE_SOURCE_PROFILE
    from carr_library.ingest.source_profiles import PdfProfile
    from carr_library.library_backup import restore_backup, verify_backup
    report = {}
    source = root / 'Large Український original.pdf'
    original_digest = timed(report, 'fixture', lambda: make_pdf(source, mebibytes))
    original_bytes = source.stat().st_size
    assert original_bytes > 128 * MIB
    app = LibraryApp(root / 'pdf library')
    workspace = app.create_workspace('Measured file-backed PDF')
    result = timed(report, 'owned_import', lambda: app.import_file(workspace, source,
        source_profile=PdfProfile(original_bytes), timeout=timeout))
    assert result['state'] == 'complete' and result['asset'] == original_digest, result
    hit = app.search_page(workspace, 'Pdfprofileproof exact painted source',
                           limit=1, timeout=60).results[0]
    assert not hit.verified
    timed(report, 'explicit_page_review', lambda: app.review_pdf_page(
        workspace, hit.ref, hit.revision, 'Synthetic fixture reviewer', confirmed=True))
    verified = app.search_page(workspace, 'Pdfprofileproof exact painted source',
                                limit=1, timeout=60).results[0]
    assert verified.verified
    assert app.copy(workspace, verified.ref, verified.revision, 'quote') == 'Pdfprofileproof exact painted source'
    backup = root / 'pdf backup'
    digest = timed(report, 'backup', lambda: app.backup(backup, capacity_profile=LARGE_SOURCE_PROFILE))
    timed(report, 'verify', lambda: verify_backup(backup, digest))
    source.unlink()
    shutil.rmtree(app.root)
    restored = timed(report, 'restore', lambda: restore_backup(backup, root / 'restored pdf', digest))
    reopened = open_existing_library(restored)
    hit = reopened.search_page(workspace, 'Pdfprofileproof exact painted source',
                                limit=1, timeout=60).results[0]
    assert hit.verified
    report.update(status='PASS', original_bytes=original_bytes, original_sha256=original_digest,
        actual_physical_pages=1, backup_sha256=digest,
        checks=['real PDF original above 128 MiB', 'current isolated pypdf/PDFium page extraction',
                'explicit independent approval boundary', 'complete original checksum',
                'external original removed', 'typed complete restore and reviewed quotation'],
        ocr_quality='NOT_MEASURED', multi_page_complex_pdf_performance='NOT_MEASURED')
    return report


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-package', type=Path, required=True,
                        help='Exact installed carr_library/__init__.py; source shadowing is refused')
    parser.add_argument('--txt-mib', type=int, default=257)
    parser.add_argument('--pdf-mib', type=int, default=129)
    parser.add_argument('--timeout', type=float, default=3600)
    args = parser.parse_args(arguments)
    if (not 257 <= args.txt_mib <= 3072 or not 129 <= args.pdf_mib <= 3072
            or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 7200):
        parser.error('Select finite real fixture sizes and a 1–7200 second owned-worker allowance.')
    import carr_library
    if Path(carr_library.__file__).resolve(strict=True) != args.expected_package.resolve(strict=True):
        raise ValueError('The acceptance process did not import the explicitly selected installed package.')
    workdir = args.workdir.resolve(strict=True)
    if not workdir.is_dir():
        raise ValueError('Choose an existing private fixture work directory.')
    # The phases run in separate temporary trees. Admit the larger peak:
    # TXT retains expanded journal/catalog copies; PDF retains its original,
    # owned input and complete-backup copies without expanding the whole file.
    required = max(12 * (args.txt_mib * MIB), 4 * (args.pdf_mib * MIB)) + 2 * 1024**3
    if shutil.disk_usage(workdir).free < required:
        raise ValueError('Insufficient free space for the declared measured fixture/backup phases.')
    report = dict(status='RUNNING', installed_package_verified=True, peak_ram='NOT_MEASURED',
                  peak_vram='NOT_MEASURED', physical_audio='NOT_APPLICABLE')
    try:
        with TemporaryDirectory(prefix='carr-large-txt-', dir=workdir) as temporary:
            report['txt'] = text_case(Path(temporary), args.txt_mib, args.timeout)
        with TemporaryDirectory(prefix='carr-large-pdf-', dir=workdir) as temporary:
            report['pdf'] = pdf_case(Path(temporary), args.pdf_mib, args.timeout)
        report['status'] = 'PASS'
    except BaseException as exc:
        report.update(status='FAIL', error_type=type(exc).__name__)
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                               encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
