"""Native wx focus, effect editing and actual FFmpeg conversion smoke gate.

Run with a display and installed package:
    python -I tests/ui/native_audio_effects_smoke.py

The installed local FFmpeg/ffprobe must be on PATH. This probe creates its own
synthetic source. It verifies native controls and audio, and explicitly records
manual NVDA as NOT_RUN. There is no mock GUI or substitute audio engine.
With --allow-unavailable-runtime, a missing executable is explicitly NOT_RUN;
an available runtime's failed native or audio checks still fail the probe.
"""
from array import array
import argparse
import json
import math
from pathlib import Path
import platform
import shutil
import tempfile
import traceback
import wave

import wx

from carr_library.audio.render import file_sha256, tool_path
from carr_library.ui.audio_effects import NativeAudioEffects, install_audio_effects


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-unavailable-runtime", action="store_true")
    args = parser.parse_args()
    try:
        tool_path('ffmpeg')
        tool_path('ffprobe')
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"status": "NOT_RUN" if args.allow_unavailable_runtime else "FAIL",
            "reason": "The local FFmpeg and ffprobe executables are unavailable.",
            "native_effects": "NOT_RUN", "manual_nvda": "NOT_RUN"}))
        return 0 if args.allow_unavailable_runtime else 1
    failures, checks, report = [], [], {}
    ui = None

    def stop(detail):
        failures.append(detail)
        if ui is not None:
            ui.controller.close()
            if ui.window.IsModal():
                ui.window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            stop(traceback.format_exc())
            return False

    app = ProbeApp(False)
    parent = wx.Frame(None, title="Native audio effects probe parent")
    parent.menu_bar = wx.MenuBar()
    parent.menu_bar.Append(wx.Menu(), "Audio &Studio")
    parent.SetMenuBar(parent.menu_bar)
    parent.report = lambda message: stop(message)
    install_audio_effects(parent)
    assert parent.menu_bar.FindMenuItem("Audio Studio", "Audio effects and conversion...") != wx.NOT_FOUND
    checks.append("effects entry in the native Audio Studio menu")
    note = wx.TextCtrl(parent, value="An unsaved research note", style=wx.TE_MULTILINE)
    note.MarkDirty()
    original_note = note.GetValue(), note.IsModified()
    parent.Show()

    def guarded(callback):
        try:
            callback()
        except BaseException:
            stop(traceback.format_exc())

    def later(callback):
        wx.CallLater(40, lambda: guarded(callback))

    def event(control):
        assert control.IsEnabled(), "A requested native control is disabled."
        command = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        command.SetEventObject(control)
        assert control.GetEventHandler().ProcessEvent(command)

    def button(label, parent=None):
        return next(child for child in (parent or ui.window).GetChildren()
                    if isinstance(child, wx.Button) and child.GetLabel().replace("&", "") == label)

    with tempfile.TemporaryDirectory(prefix="carr-native-effects-") as directory:
        source, destination = Path(directory) / "synthetic source.wav", Path(directory) / "processed.flac"
        samples = array("h", (int(5000 * math.sin(2 * math.pi * 440 * index / 24000))
                              for index in range(24000)))
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(samples.tobytes())
        original_audio = file_sha256(source)
        ui = NativeAudioEffects(parent, source=source)

        def parameters():
            dialog = next(window for window in wx.GetTopLevelWindows()
                          if isinstance(window, wx.Dialog) and window is not ui.window and window.IsModal())
            control = next(child for child in dialog.GetChildren()
                           if isinstance(child, wx.TextCtrl) and child.GetName().startswith("frequency hz (Hz"))
            assert wx.Window.FindFocus() is control
            control.ChangeValue("100")
            event(button("Apply parameters", dialog))

        def ready():
            if ui.controller.busy:
                later(ready)
                return
            assert ui.controller.state == "ready", ui.status.GetValue()
            assert wx.Window.FindFocus() is ui.status
            for control in (ui.source, ui.output, ui.format, ui.rate, ui.effect_choice, ui.chain, ui.status):
                assert control.GetName().strip(), "A native audio control has no accessible name."
            ui.source.SetFocus()
            assert ui.source.Navigate(), "Native forward keyboard traversal failed."
            assert wx.Window.FindFocus() is button("Browse source audio")
            checks.extend(("named native controls", "initial readable status focus", "native forward keyboard traversal"))
            event(button("Replace chain with preset"))
            assert [row.effect.kind for row in ui.controller.rows] == ["highpass", "compressor", "limiter"]
            assert wx.Window.FindFocus() is ui.chain
            event(button("Move down"))
            assert ui.chain.GetSelection() == 1 and ui.controller.rows[1].effect.kind == "highpass"
            event(button("Enable / bypass"))
            assert not ui.controller.rows[1].enabled
            later(parameters)
            event(button("Edit parameters…"))
            assert dict(ui.controller.rows[1].effect.parameters)["frequency_hz"] == 100
            assert wx.Window.FindFocus() is ui.chain
            checks.extend(("ordered preset and explicit move", "bypass retained", "native numeric parameter dialog and return focus"))
            ui.output.ChangeValue(str(destination))
            ui.format.SetStringSelection("flac")
            ui.rate.SetStringSelection("24000")
            event(ui.convert_button)
            assert ui.cancel_button.IsEnabled() and not ui.source.IsEnabled()
            assert wx.Window.FindFocus() is ui.cancel_button
            later(completed)

        def graphic_parameters():
            dialog = None
            try:
                dialog = next(window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.GetParent() is ui.window
                    and window.GetTitle() == "Graphic equalizer (10 bands) parameters"
                    and window.IsModal())
                forms = [child for child in dialog.GetChildren() if isinstance(child, wx.ScrolledWindow)]
                assert len(forms) == 1
                controls = [child for child in forms[0].GetChildren() if isinstance(child, wx.TextCtrl)]
                assert len(controls) == 10 and all(control.GetName().strip() for control in controls)
                first = next(control for control in controls
                             if control.GetName().startswith("31.5 Hz band gain"))
                centre = next(control for control in controls
                              if control.GetName().startswith("1000 Hz band gain"))
                last = next(control for control in controls
                            if control.GetName().startswith("16000 Hz band gain"))
                assert first.GetValue() == last.GetValue() == "0"
                assert "keep 0 dB" in last.GetName()
                last.SetFocus()
                assert wx.Window.FindFocus() is last and last.Navigate()
                previous = ui.controller.rows
                last.ChangeValue("1")
                event(button("Apply parameters", dialog))
                status = next(child for child in dialog.GetChildren()
                    if isinstance(child, wx.TextCtrl)
                    and child.GetName() == "Effect parameter validation status")
                assert dialog.IsModal() and ui.controller.rows is previous
                assert "half the selected sample rate" in status.GetValue()
                assert wx.Window.FindFocus() is status
                last.ChangeValue("0")
                centre.ChangeValue("3")
                event(button("Apply parameters", dialog))
                checks.extend(("ten named scrollable graphic EQ numeric controls",
                               "native access to the last fixed band",
                               "Nyquist refusal preserves the reviewed chain"))
            except BaseException:
                if dialog is not None and dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
                raise

        def begin_extended(original_result):
            ui.preset_choice.SetStringSelection("Dry conversion")
            event(button("Replace chain with preset"))
            assert not ui.controller.rows
            kinds = ("lowshelf", "highshelf", "notch", "chorus", "graphic_equalizer")
            for kind in kinds:
                assert ui.controller.schema[kind]["available"]
                ui.effect_choice.SetSelection(ui._effect_keys.index(kind))
                event(button("Add effect"))
            assert tuple(row.effect.kind for row in ui.controller.rows) == kinds
            assert wx.Window.FindFocus() is ui.chain
            armed = [True]
            fill = wx.CallLater(40, lambda: guarded(graphic_parameters) if armed[0] else None)
            try:
                event(button("Edit parameters…"))
            finally:
                armed[0] = False
                fill.Stop()
            if failures:
                return
            assert ui.controller.rows[-1].effect.parameters == tuple(sorted(
                (name, 3.0 if name == "band_1000_db" else 0.0)
                for name in ui.controller.schema["graphic_equalizer"]["parameters"]))
            assert wx.Window.FindFocus() is ui.chain
            extra_output = Path(directory) / "extended-effects.wav"
            ui.output.ChangeValue(str(extra_output))
            ui.format.SetStringSelection("wav")
            event(ui.convert_button)
            assert ui.controller.busy and ui.cancel_button.IsEnabled() and not ui.source.IsEnabled()
            later(lambda: completed_extended(original_result, extra_output))

        def completed_extended(result, extra_output):
            if ui.controller.busy:
                later(lambda: completed_extended(result, extra_output))
                return
            assert ui.controller.state == "complete", ui.status.GetValue()
            extra = ui.controller.result
            assert Path(extra.path).samefile(extra_output)
            assert extra.sample_rate == 24000 and extra.channels == 2
            assert extra.frames == 24000 + round(.042 * 24000)
            assert file_sha256(source) == original_audio and destination.is_file()
            assert (note.GetValue(), note.IsModified()) == original_note
            assert wx.Window.FindFocus() is ui.status and not ui.cancel_button.IsEnabled()
            checks.extend(("native addition of shelves, notch, chorus and graphic EQ",
                           "native numeric values and return focus retained",
                           "actual converter chorus tail and unchanged source"))
            report.update(extended_effects={"frames": extra.frames, "sample_rate": extra.sample_rate,
                "channels": extra.channels, "live_playback": "NOT_RUN"})
            report.update(status="PASS", platform=platform.system(), wx_version=wx.version(),
                checks=checks, frames=result.frames, sample_rate=result.sample_rate,
                source="synthetic local PCM", audio_engine="actual local FFmpeg", manual_nvda="NOT_RUN")
            event(ui.close_button)

        def completed():
            if ui.controller.busy:
                later(completed)
                return
            assert ui.controller.state == "complete", ui.status.GetValue()
            result = ui.controller.result
            # Windows temp roots may arrive with an 8.3 spelling. Rendering
            # canonicalizes the parent; compare the actual published file.
            assert Path(result.path).samefile(destination), (result.path, destination)
            assert result.sample_rate == 24000 and result.channels == 2, (
                result.sample_rate, result.channels)
            assert result.frames > 0 and destination.is_file()
            assert file_sha256(source) == original_audio
            assert wx.Window.FindFocus() is ui.status and "Saved and verified" in ui.status.GetValue()
            assert not ui.cancel_button.IsEnabled() and ui.source.IsEnabled()
            assert (note.GetValue(), note.IsModified()) == original_note
            checks.extend(("asynchronous conversion and cancellable focus", "real verified local FLAC output",
                           "unchanged source bytes and unsaved parent note", "completed status focus"))
            begin_extended(result)

        timeout = wx.CallLater(60000, lambda: stop("Native effects smoke exceeded 60 seconds."))
        later(ready)
        try:
            ui.show()
        except BaseException:
            failures.append(traceback.format_exc())
        finally:
            timeout.Stop()
            parent.Destroy()
            app.ProcessPendingEvents()
    if failures or report.get("status") != "PASS":
        print(json.dumps({"status": "FAIL", "errors": failures or ["Probe did not finish."],
                          "manual_nvda": "NOT_RUN"}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
