"""Required optional-tool gate: actual selected LibreOffice, never a skip.

All binary inputs are generated from synthetic text locally. No office runtime,
third-party book, macro payload or proprietary fixture is embedded in git.
"""
from hashlib import sha256
import os
from pathlib import Path
import struct
from xml.sax.saxutils import escape

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion.office import PROFILE
from carr_library.audio.models import AudioBudget
from carr_library.audio.process import run_process


def office():
    selected = os.environ.get('CARR_SOFFICE')
    assert selected, 'This acceptance gate must explicitly provision CARR_SOFFICE.'
    return Path(selected).absolute()


def flat_document(kind, text):
    body = {'doc': '<office:text><text:p>' + escape(text) + '</text:p></office:text>',
            'ppt': '<office:presentation><draw:page draw:name="Slide1"><draw:frame>'
                   '<draw:text-box><text:p>' + escape(text) + '</text:p></draw:text-box>'
                   '</draw:frame></draw:page></office:presentation>',
            'xls': '<office:spreadsheet><table:table table:name="Sheet1"><table:table-row>'
                   '<table:table-cell office:value-type="string"><text:p>' + escape(text) + '</text:p>'
                   '</table:table-cell></table:table-row></table:table></office:spreadsheet>'}[kind]
    mime = {'doc': 'text', 'ppt': 'presentation', 'xls': 'spreadsheet'}[kind]
    return ('''<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.''' + mime + '''">
<office:body>''' + body + '</office:body></office:document>').encode()


def legacy_office(tmp_path, kind, text):
    stage = tmp_path / ('fixture-' + kind)
    stage.mkdir()
    profile = stage / 'profile'
    (profile / 'user').mkdir(parents=True)
    (profile / 'user/registrymodifications.xcu').write_bytes(PROFILE)
    suffix = {'doc': 'fodt', 'ppt': 'fodp', 'xls': 'fods'}[kind]
    original = stage / ('synthetic.' + suffix)
    original.write_bytes(flat_document(kind, text))
    filter_name = {'doc': 'MS Word 97', 'ppt': 'MS PowerPoint 97', 'xls': 'MS Excel 97'}[kind]
    result = run_process([str(office()), '-env:UserInstallation=' + profile.as_uri(),
        '--headless', '--nologo', '--norestore', '--convert-to', kind + ':' + filter_name,
        '--outdir', str(stage), str(original)],
        budget=AudioBudget(timeout=60, max_output_bytes=64 * 1024**2, max_staging_files=4096),
        owned_tree=True, watch=(stage,), cwd=stage,
        env=dict(os.environ, SAL_USE_VCLPLUGIN='svp', SAL_DISABLE_OPENCL='1', MAX_CONCURRENCY='1'))
    generated = stage / ('synthetic.' + kind)
    assert generated.is_file(), result.stdout.decode('utf-8', 'replace')[:2000]
    assert generated.read_bytes().startswith(bytes.fromhex('d0cf11e0a1b11ae1'))
    return generated


def palmdoc(text):
    body = text.encode('ascii')
    header = bytearray(78)
    header[:18] = b'Synthetic PalmDoc\0'
    header[60:68] = b'TEXtREAd'
    header[76:78] = struct.pack('>H', 2)
    records = struct.pack('>II', 96, 0) + struct.pack('>II', 112, 1) + b'\0\0'
    metadata = struct.pack('>HHIHHI', 1, 0, len(body), 1, 4096, 0)
    return bytes(header) + records + metadata + body


def wordperfect(text):
    # Minimal WordPerfect 5 document-header family; text-only synthetic body.
    return b'\xffWPC' + struct.pack('<I', 32) + bytes((1, 10, 0, 0)) + b'\0' * 20 + text.encode('ascii')


def microsoft_write(text):
    # A synthetic BE31 text-only FIB; no objects, macros or external content.
    body = text.encode('cp1252') + b'\r\n'
    end = 128 + len(body)
    pages = (end + 127) // 128
    header = bytearray(128)
    header[:6] = b'\x31\xbe\x00\x00\x00\xab'
    struct.pack_into('<I', header, 14, end)
    for offset in (18, 20, 22, 24, 26, 28, 96):
        struct.pack_into('<H', header, offset, pages)
    return (bytes(header) + body).ljust(pages * 128, b'\0')


@pytest.mark.parametrize('kind', ['doc', 'ppt', 'xls', 'wpd', 'pdb', 'wri'])
def test_real_selected_legacy_filter_retains_original_and_exact_provenance(tmp_path, kind):
    text = 'Selected legacy reading document.'
    if kind in ('doc', 'ppt', 'xls'):
        source = legacy_office(tmp_path, kind, text)
    else:
        source = tmp_path / ('Оригінал.' + kind)
        source.write_bytes({'wpd': wordperfect, 'pdb': palmdoc, 'wri': microsoft_write}[kind](text))
    original = source.read_bytes()
    destination = tmp_path / 'Читальна копія'
    destination.mkdir()
    result = ConversionService(destination, input_roots=(source,), office=office(),
        budget=ConversionBudget(memory_bytes=4 * 1024**3)).extract(source, 'reading.txt')
    assert text in result.path.read_text(encoding='utf-8')
    assert source.read_bytes() == original
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['verified_for_quotation'] is False
    assert result.report['engine'] == 'selected-libreoffice-' + kind
    assert 'macros_active_content_and_untrusted_links_disabled' in result.report['warnings']
    for row in result.report['sections']:
        assert row['physical_index'] is None and row['printed_label'] is None
    assert result.report['metadata']['source_review_required'] is True
    for locator in result.report['metadata']['source_locators']:
        assert locator['private_profile_sha256'] == sha256(PROFILE).hexdigest()
    assert len(list(destination.iterdir())) == 2


def test_selected_office_cancellation_settles_family_before_private_profile_cleanup(tmp_path):
    source = legacy_office(tmp_path, 'doc', 'The original must survive cancellation.')
    before = source.read_bytes()
    output = tmp_path / 'output'
    output.mkdir()
    observed = []
    def cancelled():
        profiles = list(output.glob('.pending-conversion-*/libreoffice-profile/user/registrymodifications.xcu'))
        if profiles:
            observed.append(profiles[0])
        return bool(observed)
    with pytest.raises(InterruptedError):
        ConversionService(output, input_roots=(source,), office=office(),
        budget=ConversionBudget(memory_bytes=4 * 1024**3)).extract(source, 'reading.txt', cancelled=cancelled)
    assert observed and not observed[0].exists()
    assert list(output.iterdir()) == [] and source.read_bytes() == before


def test_selected_office_text_budget_keeps_destination_empty(tmp_path):
    source = legacy_office(tmp_path, 'doc', 'Longer than the selected reading text budget.')
    output = tmp_path / 'output'
    output.mkdir()
    with pytest.raises(ConversionError, match='budget|limit|characters'):
        ConversionService(output, input_roots=(source,), office=office(),
            budget=ConversionBudget(max_text_bytes=4, memory_bytes=4 * 1024**3)).extract(source, 'reading.txt')
    assert list(output.iterdir()) == []
