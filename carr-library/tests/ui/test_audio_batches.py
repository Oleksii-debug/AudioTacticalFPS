"""Portable queue control policy; actual wx coverage is a separate native probe."""
from dataclasses import asdict
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.audio.batch_models import AudioPreset, Selection
from carr_library.ui.audio_batches import AudioBatchDialog, BatchDraft, preset_values


def test_per_item_edits_and_reordering_preserve_explicit_source_and_stream():
    draft = BatchDraft()
    first = Selection('video', '/local/first.mkv', 'First', AudioPreset(), audio_stream=1)
    second = Selection('audio', '/local/second.wav', 'Second', AudioPreset(output_format='flac'))
    draft.add(first)
    draft.add(second)
    draft.replace(0, title='Delivery name', preset=AudioPreset(output_format='wav', channels=1), audio_stream=2)
    draft.move(0, 1)
    assert draft.items[0] == second
    assert draft.items[1].source == first.source and draft.items[1].audio_stream == 2
    assert first.title == 'First' and first.audio_stream == 1
    assert 'Delivery name' in draft.preview() and 'selected audio stream: 2' in draft.preview()


@pytest.mark.parametrize('change', [
    {'sample_rate': 'auto'}, {'channels': '0'}, {'channels': 'True'},
    {'bitrate_kbps': 'nan'}, {'max_seconds': 'nan'}, {'timeout_seconds': '-1'},
    {'output_format': 'm3u8'}, {'split': 'guess chapters'},
])
def test_native_preset_values_refuse_automatic_or_invalid_settings(change):
    values = {key: str(value) for key, value in asdict(AudioPreset()).items()}
    with pytest.raises(ValueError):
        preset_values({**values, **change})


class Control:
    def __init__(self):
        self.enabled = True
    def Enable(self, value=True):
        self.enabled = value
    def Disable(self):
        self.enabled = False


class Host:
    def __init__(self):
        self.busy = self.closed = False
        self.cancelled = Event()
        self.work = self.done = None
    def _run(self, label, action, done):
        self.busy = True
        self.work, self.done = action, done
    def finish(self):
        result = self.work()
        self.busy = False
        self.done(result)
        return result


@pytest.fixture
def view():
    value = AudioBatchDialog.__new__(AudioBatchDialog)
    value.host = Host()
    value.busy = value.closed = value.close_pending = False
    value.token = None
    value.controls = [Control()]
    value.batch_id = 'a' * 32
    value.messages, value.pending, value.actions, value.refreshes, value.ends = [], [], [], [], []
    value.wx = SimpleNamespace(CallAfter=value.pending.append, ID_CANCEL=1)
    value.message = lambda message, **kwargs: value.messages.append(str(message))
    value.refresh = lambda: value.refreshes.append(True)
    value.service = SimpleNamespace(control=lambda identity, action: value.actions.append((identity, action)))
    value.dialog = SimpleNamespace(IsModal=lambda: True, EndModal=lambda result: value.ends.append(result))
    return value


@pytest.mark.parametrize('status', ['complete', 'paused', 'cancelled', 'failed'])
def test_existing_studio_worker_receives_the_actual_durable_result_state(view, status):
    accepted = []
    value = SimpleNamespace(status=status)
    view._start('Actual durable state', lambda report: value, accepted.append)
    assert view.busy and not view.controls[0].enabled
    envelope = view.host.finish()
    assert envelope['status'] == status and accepted == [value]
    assert not view.busy and view.controls[0].enabled


def test_worker_failure_is_reported_and_never_relabelled_as_success(view):
    accepted = []
    def fail(report):
        raise ValueError('Actual producer refusal')
    view._start('Run', fail, accepted.append)
    envelope = view.host.finish()
    assert envelope['status'] == 'failed' and accepted == []
    assert view.messages[-1] == 'Actual producer refusal'
    assert not view.busy and view.controls[0].enabled


def test_late_status_callback_cannot_borrow_a_replacement_operation_token(view):
    def work(report):
        report('Old operation status')
        return SimpleNamespace(status='complete')
    view._start('First owner', work, lambda value: None)
    view.host.finish()
    assert len(view.pending) == 1
    view._start('Replacement owner', lambda report: None, lambda value: None)
    view.pending.pop()()
    assert 'Old operation status' not in view.messages
    assert view.messages[-1] == 'Replacement owner'


def test_late_result_never_applies_to_a_closed_dialog(view):
    accepted = []
    view._start('Run', lambda report: 'verified result', accepted.append)
    view.closed = True
    view.host.finish()
    assert accepted == [] and view.refreshes == []
    assert not view.controls[0].enabled


def test_close_requests_pause_and_waits_for_the_existing_worker_before_native_close(view):
    veto = []
    event = SimpleNamespace(CanVeto=lambda: True, Veto=lambda: veto.append(True))
    view._start('Run', lambda report: SimpleNamespace(status='paused'), lambda value: None)
    view.close(event)
    assert view.close_pending and view.host.cancelled.is_set()
    assert view.actions == [('a' * 32, 'pause')] and veto == [True]
    assert view.ends == [] and not view.closed
    view.host.finish()
    assert view.closed and view.ends == [1]


def test_failed_pause_write_still_cancels_the_owned_worker_before_close(view):
    def fail(*args):
        raise RuntimeError('Control publication failed')
    view.service.control = fail
    view._start('Run', lambda report: SimpleNamespace(status='cancelled'), lambda value: None)
    view.close()
    assert view.host.cancelled.is_set() and view.close_pending
    assert view.ends == []
    view.host.finish()
    assert view.closed and view.ends == [1]


def test_host_startup_refusal_does_not_leave_batch_controls_permanently_disabled(view):
    view.host._run = lambda label, action, done: None
    view._start('Run', lambda report: None, lambda value: None)
    assert not view.busy and view.controls[0].enabled
    assert 'could not start' in view.messages[-1]


def test_existing_host_work_blocks_a_second_batch_worker(view):
    view.host.busy = True
    with pytest.raises(ValueError, match='settled'):
        view._start('Second run', lambda report: None, lambda value: None)
    assert not view.busy and view.host.work is None


def test_saved_queue_cannot_be_silently_edited_in_place(view):
    with pytest.raises(ValueError, match='immutable'):
        view._edit_guard()
    view.batch_id = None
    view._edit_guard()
