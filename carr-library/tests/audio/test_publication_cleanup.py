"""Committed publication receipts survive actual guard-release faults.

Fixtures render real synthetic PCM with the selected FFmpeg. They do not
qualify a voice, acoustic quality, or a native output device.
"""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Event

import pytest

from carr_library.audio import jobs, publishing
from carr_library.audio.capture_types import AudioCleanupPending
from carr_library.audio.cleanup import CleanupOwner
from carr_library.operations import ResourceBroker, ResourceLease
from tests.audio.test_book_plans import library
from tests.audio.test_measured_book_audio import completed
from tests.audio.test_publication import rendered, _export


def publication_case(request, tmp_path, kind):
    if kind == 'pcm-parts':
        books, plan, _, root = request.getfixturevalue('completed')
        output = tmp_path / 'retained-measured.zip'
        preview = books.review_duration(plan.id, 1, output, maximum_ms=70)
        def publish():
            return books.publish_duration(plan.id, 1, output, maximum_ms=70,
                                           review_sha256=preview['approval_sha256'])
        return dict(root=root, output=output, publish=publish,
                    verify=lambda identity: books.verify_duration(plan.id, 1, identity),
                    broker=ResourceBroker.for_app(books.library.root))
    app, _, _, service, result = request.getfixturevalue('rendered')
    service.broker = ResourceBroker.for_app(app.root)
    output = tmp_path / ('retained.epub' if kind == 'epub' else 'retained-daisy.zip')
    return dict(root=service.root, output=output,
                publish=lambda: _export(service, result, output, kind),
                verify=service.verify, broker=service.broker)


def release_fault(patch, boundary, failure):
    released = []
    if boundary == 'project-lock':
        real = publishing.project_lock
        @contextmanager
        def locked(*args, **kwargs):
            with real(*args, **kwargs) as held:
                yield held
            # This is after the actual native file lock has closed.
            released.append(Path(args[0]))
            raise failure
        patch.setattr(publishing, 'project_lock', locked)
    else:
        real = ResourceLease.release
        def release(lease):
            real(lease)
            assert lease._released and not lease._lock.held
            released.append(lease)
            raise failure
        patch.setattr(ResourceLease, 'release', release)
    return released


def saved_receipt(case):
    paths = tuple(case['root'].glob('publication-*.json'))
    assert len(paths) == 1
    return jobs.read_json(paths[0], limit=publishing.MAX_JSON_BYTES)


@pytest.mark.parametrize('kind', ['epub', 'daisy', 'pcm-parts'],
                         ids=['epub3', 'daisy202', 'measured-pcm'])
@pytest.mark.parametrize('boundary', ['project-lock', 'broker'])
def test_real_release_failure_returns_exact_committed_receipt_with_warning(
        request, tmp_path, monkeypatch, kind, boundary):
    case = publication_case(request, tmp_path, kind)
    with monkeypatch.context() as patch:
        released = release_fault(patch, boundary, OSError('injected after actual release'))
        result = case['publish']()
    assert len(released) == 1
    receipt = saved_receipt(case)
    assert receipt['status'] == 'complete'
    assert {key: value for key, value in result.items() if key != 'cleanup_warning'} == receipt
    assert 'already complete' in result['cleanup_warning'] and receipt['id'] in result['cleanup_warning']
    assert 'cleanup_warning' not in receipt
    assert sha256(case['output'].read_bytes()).hexdigest() == receipt['artifact']['sha256']
    with jobs.project_lock(case['root']):
        pass
    assert case['broker'].snapshot()['leases'] == []
    assert case['verify'](receipt['id']) == receipt
    original = case['output'].read_bytes()
    with pytest.raises((FileExistsError, ValueError)):
        case['publish']()
    assert case['output'].read_bytes() == original


@pytest.mark.parametrize('kind', ['epub', 'daisy', 'pcm-parts'],
                         ids=['epub3', 'daisy202', 'measured-pcm'])
def test_precommit_failure_remains_failure_and_prepared_receipt_is_not_accepted(
        request, tmp_path, monkeypatch, kind):
    case = publication_case(request, tmp_path, kind)
    def before_move(*args, **kwargs):
        raise OSError('injected before no-replace publication')
    with monkeypatch.context() as patch:
        patch.setattr(publishing, 'publish_noreplace', before_move)
        with pytest.raises(RuntimeError, match='explicit verification'):
            case['publish']()
    assert not case['output'].exists()
    assert saved_receipt(case)['status'] == 'prepared'
    assert case['broker'].snapshot()['leases'] == []


@pytest.mark.parametrize('wrapped', [False, True], ids=['direct-owner', 'owner-in-cleanup-chain'])
def test_complete_receipt_never_turns_unconfirmed_native_owner_into_normal_success(
        request, tmp_path, monkeypatch, wrapped):
    case = publication_case(request, tmp_path, 'pcm-parts')
    allowed = Event()
    owner = CleanupOwner(allowed.is_set)
    pending = AudioCleanupPending('The actual cleanup owner is not quiescent.', cleanup_owner=owner)
    failure = pending
    if wrapped:
        try:
            raise pending
        except AudioCleanupPending:
            try:
                raise OSError('a later guard also failed')
            except OSError as error:
                failure = error
    try:
        with monkeypatch.context() as patch:
            released = release_fault(patch, 'project-lock', failure)
            with pytest.raises(AudioCleanupPending) as caught:
                case['publish']()
        assert len(released) == 1 and caught.value is pending
        assert caught.value.cleanup_owner is owner and not owner.quiescent_event.is_set()
        assert caught.value.publication_receipt == saved_receipt(case)
        assert caught.value.publication_receipt['status'] == 'complete'
        assert 'already complete' in str(caught.value)
        assert not owner.close()
    finally:
        allowed.set()
        assert owner.close()
    receipt = saved_receipt(case)
    assert case['verify'](receipt['id']) == receipt


def test_unconfirmed_final_receipt_write_still_requires_explicit_verification(
        request, tmp_path, monkeypatch):
    case = publication_case(request, tmp_path, 'pcm-parts')
    real = publishing.atomic_json
    def after_write(path, value):
        real(path, value)
        if value.get('status') == 'complete':
            raise OSError('injected final receipt durability uncertainty')
    with monkeypatch.context() as patch:
        patch.setattr(publishing, 'atomic_json', after_write)
        with pytest.raises(RuntimeError, match='explicit verification'):
            case['publish']()
    receipt = saved_receipt(case)
    assert receipt['status'] == 'complete' and case['output'].is_file()
    assert 'cleanup_warning' not in receipt
    assert case['verify'](receipt['id']) == receipt


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit],
                         ids=['keyboard-interrupt', 'system-exit'])
def test_postcommit_control_signal_propagates_with_accepted_receipt(
        request, tmp_path, monkeypatch, signal_type):
    case = publication_case(request, tmp_path, 'pcm-parts')
    signal = signal_type('explicit postcommit control signal')
    with monkeypatch.context() as patch:
        released = release_fault(patch, 'project-lock', signal)
        with pytest.raises(signal_type) as caught:
            case['publish']()
    assert len(released) == 1 and caught.value is signal
    receipt = saved_receipt(case)
    assert receipt['status'] == 'complete' and case['output'].is_file()
    assert caught.value.publication_receipt == receipt
    assert 'cleanup_warning' not in receipt
    assert case['broker'].snapshot()['leases'] == []
    assert case['verify'](receipt['id']) == receipt
