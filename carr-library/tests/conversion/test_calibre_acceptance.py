"""Gate admission tests; synthetic headers are never format qualification."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def script(name):
    path = ROOT / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_carr_test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manifest(tmp_path):
    values = script('calibre_conversion_acceptance')
    files = []
    for suffix in sorted(values.CALIBRE_SUFFIXES):
        path = tmp_path / ('fixture' + suffix)
        data = ('Inventory-only test: ' + suffix).encode('ascii')
        path.write_bytes(data)
        files.append({'format': suffix, 'path': path.name, 'sha256': sha256(data).hexdigest(),
                      'contains': ['Inventory-only']})
    path = tmp_path / 'fixtures.json'
    value = {'schema': values.SCHEMA, 'files': files}
    path.write_text(json.dumps(value), encoding='utf-8')
    return values, path, value


def test_inventory_requires_all_eight_exact_bound_inputs(manifest):
    module, path, value = manifest
    rows, checksum = module.fixtures(path)
    assert {row['format'] for row, _ in rows} == module.CALIBRE_SUFFIXES and len(rows) == 8
    assert checksum == sha256(path.read_bytes()).hexdigest()
    assert all(source.read_bytes().startswith(b'Inventory-only test:') for _, source in rows)


@pytest.mark.parametrize('change', ['missing-format', 'duplicate-format', 'wrong-hash', 'relative-traversal',
                                   'wrong-suffix', 'unknown-field', 'duplicate-key', 'linked-input',
                                   'oversize-manifest', 'empty-assertion'])
def test_gate_inventory_cannot_silently_skip_or_rebind_a_format(manifest, tmp_path, change):
    import os
    module, path, value = manifest
    if change == 'missing-format':
        value['files'].pop()
    elif change == 'duplicate-format':
        value['files'][-1] = dict(value['files'][0])
    elif change == 'wrong-hash':
        value['files'][0]['sha256'] = '0' * 64
    elif change == 'relative-traversal':
        value['files'][0]['path'] = '../fixture.azw'
    elif change == 'wrong-suffix':
        value['files'][0]['path'] = value['files'][1]['path']
    elif change == 'unknown-field':
        value['skip_missing'] = True
    elif change == 'linked-input':
        os.link(path.parent / value['files'][0]['path'], tmp_path / 'unselected-alias')
    elif change == 'empty-assertion':
        value['files'][0]['contains'] = []
    raw = json.dumps(value)
    if change == 'duplicate-key':
        raw = raw.replace('"schema":', '"schema": "untrusted", "schema":', 1)
    elif change == 'oversize-manifest':
        raw += ' ' * module.MAX_MANIFEST_BYTES
    path.write_text(raw, encoding='utf-8')
    with pytest.raises((OSError, ValueError)):
        module.fixtures(path)


def test_source_authored_containers_hold_actual_chapters_and_jpeg_text_layer():
    import struct
    from io import BytesIO
    from PIL import Image
    generator = script('create_calibre_fixtures')
    chm = generator.authored_chm()
    assert chm[:4] == b'ITSF' and len(chm) > 4096
    directory_offset, directory_size, data_offset = struct.unpack_from('<QQQ', chm, 0x48)
    assert chm[directory_offset:directory_offset + 4] == b'ITSP'
    assert chm[directory_offset + 0x54:directory_offset + 0x58] == b'PMGL'
    assert data_offset == directory_offset + directory_size
    for text in generator.TEXTS:
        assert text.encode('utf-8') in chm[data_offset:]
    djvu = generator.authored_djvu()
    assert djvu[:8] == b'AT&TFORM' and djvu[12:16] == b'DJVU'
    assert int.from_bytes(djvu[8:12], 'big') == len(djvu) - 12
    chunks, position = {}, 16
    while position < len(djvu):
        kind = djvu[position:position + 4]
        size = int.from_bytes(djvu[position + 4:position + 8], 'big')
        chunks[kind] = djvu[position + 8:position + 8 + size]
        position += 8 + size + size % 2
    assert set(chunks) == {b'INFO', b'BGjp', b'TXTa'}
    with Image.open(BytesIO(chunks[b'BGjp'])) as image:
        image.load()
        assert image.size == (32, 32) and image.format == 'JPEG'
    raw = chunks[b'TXTa']
    size = int.from_bytes(raw[:3], 'big')
    assert raw[3:3 + size].decode('utf-8').splitlines() == list(generator.TEXTS)
    assert b'TXTa' not in generator.authored_djvu(with_text=False)
    # Native CHM/DjVu parsing remains mandatory in calibre_conversion_acceptance;
    # these structure checks do not count as successful document conversions.


def test_fixture_generator_rejects_preexisting_directory_without_running_tool(tmp_path, monkeypatch):
    generator = script('create_calibre_fixtures')
    existing = tmp_path / 'existing'
    existing.mkdir()
    retained = existing / 'keep'
    retained.write_bytes(b'Never removed by fixture generation.')
    monkeypatch.setattr(generator.CalibreSelection, 'capture',
        lambda *args: pytest.fail('A preexisting output should fail before tool selection.'))
    with pytest.raises(FileExistsError):
        generator.generate(tmp_path / 'unselected.exe', existing)
    assert retained.read_bytes() == b'Never removed by fixture generation.'


@pytest.mark.parametrize('executable', ['ebook-convert', 'ebook-convert.exe'])
def test_fixture_gate_reads_the_official_two_line_identity(executable):
    generator = script('create_calibre_fixtures')
    first = executable + ' (calibre 9.19.0)'
    raw = (first + '\r\nCreated by: Kovid Goyal <kovid@kovidgoyal.net>\r\n').encode('utf-8')
    assert generator._fixture_version(raw) == first


@pytest.mark.parametrize('version', ['9.19', '9.19.1', '9.18.0'])
def test_fixture_gate_still_requires_the_exact_pinned_release(version):
    generator = script('create_calibre_fixtures')
    raw = ('ebook-convert.exe (calibre ' + version + ')\r\n'
           'Created by: Kovid Goyal <kovid@kovidgoyal.net>\r\n').encode('utf-8')
    with pytest.raises(ValueError, match='Calibre 9.19.0'):
        generator._fixture_version(raw)
