"""Portable admission/failure tests; no native office runtime is simulated as qualified."""
from hashlib import sha256
import os
from pathlib import Path
import time

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion import office


def installed(tmp_path):
    root = tmp_path / 'chosen-office'
    (root / 'program').mkdir(parents=True)
    (root / 'share/registry').mkdir(parents=True)
    for name in ('soffice', 'soffice.bin'):
        (root / 'program' / name).write_bytes(b'Synthetic admission-only component, never executed.')
    prefix = '<oor:data xmlns:oor="http://openoffice.org/2001/registry">'
    properties = ''.join('<prop oor:name="' + name + '"/>' for name in (
        'DisableMacrosExecution', 'DisableActiveContent', 'DisableOLEAutomation',
        'BlockUntrustedRefererLinks', 'SecureURL'))
    (root / 'share/registry/main.xcd').write_text(prefix + properties + '</oor:data>')
    for module in ('writer.xcd', 'calc.xcd', 'impress.xcd'):
        names = sorted({name for inp, mod, _, out in office.FILTERS.values()
                        if mod == module for name in (inp, out)})
        values = ''.join('<node oor:name="' + name + '"><prop oor:name="Flags">'
                         '<value>IMPORT EXPORT</value></prop></node>' for name in names)
        (root / 'share/registry' / module).write_text(prefix +
            '<oor:component-data oor:package="org.openoffice.TypeDetection" oor:name="Filter">'
            '<node oor:name="Filters">' + values + '</node></oor:component-data></oor:data>')
    return root / 'program/soffice'


def test_selected_inventory_checks_fixed_installed_filter_without_executing(tmp_path, monkeypatch):
    path = installed(tmp_path)
    selection = office.OfficeSelection.capture(path).snapshot()
    monkeypatch.setattr(office.subprocess, 'Popen', lambda *a, **k: pytest.fail('Admission executed code.'))
    for suffix in office.OFFICE_SUFFIXES:
        assert office._admit(selection, suffix, time.monotonic() + 5) == path
    assert set(selection) == {'root', 'root_identity', 'executable', 'files'}
    for row in selection['files']:
        raw = (path.parent.parent / row['name']).read_bytes()
        assert row['bytes'] == len(raw) and row['sha256'] == sha256(raw).hexdigest()


def test_snapshot_rechecks_earlier_component_after_last_hash(tmp_path):
    path = installed(tmp_path)
    selection = office.OfficeSelection.capture(path)
    changed = False
    def cancelled():
        nonlocal changed
        # Called before each block and once after the complete inventory.
        return False
    real = selection._unchanged
    calls = []
    from unittest.mock import patch
    def final_check():
        nonlocal changed
        calls.append(1)
        if len(calls) == 2:
            path.write_bytes(b'changed after hashing')
            changed = True
        return real()
    with patch.object(office.OfficeSelection, '_unchanged', lambda self: final_check()):
        with pytest.raises(ConversionError, match='changed'):
            selection.snapshot(cancelled=cancelled)
    assert changed


@pytest.mark.parametrize('change', ['hash', 'extra-row', 'wrong-root', 'filter', 'security-property'])
def test_worker_admission_refuses_changed_or_incomplete_inventory(tmp_path, change):
    path = installed(tmp_path)
    selection = office.OfficeSelection.capture(path).snapshot()
    if change == 'hash':
        selection['files'][0]['sha256'] = '0' * 64
    elif change == 'extra-row':
        selection['files'].append(dict(selection['files'][0]))
    elif change == 'wrong-root':
        selection['root_identity'][1] += 1
    else:
        selected = path.parent.parent / 'share/registry' / ('writer.xcd' if change == 'filter' else 'main.xcd')
        raw = selected.read_text().replace('MS Word 97', 'Other Filter') if change == 'filter' else selected.read_text().replace('DisableMacrosExecution', 'UnsupportedSetting')
        selected.write_text(raw)
        # This newly selected but incapable installation has honest hashes.
        selection = office.OfficeSelection.capture(path).snapshot()
    with pytest.raises((ConversionError, ValueError, RuntimeError)):
        office._admit(selection, '.doc', time.monotonic() + 5)


@pytest.mark.parametrize('suffix,data', [
    ('.doc', b'renamed text'), ('.ppt', b'renamed text'), ('.xls', b'renamed text'),
    ('.wpd', b'renamed text'), ('.pdb', b'\0' * 78),
], ids=['doc', 'ppt', 'xls', 'wordperfect', 'foreign-pdb'])
def test_fixed_adapter_rejects_false_format_before_runtime_admission(tmp_path, monkeypatch, suffix, data):
    monkeypatch.setattr(office, '_admit', lambda *a, **k: pytest.fail('Invalid header reached native admission.'))
    with pytest.raises(ConversionError):
        office.office_records(tmp_path / ('source' + suffix), data, suffix,
                              ConversionBudget(), time.monotonic() + 5, {})


def test_expired_deadline_never_spawns_a_native_process(tmp_path, monkeypatch):
    monkeypatch.setattr(office.subprocess, 'Popen', lambda *a, **k: pytest.fail('Expired operation executed code.'))
    with pytest.raises(TimeoutError):
        office._run([tmp_path / 'selected'], tmp_path, time.monotonic() - 1)


def test_native_launch_keeps_existing_owner_and_scrubs_unselected_configuration(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setenv('PYTHONPATH', 'foreign')
    monkeypatch.setenv('LD_PRELOAD', 'foreign')
    monkeypatch.setenv('http_proxy', 'foreign')
    monkeypatch.setenv('PATH', 'foreign')
    class Child:
        returncode = 0
        def __init__(self, args, **options):
            observed.update(args=args, **options)
            options['stdout'].write(b'LibreOffice 25.2\n')
        def poll(self): return 0
        def wait(self): observed['settled'] = True
    monkeypatch.setattr(office.subprocess, 'Popen', Child)
    raw, warnings = office._run([tmp_path / 'soffice', '--version'], tmp_path, time.monotonic() + 5)
    assert raw == b'LibreOffice 25.2\n' and warnings is False
    assert observed['shell'] is False and observed['start_new_session'] is False and observed['settled']
    assert set(observed['env']).isdisjoint({'PYTHONPATH', 'LD_PRELOAD', 'http_proxy'})
    assert observed['env']['PATH'] != 'foreign' and observed['env']['MAX_CONCURRENCY'] == '1'
    assert observed['env']['TMPDIR'] == str(tmp_path)


@pytest.mark.parametrize('kind', ['bytes', 'entries', 'lookup', 'link'])
def test_staging_budget_and_lookup_failures_cannot_report_complete(tmp_path, monkeypatch, kind):
    (tmp_path / 'file').write_bytes(b'0123456789')
    maximum = 100
    if kind == 'bytes': maximum = 9
    elif kind == 'entries': monkeypatch.setattr(office, 'MAX_STAGE_ENTRIES', 0)
    elif kind == 'lookup':
        monkeypatch.setattr(office.os, 'scandir', lambda value: (_ for _ in ()).throw(PermissionError('synthetic')))
    else:
        # A portable hardlink is a real alias; no symlink privilege is needed.
        os.link(tmp_path / 'file', tmp_path / 'alias')
    with pytest.raises((ConversionError, PermissionError, ValueError)):
        office._stage_usage(tmp_path, maximum, time.monotonic() + 5)


def test_profile_is_per_operation_and_disables_active_content_and_external_updates():
    from defusedxml import ElementTree
    root = ElementTree.fromstring(office.PROFILE)
    values = {(item.attrib[office._NS + 'path'], prop.attrib[office._NS + 'name']): prop.findtext('value')
              for item in root for prop in item}
    scripting = '/org.openoffice.Office.Common/Security/Scripting'
    for key in ('DisableMacrosExecution', 'DisableActiveContent', 'DisableOLEAutomation', 'BlockUntrustedRefererLinks'):
        assert values[scripting, key] == 'true'
    assert values[scripting, 'MacroSecurityLevel'] == '3'
    assert not values[scripting, 'SecureURL']
    assert values['/org.openoffice.Office.Calc/Content/Update', 'Link'] == '1'


@pytest.mark.parametrize('suffix', ['.doc', '.ppt', '.xls', '.wpd', '.pdb'])
def test_fixed_xls_route_alone_selects_inactive_page_omission_with_real_intermediate_parser(
        tmp_path, monkeypatch, suffix):
    # Stub only the external native program. Inventory, held component readers,
    # actual intermediate ZIP parsing and notice propagation remain production.
    from io import BytesIO
    from zipfile import ZipFile, ZIP_STORED
    selected = installed(tmp_path)
    selection = office.OfficeSelection.capture(selected).snapshot()
    component_bytes = {row['name']: (selected.parent.parent / row['name']).read_bytes()
                       for row in selection['files']}
    stage = tmp_path / 'owned-parser-stage'
    stage.mkdir()
    source = stage / ('source' + suffix)
    if suffix in ('.doc', '.ppt', '.xls'):
        raw = bytes.fromhex('d0cf11e0a1b11ae1') + b'Synthetic parser-boundary fixture.'
    elif suffix == '.wpd':
        raw = b'\xffWPC' + b'Synthetic parser-boundary fixture.'
    else:
        raw = b'\0' * 60 + b'TEXtREAd' + b'\0' * 10
    source.write_bytes(raw)
    target = office.FILTERS[suffix][2]
    namespace = 'urn:oasis:names:tc:opendocument:xmlns:'
    kind = {'odt': 'text', 'ods': 'spreadsheet', 'odp': 'presentation'}[target]
    expected = 'Книга kôň e\u0301'
    paragraph = '<text:p>' + expected + '</text:p>'
    if target == 'ods':
        body = '<table:table><table:table-row><table:table-cell>' + paragraph + '</table:table-cell></table:table-row></table:table>'
    elif target == 'odp':
        body = '<draw:page><draw:frame><draw:text-box>' + paragraph + '</draw:text-box></draw:frame></draw:page>'
    else:
        body = paragraph
    mime = 'application/vnd.oasis.opendocument.' + kind
    content = (f'<office:document-content xmlns:office="{namespace}office:1.0" '
               f'xmlns:text="{namespace}text:1.0" xmlns:table="{namespace}table:1.0" '
               f'xmlns:draw="{namespace}drawing:1.0"><office:body><office:{kind}>'
               + body + f'</office:{kind}></office:body></office:document-content>')
    manifest = (f'<manifest:manifest xmlns:manifest="{namespace}manifest:1.0">'
                f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{mime}"/>'
                '</manifest:manifest>')
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', mime, compress_type=ZIP_STORED)
        archive.writestr('content.xml', content)
        archive.writestr('META-INF/manifest.xml', manifest)
        if suffix == '.xls':
            archive.writestr('styles.xml',
                f'<office:document-styles xmlns:office="{namespace}office:1.0" '
                f'xmlns:style="{namespace}style:1.0" xmlns:text="{namespace}text:1.0">'
                '<office:master-styles><style:master-page><style:header-left style:display="false">'
                '<text:p><text:span text:style-name="T18"><text:sheet-name>PRIVATE_TEMPLATE</text:sheet-name>'
                '</text:span></text:p></style:header-left></style:master-page>'
                '</office:master-styles></office:document-styles>')
    intermediate = stream.getvalue()
    original_parser = office.extract_document
    calls, parser_calls = [], []
    def external_program(args, root, deadline, **options):
        calls.append(tuple(str(value) for value in args))
        if '--version' in args:
            return b'LibreOffice 26.8.3.1\n', False
        assert '--infilter=' + office.FILTERS[suffix][0] in args
        assert args[-1] == source
        options['output'].write_bytes(intermediate)
        return b'', False
    def parser(data, declared, **options):
        parser_calls.append((data, declared, dict(options)))
        return original_parser(data, declared, **options)
    monkeypatch.setattr(office, '_run', external_program)
    monkeypatch.setattr(office, 'extract_document', parser)
    engine, separator, records, warnings = office.office_records(
        source, raw, suffix, ConversionBudget(), time.monotonic() + 30, selection)
    assert len(calls) == 2 and len(parser_calls) == 1
    assert parser_calls[0][0] == intermediate and parser_calls[0][1] == '.' + target
    if suffix == '.xls':
        assert parser_calls[0][2]['allow_inactive_page_decorations'] is True
        assert 'inactive_page_decorations_not_read' in warnings
        with pytest.raises(ValueError, match='Hidden'):
            original_parser(intermediate, '.ods')
    else:
        assert 'allow_inactive_page_decorations' not in parser_calls[0][2]
        assert 'inactive_page_decorations_not_read' not in warnings
    assert [row['raw_text'] for row in records] == [expected]
    assert all(row['status'] == 'review_required' and row['physical_index'] is None
               and row['printed_label'] is None and row['printed_label_verified'] is False
               and row['logical_locator']['intermediate_sha256'] == sha256(intermediate).hexdigest()
               for row in records)
    assert engine == 'selected-libreoffice-' + suffix[1:] and separator == '\n\n'
    assert 'headers_footers_styles_not_rendered' in warnings and source.read_bytes() == raw
    assert all((selected.parent.parent / name).read_bytes() == value for name, value in component_bytes.items())
