"""Parent-owned Windows Office workspace boundaries; native qualification is separate."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion import office, service as service_module
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceUnavailable
from tests.conversion.test_office_selection import installed


@pytest.fixture
def native_parent(monkeypatch):
    with TemporaryDirectory(prefix='co-test-') as directory:
        parent = Path(directory).resolve()
        monkeypatch.setattr(service_module, 'gettempdir', lambda: str(parent))
        # Module-local OS proxy: do not change pathlib or global stdlib state.
        monkeypatch.setattr(service_module, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))
        yield parent
        assert list(parent.iterdir()) == []


def selected(tmp_path):
    source = tmp_path / 'Оригінал.doc'
    original = bytes.fromhex('d0cf11e0a1b11ae1') + b'SYNTHETIC ORIGINAL'
    source.write_bytes(original)
    output = tmp_path / 'Читальна копія'
    output.mkdir()
    converter = ConversionService(output, input_roots=(source,), office=installed(tmp_path))
    return source, original, output, converter


def complete_reply(message):
    source = Path(message['source'])
    original = source.read_bytes()
    assert source.name == 'source.doc'
    assert sha256(original).hexdigest() == message['sha256']
    assert len(original) == message['source_size']
    text = 'Exact synthetic reading text.'
    Path(message['output']).write_bytes(text.encode('utf-8'))
    details = dict(engine='synthetic-office-boundary', separator='\n\n',
        segments=[dict(ordinal=1, logical_locator={'synthetic': True},
            physical_index=None, printed_label=None, printed_label_verified=False,
            output_characters=[0, len(text)], text_sha256=sha256(text.encode()).hexdigest(),
            status='review_required', warnings=[])], warnings=[],
        source_sha256=message['sha256'], source_size=message['source_size'])
    Path(message['report']).write_text(json.dumps(details), encoding='utf-8')
    return dict(state='complete', text_bytes=len(text.encode()), records=1)


@pytest.mark.parametrize('outcome', ['complete', 'cancelled', 'failed'])
def test_parent_keeps_both_stages_and_one_multivolume_lease_until_worker_settles(
        tmp_path, monkeypatch, native_parent, outcome):
    source, original, output, converter = selected(tmp_path)
    broker = ResourceBroker(tmp_path / 'resources', limits=ResourceLimits(min_free_disk_bytes=0))
    state = {'settled': False}
    cleaned = []
    actual_temp = service_module.TemporaryDirectory

    @contextmanager
    def owned_directory(*args, **options):
        with actual_temp(*args, **options) as directory:
            path = Path(directory)
            try:
                yield directory
            finally:
                assert state['settled'], 'A caller-owned stage was released before worker settlement.'
                assert len(broker.snapshot()['leases']) == 1
                cleaned.append(path)

    def invoke(message, **options):
        worker_stage = Path(message['source']).parent
        state['worker_stage'] = worker_stage
        assert worker_stage.parent == native_parent
        assert len(str(worker_stage).encode('utf-16-le')) // 2 <= 120
        stages = list(output.glob('.carr-conversion-*'))
        assert len(stages) == 1 and stages[0] != worker_stage
        active = broker.snapshot()['leases']
        assert len(active) == 1
        request = active[0]['request']
        assert request['disk_path'] == str(output.resolve())
        assert request['additional_disks'] == [[str(native_parent), (
            2 * converter.budget.max_source_bytes + converter.budget.max_text_bytes
            + service_module.MAX_REPORT_BYTES + 64 * 1024**2)]]
        assert request['disk_bytes'] == converter.budget.max_text_bytes + service_module.MAX_REPORT_BYTES
        try:
            if outcome == 'complete':
                return complete_reply(message)
            return dict(state=outcome, message='Synthetic boundary outcome')
        finally:
            state['settled'] = True

    monkeypatch.setattr(service_module, 'TemporaryDirectory', owned_directory)
    monkeypatch.setattr(service_module, '_invoke', invoke)
    if outcome == 'complete':
        result = converter.extract(source, 'reading.txt', broker=broker)
        assert result.path.read_text(encoding='utf-8') == 'Exact synthetic reading text.'
        assert result.report['source_sha256'] == sha256(original).hexdigest()
        assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
        assert sorted(path.name for path in output.iterdir()) == [
            'reading.txt', 'reading.txt.provenance.json']
    else:
        expected = InterruptedError if outcome == 'cancelled' else ConversionError
        with pytest.raises(expected):
            converter.extract(source, 'reading.txt', broker=broker)
        assert list(output.iterdir()) == []
    assert len(cleaned) == 2 and all(not path.exists() for path in cleaned)
    assert not state['worker_stage'].exists()
    assert broker.snapshot()['leases'] == []
    assert source.read_bytes() == original


def test_worker_text_changed_before_cross_workspace_copy_never_publishes(tmp_path, monkeypatch, native_parent):
    source, original, output, converter = selected(tmp_path)
    monkeypatch.setattr(service_module, '_invoke', lambda message, **options: complete_reply(message))
    actual = service_module.publish_new

    def changed(source_path, destination, **options):
        assert options['expected_sha256'] == sha256(Path(source_path).read_bytes()).hexdigest()
        Path(source_path).write_bytes(b'Changed synthetic reading text.')
        return actual(source_path, destination, **options)

    monkeypatch.setattr(service_module, 'publish_new', changed)
    with pytest.raises((ValueError, RuntimeError)):
        converter.extract(source, 'reading.txt')
    assert list(output.iterdir()) == []
    assert source.read_bytes() == original


def test_destination_race_during_verified_workspace_copy_preserves_foreign_file(tmp_path, monkeypatch, native_parent):
    source, original, output, converter = selected(tmp_path)
    monkeypatch.setattr(service_module, '_invoke', lambda message, **options: complete_reply(message))
    actual = service_module.publish_new
    target = output / 'reading.txt'

    def changed(source_path, destination, **options):
        assert Path(destination).parent.parent == output
        assert Path(source_path).parent.parent == native_parent
        target.write_bytes(b'Foreign completed document.')
        return actual(source_path, destination, **options)

    monkeypatch.setattr(service_module, 'publish_new', changed)
    with pytest.raises(FileExistsError):
        converter.extract(source, 'reading.txt')
    assert target.read_bytes() == b'Foreign completed document.'
    assert sorted(path.name for path in output.iterdir()) == ['reading.txt']
    assert source.read_bytes() == original


def test_atomic_workspace_resource_rejection_starts_no_worker_or_private_directory(tmp_path, monkeypatch, native_parent):
    source, original, output, converter = selected(tmp_path)
    broker = ResourceBroker(tmp_path / 'resources',
        limits=ResourceLimits(disk_bytes=1, min_free_disk_bytes=0))
    monkeypatch.setattr(service_module, '_invoke',
                        lambda *args, **options: pytest.fail('Unadmitted native workspace started.'))
    with pytest.raises(ResourceUnavailable, match='disk_bytes'):
        converter.extract(source, 'reading.txt', broker=broker)
    assert list(output.iterdir()) == [] and list(native_parent.iterdir()) == []
    assert broker.snapshot()['leases'] == []
    assert source.read_bytes() == original


def test_native_workspace_free_space_is_checked_before_snapshot_or_worker(tmp_path, monkeypatch, native_parent):
    source, original, output, converter = selected(tmp_path)
    actual = service_module.shutil.disk_usage
    monkeypatch.setattr(service_module, 'shutil', SimpleNamespace(disk_usage=lambda path:
        SimpleNamespace(free=0) if Path(path) == native_parent else actual(path)))
    monkeypatch.setattr(service_module, '_invoke',
                        lambda *args, **options: pytest.fail('Insufficient native workspace started.'))
    with pytest.raises(ConversionError, match='native LibreOffice workspace'):
        converter.extract(source, 'reading.txt')
    assert list(output.iterdir()) == [] and list(native_parent.iterdir()) == []
    assert source.read_bytes() == original


def test_excessive_native_temporary_root_is_rejected_without_creating_another_location(tmp_path, monkeypatch):
    long_root = tmp_path / ('bounded-directory-' * 4)
    long_root.mkdir()
    assert len(str(long_root).encode('utf-16-le')) // 2 > 96
    monkeypatch.setattr(service_module, 'gettempdir', lambda: str(long_root))
    with pytest.raises(ConversionError, match='shorter local temporary'):
        service_module._office_workspace_parent()
    assert list(long_root.iterdir()) == []


def test_office_uses_ascii_relative_file_arguments_with_original_inactive_profile(tmp_path, monkeypatch):
    selected_executable = installed(tmp_path)
    selection = office.OfficeSelection.capture(selected_executable).snapshot()
    stage = tmp_path / 'Приватний native stage'
    stage.mkdir()
    source = stage / 'source.doc'
    original = bytes.fromhex('d0cf11e0a1b11ae1') + b'ORIGINAL'
    source.write_bytes(original)
    calls = []
    def run(arguments, working, deadline, **options):
        calls.append(arguments)
        assert working == stage
        assert all(str(value).isascii() for value in arguments[1:])
        assert arguments[1] == '-env:UserInstallation=' + (stage / 'libreoffice-profile').as_uri()
        assert (stage / 'libreoffice-profile/user/registrymodifications.xcu').read_bytes() == office.PROFILE
        if arguments[-1] == '--version':
            return b'LibreOffice 26.2.7.3 synthetic boundary\n', False
        assert arguments[-3:] == ['--outdir', 'office-output', 'source.doc']
        assert options['output'] == stage / 'office-output/source.odt'
        options['output'].write_bytes(b'SYNTHETIC INERT INTERMEDIATE')
        return b'', False
    def extract(data, suffix, **options):
        assert data == b'SYNTHETIC INERT INTERMEDIATE' and suffix == '.odt'
        return dict(records=[dict(raw_text='Reading', logical_locator={'paragraph': 1},
                                  physical_index=None)], warnings=[])
    monkeypatch.setattr(office, '_run', run)
    monkeypatch.setattr(office, 'extract_document', extract)
    engine, _, records, _ = office.office_records(
        source, original, '.doc', ConversionBudget(), time.monotonic() + 10, selection)
    assert engine == 'selected-libreoffice-doc' and len(calls) == 2
    assert records[0]['logical_locator']['private_profile_sha256'] == sha256(office.PROFILE).hexdigest()
    assert source.read_bytes() == original
