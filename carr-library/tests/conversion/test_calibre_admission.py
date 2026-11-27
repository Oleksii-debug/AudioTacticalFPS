"""Selected core/admission tests. Synthetic components are never called a qualified Calibre."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library.conversion._selected_files import file_identity, regular_file
from carr_library.conversion import ConversionBudget, ConversionError, ConversionService, ConversionUnavailable
from carr_library.conversion import calibre
from carr_library.conversion.selected_adapters import selected_records


def installed(tmp_path, layout='windows'):
    root = tmp_path / 'Книги runtime Ω'
    if layout == 'windows':
        executable = 'ebook-convert.exe'
        names = (executable, 'app/bin/calibre-launcher.dll', 'app/bin/python-lib.bypy.frozen',
                 'app/bin/python311.dll')
    else:
        executable = 'ebook-convert'
        names = (executable, 'bin/ebook-convert', 'lib/libcalibre-launcher.so',
                 'lib/calibre-extensions/python-lib.bypy.frozen', 'lib/libpython3.11.so.1.0')
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('Synthetic admission-only component: ' + name).encode())
    return root / executable


def header(suffix):
    if suffix in ('.azw', '.azw3', '.mobi', '.prc'):
        return b'\0' * 60 + b'BOOKMOBI' + b'\0' * 10
    return {'.chm': b'ITSF' + b'\0' * 80, '.lit': b'ITOLITLS' + b'\0' * 80,
            '.tcr': b'!!8-Bit!!' + b'\0' * 80,
            '.djvu': b'AT&TFORM' + b'\0' * 4 + b'DJVU' + b'\0' * 80}[suffix]


def simulated_output(monkeypatch, raw='Точний український текст Ω.\n'.encode('utf-8')):
    calls = []
    def run(args, stage, deadline, **options):
        calls.append((args, stage, options))
        if args[1] == '--version':
            return b'ebook-convert (calibre 9.19.0)\n', False
        Path(args[2]).write_bytes(raw)
        return b'', False
    monkeypatch.setattr(calibre, '_run', run)
    return calls


@pytest.mark.parametrize('layout', ['windows', 'linux'])
def test_core_selection_and_snapshot_bind_real_files_without_executing(tmp_path, monkeypatch, layout):
    path = installed(tmp_path, layout)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Admission executed a synthetic core.'))
    chosen = calibre.CalibreSelection.capture(path)
    value = chosen.snapshot()
    assert calibre._admit(value, time.monotonic() + 10) == path
    assert len(value['files']) in (4, 5)
    for row in value['files']:
        selected = path.parent / row['name']
        assert row['identity'] == list(file_identity(regular_file(selected)))
        assert row['sha256'] == sha256(selected.read_bytes()).hexdigest()
    assert json.dumps(value, ensure_ascii=False).encode() and len(json.dumps(value)) < 64000


@pytest.mark.parametrize('change', ['missing-core', 'alias-core', 'relative-path', 'wrong-name', 'empty-core'])
def test_incomplete_or_unbound_installation_is_refused_before_launch(tmp_path, change):
    path = installed(tmp_path)
    core = path.parent / 'app/bin/python-lib.bypy.frozen'
    if change == 'missing-core':
        core.unlink()
    elif change == 'alias-core':
        os.link(core, tmp_path / 'external-core-alias')
    elif change == 'relative-path':
        path = Path('ebook-convert.exe')
    elif change == 'wrong-name':
        path = path.with_name('calibre-debug.exe')
        path.write_bytes(b'not an admitted tool')
    else:
        core.write_bytes(b'')
    with pytest.raises((OSError, ValueError, RuntimeError)):
        calibre.CalibreSelection.capture(path)


@pytest.mark.parametrize('change', ['hash', 'identity', 'foreign-root', 'extra-field', 'extra-row', 'missing-row'])
def test_worker_refuses_modified_selected_inventory(tmp_path, change):
    path = installed(tmp_path)
    value = calibre.CalibreSelection.capture(path).snapshot()
    if change == 'hash':
        value['files'][0]['sha256'] = '0' * 64
    elif change == 'identity':
        value['files'][0]['identity'][1] += 1
    elif change == 'foreign-root':
        value['root_identity'][1] += 1
    elif change == 'extra-field':
        value['command'] = '--run-plugin'
    elif change == 'extra-row':
        value['files'].append(dict(value['files'][0]))
    else:
        value['files'].pop()
    with pytest.raises((ValueError, RuntimeError)):
        calibre._admit(value, time.monotonic() + 10)


@pytest.mark.parametrize('suffix', sorted(calibre.CALIBRE_SUFFIXES))
def test_false_format_header_is_refused_before_native_admission(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(calibre, '_admit', lambda *a, **k: pytest.fail('Invalid format reached runtime admission.'))
    with pytest.raises(ConversionError, match='document-family'):
        calibre.calibre_records(tmp_path / ('source' + suffix), b'renamed unrelated content',
                                suffix, ConversionBudget(), time.monotonic() + 10, {})


@pytest.mark.parametrize('suffix', sorted(calibre.CALIBRE_SUFFIXES))
def test_fixed_plain_text_arguments_and_source_lineage_for_every_enabled_family(tmp_path, monkeypatch, suffix):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path).snapshot()
    source = tmp_path / ('source' + suffix)
    source.write_bytes(header(suffix))
    original = source.read_bytes()
    calls = simulated_output(monkeypatch)
    engine, separator, records, notices = selected_records(source, original, suffix,
        ConversionBudget(), time.monotonic() + 10, {'kind': 'calibre-book', 'runtime': selection})
    assert source.read_bytes() == original and engine == 'selected-calibre-' + suffix[1:]
    assert separator == '\n\n' and len(records) == 1 and records[0]['physical_index'] is None
    assert records[0]['raw_text'] == 'Точний український текст Ω.\n'
    assert records[0]['logical_locator']['pagination'] == 'not_reconstructed'
    assert records[0]['status'] == 'review_required'
    assert calls[1][0] == [path, source, tmp_path / 'calibre-reading.txt',
        '--txt-output-encoding=utf-8', '--txt-output-formatting=plain', '--newline=unix', '--max-line-length=0']
    env = calls[1][2]['environment']
    assert Path(env['CALIBRE_CONFIG_DIRECTORY']).is_relative_to(tmp_path)
    assert env['CALIBRE_IGNORE_CUSTOM_PLUGINS'] == '1'
    assert 'selected_native_tool_is_not_an_os_sandbox' in notices
    if suffix == '.djvu':
        assert 'embedded_djvu_text_only_no_new_ocr' in notices


@pytest.mark.parametrize('raw', [b'', b' \n\t', b'a\0b', b'\xff'], ids=['empty', 'whitespace', 'nul', 'invalid-utf8'])
def test_invalid_or_empty_converter_text_is_not_accepted(tmp_path, monkeypatch, raw):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path).snapshot()
    source = tmp_path / 'source.mobi'
    source.write_bytes(header('.mobi'))
    simulated_output(monkeypatch, raw)
    with pytest.raises((ValueError, UnicodeError)):
        calibre.calibre_records(source, source.read_bytes(), '.mobi', ConversionBudget(),
                                time.monotonic() + 10, selection)


def test_private_environment_does_not_inherit_user_plugins_python_hooks_or_secrets(tmp_path, monkeypatch):
    for name in ('CALIBRE_DEVELOP_FROM', 'CALIBRE_PORTABLE_BUILD', 'PYTHONPATH', 'PYTHONHOME',
                 'SSLKEYLOGFILE', 'HTTP_PROXY', 'HTTPS_PROXY', 'OPENAI_API_KEY', 'HOME'):
        monkeypatch.setenv(name, '/unselected/private-value')
    environment = calibre._environment(tmp_path, tmp_path / 'selected-core')
    assert environment['HOME'] == str(tmp_path / 'home')
    assert not any(key in environment for key in ('CALIBRE_DEVELOP_FROM', 'CALIBRE_PORTABLE_BUILD',
        'PYTHONPATH', 'PYTHONHOME', 'SSLKEYLOGFILE', 'HTTP_PROXY', 'HTTPS_PROXY', 'OPENAI_API_KEY'))
    assert environment['QT_QPA_PLATFORM'] == 'offscreen'
    assert environment['CALIBRE_CONFIG_DIRECTORY'] != '/unselected/private-value'


def test_early_cancellation_never_admits_native_code(tmp_path, monkeypatch):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Cancelled selection executed code.'))
    with pytest.raises(InterruptedError):
        selection.snapshot(cancelled=lambda: True)


def test_native_invocation_inherits_existing_owner_and_uses_fixed_binary_streams(tmp_path, monkeypatch):
    captured = {}
    class Process:
        returncode = 0
        def poll(self):
            return self.returncode
        def wait(self):
            captured['waited'] = True
            return 0
    def launch(args, **options):
        captured.update(args=args, options=options)
        captured['stdin_eof'] = options['stdin'].read(1)
        options['stdout'].write(b'bounded diagnostic')
        options['stdout'].flush()
        return Process()
    monkeypatch.setattr(subprocess, 'Popen', launch)
    environment = calibre._environment(tmp_path, tmp_path)
    result = calibre._run([str(Path(sys.executable).absolute()), '--version'], tmp_path,
                          time.monotonic() + 10, environment=environment)
    assert result == (b'bounded diagnostic', False)
    assert captured['options']['start_new_session'] is False
    assert captured['options']['shell'] is False
    assert captured['stdin_eof'] == b'' and captured['options']['stdin'].closed
    assert captured['options']['env'] is environment and captured['waited']
    assert captured['options']['stdout'].closed and captured['options']['stderr'].closed


def test_fast_finished_child_cannot_bypass_diagnostic_byte_limit(tmp_path, monkeypatch):
    class Process:
        returncode = 0
        def poll(self):
            return 0
        def wait(self):
            return 0
    def launch(args, **options):
        options['stdout'].write(b'x' * (calibre.MAX_LOG_BYTES + 1))
        options['stdout'].flush()
        return Process()
    monkeypatch.setattr(subprocess, 'Popen', launch)
    with pytest.raises(ConversionError, match='diagnostics'):
        calibre._run([str(Path(sys.executable).absolute())], tmp_path, time.monotonic() + 10,
                     environment=calibre._environment(tmp_path, tmp_path))


def test_expired_adapter_never_starts_another_native_child(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('An expired adapter launched a child.'))
    with pytest.raises(TimeoutError):
        calibre._run([str(Path(sys.executable).absolute())], tmp_path, time.monotonic() - 1,
                     environment=calibre._environment(tmp_path, tmp_path))


@pytest.mark.parametrize('kind', ['bytes', 'entries', 'aliased-stage'])
def test_existing_complete_staging_bounds_are_retained(tmp_path, monkeypatch, kind):
    stage = tmp_path / 'private'
    stage.mkdir()
    (stage / 'owned').write_bytes(b'1234')
    maximum = 3 if kind == 'bytes' else 100
    if kind == 'entries':
        from carr_library.conversion import office
        monkeypatch.setattr(office, 'MAX_STAGE_ENTRIES', 1)
        (stage / 'other').write_bytes(b'1')
    elif kind == 'aliased-stage':
        os.link(stage / 'owned', tmp_path / 'outside-alias')
    with pytest.raises((ValueError, RuntimeError)):
        calibre._stage_usage(stage, maximum, time.monotonic() + 10)


@pytest.mark.parametrize('version', [b'ebook-convert (calibre 9.19)\n',
                                     b'ebook-convert.exe (calibre 9.19.0)\r\n'],
                         ids=['linux-entrypoint', 'windows-entrypoint'])
def test_actual_upstream_entrypoint_version_names_are_accepted(tmp_path, monkeypatch, version):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path).snapshot()
    source = tmp_path / 'source.tcr'
    source.write_bytes(header('.tcr'))
    def run(args, stage, deadline, **options):
        if args[1] == '--version':
            return version, False
        Path(args[2]).write_text('Selected public reading fixture.', encoding='utf-8')
        return b'', False
    monkeypatch.setattr(calibre, '_run', run)
    result = calibre.calibre_records(source, source.read_bytes(), '.tcr', ConversionBudget(),
                                    time.monotonic() + 10, selection)
    assert result[2][0]['logical_locator']['converter'] == version.decode().strip()


def test_prc_palmdoc_family_is_admitted_without_enabling_arbitrary_pdb():
    calibre._input_header(b'\0' * 60 + b'TEXtREAd' + b'\0' * 10, '.prc')
    with pytest.raises(ConversionError):
        calibre._input_header(b'\0' * 60 + b'foreign!' + b'\0' * 10, '.prc')
    with pytest.raises(ConversionError):
        calibre._input_header(b'\0' * 60 + b'TEXtREAd' + b'\0' * 10, '.azw3')


def test_observed_calibre_creator_banner_keeps_exact_version_identity(tmp_path, monkeypatch):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path).snapshot()
    source = tmp_path / 'source.tcr'
    source.write_bytes(header('.tcr'))
    original = source.read_bytes()
    calls = []
    def run(args, stage, deadline, **options):
        calls.append(tuple(args))
        if args[1] == '--version':
            return (b'ebook-convert.exe (calibre 9.19.0)\r\n'
                    b'Created by: Kovid Goyal <kovid@kovidgoyal.net>\r\n'), False
        Path(args[2]).write_bytes(b'Actual observed banner; controlled reading text.')
        return b'', False
    monkeypatch.setattr(calibre, '_run', run)
    result = calibre.calibre_records(source, original, '.tcr', ConversionBudget(),
                                    time.monotonic() + 10, selection)
    assert len(calls) == 2 and calls[0] == (path, '--version')
    assert result[2][0]['logical_locator']['converter'] == 'ebook-convert.exe (calibre 9.19.0)'
    assert result[2][0]['raw_text'] == 'Actual observed banner; controlled reading text.'
    assert source.read_bytes() == original


@pytest.mark.parametrize('banner', [
    b'ebook-convert.exe (calibre 9.19.0)\nCreated by: unexpected\n',
    b'ebook-convert.exe (calibre 9.19.0)\nCreated by: Kovid Goyal <kovid@kovidgoyal.net>\nextra\n',
    b'other-tool (calibre 9.19.0)\n',
    b'ebook-convert.exe (calibre unknown)\n',
], ids=['foreign-creator', 'extra-line', 'foreign-entrypoint', 'invalid-version'])
def test_unknown_calibre_version_banner_never_reaches_conversion(tmp_path, monkeypatch, banner):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path).snapshot()
    source = tmp_path / 'source.tcr'
    source.write_bytes(header('.tcr'))
    original = source.read_bytes()
    calls = []
    def run(args, stage, deadline, **options):
        calls.append(tuple(args))
        assert args[1] == '--version', 'Unrecognized CLI reached document conversion.'
        return banner, False
    monkeypatch.setattr(calibre, '_run', run)
    with pytest.raises(ConversionUnavailable, match='compatible bounded Calibre CLI'):
        calibre.calibre_records(source, original, '.tcr', ConversionBudget(),
                                time.monotonic() + 10, selection)
    assert calls == [(path, '--version')]
    assert not (tmp_path / 'calibre-reading.txt').exists()
    assert source.read_bytes() == original
