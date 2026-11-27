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
            self.parent, self.title = parent, kwargs.get('title', '')
            self.handlers, self.enabled = {}, True
            widgets.append(self)
        def check(self):
            if get_ident() != owner or self.destroyed:
                forbidden.append(self.name or type(self).__name__)
            assert get_ident() == owner and not self.destroyed, 'Native access after dialog close or from worker'
        def __bool__(self): return not self.destroyed
        def GetId(self): self.check(); return id(self)
        def Bind(self, event, callback, *args): self.check(); self.handlers[event] = callback
        def Enable(self, enabled=True): self.check(); self.enabled = enabled
        def ChangeValue(self, value): self.check(); self.value = value
        def SetFocus(self): self.check(); self.focused = True
        def FindWindowById(self, identity): self.check(); return self
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
                 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'ALL', 'EXPAND', 'OK', 'CANCEL',
                 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_WINDOW_DESTROY')
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'Button'):
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




def _conversion_button(view, name):
    return next(widget for widget in view.widgets if widget.name == name)


def _finish_effect_inventory(view, job):
    # The actual selected audio runtime and broker run on a real worker. Only
    # the surrounding native modal decisions are supplied by this fixture.
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(job[1]).result(timeout=30)
    job[2](result)
    return result


@pytest.mark.parametrize('chain_kind', ['dry', 'gain', 'two-tempos-and-bypass'])
def test_reviewed_ordered_chain_reaches_actual_conversion_with_shared_admission(
        view, monkeypatch, chain_kind):
    from carr_library.audio import Effect
    from carr_library.operations import ResourceBroker
    from carr_library.ui import effects_dialog

    chains = {'dry': (), 'gain': (Effect('gain', gain_db=-7),),
              'two-tempos-and-bypass': (Effect('tempo', factor=1.25),
                  Effect('tempo', factor=.8), Effect('tempo', factor=.5, enabled=False))}
    selected = chains[chain_kind]
    broker = ResourceBroker.for_app(view.frame.library.root)
    capabilities, convert = AudioRuntime.capabilities, AudioRuntime.convert
    admissions, conversions, workers = [], [], []
    def inspect(runtime, **kwargs):
        rows = broker.snapshot()['leases']
        assert len(rows) == 1
        if rows[0]['operation_id'].startswith('effect-capabilities:'):
            admissions.append(rows[0])
            workers.append(get_ident())
            assert rows[0]['request']['cpu_slots'] == runtime.budget.threads
        return capabilities(runtime, **kwargs)
    def converted(runtime, source, output, **kwargs):
        assert kwargs['effects'] == selected
        row, = broker.snapshot()['leases']
        assert row['operation_id'].startswith('conversion:')
        # Multiple enabled tempo stages multiply; an absent or bypassed tempo
        # cannot make reservation computation fail before actual rendering.
        assert row['request']['disk_bytes'] > runtime.budget.reserve_disk_bytes
        conversions.append(tuple(kwargs['effects']))
        return convert(runtime, source, output, **kwargs)
    monkeypatch.setattr(AudioRuntime, 'capabilities', inspect)
    monkeypatch.setattr(AudioRuntime, 'convert', converted)

    def review(parent, root, initial, *, sample_rate, available, guard):
        guard()
        assert root == view.frame.library.root and sample_rate == 24000
        assert all(effect.kind in available for effect in selected if effect.enabled)
        return selected
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', review)
    def modal(widget):
        widget.check()
        if widget.title == 'Convert audio and apply effects':
            _conversion_button(view, 'Edit ordered effect chain and presets').handlers[view.wx.EVT_BUTTON](None)
            assert not _conversion_button(view, RATE).enabled
            _finish_effect_inventory(view, view.jobs[0])
            assert not _conversion_button(view, 'Gain in decibels, -60 to 12').enabled
            assert 'Ordered chain active' in _conversion_button(view, 'Audio conversion effect settings').value
            assert broker.snapshot()['leases'] == []
        return view.wx.ID_OK
    monkeypatch.setattr(view.widget_type, 'ShowModal', modal)
    output = view.root / (chain_kind + '.wav')
    before = sha256(view.source.read_bytes()).hexdigest()
    view.choices.extend([view.source, output])
    ui.show_audio_conversion(view.frame)
    assert len(view.jobs) == 2 and len(admissions) == 1
    assert workers and all(identity != get_ident() for identity in workers)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(view.jobs[1][1]).result(timeout=30)
    view.jobs[1][2](result)
    info, measured, warning = result
    assert conversions == [selected] and measured is not None and warning == ''
    assert info.sha256 == sha256(output.read_bytes()).hexdigest()
    assert sha256(view.source.read_bytes()).hexdigest() == before
    assert view.notices[-1].startswith('Saved ') and not view.forbidden
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('decision', ['cancel-chain', 'return-to-basic'])
def test_cancelled_or_reset_ordered_review_preserves_basic_conversion_controls(view, monkeypatch, decision):
    from carr_library.audio import Effect
    from carr_library.ui import effects_dialog

    def review(*args, **kwargs):
        kwargs['guard']()
        return None if decision == 'cancel-chain' else (Effect('gain', gain_db=-12),)
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', review)
    def modal(widget):
        widget.check()
        if widget.title == 'Convert audio and apply effects':
            _conversion_button(view, 'Edit ordered effect chain and presets').handlers[view.wx.EVT_BUTTON](None)
            _finish_effect_inventory(view, view.jobs[0])
            if decision == 'return-to-basic':
                _conversion_button(view, 'Use basic effect controls').handlers[view.wx.EVT_BUTTON](None)
            assert _conversion_button(view, 'Gain in decibels, -60 to 12').enabled
            assert not _conversion_button(view, 'Use basic effect controls').enabled
        return view.wx.ID_OK
    monkeypatch.setattr(view.widget_type, 'ShowModal', modal)
    view.fields['Gain in decibels, -60 to 12'] = '-4'
    output = view.root / 'basic-retained.wav'
    view.choices.extend([view.source, output])
    ui.show_audio_conversion(view.frame)
    # Inspect the actual frozen job input by wrapping the existing converter;
    # the converter still runs and verifies a real output.
    convert, observed = AudioRuntime.convert, []
    def selected(runtime, source, output, **kwargs):
        observed.extend(kwargs['effects'])
        return convert(runtime, source, output, **kwargs)
    monkeypatch.setattr(AudioRuntime, 'convert', selected)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(view.jobs[1][1]).result(timeout=30)
    assert next(effect for effect in observed if effect.kind == 'gain').parameters['gain_db'] == -4
    assert result[0].sha256 == sha256(output.read_bytes()).hexdigest()


def test_shared_policy_denial_rejects_effect_inventory_without_starting_native_process(view, monkeypatch):
    from carr_library.operations import ResourceBroker, ResourceUnavailable

    broker = ResourceBroker.for_app(view.frame.library.root)
    broker.configure(replace(broker.limits, memory_bytes=0))
    def forbidden(*args, **kwargs):
        pytest.fail('Denied effect inventory must not run the native capability process.')
    monkeypatch.setattr(AudioRuntime, 'capabilities', forbidden)
    def modal(widget):
        widget.check()
        if widget.title == 'Convert audio and apply effects':
            _conversion_button(view, 'Edit ordered effect chain and presets').handlers[view.wx.EVT_BUTTON](None)
            job = view.jobs[0]
            with ThreadPoolExecutor(max_workers=1) as executor:
                with pytest.raises(ResourceUnavailable) as error:
                    executor.submit(job[1]).result(timeout=10)
            job[3](error.value)
            assert 'Effect review did not open' in _conversion_button(view, 'Audio conversion effect settings').value
            return view.wx.ID_CANCEL
        return view.wx.ID_OK
    monkeypatch.setattr(view.widget_type, 'ShowModal', modal)
    view.choices.append(view.source)
    ui.show_audio_conversion(view.frame)
    assert len(view.jobs) == 1 and broker.snapshot()['leases'] == []
    assert not view.forbidden


@pytest.mark.parametrize('close_path', ['direct', 'native-parent'])
def test_completed_effect_inventory_cannot_reopen_an_editor_after_native_owner_death(view, monkeypatch, close_path):
    from carr_library.operations import ResourceBroker
    from carr_library.ui import effects_dialog

    results, deliveries = [], []
    def forbidden(*args, **kwargs):
        pytest.fail('A completed inventory cannot open a modal on a deleted conversion owner.')
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', forbidden)
    def modal(widget):
        widget.check()
        if widget.title == 'Convert audio and apply effects':
            _conversion_button(view, 'Edit ordered effect chain and presets').handlers[view.wx.EVT_BUTTON](None)
            job = view.jobs[0]
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(job[1]).result(timeout=30)
            results.append(result)
            identity = widget.GetId()
            if close_path == 'direct':
                widget.Destroy()
            else:
                view.widget_type.Destroy(widget)
                widget.handlers[view.wx.EVT_WINDOW_DESTROY](SimpleNamespace(
                    GetId=lambda: identity, GetEventObject=lambda: SimpleNamespace(),
                    Skip=lambda: None))
            for peer in view.widgets:
                peer.destroyed = True
            assert widget._effect_alive is False
            job[2](result)
            job[3](RuntimeError('An already queued reporting failure'))
            deliveries.extend(('success', 'failure'))
            return view.wx.ID_OK
        return view.wx.ID_OK
    monkeypatch.setattr(view.widget_type, 'ShowModal', modal)
    view.choices.append(view.source)
    ui.show_audio_conversion(view.frame)
    assert results and deliveries == ['success', 'failure'] and len(view.jobs) == 1
    assert not view.frame.cancel_event.is_set() and not view.forbidden
    assert ResourceBroker.for_app(view.frame.library.root).snapshot()['leases'] == []



def test_closing_conversion_cancels_its_queued_inventory_without_stopping_an_unrelated_host_job(view, monkeypatch):
    from carr_library.operations import ResourceBroker, ResourceCancelled

    def forbidden(*args, **kwargs):
        pytest.fail('A cancelled effect inventory must not start its native capability process.')
    monkeypatch.setattr(AudioRuntime, 'capabilities', forbidden)
    def modal(widget):
        widget.check()
        if widget.title == 'Convert audio and apply effects':
            _conversion_button(view, 'Edit ordered effect chain and presets').handlers[view.wx.EVT_BUTTON](None)
            widget.Destroy()
            return view.wx.ID_CANCEL
        return view.wx.ID_OK
    monkeypatch.setattr(view.widget_type, 'ShowModal', modal)
    view.choices.append(view.source)
    ui.show_audio_conversion(view.frame)
    assert len(view.jobs) == 1 and not view.frame.cancel_event.is_set()
    job = view.jobs[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(ResourceCancelled) as error:
            executor.submit(job[1]).result(timeout=10)
    job[3](error.value)
    assert not view.frame.cancel_event.is_set() and not view.forbidden
    assert ResourceBroker.for_app(view.frame.library.root).snapshot()['leases'] == []
