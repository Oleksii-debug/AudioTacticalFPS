# Actual 891 audio follow-up

Source: `8911b2ae232b4786677890c40560f4393a0202a9`.
Completed [Windows audio job140547902143](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46011505846/job/140547902143)
was read after completion on2026-11-27. This is a failure diagnosis, not a
passing result for a later source.

## New observed causes

- Reusing an accepted completed Book tried to construct a read-only CPU/memory
  reservation with zero disk bytes and a nonempty disk path. The current broker
  correctly refuses that inconsistent request. Six of11 Book batch cases and
  the completed-book re-export part of the duration CLI failed at this boundary.
  The correction omits disk fields for verification; the existing real copy
  reservation still declares its positive output budget.
- The isolated Windows Book CLI used the console's local codec for non-ASCII
  JSON. A capture completed but its acknowledgement could not encode source
  metadata. Machine JSON now escapes non-ASCII characters; parsing recovers the
  exact Unicode values. The existing isolated process test remains, with actual
  cp1252 stdout/stderr regression coverage.
- The measured-PCM suite passed14 cases; one expected Python tuples to compare
  directly to JSON arrays in the embedded Book plan. Its expected value now uses
  the JSON representation. All full PCM concatenation, original-byte and
  publication checks remain.

The three large-PDF suites also failed at the already identified pypdf integer
subclass read. Source798 contains that separate correction. The isolated
loopback step was refused before pytest because the earlier checker required
unshipped bootstrap `semantic_compat/probe.py`; source798 contains the
declared-package inventory correction.
Neither correction is counted as executed by this891 result.

## Actual native outcomes

The installed native Book window passed51 checks, including ordered preparation,
existing FFmpeg receipt reuse, measured completed PCM, close settlement and
unchanged retained originals. The prior caption collision is therefore resolved
at this source.

Named assistant templates passed native save/rename/load, fresh-scope review and
backup/restore. The large-source native import probe passed its actual three
fragments through the existing worker. Both Google-v2 and local-LibreTranslate
native UI probes passed with explicitly injected synthetic provider responses.

The foreground scheduling probe exceeded its90-second deadline. That is a real
failed required probe and remains under diagnosis; other native successes do
not turn the workflow green. No model inference, physical audio quality,
manual NVDA, paid provider call or release qualification is inferred.

New source corrections require the exact composed source/installed/native gates.
