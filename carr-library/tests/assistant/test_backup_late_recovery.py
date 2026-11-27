"""Real backup publication survives a late cooperative interruption.

The saved producer hash closes the post-return/receipt window only. Publication
and checkpoint persistence are not atomic: a lost producer return remains a
review-required existing directory, never an automatically adopted backup.
"""
from hashlib import file_digest, sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import backup_receipt
from carr_library.assistant import service as service_module
from carr_library.assistant.backup_state import validate_state_bytes
from carr_library.assistant.models import canonical
from carr_library.library_backup import restore_backup


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Late backup recovery')
    app.add_text(workspace, 'Original', 'Unchanged retained source for backup recovery.')
    output = tmp_path / 'output'
    output.mkdir()
    return app, workspace, output


def submit(library, *, steps=None, production=None):
    app, workspace, output = library
    service = AssistantService(app, production=production)
    scope = Scope(workspace, (), output, allow_full_backup=True,
                  max_bytes=8 * 1024 * 1024, max_files=100, max_seconds=120)
    plan = Plan(scope, steps or (Step('library.backup', {'name': 'saved'}),))
    return service, service.submit(plan), output / 'saved', scope


def digest(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def tree(path):
    directories, files = [], {}
    for child in sorted(path.rglob('*')):
        name = child.relative_to(path).as_posix()
        if child.is_dir():
            directories.append(name)
        else:
            files[name] = digest(child)
    return tuple(directories), files


def forbid_backup(*_args, **_kwargs):
    pytest.fail('The producer must not run again for an existing backup.')


def pause_after_publication(service, identity, monkeypatch):
    producer, calls = service.app.backup, []

    def publish(destination, **options):
        calls.append(destination)
        checksum = producer(destination, **options)
        service.pause(identity)
        return checksum

    monkeypatch.setattr(service.app, 'backup', publish)
    report = service.run(identity)
    assert report.status == 'paused'
    assert calls
    return report, calls


@pytest.mark.parametrize('phase', ['after_publication', 'after_receipt'])
@pytest.mark.parametrize('command', ['pause', 'cancel', 'deadline'])
def test_real_published_backup_resumes_after_late_stop_without_another_producer(
        library, monkeypatch, phase, command):
    service, identity, target, _scope = submit(library)
    clock = [0.0]
    monkeypatch.setattr(service_module, 'monotonic', lambda: clock[0])
    producer, capture = service.app.backup, backup_receipt.capture_backup_receipt
    calls, captured = [], []

    def stop():
        if command == 'deadline':
            clock[0] = 121.0
        else:
            getattr(service, command)(identity)

    def publish(destination, **options):
        checksum = producer(destination, **options)
        calls.append(checksum)
        if phase == 'after_publication':
            stop()
        return checksum

    def scan(*args, **kwargs):
        receipt = capture(*args, **kwargs)
        captured.append(receipt)
        if phase == 'after_receipt':
            stop()
        return receipt

    monkeypatch.setattr(service.app, 'backup', publish)
    monkeypatch.setattr(backup_receipt, 'capture_backup_receipt', scan)
    report = service.run(identity)
    assert report.status == ('cancelled' if command == 'cancel' else 'paused'), report.message
    assert report.completed == 0 and len(calls) == 1
    assert len(captured) == (phase == 'after_receipt')
    record = service.store.load(identity)
    assert record['steps'][0]['intent'] == {'backup_manifest_sha256': calls[0]}
    assert record['steps'][0]['result'] is None
    validate_state_bytes('state/assistant/' + identity + '.json', canonical(record).encode('utf-8'))
    assert digest(target / 'manifest.json') == calls[0]
    before = tree(target)

    monkeypatch.setattr(backup_receipt, 'capture_backup_receipt', capture)
    reopened = LibraryApp(service.app.root)
    monkeypatch.setattr(reopened, 'backup', forbid_backup)
    resumed = AssistantService(reopened).resume(identity)
    assert resumed.status == 'complete' and resumed.completed == 1, resumed.message
    assert resumed.results[0]['manifest_sha256'] == calls[0]
    assert resumed.results[0]['directory'] == str(target)
    assert tree(target) == before and len(calls) == 1


def test_recovered_backup_is_charged_once_between_completed_and_new_outputs(library, monkeypatch):
    calls = []

    def output(settings, directory, cancelled, progress):
        assert not cancelled()
        calls.append((settings['max_files'], settings['max_bytes']))
        path = directory / ('fixture-' + settings['operation_id'] + '.bin')
        with path.open('xb') as stream:
            stream.write(b'123456')
        return {'artifacts': [str(path)]}

    steps = (Step('audio.produce', {'text': 'Synthetic budget output before backup'}),
             Step('library.backup', {'name': 'saved'}),
             Step('audio.produce', {'text': 'Synthetic budget output after backup'}))
    service, identity, target, scope = submit(library, steps=steps, production=output)
    paused, produced = pause_after_publication(service, identity, monkeypatch)
    assert paused.completed == 1 and len(produced) == 1
    before = tree(target)
    monkeypatch.setattr(service.app, 'backup', forbid_backup)
    restarted = AssistantService(service.app, production=output)
    completed = restarted.resume(identity)
    assert completed.status == 'complete' and completed.completed == 3, completed.message
    receipt = completed.results[1]['backup_receipt']
    assert calls == [(scope.max_files, scope.max_bytes),
                     (scope.max_files - 1 - receipt['files'], scope.max_bytes - 6 - receipt['total_bytes'])]
    assert tree(target) == before
    assert restarted.resume(identity).status == 'complete'
    assert len(calls) == 2 and len(produced) == 1


@pytest.mark.parametrize('damage', [
    'manifest', 'database', 'original', 'sidecar', 'missing_manifest',
    'missing_original', 'missing_target', 'extra_file', 'extra_empty_directory',
])
def test_late_backup_recovery_rejects_changed_or_missing_producer_payload(
        library, monkeypatch, damage):
    service, identity, target, _scope = submit(library)
    _report, calls = pause_after_publication(service, identity, monkeypatch)
    manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
    original = target / 'originals' / manifest['assets'][0]['filename']
    snapshot = target
    if damage == 'missing_target':
        snapshot = target.with_name('moved-away')
        target.rename(snapshot)
    elif damage in ('missing_manifest', 'missing_original'):
        selected = target / 'manifest.json' if damage == 'missing_manifest' else original
        selected.rename(target.parent / ('retained-' + selected.name))
    elif damage == 'extra_file':
        (target / 'originals' / 'unexpected.txt').write_bytes(b'Unapproved extra bytes.')
    elif damage == 'extra_empty_directory':
        (target / 'originals' / 'unexpected-empty').mkdir()
    else:
        selected = {
            'manifest': target / 'manifest.json',
            'database': target / manifest['database']['filename'],
            'original': original,
            'sidecar': target / manifest['sidecars'][0]['filename'],
        }[damage]
        with selected.open('r+b') as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 1]))  # same length, different bytes
    before = tree(snapshot)
    monkeypatch.setattr(service.app, 'backup', forbid_backup)
    result = AssistantService(service.app).resume(identity)
    assert result.status in ('failed', 'attention') and result.completed == 0, result.message
    assert tree(snapshot) == before and len(calls) == 1
    if damage == 'missing_target':
        assert result.status == 'attention' and 'review' in result.message
        assert not target.exists()
    # Recovery verification never damages the live retained source.
    hit, = service.app.search(library[1], 'Unchanged retained source')
    assert hit.quote == 'Unchanged retained source'


@pytest.mark.parametrize('kind', ['empty_directory', 'ordinary_file', 'valid_unowned_backup'])
def test_existing_output_without_saved_producer_hash_is_never_adopted(library, monkeypatch, kind):
    service, identity, target, _scope = submit(library)
    if kind == 'valid_unowned_backup':
        service.app.backup(target)
    elif kind == 'ordinary_file':
        target.write_bytes(b'Existing user data.')
    else:
        target.mkdir()
    before = digest(target) if target.is_file() else tree(target)
    monkeypatch.setattr(service.app, 'backup', forbid_backup)
    result = service.run(identity)
    assert result.status == 'attention' and result.completed == 0
    assert service.store.load(identity)['steps'][0]['intent'] == {}
    assert (digest(target) if target.is_file() else tree(target)) == before


def test_publication_without_returned_hash_remains_review_required(library, monkeypatch):
    service, identity, target, _scope = submit(library)
    producer, calls = service.app.backup, []

    def lose_return(destination, **options):
        checksum = producer(destination, **options)
        calls.append(checksum)
        raise InterruptedError('Injected interruption before the producer returned its hash.')

    monkeypatch.setattr(service.app, 'backup', lose_return)
    assert service.run(identity).status == 'paused'
    assert digest(target / 'manifest.json') == calls[0]
    assert service.store.load(identity)['steps'][0]['intent'] == {}
    before = tree(target)
    monkeypatch.setattr(service.app, 'backup', forbid_backup)
    result = service.resume(identity)
    assert result.status == 'attention' and result.completed == 0
    assert tree(target) == before and len(calls) == 1


@pytest.mark.parametrize('checksum', [
    None, True, 7, [], {}, '', 'f' * 63, 'g' * 64, 'A' * 64, '0' * 64 + '\n',
])
def test_saved_backup_hash_has_the_same_strict_type_at_runtime_and_restore(library, monkeypatch, checksum):
    service, identity, target, _scope = submit(library)
    record = service.store.load(identity)
    record['steps'][0]['intent']['backup_manifest_sha256'] = checksum
    service.store.save(record)
    with pytest.raises(ValueError, match='backup manifest SHA256 intent'):
        validate_state_bytes('state/assistant/' + identity + '.json', canonical(record).encode('utf-8'))
    monkeypatch.setattr(service.app, 'backup', forbid_backup)
    with pytest.raises(ValueError, match='backup manifest SHA256 intent'):
        service.resume(identity)
    assert not target.exists()


def test_backup_hash_intent_is_rejected_on_another_tool(library):
    service, identity, _target, _scope = submit(library)
    record = service.store.load(identity)
    record['plan']['steps'][0] = {'tool': 'library.list', 'arguments': {}}
    record['plan_sha256'] = sha256(canonical(record['plan']).encode('utf-8')).hexdigest()
    record['steps'][0]['intent']['backup_manifest_sha256'] = 'a' * 64
    service.store.save(record)
    with pytest.raises(ValueError, match='backup manifest SHA256 intent'):
        validate_state_bytes('state/assistant/' + identity + '.json', canonical(record).encode('utf-8'))
    with pytest.raises(ValueError, match='backup manifest SHA256 intent'):
        service.get(identity)


def test_recovered_job_hash_survives_real_backup_restore_as_reviewed_history(library, monkeypatch, tmp_path):
    service, identity, _target, _scope = submit(library)
    producer = service.app.backup
    pause_after_publication(service, identity, monkeypatch)
    monkeypatch.setattr(service.app, 'backup', forbid_backup)
    assert service.resume(identity).status == 'complete'
    history = tmp_path / 'history'
    checksum = producer(history)
    restored = tmp_path / 'restored'
    restore_backup(history, restored, expected_manifest_sha256=checksum)
    recovered = AssistantService(LibraryApp(restored))
    report = recovered.get(identity)
    assert report.review_required is True and report.completed == 1
    with pytest.raises(PermissionError, match='new review'):
        recovered.resume(identity)


def test_legacy_completed_backup_without_new_intent_still_verifies(library, monkeypatch):
    service, identity, target, _scope = submit(library)
    assert service.run(identity).status == 'complete'
    record = service.store.load(identity)
    record['steps'][0]['intent'].pop('backup_manifest_sha256')
    service.store.save(record)
    validate_state_bytes('state/assistant/' + identity + '.json', canonical(record).encode('utf-8'))
    before = tree(target)
    monkeypatch.setattr(service.app, 'backup', forbid_backup)
    assert service.resume(identity).status == 'complete'
    assert tree(target) == before
