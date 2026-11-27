"""Actual export commit boundaries for both saved ASR schemas.

All recognition inputs/backends are public synthetic protocol fixtures. These
tests exercise real retained bytes, publication and cancellation, never inference.
"""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from threading import Event

import pytest

from carr_library import transcription as legacy_module
from carr_library.audio.models import AudioBudget, AudioCancelled
from carr_library.integrations import transcription_protocol as protocol
from carr_library.integrations.transcription_exports import TranscriptionExportedError


def peer(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_v5 = peer('test_v5_worker_profile.py', 'export_v5_protocol_fixture')
_builder = peer('fixture_runtime.py', 'export_canonical_protocol_fixture')
v5 = _v5.v5


@pytest.fixture
def legacy(v5):
    service, source = v5[2:4]
    identity = service.submit([source])
    result = service.run(identity, timeout=10)
    assert result['status'] == 'completed', result
    assert {item['format'] for item in result['artifacts']} == {
        'txt', 'srt', 'vtt', 'json', 'words.srt', 'words.vtt'}
    edit = service.save_edit(identity, result['sources'][0]['filename'], 'Separate reviewed edit.')
    return service, identity, result, edit


@pytest.fixture
def canonical(tmp_path):
    runtime = _builder.make_runtime(tmp_path / 'runtime')
    service = _builder.make_service(tmp_path / 'library/state/transcription',
        budget=AudioBudget(timeout=10, reserve_disk_bytes=0), stop_grace=.1)
    profile = _builder.register(service, runtime)
    source = tmp_path / 'selected.wav'
    source.write_bytes(b'Synthetic retained media, no inference.')
    requested_formats = ('txt', 'srt', 'vtt', 'json')
    state = service.run(service.create_job(profile['id'], [source],
        settings={'output_' + name: True for name in requested_formats})['id'])
    assert state['status'] == 'complete', state
    assert all(state['settings']['output_' + name] for name in requested_formats)
    destination = tmp_path / 'canonical-export'
    destination.mkdir()
    return service, state, destination


def check_legacy_payload(service, identity, destination, original, edit):
    saved = json.loads((destination / 'transcription-manifest.json').read_text(encoding='utf-8'))
    assert saved['job'] == identity and len(saved['artifacts']) == 6
    assert saved['edits'][0]['id'] == edit
    assert (destination / 'edited' / (edit + '.txt')).read_text(encoding='utf-8') == 'Separate reviewed edit.'
    for item in saved['artifacts']:
        payload = (destination / item['filename']).read_bytes()
        assert len(payload) == item['size_bytes'] and sha256(payload).hexdigest() == item['sha256']
    assert service.verify(identity)['artifacts'] == original['artifacts']
    assert service.get(identity)['status'] == 'completed'


def test_legacy_competing_empty_directory_is_never_replaced(legacy, tmp_path, monkeypatch):
    service, identity, original, edit = legacy
    destination = tmp_path / 'competing-export'
    publish = legacy_module.publish_directory_no_replace
    marker = {}

    def competing_directory(source, target):
        assert Path(target) == destination
        destination.mkdir()
        marker['identity'] = destination.stat().st_ino
        return publish(source, target)

    monkeypatch.setattr(legacy_module, 'publish_directory_no_replace', competing_directory)
    with pytest.raises(FileExistsError):
        service.export(identity, destination)
    assert destination.stat().st_ino == marker['identity']
    assert list(destination.iterdir()) == []
    assert service.get(identity)['status'] == 'completed'
    assert not list(tmp_path.glob('.pending-transcription-export-*'))


def test_legacy_precommit_stop_publishes_nothing(legacy, tmp_path, monkeypatch):
    service, identity, original, edit = legacy
    destination = tmp_path / 'cancelled-export'
    token = Event()
    sync = legacy_module.sync_directory

    def after_payload(directory):
        sync(directory)
        if Path(directory).name == 'payload':
            token.set()

    monkeypatch.setattr(legacy_module, 'sync_directory', after_payload)
    with pytest.raises(AudioCancelled):
        service.export(identity, destination, cancelled=token.is_set)
    assert not destination.exists()
    assert not list(tmp_path.glob('.pending-transcription-export-*'))
    assert service.get(identity)['status'] == 'completed'


@pytest.mark.parametrize('failure', ['parent_fsync', 'cleanup', 'keyboard_interrupt'])
def test_legacy_postcommit_failure_reports_published_six_formats_and_edit(
        legacy, tmp_path, monkeypatch, failure):
    service, identity, original, edit = legacy
    destination = tmp_path / ('committed-' + failure)
    sync = legacy_module.sync_directory
    temporary = legacy_module.TemporaryDirectory
    if failure == 'cleanup':
        class CleanupFailure:
            def __init__(self, *args, **kwargs):
                self.inner = temporary(*args, **kwargs)
            def __enter__(self):
                return self.inner.__enter__()
            def __exit__(self, kind, value, traceback):
                answer = self.inner.__exit__(kind, value, traceback)
                if kind is None:
                    raise OSError('Synthetic cleanup failure after publication.')
                return answer
        monkeypatch.setattr(legacy_module, 'TemporaryDirectory', CleanupFailure)
    else:
        def final_sync(directory):
            if Path(directory) == destination.parent:
                assert destination.is_dir()
                if failure == 'keyboard_interrupt':
                    raise KeyboardInterrupt('Synthetic late stop after publication.')
                raise OSError('Synthetic final directory durability failure.')
            sync(directory)
        monkeypatch.setattr(legacy_module, 'sync_directory', final_sync)
    with pytest.raises(TranscriptionExportedError) as caught:
        service.export(identity, destination)
    error = caught.value
    assert error.receipt['status'] == 'published'
    assert error.receipt['complete_payload'] is True
    assert error.receipt['path'] == str(destination)
    assert error.durability_confirmed is (failure == 'cleanup')
    check_legacy_payload(service, identity, destination, original, edit)


@pytest.mark.parametrize('failure', ['second_file', 'cancel', 'manifest'])
def test_flat_export_retains_actual_prior_files_and_reports_partial(
        canonical, monkeypatch, failure):
    service, state, destination = canonical
    write = protocol.write_binary_export
    published, token = [], Event()

    def controlled(path, payload, **kwargs):
        if ((failure == 'second_file' and len(published) == 1)
                or (failure == 'manifest' and Path(path).name.endswith('-manifest.json'))):
            raise OSError('Synthetic next-file publication failure.')
        receipt = write(path, payload, **kwargs)
        published.append((receipt.path, bytes(payload)))
        if failure == 'cancel':
            token.set()
        return receipt

    monkeypatch.setattr(protocol, 'write_binary_export', controlled)
    with pytest.raises(TranscriptionExportedError) as caught:
        service.export(state['id'], destination, formats=['txt', 'srt', 'vtt', 'json'],
                       cancel=token.is_set)
    receipt = caught.value.receipt
    expected = 4 if failure == 'manifest' else 1
    assert receipt['status'] == 'partial' and receipt['complete_payload'] is False
    assert len(published) == len(receipt['files']) == len(receipt['published_files']) == expected
    assert receipt['uncertain_files'] == [] and receipt['manifest_path'] is None
    assert len(list(destination.iterdir())) == expected
    for path, payload in published:
        assert path.read_bytes() == payload
    assert service.get_job(state['id'])['status'] == 'complete'
    assert service.get_job(state['id'])['results'] == state['results']


@pytest.mark.parametrize('carried_receipt', [False, True])
def test_flat_writer_error_after_real_commit_is_never_reported_as_no_output(
        canonical, monkeypatch, carried_receipt):
    service, state, destination = canonical
    write = protocol.write_binary_export
    published = []

    def fail_after_commit(path, payload, **kwargs):
        receipt = write(path, payload, **kwargs)
        published.append((receipt.path, bytes(payload)))
        error = OSError('Synthetic failure after actual file commit.')
        if carried_receipt:
            error.receipt = receipt
        raise error

    monkeypatch.setattr(protocol, 'write_binary_export', fail_after_commit)
    with pytest.raises(TranscriptionExportedError) as caught:
        service.export(state['id'], destination, formats=['txt'])
    receipt = caught.value.receipt
    assert receipt['status'] == 'uncertain' and receipt['complete_payload'] is False
    assert len(published) == 1
    assert published[0][0].read_bytes() == published[0][1]
    assert len(receipt['published_files']) == int(carried_receipt)
    assert len(receipt['uncertain_files']) == int(not carried_receipt)
    assert service.get_job(state['id'])['status'] == 'complete'


def test_flat_postcommit_byte_verification_failure_retains_output_and_receipt(canonical, monkeypatch):
    service, state, destination = canonical
    def verification(path):
        assert Path(path).is_file()
        raise OSError('Synthetic postcommit verification error.')
    monkeypatch.setattr(protocol, '_published_identity', verification)
    with pytest.raises(TranscriptionExportedError) as caught:
        service.export(state['id'], destination, formats=['txt'])
    receipt = caught.value.receipt
    assert receipt['status'] == 'uncertain'
    assert len(receipt['published_files']) == 1
    assert receipt['published_files'][0]['verification'] == 'pending'
    assert len(list(destination.iterdir())) == 1
    assert service.get_job(state['id'])['status'] == 'complete'


def test_flat_final_fsync_error_reports_all_published_bytes(canonical, monkeypatch):
    service, state, destination = canonical
    def final_sync(path):
        assert Path(path) == destination
        raise OSError('Synthetic final directory durability failure.')
    monkeypatch.setattr(protocol, 'sync_directory', final_sync)
    with pytest.raises(TranscriptionExportedError) as caught:
        service.export(state['id'], destination, formats=['txt'])
    receipt = caught.value.receipt
    assert receipt['status'] == 'published' and receipt['complete_payload'] is True
    assert caught.value.durability_confirmed is False
    assert len(receipt['files']) == 1 and len(receipt['published_files']) == 2
    assert Path(receipt['manifest_path']).is_file()
    assert service.get_job(state['id'])['status'] == 'complete'


def test_flat_first_file_collision_remains_a_prepublication_failure(canonical):
    service, state, destination = canonical
    source_id = state['results'][0]['source_id']
    target = destination / f"transcript-{state['id'][:8]}-{source_id[:12]}.txt"
    target.write_bytes(b'Competing owner bytes.')
    with pytest.raises(FileExistsError):
        service.export(state['id'], destination, formats=['txt'])
    assert target.read_bytes() == b'Competing owner bytes.'
    assert list(destination.iterdir()) == [target]


def test_cli_surfaces_real_postcommit_receipt_without_resume_advice(
        legacy, tmp_path, monkeypatch, capsys):
    from carr_library.transcription_cli import main
    service, identity, original, edit = legacy
    destination = tmp_path / 'cli-committed-export'
    sync = legacy_module.sync_directory
    def final_sync(path):
        if Path(path) == destination.parent:
            assert destination.is_dir()
            raise OSError('Synthetic CLI postcommit durability failure.')
        sync(path)
    monkeypatch.setattr(legacy_module, 'sync_directory', final_sync)
    code = main(['--data-dir', str(service.app.root), '--workspace', service.workspace,
                 'export', identity, str(destination)])
    output = capsys.readouterr()
    receipt = json.loads(output.out)
    assert code == 2 and receipt['status'] == 'published'
    assert receipt['complete_payload'] is True and receipt['path'] == str(destination)
    assert 'published' in output.err and 'Resume the saved job' not in output.err
    check_legacy_payload(service, identity, destination, original, edit)
