"""Real source derivatives at commit, cancellation and native process boundaries."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import sys
from threading import Event
from zipfile import ZipFile

import pytest

from carr_library.conversion import ConversionService, ConversionBudget, ConversionError
import carr_library.conversion.service as service_module
from carr_library.conversion.safety import ProcessBudget, run_process


@pytest.fixture
def source_service(tmp_path):
    source = tmp_path / 'original.txt'
    source.write_bytes('Exact original.\r\nУкраїнська та slovenčina.'.encode())
    output = tmp_path / 'exports'
    output.mkdir()
    return source, ConversionService(output, input_roots=(source,)), output


def assert_receipt(result, source):
    raw = result.path.read_bytes()
    assert raw == source.read_bytes()
    report = json.loads(result.report_path.read_bytes())
    assert report == result.report
    assert report['verified_for_quotation'] is False
    assert report['text_sha256'] == sha256(raw).hexdigest()
    assert report['source_sha256'] == sha256(source.read_bytes()).hexdigest()
    assert result.path.stat().st_nlink == result.report_path.stat().st_nlink == 1


def test_directory_flush_failure_preserves_both_committed_files(source_service, monkeypatch):
    source, converter, output = source_service
    def fail_flush(_):
        raise OSError('synthetic directory flush fault')
    monkeypatch.setattr(service_module, 'sync_directory', fail_flush)
    result = converter.extract(source, 'reading.txt')
    assert_receipt(result, source)
    assert result.warnings and 'durability' in result.warnings[0]
    assert sorted(path.name for path in output.iterdir()) == ['reading.txt', 'reading.txt.provenance.json']


def test_cleanup_failure_is_a_committed_warning_without_hardlink_twins(source_service, monkeypatch):
    source, converter, output = source_service
    original = service_module.shutil.rmtree
    def fail_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith('.pending-conversion-'):
            raise PermissionError('synthetic cleanup fault')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(service_module.shutil, 'rmtree', fail_cleanup)
    result = converter.extract(source, 'reading.txt')
    assert_receipt(result, source)
    assert any('cleanup' in warning for warning in result.warnings)
    pending, = output.glob('.pending-conversion-*')
    assert not (pending / 'reading.txt').exists()
    assert not (pending / 'provenance.json').exists()


def test_cancel_between_text_and_receipt_rolls_back_owned_text(source_service, monkeypatch):
    source, converter, output = source_service
    stop = Event()
    original = service_module._publish_file
    def cancel_after_first(incoming, destination):
        original(incoming, destination)
        if Path(destination).suffix == '.txt':
            stop.set()
    monkeypatch.setattr(service_module, '_publish_file', cancel_after_first)
    before = source.read_bytes()
    with pytest.raises(InterruptedError):
        converter.extract(source, 'reading.txt', cancelled=stop.is_set)
    assert source.read_bytes() == before and not list(output.iterdir())


def test_cancellation_after_receipt_does_not_misreport_a_committed_conversion(source_service, monkeypatch):
    source, converter, _ = source_service
    stop = Event()
    original = service_module._publish_file
    def cancel_after_receipt(incoming, destination):
        original(incoming, destination)
        if Path(destination).name.endswith('.provenance.json'):
            stop.set()
    monkeypatch.setattr(service_module, '_publish_file', cancel_after_receipt)
    result = converter.extract(source, 'reading.txt', cancelled=stop.is_set)
    assert stop.is_set()
    assert_receipt(result, source)


@pytest.mark.parametrize('conflict', ['reading.txt', 'reading.txt.provenance.json'])
def test_concurrent_output_is_never_overwritten_or_deleted(source_service, monkeypatch, conflict):
    source, converter, output = source_service
    original = service_module._publish_file
    def create_competing_file(incoming, destination):
        if Path(destination).name == conflict:
            Path(destination).write_bytes(b'owned by another writer')
        return original(incoming, destination)
    monkeypatch.setattr(service_module, '_publish_file', create_competing_file)
    with pytest.raises(FileExistsError):
        converter.extract(source, 'reading.txt')
    assert (output / conflict).read_bytes() == b'owned by another writer'
    assert [path.name for path in output.iterdir()] == [conflict]


def test_source_change_in_final_callback_invalidates_receipt(source_service, monkeypatch):
    source, converter, output = source_service
    original = service_module._publish_file
    def change_source_after_text(incoming, destination):
        original(incoming, destination)
        if Path(destination).suffix == '.txt':
            source.write_bytes(b'changed independently before the receipt')
    monkeypatch.setattr(service_module, '_publish_file', change_source_after_text)
    with pytest.raises(ConversionError, match='source changed'):
        converter.extract(source, 'reading.txt')
    assert source.read_bytes() == b'changed independently before the receipt'
    assert not list(output.iterdir())


def test_output_root_replacement_is_refused_and_foreign_tree_preserved(source_service, monkeypatch):
    source, converter, output = source_service
    original = service_module.run_process
    previous = output.with_name('original-export-directory')
    def replace_after_parsing(*args, **kwargs):
        result = original(*args, **kwargs)
        output.rename(previous)
        output.mkdir()
        (output / 'foreign.txt').write_bytes(b'leave intact')
        return result
    monkeypatch.setattr(service_module, 'run_process', replace_after_parsing)
    with pytest.raises((ValueError, FileNotFoundError)):
        converter.extract(source, 'reading.txt')
    assert (output / 'foreign.txt').read_bytes() == b'leave intact'
    assert not (output / 'reading.txt').exists()
    assert not (previous / 'reading.txt').exists()


def test_explicit_file_scope_does_not_authorize_its_neighbour(source_service):
    source, converter, output = source_service
    neighbour = source.with_name('not-selected.txt')
    neighbour.write_bytes(b'not authorized')
    with pytest.raises(PermissionError):
        converter.extract(neighbour, 'reading.txt')
    assert not list(output.iterdir())


@pytest.mark.parametrize('name', ['COM\u00b9.txt', 'LPT\u00b2.txt', 'CON .txt', 'reading.txt:stream', '../outside.txt'])
def test_portable_output_aliases_and_traversal_are_rejected(source_service, name):
    source, converter, output = source_service
    with pytest.raises(ValueError):
        converter.extract(source, name)
    assert not list(output.iterdir())


def test_hardlinked_source_is_not_an_authorized_ordinary_file(source_service):
    source, converter, output = source_service
    os.link(source, source.with_name('second-name.txt'))
    with pytest.raises(ValueError):
        converter.extract(source, 'reading.txt')
    assert not list(output.iterdir())


def test_isolated_worker_ignores_cwd_and_pythonpath(source_service, monkeypatch):
    source, converter, output = source_service
    rogue = output / 'carr_library' / 'conversion'
    rogue.mkdir(parents=True)
    marker = output / 'untrusted-import.txt'
    (rogue.parent / '__init__.py').write_text('raise RuntimeError("untrusted package")')
    (rogue / 'worker.py').write_text(f'from pathlib import Path;Path({str(marker)!r}).write_text("bad")')
    monkeypatch.setenv('PYTHONPATH', str(output))
    monkeypatch.chdir(output)
    result = converter.extract(source, 'reading.txt')
    assert_receipt(result, source)
    assert not marker.exists()


def test_operation_deadline_includes_snapshot_and_publication(source_service, monkeypatch):
    source, _, output = source_service
    converter = ConversionService(output, input_roots=(source,),
                                  budget=replace(ConversionBudget(), timeout=1))
    clock = [100.0]
    monkeypatch.setattr(service_module, 'monotonic', lambda: clock[0])
    def advance_after_snapshot():
        if next(output.glob('.pending-conversion-*/source.txt'), None) is not None:
            clock[0] += 2
        return False
    with pytest.raises(TimeoutError):
        converter.extract(source, 'reading.txt', cancelled=advance_after_snapshot)
    assert not list(output.iterdir())


def test_actual_child_output_budget_checks_even_a_fast_exit(tmp_path):
    output = tmp_path / 'oversized.txt'
    command = [sys.executable, '-I', '-c',
               'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"x"*4096)', str(output)]
    with pytest.raises(ConversionError, match='byte budget'):
        run_process(command, budget=ProcessBudget(5, 128, 0), cancelled=lambda: False, watch=(output,))
    assert output.stat().st_size == 4096


def test_actual_child_private_staging_inventory_is_bounded(tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir()
    command = [sys.executable, '-I', '-c',
               'import pathlib,sys; root=pathlib.Path(sys.argv[1]); '
               '[(root/str(n)).write_bytes(b"x") for n in range(129)]', str(stage)]
    with pytest.raises(ConversionError, match='staging file budget'):
        run_process(command, budget=ProcessBudget(5, 1024, 0), cancelled=lambda: False, watch=(stage,))


def test_actual_child_is_stopped_by_cancellation(tmp_path):
    marker = tmp_path / 'started'
    command = [sys.executable, '-I', '-c',
        'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("started"); time.sleep(30)', str(marker)]
    with pytest.raises(InterruptedError):
        run_process(command, budget=ProcessBudget(5, 1024, 0), cancelled=marker.exists, watch=(marker,))
    assert marker.read_text() == 'started'


def test_actual_child_is_stopped_by_its_time_budget(tmp_path):
    marker = tmp_path / 'started'
    command = [sys.executable, '-I', '-c',
        'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("started"); time.sleep(30)', str(marker)]
    with pytest.raises(TimeoutError):
        run_process(command, budget=ProcessBudget(.2, 1024, 0), cancelled=lambda: False, watch=(marker,))
    assert marker.read_text() == 'started'


@pytest.mark.parametrize('windows_audit_shape', [False, True])
def test_real_ocr_and_provenance_under_unicode_input_and_output_directories(tmp_path, monkeypatch, windows_audit_shape):
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract.ocr_runtime import discover_ocr_runtime
    runtime = discover_ocr_runtime('eng')
    assert runtime is not None, 'This real OCR regression requires the prepared local English OCR runtime.'
    if windows_audit_shape:
        # CPython 3.11 Windows reports the explicit executable parameter
        # (possibly None), not argv[0], in its Popen audit event. Reproduce
        # that event in the real isolated worker before actual local OCR.
        original_command = service_module.worker_command
        def audited_command(request):
            command = original_command(request)
            index = command.index('-c') + 1
            prefix = (
                "import subprocess,sys\n"
                "original_popen=subprocess.Popen\n"
                "def windows_audited_popen(args, **kw):\n"
                " sys.audit('subprocess.Popen', kw.get('executable'), subprocess.list2cmdline(args), kw.get('cwd'), kw.get('env'))\n"
                " return original_popen(args, **kw)\n"
                "subprocess.Popen=windows_audited_popen\n")
            command[index] = prefix + command[index]
            return command
        monkeypatch.setattr(service_module, 'worker_command', audited_command)
    source_dir, output = tmp_path / 'Джерела Ľudská', tmp_path / 'Čитання Україна'
    source_dir.mkdir()
    output.mkdir()
    source = source_dir / 'Оригінал stránky.png'
    image = Image.new('RGB', (1200, 180), 'white')
    ImageDraw.Draw(image).text((30, 45), 'SYNTHETIC UNICODE PATH', fill='black',
                              font=ImageFont.load_default(size=48))
    image.save(source)
    original = source.read_bytes()
    converter = ConversionService(output, input_roots=(source,))
    result = converter.extract(source, 'Похідний текст.txt', ocr_languages=('eng',))
    assert 'SYNTHETIC UNICODE PATH' in result.path.read_text(encoding='utf-8')
    assert source.read_bytes() == original
    assert result.report['verified_for_quotation'] is False
    identity = result.report['metadata']['runtime_identity']['ocr']
    assert identity['state'] == 'present' and len(identity['executable_sha256']) == 64
    assert set(identity['model_sha256']) == {'eng'}
    assert result.report['metadata']['model_profile'] == 'installed'
    assert result.report['source_sha256'] == sha256(original).hexdigest()


def test_archive_actual_count_is_checked_before_zipfile_allocation(monkeypatch):
    from carr_library.conversion import formats
    payload = BytesIO()
    with ZipFile(payload, 'w') as archive:
        for index in range(3):
            archive.writestr(str(index) + '.xml', '<root/>')
    data = bytearray(payload.getvalue())
    struct.pack_into('<HH', data, len(data) - 22 + 8, 1, 1)
    def forbidden(*args, **kwargs):
        raise AssertionError('ZipInfo objects allocated before central-record validation')
    monkeypatch.setattr(formats, 'ZipFile', forbidden)
    with pytest.raises(ConversionError, match='directory budget'):
        formats.Archive(bytes(data), ConversionBudget())


def test_invalid_worker_authority_fields_are_not_merged_into_receipt(source_service, monkeypatch):
    source, converter, output = source_service
    original = service_module.run_process
    def add_false_authority(*args, **kwargs):
        result = original(*args, **kwargs)
        details, = output.glob('.pending-conversion-*/details.json')
        value = json.loads(details.read_bytes())
        value['verified_for_quotation'] = True
        details.write_text(json.dumps(value))
        return result
    monkeypatch.setattr(service_module, 'run_process', add_false_authority)
    with pytest.raises(ConversionError, match='provenance'):
        converter.extract(source, 'reading.txt')
    assert not list(output.iterdir())
