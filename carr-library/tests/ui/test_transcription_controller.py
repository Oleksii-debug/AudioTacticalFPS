"""Desktop routing and partial completion; no wx/NVDA claim."""
from pathlib import Path

import pytest

from carr_library.audio.jobs import RenderCancelled

from carr_library.ui.transcription_dialog import (TranscriptionController, TranscriptionBatchError,
                                                  completed_entries, completion_message)


def test_completed_result_selection_keeps_partial_batch_and_same_named_inputs_distinct(tmp_path):
    sources = (tmp_path / 'first' / 'book.wav', tmp_path / 'second' / 'book.wav')
    first = {'status': 'complete', 'files': {'txt': 'first.txt'}}
    second = {'status': 'complete', 'files': {'txt': 'second.txt'}}
    assert completed_entries(sources, ()) == ()
    assert completed_entries(sources, (first,)) == ((str(sources[0]), first),)
    assert completed_entries(sources, (first, second)) == (
        (str(sources[0]), first), (str(sources[1]), second))
    with pytest.raises(ValueError, match='more results'):
        completed_entries(sources[:1], (first, second))
    with pytest.raises(ValueError, match='Only completed'):
        completed_entries(sources, ({'status': 'partial'},))


@pytest.fixture
def setup(tmp_path):
    model = tmp_path / 'model'
    model.mkdir()
    manifest = model / 'manifest.json'
    manifest.write_text('{}')
    output = tmp_path / 'results'
    output.mkdir()
    sources = []
    for name in ('перший.wav', 'другий.wav'):
        path = tmp_path / name
        path.write_bytes(b'synthetic media')
        sources.append(path)
    calls = []
    factories = []
    class Session:
        def __init__(self, path):
            factories.append(path)
        def transcribe(self, source, folder, **options):
            calls.append((source, folder, options))
            options['on_status']('Segment complete')
            return {'status': 'complete', 'files': {'txt': 'synthetic'}, 'cached': False}
    controller = TranscriptionController(tmp_path / 'private', session_factory=Session)
    return controller, sources, output, manifest, calls, factories


def test_batch_and_later_runs_reuse_one_model_session_and_shared_cancel(setup):
    controller, sources, output, manifest, calls, factories = setup
    progress = []
    cancelled = lambda: False
    first = controller.transcribe(sources, output, manifest, settings='fixture-settings',
        formats=('txt',), cancelled=cancelled, on_status=progress.append)
    second = controller.transcribe(sources[:1], output, manifest, settings='other-settings', formats=('srt',))
    assert len(first) == 2 and len(second) == 1 and len(factories) == 1
    assert calls[0][2]['cancelled'] is cancelled
    assert calls[2][2]['settings'] == 'other-settings'
    assert calls[0][2]['on_status'] is not None
    assert 'Segment complete' in progress


def test_later_failure_retains_completed_receipts_and_never_counts_failed_file(setup):
    controller, sources, output, manifest, calls, _ = setup
    original = controller.session_factory
    def factory(path):
        session = original(path)
        perform = session.transcribe
        def transcribe(source, folder, **options):
            if source == sources[1]:
                raise OSError('Synthetic disk full')
            return perform(source, folder, **options)
        session.transcribe = transcribe
        return session
    controller.session_factory = factory
    with pytest.raises(TranscriptionBatchError, match='disk full') as raised:
        controller.transcribe(sources, output, manifest, settings=None, formats=('txt',))
    assert len(raised.value.completed) == 1
    assert '1 completed file(s)' in str(raised.value)


def test_missing_or_partial_service_success_is_rejected(setup):
    controller, sources, output, manifest, _, _ = setup
    class Partial:
        def __init__(self, path): pass
        def transcribe(self, *args, **kwargs): return {'status': 'partial'}
    controller.session_factory = Partial
    with pytest.raises(TranscriptionBatchError, match='complete result') as raised:
        controller.transcribe(sources, output, manifest, settings=None, formats=('txt',))
    assert raised.value.completed == ()


def test_private_outputs_model_outputs_and_cancel_fail_before_session_creation(setup, tmp_path):
    controller, sources, output, manifest, calls, factories = setup
    private = tmp_path / 'private'
    private.mkdir()
    for destination in (private, manifest.parent):
        with pytest.raises(ValueError):
            controller.transcribe(sources, destination, manifest, settings=None, formats=('txt',))
    with pytest.raises(InterruptedError):
        controller.transcribe(sources, output, manifest, settings=None, formats=('txt',), cancelled=lambda: True)
    assert not calls and not factories


def test_model_selection_change_creates_one_new_session(setup, tmp_path):
    controller, sources, output, manifest, _, factories = setup
    controller.transcribe(sources[:1], output, manifest, settings=None, formats=('txt',))
    other = tmp_path / 'other-model'
    other.mkdir()
    new_manifest = other / 'model.json'
    new_manifest.write_text('{}')
    controller.transcribe(sources[:1], output, new_manifest, settings=None, formats=('txt',))
    assert factories == [manifest, new_manifest]


def test_success_message_includes_actionable_private_snapshot_location(setup):
    controller, sources, output, manifest, _, _ = setup
    pending = output / '.transcription-locked' / 'source.wav'
    class Session:
        def __init__(self, path): pass
        def transcribe(self, *args, **kwargs):
            return {'status': 'complete', 'cached': False, 'files': {'txt': 'saved.txt'},
                    'warnings': ['Temporary snapshot could not be removed.'],
                    'cleanup_pending': str(pending)}
    controller.session_factory = Session
    results = controller.transcribe(sources[:1], output, manifest, settings=None, formats=('txt',))
    message = completion_message(results, output)
    assert 'Transcription complete: 1 file(s)' in message
    assert 'Temporary snapshot could not be removed.' in message
    assert str(pending) in message


def test_partial_batch_message_retains_prior_cleanup_path_and_current_error_notes(setup):
    controller, sources, output, manifest, _, _ = setup
    previous_snapshot = output / '.transcription-previous'
    current_snapshot = output / '.transcription-current'
    class Session:
        def __init__(self, path): pass
        def transcribe(self, source, *args, **kwargs):
            if source == sources[1]:
                error = OSError('Synthetic failed inference')
                error.add_note('The temporary audio snapshot also needs cleanup: ' + str(current_snapshot))
                raise error
            return {'status': 'complete', 'files': {'txt': 'saved.txt'},
                    'warnings': ['Previous snapshot could not be removed.'],
                    'cleanup_pending': str(previous_snapshot)}
    controller.session_factory = Session
    with pytest.raises(TranscriptionBatchError) as raised:
        controller.transcribe(sources, output, manifest, settings=None, formats=('txt',))
    message = str(raised.value)
    assert len(raised.value.completed) == 1 and '1 completed file(s)' in message
    assert 'Previous snapshot could not be removed.' in message
    assert str(previous_snapshot) in message and str(current_snapshot) in message
    assert 'Synthetic failed inference' in message

def test_between_file_cancellation_preserves_its_type_and_completed_receipt(setup):
    controller, sources, output, manifest, calls, _ = setup
    stopped = [False]
    def status(message):
        if message == 'Segment complete':
            stopped[0] = True
    with pytest.raises(InterruptedError, match='cancelled') as raised:
        controller.transcribe(sources, output, manifest, settings=None, formats=('txt',),
                             cancelled=lambda: stopped[0], on_status=status)
    assert len(calls) == 1
    assert len(raised.value.completed) == 1
    assert raised.value.completed[0]['status'] == 'complete'
    assert '1 completed file(s)' in str(raised.value)
    assert isinstance(raised.value.__cause__, InterruptedError)


@pytest.mark.parametrize('cancel_type', [InterruptedError, RenderCancelled])
def test_engine_and_output_hash_cancellation_remain_typed_with_partial_receipts(setup, cancel_type):
    controller, sources, output, manifest, calls, _ = setup
    original = controller.session_factory
    def factory(path):
        session = original(path)
        perform = session.transcribe
        def transcribe(source, folder, **options):
            if source == sources[1]:
                error = cancel_type('Transcription cancelled at the inference or export boundary.')
                error.add_note('Current snapshot needs cleanup: ' + str(output / '.current'))
                raise error
            receipt = perform(source, folder, **options)
            receipt.update(warnings=['Previous snapshot needs cleanup.'], cleanup_pending=str(output / '.previous'))
            return receipt
        session.transcribe = transcribe
        return session
    controller.session_factory = factory
    with pytest.raises(InterruptedError, match='cancelled') as raised:
        controller.transcribe(sources, output, manifest, settings=None, formats=('txt',))
    assert len(calls) == 1
    assert len(raised.value.completed) == 1
    assert raised.value.completed[0]['status'] == 'complete'
    assert isinstance(raised.value.__cause__, cancel_type)
    assert not isinstance(raised.value, TranscriptionBatchError)
    assert 'Previous snapshot needs cleanup.' in str(raised.value)
    assert str(output / '.previous') in str(raised.value)
    assert str(output / '.current') in str(raised.value)
