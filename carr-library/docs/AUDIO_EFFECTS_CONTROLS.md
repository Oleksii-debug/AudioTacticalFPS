# Native audio effects and conversion

Open **Audio Studio → Audio effects and conversion** in the existing desktop.
Choose a source audio file, a new output filename, a supported format and sample
rate. The dialog discovers the selected local FFmpeg runtime, including the
verified bundled runtime, without downloading tools or sending audio anywhere.

The ordered chain provides **Add**, **Move up**, **Move down**, **Enable/bypass**,
**Remove** and numeric parameter editors. Labels include units and permitted
ranges. Narration, noise reduction, echo and loudness presets remain editable;
custom presets can be exported to a new JSON file and loaded transactionally.
Invalid presets leave the current chain intact.

Controls derive from the existing typed effect definitions and use the same
renderer as production jobs. Available controls include gain, high/low pass,
parametric equalizer, gate, compressor, limiter, denoise, de-esser, de-click,
de-clip, delay, room echo, tempo, pitch, stereo width and loudness. Actual local
FFmpeg filter availability determines whether an effect can be enabled.
Room echo is a multi-tap echo; it is not a convolution room simulation.

The chain has the shared renderer's 24-stage resource bound. Frequency must be
below half the selected sample rate. Opus uses 48 kHz. Unsupported output
extensions or encoders are rejected before conversion. Typed numeric controls
do not accept filter expressions, executable commands or plug-in paths.

Conversion runs outside the UI thread. **Cancel** stops the worker; closing an
active dialog first requests cancellation. Late callbacks are discarded after
closure. Successful output is checked and published to a new file. The source
and any existing destination are preserved. Editing an effects chain does not
modify an unsaved research note in the main window.

The dialog provides native text, choice, list and spin controls with predictable
focus and readable status. Offline conversion is implemented; real-time preview
and manual Windows 11/NVDA/listening acceptance are separate gates. The native
Windows probe exercises menu routing, Tab traversal, ordered numeric editing,
failure focus and actual conversion with the selected installed runtime.
