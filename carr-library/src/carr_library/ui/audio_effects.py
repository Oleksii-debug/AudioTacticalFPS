"""Native, keyboard-first local audio conversion and typed effect chains.

Runtime discovery and conversion run outside the GUI thread. This module has no
wx import until the dialog is opened, so the controller can be tested against
actual audio files without substituting a graphical runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import threading
from types import MappingProxyType

from ..audio.ffmpeg import AudioBackend, AudioCancelled, Effect
from ..audio.project import atomic_bytes, json_bytes, local_directory, read_json


SAMPLE_RATES = (8000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 192000)
EFFECT_NAMES = {
    "eq": "Equalizer", "gate": "Noise gate", "compressor": "Compressor",
    "limiter": "Limiter", "denoise": "Noise reduction", "delay": "Echo",
    "fade_in": "Fade in", "fade_out": "Fade out", "normalize": "Loudness target",
    "highpass": "High-pass filter", "lowpass": "Low-pass filter", "gain": "Gain",
}
PRESETS = {
    "Narration": (("highpass", {"frequency": 80}), ("compressor", {}), ("limiter", {})),
    "Noise reduction": (("denoise", {}), ("gate", {})),
    "Echo": (("delay", {"delay_ms": 180, "decay": .25}),),
    "Loudness target": (("normalize", {"lufs": -18, "true_peak": -1.5, "range": 11}),),
}


@dataclass(frozen=True)
class ChainEntry:
    effect: Effect
    enabled: bool = True


def _validated_entry(schema, name, parameters=None, enabled=True):
    if not isinstance(name, str) or name not in schema:
        raise ValueError("Choose a supported effect from the effect list.")
    if type(enabled) is not bool:
        raise ValueError("An effect must be explicitly enabled or bypassed.")
    description = schema[name]
    if enabled and not description["available"]:
        raise ValueError(f"The selected local runtime does not provide {EFFECT_NAMES.get(name, name)}.")
    parameters = {} if parameters is None else parameters
    expected = description["parameters"]
    if not isinstance(parameters, dict) or set(parameters) - set(expected):
        raise ValueError("The effect contains unsupported parameters.")
    values = {}
    for key, bounds in expected.items():
        value = parameters.get(key, bounds["default"])
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not bounds["minimum"] <= value <= bounds["maximum"]):
            raise ValueError(f"{key.replace('_', ' ')} must be a finite number from "
                f"{bounds['minimum']} to {bounds['maximum']} {bounds['unit']}.")
        values[key] = float(value)
    return ChainEntry(Effect(name, MappingProxyType(values)), enabled)


def entry_label(number, row, schema):
    parameters = "; ".join(f"{key.replace('_', ' ')} {value:g} {schema[row.effect.name]['parameters'][key]['unit']}"
                           for key, value in row.effect.parameters.items())
    return f"{number}. {'Enabled' if row.enabled else 'Bypassed'}: " \
           f"{EFFECT_NAMES.get(row.effect.name, row.effect.name)}; {parameters}"


class EffectsController:
    """A finite chain editor and cancellable worker, independent of native wx.

    ``dispatch`` must post its callback to the owning GUI thread. ``on_change``
    receives dictionaries containing state/message/busy and, after publication,
    the real AudioInfo result. Closing suppresses every queued late callback.
    """

    def __init__(self, *, ffmpeg=None, ffprobe=None, backend_factory=None,
                 dispatch=None, on_change=None):
        self.backend_factory = backend_factory or (
            lambda rate: AudioBackend(ffmpeg, ffprobe, sample_rate=rate))
        self.dispatch = dispatch or (lambda callback: callback())
        self.on_change = on_change or (lambda event: None)
        self.schema, self.formats, self.rows = {}, (), ()
        self.state, self.message, self.result = "new", "Preparing local audio controls.", None
        self.busy, self.closed = False, False
        self._cancel = threading.Event()
        self._thread = None
        self._generation = 0
        self._lock = threading.RLock()

    def _emit(self):
        if not self.closed:
            self.on_change({"state": self.state, "message": self.message,
                            "busy": self.busy, "result": self.result})

    def _editable(self):
        if self.closed or self.busy:
            raise ValueError("Wait for the current operation or cancel it before editing effects.")
        if not self.schema:
            raise ValueError("The local audio runtime is not ready.")

    def _selected(self, index):
        self._editable()
        if type(index) is not int or not 0 <= index < len(self.rows):
            raise ValueError("Select an effect in the chain first.")
        return self.rows[index]

    def add(self, name, parameters=None):
        self._editable()
        if len(self.rows) >= 64:
            raise ValueError("An effect chain supports at most 64 effects.")
        self.rows += (_validated_entry(self.schema, name, parameters),)
        return len(self.rows) - 1

    def update(self, index, parameters):
        previous = self._selected(index)
        updated = _validated_entry(self.schema, previous.effect.name, parameters, previous.enabled)
        self.rows = self.rows[:index] + (updated,) + self.rows[index + 1:]

    def move(self, index, delta):
        self._selected(index)
        if type(delta) is not int or delta not in (-1, 1):
            raise ValueError("Move an effect one position up or down.")
        target = index + delta
        if not 0 <= target < len(self.rows):
            raise ValueError("The selected effect is already at the end of the chain.")
        rows = list(self.rows)
        rows[index], rows[target] = rows[target], rows[index]
        self.rows = tuple(rows)
        return target

    def toggle(self, index):
        row = self._selected(index)
        updated = _validated_entry(self.schema, row.effect.name, dict(row.effect.parameters), not row.enabled)
        self.rows = self.rows[:index] + (updated,) + self.rows[index + 1:]

    def remove(self, index):
        self._selected(index)
        self.rows = self.rows[:index] + self.rows[index + 1:]
        return min(index, len(self.rows) - 1)

    def preset(self, name):
        self._editable()
        if name not in PRESETS:
            raise ValueError("Choose an available named preset.")
        rows = tuple(_validated_entry(self.schema, effect, params) for effect, params in PRESETS[name])
        self.rows = rows

    def save_preset(self, path):
        self._editable()
        path = Path(path).expanduser().absolute()
        parent = local_directory(path.parent)
        if path.suffix.lower() != ".json":
            raise ValueError("Save the effect preset as a new JSON file.")
        data = {"schema_version": 1, "kind": "carr-audio-effects", "effects": [
            {"name": row.effect.name, "enabled": row.enabled, "parameters": dict(row.effect.parameters)}
            for row in self.rows]}
        atomic_bytes(parent / path.name, json_bytes(data))

    def load_preset(self, path):
        self._editable()
        path = Path(path).expanduser().absolute()
        local_directory(path.parent)
        data = read_json(path, maximum=64 * 1024)
        if (not isinstance(data, dict) or set(data) != {"schema_version", "kind", "effects"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or data["kind"] != "carr-audio-effects" or not isinstance(data["effects"], list)
                or len(data["effects"]) > 64):
            raise ValueError("Choose a supported audio-effects preset with at most 64 effects.")
        rows = []
        for item in data["effects"]:
            if not isinstance(item, dict) or set(item) != {"name", "enabled", "parameters"}:
                raise ValueError("Invalid typed effect entry in the preset.")
            rows.append(_validated_entry(self.schema, item["name"], item["parameters"], item["enabled"]))
        self.rows = tuple(rows)

    def _start(self, state, message, operation, finish):
        with self._lock:
            if self.closed or self.busy:
                raise ValueError("An audio operation is already running or this dialog is closed.")
            self._generation += 1
            generation = self._generation
            self._cancel = threading.Event()
            cancellation = self._cancel
            self.busy, self.state, self.message, self.result = True, state, message, None
        self._emit()

        def worker():
            result = error = None
            try:
                result = operation(cancellation)
            except Exception as exc:
                error = exc

            def complete():
                with self._lock:
                    if self.closed or generation != self._generation:
                        return
                    self.busy = False
                    if error is not None:
                        self.state = "cancelled" if isinstance(error, (AudioCancelled, InterruptedError)) else "error"
                        self.message = str(error) or type(error).__name__
                    else:
                        finish(result)
                self._emit()

            self.dispatch(complete)

        self._thread = threading.Thread(target=worker, name="carr-audio-effects", daemon=True)
        self._thread.start()
        return self._thread

    def initialize(self):
        def discover(cancel):
            if cancel.is_set():
                raise AudioCancelled("Local runtime discovery cancelled.")
            backend = self.backend_factory(44100)
            schema, formats = backend.effect_schema(), tuple(backend.capabilities()["formats"])
            if not schema or not formats:
                raise ValueError("The selected local runtime does not provide supported audio effects and output formats.")
            if cancel.is_set():
                raise AudioCancelled("Local runtime discovery cancelled.")
            return schema, formats

        def finish(result):
            self.schema, self.formats = result
            self.state = "ready"
            self.message = "Ready. Choose a source, a new output file, and an effect chain."

        return self._start("loading", "Checking the selected local audio runtime…", discover, finish)

    def convert(self, source, output, *, format="flac", sample_rate=44100):
        self._editable()
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("Choose an integer sample rate from 8000 to 192000 Hz.")
        if format not in self.formats:
            raise ValueError("Choose a format supported by the selected local audio runtime.")
        if not str(source).strip() or not str(output).strip():
            raise ValueError("Choose both the source audio and a new output file.")
        effects = tuple(row.effect for row in self.rows if row.enabled)
        for effect in effects:
            if "frequency" in effect.parameters and effect.parameters["frequency"] >= sample_rate / 2:
                raise ValueError("Filter frequency must be below half the selected sample rate.")
        source, output = Path(source).expanduser().absolute(), Path(output).expanduser().absolute()

        def operation(cancel):
            return self.backend_factory(sample_rate).convert(source, output, format=format, effects=effects, cancel=cancel)

        def finish(result):
            self.result, self.state = result, "complete"
            self.message = (f"Saved and verified: {result.path}\n"
                f"{result.duration_seconds:.3f} seconds; {result.sample_rate} Hz; {result.channels} channels. "
                "The source file is unchanged.")

        return self._start("running", "Converting the selected audio. Cancel stops the current operation.", operation, finish)

    def cancel(self):
        if self.busy and not self.closed:
            self._cancel.set()
            self.state, self.message = "cancelling", "Cancelling the current audio operation…"
            self._emit()

    def close(self):
        with self._lock:
            self.closed = True
            self._generation += 1
            self._cancel.set()
            self.on_change = lambda event: None


class NativeAudioEffects:
    """Native dialog wrapper; controls are exposed for the Windows focus probe."""

    def __init__(self, parent, source=None, ffmpeg=None, ffprobe=None):
        import wx
        self.wx, self.closing, self.destroyed = wx, False, False
        self.window = wx.Dialog(parent, title="Audio effects and conversion", size=(900, 820),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.controller = EffectsController(ffmpeg=ffmpeg, ffprobe=ffprobe,
            dispatch=wx.CallAfter, on_change=self._changed)
        self.edit_controls = []
        root = wx.BoxSizer(wx.VERTICAL)

        def labelled(caption, control):
            root.Add(wx.StaticText(self.window, label=caption), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            control.SetName(caption.replace("&", ""))
            self.edit_controls.append(control)
            return control

        def path_row(caption, value, handler):
            control = labelled(caption, wx.TextCtrl(self.window, value=value))
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(control, 1, wx.EXPAND | wx.RIGHT, 6)
            button = wx.Button(self.window, label="Browse " + caption.lower())
            button.Bind(wx.EVT_BUTTON, handler)
            row.Add(button, 0)
            root.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            self.edit_controls.append(button)
            return control

        self.source = path_row("Source audio", str(source) if source else "", self._browse_source)
        suggested = str(Path(source).with_name(Path(source).stem + "-processed.flac")) if source else ""
        self.output = path_row("New output audio", suggested, self._browse_output)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self.window, label="Output format"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.format = wx.Choice(self.window)
        self.format.SetName("Output audio format")
        row.Add(self.format, 1, wx.RIGHT, 12)
        row.Add(wx.StaticText(self.window, label="Sample rate (Hz)"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.rate = wx.Choice(self.window, choices=[str(rate) for rate in SAMPLE_RATES])
        self.rate.SetName("Output sample rate in hertz")
        self.rate.SetStringSelection("44100")
        row.Add(self.rate, 1)
        self.edit_controls.extend((self.format, self.rate))
        root.Add(row, 0, wx.EXPAND | wx.ALL, 8)

        self.preset_choice = labelled("Named effect preset", wx.Choice(self.window, choices=list(PRESETS)))
        self.preset_choice.SetSelection(0)
        root.Add(self.preset_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._button(row, "Replace chain with preset", self._preset)
        self._button(row, "Load preset JSON…", self._load_preset)
        self._button(row, "Save preset JSON…", self._save_preset)
        root.Add(row, 0, wx.EXPAND | wx.ALL, 8)

        self.effect_choice = labelled("Effect to add", wx.Choice(self.window))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.effect_choice, 1, wx.RIGHT, 6)
        self._button(row, "Add effect", self._add)
        root.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.chain = labelled("Ordered effects; processing runs from top to bottom",
                              wx.ListBox(self.window, style=wx.LB_SINGLE))
        root.Add(self.chain, 1, wx.EXPAND | wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._button(row, "Edit parameters…", self._edit)
        self._button(row, "Move up", lambda event: self._move(-1))
        self._button(row, "Move down", lambda event: self._move(1))
        self._button(row, "Enable / bypass", self._toggle)
        self._button(row, "Remove effect", self._remove)
        root.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        root.Add(wx.StaticText(self.window, label="Conversion status"), 0, wx.LEFT | wx.RIGHT, 8)
        self.status = wx.TextCtrl(self.window, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 90))
        self.status.SetName("Audio effects and conversion status")
        root.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.convert_button = self._button(row, "Convert to new file", self._convert)
        self.cancel_button = self._button(row, "Cancel conversion", lambda event: self.controller.cancel(), edit=False)
        self.close_button = self._button(row, "Close", self._close, edit=False, id=wx.ID_CLOSE)
        root.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.window.SetSizer(root)
        self.window.SetMinSize((760, 680))
        self.window.SetEscapeId(self.close_button.GetId())
        self.window.Bind(wx.EVT_CLOSE, self._close)
        self.format.Bind(wx.EVT_CHOICE, self._format_changed)
        self.chain.Bind(wx.EVT_LISTBOX_DCLICK, self._edit)
        self._effect_keys, self._runtime_loaded = (), False
        self.controller.initialize()

    def _button(self, row, label, handler, *, edit=True, **kwargs):
        button = self.wx.Button(self.window, label=label, **kwargs)
        button.Bind(self.wx.EVT_BUTTON, handler)
        row.Add(button, 0, self.wx.RIGHT, 6)
        if edit:
            self.edit_controls.append(button)
        return button

    def _report(self, message, *, focus=True):
        if self.destroyed:
            return
        self.status.ChangeValue(str(message))
        if focus:
            self.status.SetFocus()

    def _changed(self, event):
        if self.destroyed:
            return
        ready = bool(self.controller.schema)
        if ready and not self._runtime_loaded:
            self.format.Set(list(self.controller.formats))
            self.format.SetStringSelection("flac" if "flac" in self.controller.formats else self.controller.formats[0])
            self._effect_keys = tuple(self.controller.schema)
            self.effect_choice.Set([EFFECT_NAMES.get(name, name) +
                ("" if self.controller.schema[name]["available"] else " — unavailable in this runtime")
                for name in self._effect_keys])
            self.effect_choice.SetSelection(0)
            self._runtime_loaded = True
        for control in self.edit_controls:
            control.Enable(ready and not event["busy"])
        if self.format.GetStringSelection() == "opus":
            self.rate.SetStringSelection("48000")
            self.rate.Enable(False)
        self.cancel_button.Enable(event["busy"])
        self._report(event["message"], focus=not event["busy"])
        if event["state"] == "running":
            self.cancel_button.SetFocus()
        if self.closing and not event["busy"]:
            self._end()

    def _try(self, operation):
        try:
            return operation()
        except (ValueError, OSError, RuntimeError) as exc:
            self._report(str(exc) or type(exc).__name__)
            return None

    def _refresh_chain(self, selected=0):
        self.chain.Set([entry_label(index, row, self.controller.schema)
                        for index, row in enumerate(self.controller.rows, 1)])
        if self.controller.rows:
            self.chain.SetSelection(max(0, min(selected, len(self.controller.rows) - 1)))
        self.chain.SetFocus()

    def _add(self, event):
        def add():
            index = self.effect_choice.GetSelection()
            if index < 0:
                raise ValueError("Choose an effect to add.")
            self._refresh_chain(self.controller.add(self._effect_keys[index]))
        self._try(add)

    def _preset(self, event):
        def apply():
            self.controller.preset(self.preset_choice.GetStringSelection())
            self._refresh_chain()
        self._try(apply)

    def _move(self, delta):
        self._try(lambda: self._refresh_chain(self.controller.move(self.chain.GetSelection(), delta)))

    def _toggle(self, event):
        def toggle():
            index = self.chain.GetSelection()
            self.controller.toggle(index)
            self._refresh_chain(index)
        self._try(toggle)

    def _remove(self, event):
        self._try(lambda: self._refresh_chain(self.controller.remove(self.chain.GetSelection())))

    def _edit(self, event):
        def edit():
            index = self.chain.GetSelection()
            entry = self.controller._selected(index)
            wx = self.wx
            dialog = wx.Dialog(self.window, title=EFFECT_NAMES.get(entry.effect.name, entry.effect.name) + " parameters",
                               style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            root, fields = wx.BoxSizer(wx.VERTICAL), {}
            for name, spec in self.controller.schema[entry.effect.name]["parameters"].items():
                label = f"{name.replace('_', ' ')} ({spec['unit']}; {spec['minimum']} to {spec['maximum']})"
                root.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 6)
                control = wx.TextCtrl(dialog, value=f"{entry.effect.parameters[name]:g}")
                control.SetName(label)
                fields[name] = control
                root.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
            message = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(500, 60))
            message.SetName("Effect parameter validation status")
            root.Add(message, 0, wx.EXPAND | wx.ALL, 6)
            buttons = wx.StdDialogButtonSizer()
            ok, cancel = wx.Button(dialog, wx.ID_OK, label="Apply parameters"), wx.Button(dialog, wx.ID_CANCEL)
            buttons.AddButton(ok)
            buttons.AddButton(cancel)
            buttons.Realize()
            root.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 6)
            def apply(event):
                try:
                    values = {name: float(control.GetValue()) for name, control in fields.items()}
                    self.controller.update(index, values)
                except (ValueError, OverflowError) as exc:
                    message.ChangeValue(str(exc) or "Enter a finite number for every parameter.")
                    message.SetFocus()
                    return
                dialog.EndModal(wx.ID_OK)
            ok.Bind(wx.EVT_BUTTON, apply)
            dialog.SetSizerAndFit(root)
            next(iter(fields.values())).SetFocus()
            try:
                if dialog.ShowModal() == wx.ID_OK:
                    self._refresh_chain(index)
            finally:
                dialog.Destroy()
        self._try(edit)

    def _file(self, caption, *, save=False, preset=False):
        wx = self.wx
        wildcard = "JSON presets (*.json)|*.json" if preset else "Audio files|*.wav;*.flac;*.mp3;*.ogg;*.opus;*.aac;*.m4a;*.m4b;*.aiff;*.aif;*.wma|All files|*.*"
        style = wx.FD_SAVE if save else wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
        name = "audio-effects.json" if preset else (Path(self.output.GetValue()).name or "processed." + self.format.GetStringSelection())
        dialog = wx.FileDialog(self.window, caption, defaultFile=name if save else "", wildcard=wildcard, style=style)
        try:
            return dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def _browse_source(self, event):
        path = self._file("Choose local source audio")
        if path:
            self.source.ChangeValue(path)
            if not self.output.GetValue():
                self.output.ChangeValue(str(Path(path).with_name(Path(path).stem + "-processed." + self.format.GetStringSelection())))
            self.source.SetFocus()

    def _browse_output(self, event):
        path = self._file("Choose a new output file; existing files are preserved", save=True)
        if path:
            self.output.ChangeValue(path)
            self.output.SetFocus()

    def _format_changed(self, event):
        opus = self.format.GetStringSelection() == "opus"
        if opus:
            self.rate.SetStringSelection("48000")
        self.rate.Enable(not opus and not self.controller.busy)
        value = self.output.GetValue()
        if value:
            self.output.ChangeValue(str(Path(value).with_suffix("." + self.format.GetStringSelection())))

    def _load_preset(self, event):
        path = self._file("Load a typed effect preset", preset=True)
        if path:
            def load():
                self.controller.load_preset(path)
                self._refresh_chain()
            self._try(load)

    def _save_preset(self, event):
        path = self._file("Save a new typed effect preset", save=True, preset=True)
        if path:
            def save():
                self.controller.save_preset(path)
                self._report("Saved effect preset: " + path)
            self._try(save)

    def _convert(self, event):
        self._try(lambda: self.controller.convert(self.source.GetValue(), self.output.GetValue(),
            format=self.format.GetStringSelection(), sample_rate=int(self.rate.GetStringSelection())))

    def _end(self):
        self.controller.close()
        if self.window.IsModal():
            self.window.EndModal(self.wx.ID_CLOSE)

    def _close(self, event):
        if self.controller.busy:
            self.closing = True
            self.controller.cancel()
            self._report("Cancelling the current operation before closing…")
            if hasattr(event, "CanVeto") and event.CanVeto():
                event.Veto()
        else:
            self._end()

    def show(self):
        try:
            return self.window.ShowModal()
        finally:
            self.destroyed = True
            self.controller.close()
            self.window.Destroy()


def show_audio_effects(parent, source=None, ffmpeg=None, ffprobe=None):
    return NativeAudioEffects(parent, source, ffmpeg, ffprobe).show()
