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


@pytest.mark.parametrize('change', ['component', 'replace-root', 'add-launch-helper'])
def test_queued_selection_refuses_new_authority_before_worker(tmp_path, monkeypatch, change):
    path = installed(tmp_path)
    source, output = tmp_path / 'original.doc', tmp_path / 'output'
    source.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'original')
    output.mkdir()
    converter = ConversionService(output, input_roots=(source,), office=path)
    if change == 'component':
        (path.parent / 'soffice.bin').write_bytes(b'changed component')
    elif change == 'replace-root':
        path.parent.parent.rename(tmp_path / 'retained-original-office')
        installed(tmp_path)
    else:
        (path.parent / 'oosplash').write_bytes(b'new launch helper')
    from carr_library.conversion import service
    monkeypatch.setattr(service, 'run_process', lambda *a, **k: pytest.fail('Changed selection reached worker.'))
    with pytest.raises(ConversionError, match='changed'):
        converter.extract(source, 'reading.txt')
    assert source.read_bytes().endswith(b'original') and list(output.iterdir()) == []


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
    ('.wpd', b'renamed text'), ('.pdb', b'\0' * 78), ('.wri', b'\0' * 128),
], ids=['doc', 'ppt', 'xls', 'wordperfect', 'foreign-pdb', 'foreign-wri'])
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



def test_real_selected_reader_prevents_or_detects_same_size_mutation(tmp_path):
    path = tmp_path / 'selected-component.bin'
    original = b'first verified component'
    path.write_bytes(original)
    expected = office.file_identity(office.regular_file(path))
    if os.name == 'nt':
        with office.verified_reader(path, expected_identity=expected) as stream:
            with pytest.raises(OSError):
                path.write_bytes(b'other verified component')
            assert stream.read() == original
        assert path.read_bytes() == original
    else:
        with pytest.raises(ConversionError, match='changed'):
            with office.verified_reader(path, expected_identity=expected):
                path.write_bytes(b'other verified component')


def test_actual_open_race_refuses_fifo_or_link_before_reading_with_finite_child(tmp_path):
    import subprocess
    import sys
    program = r"""
import os
from pathlib import Path
import sys
from carr_library.conversion import office
root = Path(sys.argv[1])
path, target = root / 'selected.bin', root / 'foreign.bin'
path.write_bytes(b'approved')
target.write_bytes(b'foreign!')
original = office._open_payload_file
def swapped(selected):
    path.unlink()
    if os.name == 'nt':
        os.link(target, path)
    else:
        os.mkfifo(path)
    return original(selected)
office._open_payload_file = swapped
try:
    with office.verified_reader(path) as stream:
        stream.read()
except (ValueError, RuntimeError, OSError):
    assert target.read_bytes() == b'foreign!'
    print('refused-before-consumption')
else:
    raise AssertionError('A substituted special/linked file was admitted.')
"""
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(tmp_path)],
        capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == 'refused-before-consumption'
