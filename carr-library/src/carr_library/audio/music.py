"""Original procedural cues and symbolic MIDI, with truthful engine discovery.

The small built-in composer is deterministic and local. Its output is labelled
procedural; it is not MusicGen, ACE-Step, a learned model or a quality substitute
for those engines. Neural engines require an explicitly installed and reviewed
model/runtime. This module never downloads anything or falls back to an API.
"""
from __future__ import annotations

import math
from pathlib import Path
import random
import shutil
import struct
import tempfile

from .ffmpeg import (AudioBackend, AudioError, _check_cancel, _destination,
                     _frames, _local, _number, _publish, _run)


MOODS = {
    "calm": {"roots": (48, 53, 57, 55), "third": 4, "tempo": 72},
    "bright": {"roots": (60, 57, 53, 55), "third": 4, "tempo": 104},
    "suspense": {"roots": (45, 41, 40, 45), "third": 3, "tempo": 60},
    "tense": {"roots": (45, 41, 40, 45), "third": 3, "tempo": 60},
}


def engines():
    """No model is assumed present from a model name or importable package."""
    return (
        {"id": "procedural", "available": bool(shutil.which("ffmpeg")),
         "kind": "deterministic original tone composition",
         "reason": "Requires the selected FFmpeg build; no weights, account or network."},
        {"id": "midi", "available": True, "kind": "symbolic original composition",
         "reason": "Standard MIDI export. Audible SoundFont rendering is separate."},
        {"id": "fluidsynth", "available": False, "runtime_detected": bool(shutil.which("fluidsynth")),
         "kind": "SoundFont MIDI rendering",
         "reason": "Select a local FluidSynth executable and a licensed SoundFont explicitly."},
        {"id": "musicgen", "available": False, "kind": "optional local neural generation",
         "reason": "Model and worker are not configured or verified; no weights were downloaded.",
         "license_note": "AudioCraft code MIT; MusicGen weights CC-BY-NC 4.0. Private noncommercial profile supported after exact model verification."},
        {"id": "ace-step", "available": False, "kind": "optional local neural generation",
         "reason": "Model and worker are not configured or verified; no weights were downloaded."},
    )


def _settings(duration_seconds, tempo, mood, seed):
    # Representation bound is far beyond ordinary long-form production; CPU,
    # disk and cancellation are controlled by the durable caller's budget.
    duration = _number(duration_seconds, 1 / 192000, 2**52 / 192000, "duration_seconds")
    if mood not in MOODS:
        raise ValueError("Choose calm, bright or tense procedural music.")
    tempo = _number(MOODS[mood]["tempo"] if tempo is None else tempo, 20, 300, "tempo")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("The composition seed must be an unsigned 64-bit integer.")
    return duration, tempo, MOODS[mood], seed


def _vlq(number):
    if not 0 <= number <= 0x0fffffff:
        raise ValueError("MIDI event delta exceeds the standard representation.")
    result = bytearray([number & 0x7f])
    while number := number >> 7:
        result.insert(0, 0x80 | (number & 0x7f))
    return bytes(result)


def compose_midi(output, *, duration_seconds, tempo=None, mood="calm", seed=0,
                 max_notes=1_000_000, cancel=None, deadline=None):
    """Write a streaming SMF0 composition with deterministic chord/arpeggio notes.

    max_notes is an explicit resource budget, not a fixed duration ceiling.
    Its required value is known before any output is created.
    """
    duration, tempo, palette, seed = _settings(duration_seconds, tempo, mood, seed)
    if Path(output).suffix.lower() not in (".mid", ".midi"):
        raise ValueError("Choose a .mid or .midi output filename.")
    steps = max(1, math.ceil(duration * tempo / 60 * 2))
    if type(max_notes) is not int or max_notes < 1 or steps > max_notes:
        raise ValueError(f"The MIDI cue needs {steps} notes, exceeding the selected note budget.")
    ticks_per_quarter = 480
    total_ticks = max(1, round(duration * tempo / 60 * ticks_per_quarter))
    micros = round(60_000_000 / tempo)
    rng = random.Random(seed)
    previous_tick = 0
    with _destination(output) as (target, staged):
        with staged.open("xb+") as stream:
            stream.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, ticks_per_quarter))
            stream.write(b"MTrk\x00\x00\x00\x00")
            start = stream.tell()
            stream.write(b"\x00\xff\x51\x03" + micros.to_bytes(3, "big"))
            title = f"Original procedural {mood} cue".encode("ascii")
            stream.write(b"\x00\xff\x03" + _vlq(len(title)) + title)
            stream.write(b"\x00\xc0\x00")  # General MIDI acoustic piano, no SysEx.
            for index in range(steps):
                _check_cancel(cancel, deadline)
                tick = index * 240
                if tick >= total_ticks:
                    break
                root = palette["roots"][(index // 8) % 4]
                intervals = (0, palette["third"], 7, 12)
                note = root + intervals[(index + seed % 4) % 4]
                velocity = 38 + rng.randrange(15)
                stream.write(_vlq(tick - previous_tick) + bytes((0x90, note, velocity)))
                end_tick = min(total_ticks, tick + 210)
                stream.write(_vlq(end_tick - tick) + bytes((0x80, note, 0)))
                previous_tick = end_tick
            stream.write(_vlq(total_ticks - previous_tick) + b"\xff\x2f\x00")
            size = stream.tell() - start
            if size > 0xffffffff:
                raise ValueError("MIDI track exceeds the format's 32-bit size field; split into chapters.")
            stream.seek(start - 4)
            stream.write(struct.pack(">I", size))
        _check_cancel(cancel, deadline)
        _publish(staged, target)
    return target


def render_procedural(backend: AudioBackend, output, *, duration_seconds=None,
                      duration_frames=None, tempo=None, mood="calm", seed=0,
                      cancel=None, deadline=None):
    """Render a quiet original cue through FFmpeg's local oscillator source.

    Repeating four-chord motifs are intentional and reported as procedural
    composition, never as hours of independently generated neural music.
    """
    if duration_frames is not None:
        _frames(duration_frames)
        if duration_seconds is not None:
            raise ValueError("Specify cue duration in frames or seconds, not both.")
        duration_seconds = duration_frames / backend.sample_rate
    duration, tempo, palette, seed = _settings(duration_seconds, tempo, mood, seed)
    frames = duration_frames if duration_frames is not None else max(1, round(duration * backend.sample_rate))
    duration = frames / backend.sample_rate
    roots = palette["roots"]
    phase = seed % 4
    bar = 240 / tempo
    index = f"mod(floor(t/{bar:.12f})+{phase},4)"
    root = f"if(eq({index},0),{roots[0]},if(eq({index},1),{roots[1]},if(eq({index},2),{roots[2]},{roots[3]})))"
    frequency = f"(440*pow(2,(({root})-69)/12))"
    # FFmpeg owns sample generation and all DSP. Every interpolated value is a
    # validated number or internal fixed expression, never source/user text.
    third = 2 ** (palette["third"] / 12)
    fifth = 2 ** (7 / 12)
    pulse = f"(0.75+0.25*cos(2*PI*t*{tempo/60:.12f}))"
    # Brief soft edges suppress phase discontinuities when the chord changes.
    edge = f"min(1,mod(t,{bar:.12f})/0.03)*min(1,({bar:.12f}-mod(t,{bar:.12f}))/0.03)"
    fade = min(.5, duration / 4)
    envelope = f"min(1,t/{fade:.12f})*min(1,({duration:.12f}-t)/{fade:.12f})"
    notes = f"(0.065*sin(2*PI*{frequency}*t)+0.04*sin(2*PI*{frequency}*{third:.12f}*t)+0.025*sin(2*PI*{frequency}*{fifth:.12f}*t))"
    source = f"aevalsrc='{notes}*{pulse}*{envelope}*{edge}':s={backend.sample_rate}:d={duration:.12f}"
    return backend._generated(source, output, duration_frames=frames, cancel=cancel, deadline=deadline)


def render_midi_with_fluidsynth(backend: AudioBackend, midi, soundfont, output, *,
                               fluidsynth=None, soundfont_rights_confirmed=False,
                               cancel=None, deadline=None):
    """Explicit optional SoundFont rendering; never installs or downloads assets."""
    if soundfont_rights_confirmed is not True:
        raise ValueError("Confirm the chosen SoundFont's usage rights before rendering.")
    binary = fluidsynth or shutil.which("fluidsynth")
    if not binary:
        raise AudioError("FluidSynth is not installed. MIDI export and procedural audio remain available.")
    binary, midi, soundfont = _local(binary), _local(midi), _local(soundfont)
    if midi.suffix.lower() not in (".mid", ".midi") or soundfont.suffix.lower() not in (".sf2", ".sf3"):
        raise ValueError("Select a MIDI file and a separately licensed SF2/SF3 SoundFont.")
    target = _local(output, existing=False)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    with tempfile.TemporaryDirectory(prefix=".carr-midi-", dir=target.parent) as temp:
        folder = Path(temp)
        config = folder / "empty.conf"
        config.write_text("", encoding="ascii")
        wave = folder / "synth.wav"
        _run([binary, "-ni", "-f", config, "-R", "0", "-C", "0", "-g", "0.5",
              "-r", backend.sample_rate, "-T", "wav", "-F", wave, soundfont, midi],
             cancel=cancel, deadline=deadline)
        return backend.convert(wave, target, format=target.suffix.lower().lstrip("."),
                               cancel=cancel, deadline=deadline)
