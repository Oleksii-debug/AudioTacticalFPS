"""Execute the actual Studio inspection closure with a genuine existing worker.

The native controls are small protocol fixtures; the inspection below uses actual
completed PCM/receipts. Physical keyboard/wx acceptance is a separate native gate.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.studio import silence


_shared = runpy.run_path(str(Path(__file__).with_name('test_progress_voice_actions.py')))
_audio = runpy.run_path(str(Path(__file__).parents[1] / 'studio' / 'test_silence.py'))


class Field:
    def __init__(self, value):
        self.value = value
    def GetValue(self):
        return self.value


@pytest.fixture
def ui(tmp_path, monkeypatch):
    source = _audio['completed'].__wrapped__(tmp_path)
    service, state, before = next(source)
    library = SimpleNamespace(root=tmp_path / 'library')
    frame = SimpleNamespace(library=library)
    cancel = threading.Event()
    entered, release = threading.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='controlled-existing-studio')
    actual = silence.inspect_completed_silence
    owner = SimpleNamespace(future=None, completed=None, messages=[], reports=[], hold=False)
    def inspection(*args, **kwargs):
        entered.set()
        if owner.hold:
            assert release.wait(10), 'The controlled worker was not released.'
        return actual(*args, **kwargs)
    monkeypatch.setattr(silence, 'inspect_completed_silence', inspection)
    def run(operation, completed, *, phase, with_progress):
        assert with_progress and owner.future is None
        owner.completed = completed
        owner.future = executor.submit(operation, owner.messages.append)
        return True
    fields = Field('-80'), Field('.3')
    values = dict(alive=True, busy=False, current_job=state['id'], frame=frame,
        library_at_open=library, library_root=library.root, service=service, cancel=cancel,
        silence_threshold=fields[0], silence_minimum=fields[1], dialog=object(), run=run,
        report=lambda text, focus=False: owner.reports.append((text, focus)))
    handlers = _shared['callbacks']('studio_dialog.py', 'show_studio', ['inspect_silence', 'dispose'], values)
    fixture = SimpleNamespace(owner=owner, handlers=handlers, service=service, state=state,
        before=before, frame=frame, library=library, cancel=cancel, fields=fields,
        entered=entered, release=release)
    try:
        yield fixture
    finally:
        release.set()
        handlers.dispose()
        executor.shutdown(wait=True)
        source.close()


def test_actual_completed_inspection_uses_captured_options_and_existing_async_worker(ui):
    ui.owner.hold = True
    ui.handlers.inspect_silence()
    assert ui.entered.wait(5) and not ui.owner.future.done()
    ui.fields[0].value = '-10'
    ui.fields[1].value = '99'
    ui.release.set()
    result = ui.owner.future.result(timeout=15)
    assert result['criteria']['threshold_dbfs'] == -80
    assert result['criteria']['minimum_seconds'] == .3
    ui.owner.completed(result)
    assert len(ui.owner.reports) == 1 and 'Review warnings only' in ui.owner.reports[0][0]
    assert ui.owner.reports[0][1] is True, 'The existing read-only result must receive focus.'
    assert ui.service.get(ui.state['id']) == ui.state
    assert not ui.service.broker._active()


@pytest.mark.parametrize('change', ['library-object', 'library-root', 'closed'])
def test_rebound_or_destroyed_view_cancels_the_actual_owned_worker(ui, change):
    ui.owner.hold = True
    ui.handlers.inspect_silence()
    assert ui.entered.wait(5)
    if change == 'library-object':
        ui.frame.library = SimpleNamespace(root=ui.library.root)
    elif change == 'library-root':
        ui.library.root = ui.library.root / 'foreign'
    else:
        ui.handlers.dispose()
    assert not ui.owner.future.done(), 'Cancellation is not actual worker completion.'
    ui.release.set()
    with pytest.raises(AudioCancelled):
        ui.owner.future.result(timeout=15)
    assert all('Review warnings only' not in text for text, _focus in ui.owner.reports)
    assert ui.service.get(ui.state['id']) == ui.state


def test_completed_result_is_not_delivered_into_a_new_library(ui):
    ui.handlers.inspect_silence()
    result = ui.owner.future.result(timeout=15)
    ui.frame.library = SimpleNamespace(root=ui.library.root)
    with pytest.raises(ValueError, match='changed'):
        ui.owner.completed(result)
    assert all('Review warnings only' not in text for text, _focus in ui.owner.reports)


@pytest.mark.parametrize('threshold,minimum', [('nan', '2'), ('-121', '1'), ('-60', '0'), ('-60', 'inf'), ('letters', '1')])
def test_invalid_native_field_values_do_not_admit_an_operation(ui, threshold, minimum):
    ui.fields[0].value, ui.fields[1].value = threshold, minimum
    ui.handlers.inspect_silence()
    assert ui.owner.future is None and ui.owner.reports
    assert all('Review warnings only' not in text for text, _focus in ui.owner.reports)
    assert ui.service.get(ui.state['id']) == ui.state
