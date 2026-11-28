"""Author eight public, unprotected books for the selected-Calibre native gate.

The text is authored here and dedicated to CC0. Native MOBI/KF8/LIT/PalmDoc/TCR
files are produced by the explicitly selected Calibre, not fabricated headers.
The uncompressed CHM directory and DjVu INFO/BGjp/TXTa container are source-defined
fixtures following the cited public parsers/specification. They contain actual
HTML and JPEG/text payloads; the acceptance gate still must parse all eight.

No fixture, runtime or document is added to the distributed application bundle.
"""
import argparse
from contextlib import ExitStack
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import struct
from uuid import UUID

from carr_library.audio.cleanup import retained_temporary_directory
from carr_library.audio.models import AudioBudget, AudioCleanupPending
from carr_library.audio.process import run_process
from carr_library.conversion.calibre import CalibreSelection, _environment, _version_text
from carr_library.conversion.office import checked_directory, regular_file, verified_reader

TEXTS = (
    'Amber compass records the first reviewed chapter.',
    'Silver lantern records the second reviewed chapter.',
)
TITLE = 'Resonance authored conversion fixture'
REFERENCES = {
    'calibre': 'https://github.com/kovidgoyal/calibre/tree/v9.19.0',
    'chm_header': 'https://api.github.com/repos/jedwing/CHMLib/git/blobs/ffd213c4f66d4319b7bf95ef31eac78b65dad6b9',
    'chm_system': 'https://www.nongnu.org/chmspec/latest/Internal.html#SYSTEM',
    'djvu': 'https://djvu.sourceforge.net/doc/djvu3spec.djvu',
    'djvu_text_reader_blob': '4a6ead9045cb44744fe09a17137160308037b3dd',
}
MAX_FILE = 64 * 1024**2


def html_document(texts=TEXTS):
    return ('<!doctype html><html><head><meta charset="utf-8"><title>' + TITLE
            + '</title></head><body>' + ''.join(
                '<h2>Chapter ' + str(index) + '</h2><p>' + text + '</p>'
                for index, text in enumerate(texts, 1))
            + '</body></html>').encode('utf-8')


def _cword(value):
    if not 0 <= value < 2**28:
        raise ValueError('Fixture directory integer is outside its finite range.')
    parts = [value & 127]
    value >>= 7
    while value:
        parts.append((value & 127) | 128)
        value >>= 7
    return bytes(reversed(parts))


def authored_chm():
    """One real uncompressed CHM directory, two HTML chapters and its HHC tree."""
    def record(kind, value):
        return struct.pack('<HH', kind, len(value)) + value
    system = struct.pack('<I', 3) + b''.join((
        record(0, b'contents.hhc\0'), record(2, b'chapter1.html\0'),
        record(3, TITLE.encode('ascii') + b'\0'),
        record(4, struct.pack('<IIIII', 0x409, 0, 0, 0, 0)),
        record(16, b'utf-8\0'),
    ))
    toc = (b'<!doctype html><html><head><meta charset="utf-8"></head><body><ul>'
           + b''.join(('<li><object type="text/sitemap"><param name="Name" value="Chapter '
               + str(index) + '"><param name="Local" value="chapter' + str(index)
               + '.html"></object></li>').encode('ascii') for index in (1, 2))
           + b'</ul></body></html>')
    files = {'/#SYSTEM': system, '/contents.hhc': toc,
             '/chapter1.html': html_document(TEXTS[:1]),
             '/chapter2.html': html_document(TEXTS[1:])}
    entries, payload = bytearray(), bytearray()
    for name, data in sorted(files.items()):
        name = name.encode('utf-8')
        entries += _cword(len(name)) + name + _cword(0) + _cword(len(payload)) + _cword(len(data))
        payload += data
    chunk_bytes, directory_header = 4096, 0x54
    free = chunk_bytes - 0x14 - len(entries)
    if free < 2:
        raise AssertionError('The authored CHM directory exceeded one PMGL block.')
    pmgl = (struct.pack('<4sIIIi', b'PMGL', free, 0, 0xFFFFFFFF, -1)
            + entries + bytes(free - 2) + struct.pack('<H', len(files)))
    itsp = bytearray(directory_header)
    struct.pack_into('<4sIIIIIIiIIiII', itsp, 0, b'ITSP', 1, directory_header, 10,
                     chunk_bytes, 2, 1, -1, 0, 0, -1, 1, 0x409)
    itsp[0x34:0x44] = UUID('5d02926a-212e-11d0-9df9-00a0c922e6ec').bytes_le
    struct.pack_into('<Iiii', itsp, 0x44, directory_header, -1, -1, -1)
    directory_offset, directory_size = 0x60 + 0x18, len(itsp) + len(pmgl)
    data_offset = directory_offset + directory_size
    header = bytearray(0x60)
    struct.pack_into('<4sIIIII', header, 0, b'ITSF', 3, len(header), 1, 0, 0x409)
    header[0x18:0x28] = UUID('7c01fd10-7baa-11d0-9e0c-00a0c922e6ec').bytes_le
    header[0x28:0x38] = UUID('7c01fd11-7baa-11d0-9e0c-00a0c922e6ec').bytes_le
    struct.pack_into('<QQQQQ', header, 0x38, len(header), 0x18,
                     directory_offset, directory_size, data_offset)
    section0 = struct.pack('<IIQII', 0x01FE, 0, data_offset + len(payload), 0, 0)
    return bytes(header) + section0 + bytes(itsp) + bytes(pmgl) + bytes(payload)


def authored_djvu(*, with_text=True):
    """A real one-page alternative-JPEG DjVu, with an explicit optional text layer."""
    from PIL import Image
    def chunk(kind, data):
        return kind + len(data).to_bytes(4, 'big') + data + (b'\0' if len(data) % 2 else b'')
    image = BytesIO()
    Image.new('RGB', (32, 32), 'white').save(image, format='JPEG', quality=90)
    info = struct.pack('>HHBB', 32, 32, 26, 0) + struct.pack('<HBB', 300, 22, 1)
    body = b'DJVU' + chunk(b'INFO', info) + chunk(b'BGjp', image.getvalue())
    if with_text:
        text = ('\n'.join(TEXTS) + '\n').encode('utf-8')
        # Text-only TXTa is valid; the reference decoder treats zones as optional.
        body += chunk(b'TXTa', len(text).to_bytes(3, 'big') + text)
    return b'AT&T' + chunk(b'FORM', body)


def captured_bytes(path, maximum=MAX_FILE):
    info = regular_file(path)
    if not 0 < info.st_size <= maximum:
        raise ValueError('The generated fixture exceeds its fixed byte budget.')
    with verified_reader(path) as stream:
        data = stream.read(info.st_size + 1)
    if len(data) != info.st_size:
        raise ValueError('The generated fixture changed during capture.')
    return data


def _fixture_version(raw):
    version = _version_text(raw)
    if version not in ('ebook-convert (calibre 9.19.0)', 'ebook-convert.exe (calibre 9.19.0)'):
        raise ValueError('This fixed native fixture gate requires the reviewed Calibre 9.19.0.')
    return version


def generate(executable, output):
    output = Path(output).absolute()
    checked_directory(output.parent)
    if os.path.lexists(output):
        raise FileExistsError('Choose a new owned fixture directory.')
    chosen = CalibreSelection.capture(executable)
    selected = chosen.snapshot()
    output.mkdir(mode=0o700)
    provenance = []
    with retained_temporary_directory(prefix='.native-fixture-', dir=output) as temporary:
        stage = Path(temporary)
        for name in ('home', 'config', 'cache', 'temporary'):
            (stage / name).mkdir(mode=0o700)
        environment = _environment(stage, chosen.path.parent)
        budget = AudioBudget(timeout=120, max_log_bytes=256 * 1024,
                             max_output_bytes=512 * 1024**2, max_staging_files=4096)
        def native(arguments):
            with ExitStack() as held:
                held.enter_context(chosen.lease(selected))
                try:
                    return run_process([str(chosen.path), *map(str, arguments)],
                        budget=budget, env=environment, cwd=stage, watch=(stage,),
                        owned_tree=True).stdout
                except AudioCleanupPending as error:
                    readers = held.pop_all()
                    error.cleanup_owner.add_quiescent_callback(readers.close)
                    raise
        version = _fixture_version(native(['--version']))
        source = stage / 'authored.html'
        source_bytes = html_document()
        source.write_bytes(source_bytes)
        outputs = (
            ('.mobi', '.mobi', ['--mobi-file-type=old']),
            ('.azw3', '.azw3', []),
            ('.lit', '.lit', []),
            ('.tcr', '.tcr', []),
            ('.prc', '.pdb', ['--format=doc', '--pdb-output-encoding=cp1252']),
        )
        for suffix, container, arguments in outputs:
            target = stage / ('authored' + container)
            native([source, target, '--input-encoding=utf-8', '--title=' + TITLE,
                    '--authors=Resonance synthetic fixture', *arguments])
            raw = captured_bytes(target)
            path = output / ('authored' + suffix)
            with path.open('xb') as stream:
                stream.write(raw)
            provenance.append({'format': suffix, 'method': 'selected-calibre-ebook-convert',
                               'arguments': arguments, 'bytes': len(raw)})
        # Unprotected AZW is a MOBI-family container; no encryption marker is set.
        mobi = captured_bytes(output / 'authored.mobi')
        with (output / 'authored.azw').open('xb') as stream:
            stream.write(mobi)
        provenance.append({'format': '.azw', 'method': 'unprotected-MOBI-container-AZW-name', 'bytes': len(mobi)})
        for suffix, raw in (('.chm', authored_chm()), ('.djvu', authored_djvu())):
            with (output / ('authored' + suffix)).open('xb') as stream:
                stream.write(raw)
            provenance.append({'format': suffix, 'method': 'source-defined-real-container', 'bytes': len(raw)})
        if captured_bytes(source) != source_bytes:
            raise AssertionError('Native fixture generation changed its authored source.')
        if chosen.snapshot() != selected:
            raise AssertionError('Selected native core changed while authoring the fixtures.')
    files = []
    for suffix in sorted(('.azw', '.azw3', '.chm', '.djvu', '.lit', '.mobi', '.prc', '.tcr')):
        path = output / ('authored' + suffix)
        files.append({'format': suffix, 'path': path.name, 'sha256': sha256(captured_bytes(path)).hexdigest(),
                      'contains': list(TEXTS)})
    inventory = {'schema': 'carr-calibre-fixtures/1', 'files': files}
    inventory_bytes = (json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    with (output / 'fixtures.json').open('xb') as stream:
        stream.write(inventory_bytes)
    control = authored_djvu(with_text=False)
    with (output / 'scanned-no-text.djvu').open('xb') as stream:
        stream.write(control)
    evidence = {'schema': 'carr-calibre-authored-fixtures/1', 'status': 'GENERATED_NOT_QUALIFIED',
        'fixture_inventory_sha256': sha256(inventory_bytes).hexdigest(),
        'source_text': list(TEXTS), 'source_html_sha256': sha256(html_document()).hexdigest(),
        'fixture_text_license': 'CC0-1.0', 'generator_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
        'selected_version': version, 'selected_core_sha256': sha256(json.dumps(selected,
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest(),
        'formats': sorted(provenance, key=lambda row: row['format']), 'references': REFERENCES,
        'negative_control': {'path': 'scanned-no-text.djvu', 'sha256': sha256(control).hexdigest(),
                             'kind': 'valid-image-without-text-layer'},
        'drm_removal': False, 'downloaded_documents': False, 'distributed_with_application': False}
    with (output / 'generation.json').open('xb') as stream:
        stream.write((json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8'))
    return output / 'fixtures.json'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calibre', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(str(generate(args.calibre, args.output)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
