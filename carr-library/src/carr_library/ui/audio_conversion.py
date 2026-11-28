"""Ordered typed effects over the existing accessible Convert audio action."""
from pathlib import Path
import threading

from ..durable_state import local_path, regular


def show_audio_conversion(frame):
    return _show_guarded(frame, batch=False)


def show_audio_conversion_batch(frame):
    return _show_guarded(frame, batch=True)


def _show_guarded(frame, *, batch):
    """Capture the exact current library before any native picker or worker."""
    library = frame.library
    root = local_path(library.root, exists=True)
    initial = regular(root, directory=True)
    identity = (initial.st_dev, initial.st_ino)

    def scope_current():
        if (getattr(frame, '_closing', False) or getattr(frame, 'library', None) is not library
                or Path(library.root) != root):
            return False
        try:
            value = regular(local_path(root, exists=True), directory=True)
        except (OSError, ValueError):
            return False
        return (value.st_dev, value.st_ino) == identity

    def current_root():
        if not scope_current():
            raise ValueError('The selected library changed or is closing. Reopen audio conversion.')

    try:
        current_root()
        return _show(frame, root, current_root, scope_current, batch=batch)
    except Exception as error:
        if scope_current():
            frame.report(str(error))


def _show(frame, library_root, current_root, scope_current, *, batch=False):
    import wx
    from ..audio import Effect
    from ..audio.effect_presets import checked_chain
    from ..audio.conversion import ConversionSettings, convert_audio, discover_effects, review_conversion
    from ..audio.models import integer
    from ..durable_state import local_path
    chooser = wx.FileDialog(frame,
        'Choose explicit audio or video files for this batch' if batch else 'Choose a local audio or video source',
        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | (wx.FD_MULTIPLE if batch else 0))
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return
        sources = [local_path(value, exists=True) for value in chooser.GetPaths()] if batch else [
            local_path(chooser.GetPath(), exists=True)]
        if not sources or len(sources) > 64:
            raise ValueError('Choose between one and 64 explicit source files.')
        source = sources[0]
        current_root()
    finally:
        chooser.Destroy()
    dialog = wx.Dialog(frame, title='Batch audio conversion settings' if batch else 'Convert audio and apply effects', size=(760, 720),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    outer = wx.BoxSizer(wx.VERTICAL)
    panel = wx.ScrolledWindow(dialog)
    panel.SetScrollRate(0, 16)
    layout = wx.BoxSizer(wx.VERTICAL)
    def entry(label, value):
        layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 5)
        control = wx.TextCtrl(panel, value=value)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        return control
    start = entry('Start in seconds', '0')
    duration = entry('Duration in seconds; empty means remainder', '')
    output_rate = entry('Output sample rate in Hz, 8000 to 192000', '24000')
    output_channels = entry('Output channels, 1 or 2', '2')
    output_depth = entry('PCM bit depth, 16, 24 or 32', '16')
    output_bitrate = entry('Encoding bitrate in kbps for MP3, AAC, Opus and WMA', '192')
    gain = entry('Gain in decibels, -60 to 12', '0')
    tempo = entry('Processed-copy speed without pitch change, 0.5 to 2', '1')
    pitch = entry('Processed-copy pitch in semitones, -12 to 12; duration preserved', '0')
    loudness = entry('Target loudness in LUFS; empty leaves levels unchanged', '-18')
    effects = wx.CheckBox(panel, label='Apply speech noise reduction and gentle compression')
    effects.SetName('Apply speech noise reduction and gentle compression')
    layout.Add(effects, 0, wx.ALL, 6)
    advanced = wx.Button(panel, label='Edit ordered effect chain and presets')
    advanced.SetName('Edit ordered effect chain and presets')
    basic = wx.Button(panel, label='Use basic effect controls')
    basic.SetName('Use basic effect controls')
    basic.Disable()
    layout.Add(advanced, 0, wx.ALL, 6)
    layout.Add(basic, 0, wx.ALL, 6)
    status = wx.TextCtrl(panel, value='Basic controls are active. Choose output settings before reviewing an ordered chain.',
                         style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Audio conversion effect settings')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 6)
    panel.SetSizer(layout)
    outer.Add(panel, 1, wx.EXPAND | wx.ALL, 5)
    outer.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 6)
    dialog.SetSizer(outer)
    advanced_chain = None
    alive, pending = True, False
    query_cancelled = threading.Event()
    query_future = None
    original_controls = (gain, tempo, pitch, loudness, effects)

    def output_values():
        rate = integer(int(output_rate.GetValue()), 'Output sample rate', 8000, 192000)
        channels = integer(int(output_channels.GetValue()), 'Output channels', 1, 2)
        depth = int(output_depth.GetValue())
        if depth not in (16, 24, 32):
            raise ValueError('PCM bit depth must be 16, 24 or 32.')
        bitrate = integer(int(output_bitrate.GetValue()), 'Encoding bitrate', 32, 512)
        return rate, channels, depth, bitrate

    def basic_chain():
        gain_value, tempo_value = float(gain.GetValue()), float(tempo.GetValue())
        if not -60 <= gain_value <= 12 or not .5 <= tempo_value <= 2:
            raise ValueError('Gain or speed is outside the displayed range.')
        values = [Effect('gain', gain_db=gain_value), Effect('tempo', factor=tempo_value)]
        pitch_value = float(pitch.GetValue())
        if pitch_value != 0:
            values.append(Effect('pitch', semitones=pitch_value))
        if effects.GetValue():
            values += [Effect('denoise'), Effect('compressor')]
        if loudness.GetValue().strip():
            values.append(Effect('loudness', target_lufs=float(loudness.GetValue()), peak_db=-1))
        return checked_chain(values, output_values()[0])

    def controls():
        if not alive or not scope_current():
            return
        for control in original_controls:
            control.Enable(not pending and advanced_chain is None)
        for control in (output_rate, output_channels, output_depth, output_bitrate):
            control.Enable(not pending)
        advanced.Enable(not pending)
        basic.Enable(not pending and advanced_chain is not None)
        dialog.FindWindowById(wx.ID_OK).Enable(not pending)

    def use_basic(_event=None):
        nonlocal advanced_chain
        if not alive or not scope_current():
            return
        advanced_chain = None
        controls()
        status.ChangeValue('Basic controls are active. Choose output settings before reviewing an ordered chain.')
        gain.SetFocus()

    def edit_chain(_event=None):
        nonlocal pending, query_future
        if not alive or not scope_current():
            return
        if pending or getattr(frame, 'working', False):
            status.ChangeValue('Wait for the current operation or cancel it before reviewing effects.')
            return
        try:
            current_root()
            selected_rate = output_values()[0]
            frozen = advanced_chain if advanced_chain is not None else basic_chain()
            checked_chain(frozen, selected_rate)
        except Exception as exc:
            status.ChangeValue(str(exc))
            status.SetFocus()
            return
        pending = True
        query_cancelled.clear()
        controls()
        status.ChangeValue('Checking installed local audio effects…')

        def inspect():
            return discover_effects(library_root, guard=current_root,
                cancelled=lambda: query_cancelled.is_set() or frame.cancel_event.is_set())

        def failed(error):
            nonlocal pending
            pending = False
            if not alive or not scope_current():
                return
            controls()
            status.ChangeValue(f'Effect review did not open: {error}')

        def reviewed(available):
            nonlocal pending, advanced_chain
            if not alive or not scope_current():
                pending = False
                return
            try:
                current_root()
                from .effects_dialog import show_effect_chain
                # A batch may contain mixed input channel layouts. Spatial
                # envelopes require an explicitly reviewed mono/stereo scope;
                # expose them in Studio, where that scope is already known.
                available = tuple(kind for kind in available
                                  if kind not in ('pan_envelope', 'width_envelope'))
                value = show_effect_chain(dialog, library_root, frozen,
                    sample_rate=selected_rate, available=available, guard=current_root)
                current_root()
                if value is not None:
                    advanced_chain = value
                    status.ChangeValue(f'Ordered chain active: {len(value)} effects. Basic controls are inactive.')
                else:
                    status.ChangeValue('Effect review cancelled; the previous settings are retained.')
            except Exception as error:
                if alive and scope_current():
                    status.ChangeValue(str(error))
                    status.SetFocus()
            finally:
                pending = False
                if alive and scope_current():
                    controls()
                    advanced.SetFocus()

        try:
            query_future = frame.run_job('Checking local audio effects', inspect, reviewed, failed)
            if query_future is False or query_future is None:
                query_future = None
                failed(RuntimeError('The current audio worker did not accept effect discovery.'))
        except Exception as exc:
            failed(exc)

    advanced.Bind(wx.EVT_BUTTON, edit_chain)
    basic.Bind(wx.EVT_BUTTON, use_basic)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
        current_root()
        if pending:
            raise ValueError('Wait for the effect review to finish before converting audio.')
        start_value = float(start.GetValue())
        duration_value = float(duration.GetValue()) if duration.GetValue().strip() else None
        chain = advanced_chain if advanced_chain is not None else basic_chain()
        rate_value, channels_value, depth_value, bitrate_value = output_values()
        checked_chain(chain, rate_value)
    except Exception as exc:
        if scope_current():
            frame.report(str(exc))
        return
    finally:
        alive = False
        query_cancelled.set()
        if query_future is not None:
            query_future.cancel()
        dialog.Destroy()
    settings = ConversionSettings(start=start_value, duration=duration_value,
        sample_rate=rate_value, channels=channels_value, bit_depth=depth_value,
        bitrate=bitrate_value, effects=chain)
    if batch:
        from .audio_conversion_batch import show_reviewed_batch
        return show_reviewed_batch(frame, library_root, sources, settings,
            current_root=current_root, scope_current=scope_current)
    chooser = wx.FileDialog(frame, 'Save converted audio to a new file', defaultFile=source.stem + '-converted.wav',
                            wildcard='Audio (*.wav;*.flac;*.mp3;*.m4a;*.m4b;*.ogg;*.opus;*.aac;*.aiff;*.wma)|*.wav;*.flac;*.mp3;*.m4a;*.m4b;*.ogg;*.opus;*.aac;*.aiff;*.wma',
                            style=wx.FD_SAVE)
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return
        output = local_path(chooser.GetPath())
        current_root()
    finally:
        chooser.Destroy()
    review = review_conversion(library_root, source, output, settings=settings, guard=current_root)

    def operation():
        return convert_audio(review, guard=current_root, cancelled=frame.cancel_event.is_set)

    def completed(result):
        if not scope_current():
            return
        message = (f'Saved {result.audio.path}. Duration {result.audio.duration:.2f} seconds. '
                   f'Measured loudness {result.measurement.integrated_lufs} LUFS.')
        if result.cleanup_warning:
            message += ' ' + result.cleanup_warning
        frame.report(message)

    def failed(error):
        if scope_current():
            frame.report(str(error))

    current_root()
    frame.run_job('Converting local audio', operation, completed, failed)
