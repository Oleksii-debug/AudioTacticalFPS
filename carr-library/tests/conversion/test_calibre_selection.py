"""Selected core/admission tests. Synthetic components are never called a qualified Calibre."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library.conversion.office import file_identity, regular_file
from carr_library.conversion import ConversionBudget, ConversionError, ConversionService, ConversionUnavailable
from carr_library.conversion import calibre


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


@pytest.mark.parametrize('suffix', sorted(calibre.CALIBRE_SUFFIXES))
def test_legacy_extension_never_activates_an_implicit_runtime(tmp_path, monkeypatch, suffix):
    source = tmp_path / ('original' + suffix)
    source.write_bytes(header(suffix))
    output = tmp_path / 'output'
    output.mkdir()
    from carr_library.conversion import service
    monkeypatch.setattr(service, 'run_process', lambda *a, **k: pytest.fail('An unselected tool reached the worker.'))
    with pytest.raises(ConversionUnavailable, match='explicitly'):
        ConversionService(output, input_roots=(source,)).extract(source, 'reading.txt')
    row = next(row for row in ConversionService(output).capabilities() if row['extension'] == suffix)
    assert row['available'] is False and row['adapter'] == 'explicitly selected local Calibre'
    assert list(output.iterdir()) == []


def test_selected_calibre_does_not_implicitly_admit_other_tools(tmp_path):
    path = installed(tmp_path)
    root = tmp_path / 'outputs'
    baseline = {row['extension']: row for row in ConversionService(root).capabilities()}
    rows = {row['extension']: row for row in ConversionService(root, calibre=path).capabilities()}
    assert all(rows[key]['available'] for key in calibre.CALIBRE_SUFFIXES)
    assert not any(rows[key]['available'] for key in ('.doc', '.pdb'))
    assert all(rows[key] == baseline[key] for key in baseline if key not in calibre.CALIBRE_SUFFIXES)
    assert 'existing text layer' in rows['.djvu']['limitation']


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


@pytest.mark.parametrize('change', ['changed-bytes', 'replace-root', 'new-python-library'])
def test_queued_converter_selection_cannot_change_before_the_owned_worker(tmp_path, monkeypatch, change):
    path = installed(tmp_path)
    source, output = tmp_path / 'original.mobi', tmp_path / 'output'
    source.write_bytes(header('.mobi'))
    output.mkdir()
    selected = ConversionService(output, input_roots=(source,), calibre=path)
    if change == 'changed-bytes':
        path.write_bytes(b'changed runtime')
    elif change == 'replace-root':
        path.parent.rename(tmp_path / 'retained-old-runtime')
        installed(tmp_path)
    else:
        (path.parent / 'app/bin/python312.dll').write_bytes(b'additional Python runtime')
    from carr_library.conversion import service
    monkeypatch.setattr(service, 'run_process', lambda *a, **k: pytest.fail('Changed authority reached the native worker.'))
    with pytest.raises((ValueError, RuntimeError)):
        selected.extract(source, 'reading.txt')
    assert list(output.iterdir()) == [] and source.read_bytes() == header('.mobi')


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
    engine, separator, records, notices = calibre.calibre_records(source, original, suffix,
        ConversionBudget(), time.monotonic() + 10, selection)
    assert source.read_bytes() == original and engine == 'selected-calibre-' + suffix[1:]
    assert separator == '\n\n' and len(records) == 1 and records[0]['physical_index'] is None
    assert records[0]['raw_text'] == 'Точний український текст Ω.\n'
    assert records[0]['logical_locator']['pagination'] == 'not_reconstructed'
    assert records[0]['status'] == 'review_required'
    assert calls[1][0] == [path, tmp_path / 'calibre-private' / ('input' + suffix), tmp_path / 'calibre-reading.txt',
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



def run_real_result(args, **options):
    """Only the native process boundary is controlled; use the current worker parser."""
    from carr_library.conversion import worker
    assert args[1:4] == ['-I', '-B', '-m']
    assert args[4] == 'carr_library.conversion.worker' and options['owned_tree'] is True
    message = json.loads(Path(args[-1]).read_text(encoding='utf-8'))
    result = worker._calibre_result(Path(message['source']), ConversionBudget(**message['budget']),
                                   time.monotonic() + 10, message['calibre'])
    Path(message['output']).write_bytes(result.pop('text').encode('utf-8'))
    Path(message['report']).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)


def test_worker_and_parent_publish_existing_reading_pair_with_exact_provenance(tmp_path, monkeypatch):
    path = installed(tmp_path)
    source, output = tmp_path / 'Обрана книга Ω.mobi', tmp_path / 'reading'
    source.write_bytes(header('.mobi'))
    original = source.read_bytes()
    output.mkdir()
    from carr_library.conversion import service, worker
    simulated_output(monkeypatch)
    observed = []
    def invoke(args, **options):
        message = json.loads(Path(args[-1]).read_text(encoding='utf-8'))
        observed.append(message)
        return run_real_result(args, **options)
    monkeypatch.setattr(service, 'run_process', invoke)
    result = ConversionService(output, input_roots=(source,), calibre=path).extract(source, 'copy.txt')
    assert result.path.read_text('utf-8') == 'Точний український текст Ω.\n'
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['verified_for_quotation'] is False and result.report['original_modified'] is False
    assert result.report_path.is_file() and source.read_bytes() == original
    assert observed[0]['operation'] == 'calibre'
    with pytest.raises(FileExistsError):
        ConversionService(output, input_roots=(source,), calibre=path).extract(source, 'copy.txt')
    assert len(observed) == 1


@pytest.mark.parametrize('kind', ['source-snapshot', 'selected-core'])
def test_actual_parent_readers_remain_held_through_the_outer_worker(tmp_path, monkeypatch, kind):
    path = installed(tmp_path)
    source, output = tmp_path / 'original.mobi', tmp_path / 'reading'
    source.write_bytes(header('.mobi'))
    output.mkdir()
    from carr_library.conversion import service, worker
    simulated_output(monkeypatch)
    attempts = []
    def invoke(args, **options):
        message = json.loads(Path(args[-1]).read_text(encoding='utf-8'))
        selected = Path(message['source']) if kind == 'source-snapshot' else path
        old = selected.read_bytes()
        try:
            with selected.open('r+b') as stream:
                stream.write(b'X' * len(old))
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                stream.write(old)
                stream.flush()
                os.fsync(stream.fileno())
        except PermissionError:
            attempts.append('denied')
        else:
            attempts.append('changed')
        return run_real_result(args, **options)
    monkeypatch.setattr(service, 'run_process', invoke)
    converter = ConversionService(output, input_roots=(source,), calibre=path)
    if os.name == 'nt':
        result = converter.extract(source, 'copy.txt')
        assert result.path.is_file() and attempts == ['denied']
    else:
        with pytest.raises((ValueError, RuntimeError)):
            converter.extract(source, 'copy.txt')
        assert attempts == ['changed'] and list(output.iterdir()) == []


def test_early_cancellation_never_admits_native_code(tmp_path, monkeypatch):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Cancelled selection executed code.'))
    with pytest.raises(InterruptedError):
        selection.snapshot(cancelled=lambda: True)


def test_native_invocation_inherits_existing_owner_and_uses_fixed_binary_streams(tmp_path, monkeypatch):
    captured, directories = {}, []
    temporary_file = calibre.TemporaryFile
    def private_stream(*args, **kwargs):
        directories.append(kwargs.get('dir'))
        return temporary_file(*args, **kwargs)
    monkeypatch.setattr(calibre, 'TemporaryFile', private_stream)
    class Process:
        returncode = 0
        def poll(self):
            return self.returncode
        def wait(self):
            captured['waited'] = True
            return 0
    def launch(args, **options):
        captured.update(args=args, options=options)
        assert options['stdin'].read() == b''
        assert os.fstat(options['stdin'].fileno()).st_size == 0
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
    assert directories == [tmp_path, tmp_path, tmp_path]
    assert captured['options']['env'] is environment and captured['waited']
    assert all(captured['options'][name].closed for name in ('stdin', 'stdout', 'stderr'))


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


def test_native_private_input_is_checked_after_the_actual_converter(tmp_path, monkeypatch):
    path = installed(tmp_path)
    selection = calibre.CalibreSelection.capture(path).snapshot()
    source = tmp_path / 'source.chm'
    original = header('.chm')
    source.write_bytes(original)
    def changed(args, stage, deadline, **options):
        if args[1] == '--version':
            return b'ebook-convert (calibre 9.19.0)\n', False
        Path(args[2]).write_text('The converter returned text.', encoding='utf-8')
        Path(args[1]).write_bytes(b'Changed native input after use.')
        return b'', False
    monkeypatch.setattr(calibre, '_run', changed)
    with pytest.raises(ConversionError, match='changed'):
        calibre.calibre_records(source, original, '.chm', ConversionBudget(),
                                time.monotonic() + 10, selection)
    assert source.read_bytes() == original


def test_same_pending_native_owner_retains_exact_selected_readers_and_staging(tmp_path, monkeypatch):
    from carr_library.audio.cleanup import CleanupOwner
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.conversion import service, office
    path = installed(tmp_path)
    source, output = tmp_path / 'source.mobi', tmp_path / 'output'
    source.write_bytes(header('.mobi'))
    output.mkdir()
    selected = ConversionService(output, input_roots=(source,), calibre=path)
    active = []
    real_reader = office.verified_reader
    @contextmanager
    def observed(*args, **kwargs):
        with real_reader(*args, **kwargs) as stream:
            active.append(stream)
            try:
                yield stream
            finally:
                active.remove(stream)
    monkeypatch.setattr(office, 'verified_reader', observed)
    monkeypatch.setattr(calibre, 'verified_reader', observed)
    ready = [False]
    owner = CleanupOwner(lambda: ready[0])
    pending = AudioCleanupPending('Actual selected owner has not yet confirmed shutdown.', cleanup_owner=owner)
    stage = []
    def unsettled(args, **options):
        stage.append(Path(args[-1]).parent)
        assert options['owned_tree'] and len(active) == len(selected.calibre.files) + 1
        raise pending
    monkeypatch.setattr(service, 'run_process', unsettled)
    with pytest.raises(AudioCleanupPending) as raised:
        selected.extract(source, 'copy.txt')
    assert raised.value is pending and raised.value.cleanup_owner is owner
    assert active and all(not stream.closed for stream in active)
    assert stage[0].is_dir() and not (output / 'copy.txt').exists()
    assert owner.close() is False and active and stage[0].is_dir()
    ready[0] = True
    assert owner.close() is True
    assert active == [] and not stage[0].exists()
    assert list(output.iterdir()) == [] and source.read_bytes() == header('.mobi')


def test_late_native_return_cancel_does_not_publish_a_derivative(tmp_path, monkeypatch):
    from carr_library.conversion import service
    path = installed(tmp_path)
    source, output = tmp_path / 'source.tcr', tmp_path / 'output'
    source.write_bytes(header('.tcr'))
    output.mkdir()
    simulated_output(monkeypatch)
    stopped = [False]
    def completed(args, **options):
        result = run_real_result(args, **options)
        stopped[0] = True
        return result
    monkeypatch.setattr(service, 'run_process', completed)
    with pytest.raises(InterruptedError):
        ConversionService(output, input_roots=(source,), calibre=path).extract(
            source, 'copy.txt', cancelled=lambda: stopped[0])
    assert list(output.iterdir()) == [] and source.read_bytes() == header('.tcr')
