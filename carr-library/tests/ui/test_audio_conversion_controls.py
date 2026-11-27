"""Numeric conversion controls through actual handlers and actual FFmpeg output."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from threading import Event, get_ident
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioRuntime
from carr_library.ui import studio_dialog as ui


BASE = dict(sample_rate=24000, channels=2, bit_depth=16, bitrate=192, start=0,
            duration=None, gain=0, tempo=1, loudness=None, cleanup=False)
RATE = 'Output sample rate in Hz, 8000 to 192000; Opus requires 48000'
CHANNELS = 'Output channels: 1 for mono, 2 for stereo'
DEPTH = 'PCM bit depth: 16 or 24; 32 for WAV or AIFF only'
BITRATE = 'Target compressed bitrate in kbps, 32 to 512; MP3, AAC, Opus and WMA'


@pytest.mark.parametrize(('key', 'value'), [('sample_rate', 0), ('sample_rate', True),
    ('channels', 3), ('channels', True), ('bit_depth', 8), ('bit_depth', True), ('bitrate', 513),
    ('bitrate', 31), ('start', float('nan')), ('start', -1), ('duration', 0),
    ('duration', float('inf')), ('gain', float('nan')), ('tempo', 0), ('loudness', -41), ('cleanup', 1)])
def test_numeric_controls_reject_invalid_values_before_admission(key, value):
    with pytest.raises(ValueError):
        ui.audio_conversion_settings(**{**BASE, key: value})


@pytest.fixture
def view(tmp_path, monkeypatch):
    owner, fields, choices, jobs, notices = get_ident(), {}, [], [], []
    destroyed, widgets, forbidden = [], [], []
    class Widget:
        def __init__(self, parent=None, *args, value='', label='', **kwargs):
            self.name, self.value, self.destroyed = '', value, False
            widgets.append(self)
        def check(self):
            if get_ident() != owner or self.destroyed:
                forbidden.append(self.name or type(self).__name__)
            assert get_ident() == owner and not self.destroyed, 'Native access after dialog close or from worker'
        def __bool__(self): return not self.destroyed
        def SetName(self, value): self.check(); self.name = value
        def GetValue(self): self.check(); return fields.get(self.name, self.value)
        def SetSizer(self, *args): self.check()
        def SetScrollRate(self, *args): self.check()
        def FitInside(self): self.check()
        def Wrap(self, *args): self.check()
        def CreateButtonSizer(self, *args): self.check(); return object()
        def ShowModal(self): self.check(); return wx.ID_OK
        def Destroy(self): self.check(); self.destroyed = True; destroyed.append(self)
    class Picker(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self.path = choices.pop(0)
        def GetPath(self): self.check(); return str(self.path)
    class Checkbox(Widget):
        def __init__(self, *args, **kwargs): super().__init__(*args, value=False, **kwargs)
    class Sizer:
        def __init__(self, *args): pass
        def Add(self, *args): pass
    constants = ('FD_OPEN', 'FD_FILE_MUST_EXIST', 'ID_OK', 'ID_CANCEL', 'FD_SAVE', 'DEFAULT_DIALOG_STYLE',
                 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'ALL', 'EXPAND', 'OK', 'CANCEL')
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl'):
        setattr(wx, name, Widget)
    wx.CheckBox, wx.FileDialog, wx.BoxSizer = Checkbox, Picker, Sizer
    monkeypatch.setitem(sys.modules, 'wx', wx)
    source = tmp_path / 'synthetic-source.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', 2000 if n % 48 < 24 else -2000) for n in range(48000)))
    library = tmp_path / 'library'; library.mkdir()
    frame = SimpleNamespace(library=SimpleNamespace(root=library), _closing=False, cancel_event=Event(),
                            report=notices.append, run_job=lambda *args: jobs.append(args))
    calls = []
    from carr_library import runtime_settings
    def runtime(output, *, state_root, input_roots):
        calls.append((output, state_root, input_roots))
        return AudioRuntime(output, input_roots=input_roots)
    monkeypatch.setattr(runtime_settings, 'audio_runtime', runtime)
    fields['Target loudness in LUFS; empty leaves levels unchanged'] = ''
    return SimpleNamespace(frame=frame, fields=fields, choices=choices, jobs=jobs, notices=notices,
        calls=calls, source=source, root=tmp_path, destroyed=destroyed, runtime_factory=runtime,
        widgets=widgets, forbidden=forbidden, widget_type=Widget, wx=wx)


def admit(view, output, **values):
    view.choices.extend([view.source, output]); view.fields.update(values)
    ui.show_audio_conversion(view.frame)
    assert len(view.jobs) == 1 and len(view.destroyed) == 3
    _, operation, complete = view.jobs[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(operation).result(timeout=20)
    complete(result)
    return result


@pytest.mark.parametrize(('suffix', 'rate', 'channels', 'depth', 'codec'), [
    ('.wav', 48000, 1, 24, 'pcm_s24le'), ('.aiff', 44100, 2, 32, 'pcm_s32be'),
    ('.flac', 32000, 1, 24, 'flac'), ('.opus', 48000, 1, 16, 'opus')])
def test_actual_native_handler_options_reach_measured_conversion(view, suffix, rate, channels, depth, codec):
    output = view.root / ('new' + suffix)
    before = sha256(view.source.read_bytes()).hexdigest()
    result, measured, warning = admit(view, output, **{RATE: str(rate), CHANNELS: str(channels),
        DEPTH: str(depth), BITRATE: '96', 'Start in seconds': '0.2',
        'Duration in seconds; empty means remainder': '0.8'})
    assert (result.sample_rate, result.channels, result.codec) == (rate, channels, codec)
    assert abs(result.duration - .8) < .02
    assert result.sha256 == sha256(output.read_bytes()).hexdigest()
    assert sha256(view.source.read_bytes()).hexdigest() == before
    assert measured is not None and warning == ''
    assert view.calls[0][1] == view.frame.library.root / 'state'
    assert f'{rate} Hz; {channels} channel(s)' in view.notices[-1]
    if suffix == '.flac':
        inspected = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=bits_per_raw_sample', '-of', 'json', str(output)],
            check=True, capture_output=True, timeout=10)
        assert int(json.loads(inspected.stdout)['streams'][0]['bits_per_raw_sample']) == depth


@pytest.mark.parametrize(('suffix', 'values'), [('.opus', {RATE: '24000'}), ('.flac', {DEPTH: '32'})])
def test_codec_specific_numeric_mismatch_never_launches_runtime(view, suffix, values):
    output = view.root / ('invalid' + suffix)
    view.choices.extend([view.source, output]); view.fields.update(values)
    ui.show_audio_conversion(view.frame)
    with pytest.raises(ValueError): view.jobs[0][1]()
    assert not output.exists() and not view.calls


def test_invalid_native_field_does_not_admit_conversion(view):
    view.choices.append(view.source); view.fields[RATE] = 'nan'
    ui.show_audio_conversion(view.frame)
    assert not view.jobs and not view.calls and view.notices


@pytest.mark.parametrize('changed', ['library', 'root', 'closed'])
def test_queued_conversion_cannot_move_to_another_library(view, changed):
    output = view.root / 'new.wav'
    view.choices.extend([view.source, output]); ui.show_audio_conversion(view.frame)
    if changed == 'library': view.frame.library = SimpleNamespace(root=view.frame.library.root)
    elif changed == 'root': view.frame.library.root = view.root
    else: view.frame._closing = True
    with pytest.raises(ValueError, match='library'):
        view.jobs[0][1]()
    assert not view.calls and not output.exists()


def test_measurement_failure_after_real_publication_preserves_success(view, monkeypatch):
    from carr_library import runtime_settings
    def runtime(*args, **kwargs):
        actual = view.runtime_factory(*args, **kwargs)
        def measure(*args, **kwargs):
            view.frame.cancel_event.set()
            raise RuntimeError('Synthetic optional measurement failure')
        actual.measure = measure
        return actual
    monkeypatch.setattr(runtime_settings, 'audio_runtime', runtime)
    output = view.root / 'committed.wav'
    info, measured, warning = admit(view, output)
    assert output.is_file() and info.sha256 == sha256(output.read_bytes()).hexdigest()
    assert measured is None and 'measurement failure' in warning
    assert view.notices[-1].startswith('Saved ') and 'did not complete' in view.notices[-1]


def test_existing_output_is_never_overwritten_by_conversion(view):
    output = view.root / 'owned.wav'; output.write_bytes(b'Existing output')
    view.choices.extend([view.source, output]); ui.show_audio_conversion(view.frame)
    assert not view.jobs and not view.calls
    assert 'already exists' in view.notices[-1]
    assert output.read_bytes() == b'Existing output'
    assert not any(message.startswith('Saved ') for message in view.notices)


def test_shared_policy_denial_starts_no_inspection_or_native_conversion(view, monkeypatch):
    from carr_library.operations import ResourceBroker, ResourceUnavailable
    broker = ResourceBroker.for_app(view.frame.library.root)
    broker.configure(replace(broker.limits, memory_bytes=0))
    monkeypatch.setattr(AudioRuntime, 'inspect', lambda *args, **kwargs: pytest.fail('Denied conversion inspected native media.'))
    output = view.root / 'denied.wav'
    view.choices.extend([view.source, output]); ui.show_audio_conversion(view.frame)
    with pytest.raises(ResourceUnavailable):
        view.jobs[0][1]()
    assert not output.exists() and broker.snapshot()['leases'] == []


def test_actual_conversion_and_optional_measurement_share_bounded_existing_policy(view, monkeypatch):
    from carr_library.operations import ResourceBroker
    broker = ResourceBroker.for_app(view.frame.library.root)
    observed = []
    original = AudioRuntime.inspect
    def inspect(runtime, *args, **kwargs):
        rows = broker.snapshot()['leases']
        assert len(rows) == 1
        row = rows[0]
        assert row['request']['cpu_slots'] == runtime.budget.threads
        assert row['request']['memory_bytes'] >= 2 * runtime.budget.max_alloc_bytes
        if row['operation_id'].startswith('conversion:'):
            assert Path(row['request']['disk_path']) == view.root
            assert row['request']['disk_bytes'] > runtime.budget.reserve_disk_bytes
        observed.append(row['operation_id'])
        return original(runtime, *args, **kwargs)
    monkeypatch.setattr(AudioRuntime, 'inspect', inspect)
    output = view.root / 'admitted.wav'
    info, _, warning = admit(view, output)
    assert any(value.startswith('conversion-inspect:') for value in observed)
    assert any(value.startswith('conversion:') for value in observed)
    assert warning == '' and info.sha256 == sha256(output.read_bytes()).hexdigest()
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('when', ['queued', 'after-resource-wait'])
def test_destination_approval_rejects_replaced_parent_before_any_new_output(view, monkeypatch, when):
    from carr_library.operations import ResourceBroker
    target = view.root / 'selected'; target.mkdir()
    retained = view.root / 'original-selected'
    output = target / 'new.wav'
    view.choices.extend([view.source, output]); ui.show_audio_conversion(view.frame)
    def replace_parent():
        target.rename(retained); target.mkdir()
    if when == 'queued':
        replace_parent()
    else:
        reserve = ResourceBroker.reserve
        @contextmanager
        def changed(broker, request, **kwargs):
            with reserve(broker, request, **kwargs) as lease:
                if kwargs['operation_id'].startswith('conversion:'):
                    replace_parent()
                yield lease
        monkeypatch.setattr(ResourceBroker, 'reserve', changed)
    with pytest.raises(ValueError, match='destination changed'):
        view.jobs[0][1]()
    assert not output.exists() and not (retained / output.name).exists()
    assert ResourceBroker.for_app(view.frame.library.root).snapshot()['leases'] == []


def test_real_committed_conversion_survives_resource_release_acknowledgement_failure(view, monkeypatch):
    from carr_library.operations import ResourceBroker, ResourceLease
    release = ResourceLease.release
    def released(lease):
        release(lease)
        if lease.operation_id.startswith('conversion:'):
            raise OSError('Synthetic conversion release acknowledgement failure')
    monkeypatch.setattr(ResourceLease, 'release', released)
    output = view.root / 'released.wav'
    info, measured, warning = admit(view, output)
    assert info.sha256 == sha256(output.read_bytes()).hexdigest() and measured is not None
    assert 'saved and verified' in warning and 'acknowledgement failure' in warning
    assert ResourceBroker.for_app(view.frame.library.root).snapshot()['leases'] == []


def test_late_measurement_keyboard_interrupt_preserves_verified_saved_result(view, monkeypatch):
    def interrupted(*args, **kwargs):
        view.frame.cancel_event.set()
        raise KeyboardInterrupt('Synthetic late measurement interruption')
    monkeypatch.setattr(AudioRuntime, 'measure', interrupted)
    output = view.root / 'interrupted-measurement.wav'
    info, measured, warning = admit(view, output)
    assert measured is None and 'late measurement interruption' in warning
    assert info.sha256 == sha256(output.read_bytes()).hexdigest()
    assert view.notices[-1].startswith('Saved ')


def test_pending_measurement_cleanup_keeps_actual_resource_lease_until_exact_owner_confirms(view, monkeypatch):
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceBroker
    callbacks = []
    # A controlled native-owner boundary; the conversion itself is actual FFmpeg.
    owner = SimpleNamespace(add_quiescent_callback=callbacks.append)
    def retained(*args, **kwargs):
        raise AudioCleanupPending('Synthetic native measurement child still owned', cleanup_owner=owner)
    monkeypatch.setattr(AudioRuntime, 'measure', retained)
    output = view.root / 'retained-owner.wav'
    info, measured, warning = admit(view, output)
    broker = ResourceBroker.for_app(view.frame.library.root)
    try:
        assert len(callbacks) == 1 and len(broker.snapshot()['leases']) == 1
        assert info.sha256 == sha256(output.read_bytes()).hexdigest() and measured is None
        assert 'still owned' in warning and view.notices[-1].startswith('Saved ')
    finally:
        for callback in callbacks:
            callback()
    assert broker.snapshot()['leases'] == []


def test_foreign_equal_byte_replacement_during_late_failure_cannot_claim_verified_completion(view, monkeypatch):
    retained = view.root / 'actual-committed.wav'
    output = view.root / 'replaced.wav'
    def replaced(runtime, path, **kwargs):
        path.rename(retained)
        path.write_bytes(retained.read_bytes())
        raise OSError('Synthetic late failure after a foreign replacement')
    monkeypatch.setattr(AudioRuntime, 'measure', replaced)
    view.choices.extend([view.source, output]); ui.show_audio_conversion(view.frame)
    with pytest.raises(ValueError, match='changed during final cleanup'):
        view.jobs[0][1]()
    assert output.read_bytes() == retained.read_bytes() and not output.samefile(retained)
    assert not any(message.startswith('Saved ') for message in view.notices)


@pytest.mark.parametrize('modal_index', [0, 1, 2])
def test_nested_source_options_destination_modal_returns_after_parent_death_without_native_access(view, monkeypatch, modal_index):
    calls = []
    def modal(widget):
        widget.check(); index = len(calls); calls.append(index)
        if index == modal_index:
            view.frame._closing = True
            for peer in view.widgets:
                peer.destroyed = True
        return view.wx.ID_OK
    monkeypatch.setattr(view.widget_type, 'ShowModal', modal)
    output = view.root / 'dead-owner.wav'
    view.choices.extend([view.source, output]); ui.show_audio_conversion(view.frame)
    assert calls == list(range(modal_index + 1))
    assert view.forbidden == [] and not view.jobs and not view.calls and not output.exists()

