"""Required optional-tool gate: actual selected LibreOffice, never a skip.

All binary inputs are generated from synthetic text locally. No office runtime,
third-party book, macro payload or proprietary fixture is embedded in git.
"""
from hashlib import sha256
import os
from pathlib import Path
import struct
import subprocess
from xml.sax.saxutils import escape

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion.office import PROFILE


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
    result = subprocess.run([str(office()), '-env:UserInstallation=' + profile.as_uri(),
        '--headless', '--nologo', '--norestore', '--convert-to', kind + ':' + filter_name,
        '--outdir', str(stage), str(original)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40, check=True,
        env=dict(os.environ, SAL_USE_VCLPLUGIN='svp', SAL_DISABLE_OPENCL='1', MAX_CONCURRENCY='1',
                 PYTHONDONTWRITEBYTECODE='1'))
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


def _xls_visibility_diagnostic(message, result):
    """Inert census of this public synthetic fixture after child settlement."""
    if result.get('state') == 'complete':
        return
    import json
    from io import BytesIO
    from zipfile import ZipFile
    from defusedxml import ElementTree
    from carr_library.audio.jobs import regular_file, verified_reader, file_identity
    selected = Path(message['source']).parent / 'office-output' / 'source.ods'
    report = {'family': 'office', 'event': 'xls-visibility-census',
              'synthetic_fixture': True, 'acceptance_changed': False,
              'worker_state': result.get('state')}
    try:
        info = regular_file(selected)
        if info.st_nlink != 1 or not 0 < info.st_size <= 8 * 1024 * 1024:
            raise ValueError('Synthetic intermediate exceeds the diagnostic file bound.')
        with verified_reader(selected, expected_identity=file_identity(info)) as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) != info.st_size:
            raise ValueError('Synthetic intermediate changed during diagnostic read.')
        report.update(intermediate_bytes=len(raw), intermediate_sha256=sha256(raw).hexdigest())
        report['members'] = []
        text_ns = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
        style_ns = '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}'
        fo_ns = '{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}'
        with ZipFile(BytesIO(raw)) as archive:
            if len(archive.infolist()) > 128:
                raise ValueError('Synthetic intermediate exceeds diagnostic entry bound.')
            for name in ('content.xml', 'styles.xml'):
                entries = [row for row in archive.infolist() if row.filename == name]
                if len(entries) != 1 or not 0 < entries[0].file_size <= 1024 * 1024:
                    raise ValueError('Synthetic XML exceeds diagnostic member bound.')
                with archive.open(entries[0]) as stream:
                    content = stream.read(1024 * 1024 + 1)
                if len(content) != entries[0].file_size:
                    raise ValueError('Synthetic XML changed during diagnostic read.')
                root = ElementTree.fromstring(content)
                pending, count, offenders, references = [(root, [])], 0, [], {}
                while pending:
                    element, ancestors = pending.pop()
                    count += 1
                    if count > 10000 or len(ancestors) > 64:
                        raise ValueError('Synthetic XML exceeds diagnostic structural bound.')
                    attributes = {key: value[:256] for key, value in element.attrib.items()}
                    if len(attributes) > 64:
                        raise ValueError('Synthetic XML exceeds diagnostic attribute bound.')
                    for key, value in attributes.items():
                        if key.endswith(('}style-name', '}parent-style-name')):
                            references[value] = references.get(value, 0) + 1
                    hidden = (element.get(text_ns + 'display') in ('none', 'false')
                              or element.get(fo_ns + 'visibility') in ('hidden', 'collapse')
                              or element.get(style_ns + 'display') == 'false')
                    current = {'tag': element.tag, 'attributes': attributes}
                    if hidden:
                        if len(offenders) >= 64:
                            raise ValueError('Synthetic XML exceeds diagnostic hidden-node bound.')
                        descendants = list(element.iter())
                        if len(descendants) > 33:
                            raise ValueError('Synthetic hidden layout subtree exceeds diagnostic bound.')
                        shaped = []
                        for descendant in descendants[1:]:
                            if len(descendant.attrib) > 64:
                                raise ValueError('Synthetic layout attributes exceed diagnostic bound.')
                            shaped.append({'tag': descendant.tag,
                                'attributes': {key: value[:256] for key, value in descendant.attrib.items()},
                                'child_count': len(descendant),
                                'non_whitespace_text': bool((descendant.text or '').strip()),
                                'non_whitespace_tail': bool((descendant.tail or '').strip())})
                        offenders.append(dict(current, ancestors=ancestors[-6:],
                            child_count=len(element), child_tags=[child.tag for child in element],
                            descendant_count=len(descendants) - 1,
                            own_non_whitespace_text=bool((element.text or '').strip()),
                            subtree_non_whitespace_text=any((child.text or '').strip() for child in descendants),
                            descendant_non_whitespace_tail=any(
                                (child.tail or '').strip() for child in descendants[1:]),
                            descendants=shaped))
                    pending.extend((child, [*ancestors, current]) for child in reversed(element))
                report['members'].append({'name': name, 'bytes': len(content),
                    'sha256': sha256(content).hexdigest(), 'nodes': count,
                    'hidden_nodes': offenders, 'style_references': references})
        encoded = json.dumps(report, ensure_ascii=True, sort_keys=True)
        if len(encoded) > 64 * 1024:
            raise ValueError('Synthetic visibility census exceeds its output bound.')
    except Exception as exc:
        encoded = json.dumps(dict(report, diagnostic_error=type(exc).__name__,
                                  diagnostic_message=str(exc)[:500]), ensure_ascii=True, sort_keys=True)
        if len(encoded) > 64 * 1024:
            encoded = json.dumps({'family': 'office', 'event': 'xls-visibility-census',
                                  'diagnostic_error': 'output-limit'})
    print('NATIVE_DIAGNOSTIC ' + encoded, flush=True)


@pytest.mark.parametrize('kind', ['doc', 'ppt', 'xls', 'wpd', 'pdb'])
def test_real_selected_legacy_filter_retains_original_and_exact_provenance(tmp_path, monkeypatch, kind):
    text = 'Selected legacy reading document.'
    if kind in ('doc', 'ppt', 'xls'):
        source = legacy_office(tmp_path, kind, text)
    else:
        source = tmp_path / ('Оригінал.' + kind)
        source.write_bytes((wordperfect if kind == 'wpd' else palmdoc)(text))
    original = source.read_bytes()
    if kind == 'xls':
        from carr_library.conversion import service as service_module
        actual_invoke = service_module._invoke
        def observed(message, **options):
            result = actual_invoke(message, **options)
            _xls_visibility_diagnostic(message, result)
            return result
        monkeypatch.setattr(service_module, '_invoke', observed)
    destination = tmp_path / 'Читальна копія'
    destination.mkdir()
    result = ConversionService(destination, input_roots=(source,), office=office()).extract(source, 'reading.txt')
    assert text in result.path.read_text(encoding='utf-8')
    assert source.read_bytes() == original
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['verified_for_quotation'] is False
    assert result.report['engine'] == 'selected-libreoffice-' + kind
    assert 'macros_active_content_and_untrusted_links_disabled' in result.report['warnings']
    for row in result.report['segments']:
        assert row['physical_index'] is None and row['printed_label'] is None
        assert row['status'] == 'review_required'
        assert row['logical_locator']['private_profile_sha256'] == sha256(PROFILE).hexdigest()
    assert len(list(destination.iterdir())) == 2


def test_selected_office_cancellation_settles_family_before_private_profile_cleanup(tmp_path, monkeypatch):
    source = legacy_office(tmp_path, 'doc', 'The original must survive cancellation.')
    before = source.read_bytes()
    output = tmp_path / 'output'
    output.mkdir()
    from carr_library.conversion import service as service_module
    actual_invoke = service_module._invoke
    stages, observed = [], []
    def invoke(message, **options):
        stages.append(Path(message['source']).parent)
        return actual_invoke(message, **options)
    monkeypatch.setattr(service_module, '_invoke', invoke)
    def cancelled():
        profiles = [stage / 'libreoffice-profile/user/registrymodifications.xcu'
                    for stage in stages]
        present = [path for path in profiles if path.is_file()]
        if present:
            observed.append(present[0])
        return bool(observed)
    with pytest.raises(InterruptedError):
        ConversionService(output, input_roots=(source,), office=office()).extract(source, 'reading.txt', cancelled=cancelled)
    assert observed and not observed[0].exists()
    assert stages and all(not stage.exists() for stage in stages)
    assert list(output.iterdir()) == [] and source.read_bytes() == before


def test_selected_office_text_budget_keeps_destination_empty(tmp_path):
    source = legacy_office(tmp_path, 'doc', 'Longer than the selected reading text budget.')
    output = tmp_path / 'output'
    output.mkdir()
    with pytest.raises(ConversionError, match='budget|limit|characters'):
        ConversionService(output, input_roots=(source,), office=office(),
            budget=ConversionBudget(max_text_bytes=4)).extract(source, 'reading.txt')
    assert list(output.iterdir()) == []
