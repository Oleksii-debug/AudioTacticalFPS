"""Queued audio-batch review keeps controls on UI and work on one owned worker."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.audio import AudioCancelled, Effect
from carr_library.ui import audio_conversion_dialog as module


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.cancel_event = threading.Event()
        self._closing = self.working = False
        self.jobs, self.messages = [], []
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.thread = threading.get_ident()

    def run_job(self, title, operation, done=None, failed=None, **options):
        assert threading.get_ident() == self.thread
        assert not self.working
        self.cancel_event.clear()
        self.working = True
        self.jobs.append((title, operation, done, failed, options))

    def take(self):
        title, operation, done, failed, options = self.jobs.pop(0)
        assert options['with_progress'] and options['progress_unit'] == 'files'
        calls = []
        def progress(*values):
            assert threading.get_ident() != self.thread
            calls.append(values)
        future = self.worker.submit(operation, progress)
        try:
            result = future.result(timeout=5)
        except Exception as error:
            self.working = False
            return lambda error=error: failed(error), calls
        self.working = False
        return lambda: done(result), calls

    def report(self, text):
        assert threading.get_ident() == self.thread
        self.messages.append(text)

    def close(self):
        self._closing = True
        self.cancel_event.set()
        self.worker.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def peer(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    first, second = inputs / 'one.wav', inputs / 'two.wav'
    first.write_bytes(b'input one'); second.write_bytes(b'input two')
    destination = tmp_path / 'reviewed output'
    frame = Frame(root)
    events = []
    plan = {}
    approvals = []
    outcome = {'status': 'completed', 'done': 2, 'failed': 0, 'warning': '',
        'plan': {'directory': str(destination)}, 'items': [
            {'output': '001-one.wav', 'error': ''}, {'output': '002-two.wav', 'error': ''}]}

    def runtime(output, **options):
        assert threading.get_ident() != frame.thread
        events.append(('runtime', Path(output), options))
        return object()

    class Service:
        def __init__(self, selected, *, library_root, scope_guard):
            assert threading.get_ident() != frame.thread
            self.scope = scope_guard
            self.scope()
            events.append(('service', library_root))

        def prepare(self, sources, target, *, format, resume, cancelled, progress, **options):
            self.scope()
            if cancelled():
                raise AudioCancelled('cancelled before source inspection')
            events.append(('prepare', tuple(sources), target, format, resume, options))
            progress(0, len(sources), 'Actual selected-source review')
            plan.update({'directory': str(target), 'format': format,
                'settings': module._settings(**options),
                'sources': [{'index': i, 'source': str(path), 'output': f'{i+1:03d}-{Path(path).name}',
                    'inspection_error': '', 'information': {'duration': 1.0, 'sample_rate': 24000, 'channels': 1}}
                    for i, path in enumerate(sources)]})
            return deepcopy(plan)

        def run(self, reviewed, *, resume, cancelled, progress):
            assert threading.get_ident() != frame.thread
            self.scope()
            if cancelled():
                raise AudioCancelled('cancelled before conversion')
            assert reviewed == plan
            events.append(('run', deepcopy(reviewed), resume))
            progress(2, 2, 'Saved reviewed outputs')
            return deepcopy(outcome)

    def confirm(owner, reviewed, resume):
        assert threading.get_ident() == frame.thread
        assert owner is frame and reviewed == plan
        approvals.append((deepcopy(reviewed), resume))
        return True

    monkeypatch.setattr(module, 'audio_runtime', runtime)
    monkeypatch.setattr(module, 'AudioConversionBatch', Service)
    monkeypatch.setattr(module, '_choose_directory', lambda owner: destination)
    monkeypatch.setattr(module, '_confirm_plan', confirm)
    try:
        yield SimpleNamespace(frame=frame, events=events, plan=plan, approvals=approvals,
            sources=[first, second], destination=destination, outcome=outcome)
    finally:
        frame.close()


def test_review_and_conversion_use_worker_but_explicit_approval_uses_caller(peer):
    inputs = list(peer.sources)
    chain = [Effect('gain', gain_db=-3)]
    module.start_audio_batch(peer.frame, inputs, effects=chain, channels=1, format='flac')
    assert peer.events == [] and peer.approvals == [] and not peer.destination.exists()
    inputs.reverse()
    chain.clear()
    deliver, progress = peer.frame.take()
    assert progress and not peer.approvals
    prepare, = [event for event in peer.events if event[0] == 'prepare']
    assert prepare[1] == tuple(map(str, peer.sources))
    assert prepare[3:5] == ('flac', False)
    assert prepare[5]['effects'][0].parameters['gain_db'] == -3
    assert prepare[5]['channels'] == 1
    deliver()
    assert len(peer.approvals) == 1
    assert not any(event[0] == 'run' for event in peer.events)
    complete, progress = peer.frame.take()
    assert progress
    complete()
    assert [event[0] for event in peer.events].count('run') == 1
    assert '2 of 2 files saved' in peer.frame.messages[-1]
    assert str(peer.destination) in peer.frame.messages[-1]


def test_cancelled_explicit_review_never_queues_conversion(peer, monkeypatch):
    monkeypatch.setattr(module, '_confirm_plan', lambda *_: False)
    module.start_audio_batch(peer.frame, peer.sources)
    deliver, _ = peer.frame.take()
    deliver()
    assert not peer.frame.jobs and not any(event[0] == 'run' for event in peer.events)
    assert not peer.destination.exists()
    assert 'no conversion started' in peer.frame.messages[-1]


@pytest.mark.parametrize('boundary', ['before-worker', 'before-delivery', 'inside-confirmation'])
def test_captured_library_retargeting_never_starts_other_library_conversion(peer, monkeypatch, boundary):
    module.start_audio_batch(peer.frame, peer.sources)
    replacement = SimpleNamespace(root=peer.frame.library.root)
    if boundary == 'before-worker':
        peer.frame.library = replacement
    deliver, _ = peer.frame.take()
    if boundary == 'before-delivery':
        peer.frame.library = replacement
    if boundary == 'inside-confirmation':
        def switch(*_):
            peer.frame.library = replacement
            return True
        monkeypatch.setattr(module, '_confirm_plan', switch)
    deliver()
    assert not peer.frame.jobs and not any(event[0] == 'run' for event in peer.events)
    assert not peer.destination.exists()
    assert 'library changed' in peer.frame.messages[-1].lower()


@pytest.mark.parametrize('boundary', ['before-worker', 'before-delivery', 'inside-confirmation'])
def test_cancellation_never_starts_encoder_or_clears_a_stop_after_confirmation(peer, monkeypatch, boundary):
    module.start_audio_batch(peer.frame, peer.sources)
    if boundary == 'before-worker':
        peer.frame.cancel_event.set()
    deliver, _ = peer.frame.take()
    if boundary == 'before-delivery':
        peer.frame.cancel_event.set()
    if boundary == 'inside-confirmation':
        def stop(*_):
            peer.frame.cancel_event.set()
            return True
        monkeypatch.setattr(module, '_confirm_plan', stop)
    deliver()
    assert not peer.approvals and not peer.frame.jobs
    assert not any(event[0] == 'run' for event in peer.events)
    assert not peer.destination.exists()


def test_close_drops_late_review_without_dialog_or_unowned_operation(peer):
    module.start_audio_batch(peer.frame, peer.sources)
    deliver, _ = peer.frame.take()
    peer.frame._closing = True
    peer.frame.cancel_event.set()
    deliver()
    assert peer.approvals == [] and peer.frame.jobs == [] and peer.frame.messages == []
    assert not any(event[0] == 'run' for event in peer.events)


def test_already_committed_receipt_reports_original_destination_after_scope_changes(peer):
    module.start_audio_batch(peer.frame, peer.sources)
    deliver, _ = peer.frame.take()
    deliver()
    complete, _ = peer.frame.take()
    peer.frame.library = SimpleNamespace(root=peer.frame.library.root)
    complete()
    assert 'Audio batch completed: 2 of 2 files saved' in peer.frame.messages[-1]
    assert str(peer.destination) in peer.frame.messages[-1]


def test_partial_result_keeps_per_file_error_and_publication_warning_visible(peer):
    peer.outcome.update(status='attention', done=1, failed=1, warning='The second receipt needs explicit recovery.')
    peer.outcome['items'][1]['error'] = 'Original bytes changed.'
    module.start_audio_batch(peer.frame, peer.sources)
    deliver, _ = peer.frame.take()
    deliver()
    complete, _ = peer.frame.take()
    complete()
    message = peer.frame.messages[-1]
    assert '1 of 2 files saved; 1 failed' in message
    assert '002-two.wav: Original bytes changed.' in message
    assert peer.outcome['warning'] in message


def test_review_text_displays_exact_originals_output_settings_and_failed_inputs(peer):
    module.start_audio_batch(peer.frame, peer.sources)
    peer.frame.take()
    peer.plan['sources'][1].update(inspection_error='No audio stream.', information=None)
    text = module._review_text(peer.plan, True)
    assert 'Resume only matching saved outputs.' in text
    assert str(peer.destination) in text
    assert '24000 Hz, 2 channel(s)' in text
    assert '001-one.wav' in text and '002-two.wav' in text
    assert 'Cannot convert this file: No audio stream.' in text
    assert 'Existing output files are never overwritten.' in text
