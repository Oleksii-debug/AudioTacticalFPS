# Reader playback and saved queues

The voice reader's **Playback controls** button opens controls for its existing
recording and player. The document, original quotations and synthesis settings
remain separate from playback settings.

- Set A/B points first, then use **Enable A–B repeat** explicitly. Setting points
  alone does not enable repeat. Clear or disable repeat before seeking outside it.
- Choose backward and forward skips from 0.1 to 600 seconds. A stopped skip changes
  the cursor without starting audio; a paused skip stays paused until Resume.
- Windows live volume, mute and balance affect this player's owned output only.
  They do not change the system mixer or screen-reader volume. A backend that
  cannot confirm per-instance levels reports a failure.
- Listening speed (0.25–4) and pitch (minus 12 to 12 semitones) prepare a verified
  private PCM copy on the existing reader worker and library resource broker.
  Apply stays stopped. Play remains explicit. Position, A/B and saved cursors
  refer to the original recording; transformed playback uses the measured frame
  mapping and is a linear timing estimate.
- The existing sleep timer counts paused time and leaves explicit Resume in
  charge after expiry. The reader's existing timer and quiet boundary still own
  automatic repeat admission.

**Audio Studio → Audio playback and saved playlist** opens a separate, explicit
media session. Review selected local PCM WAV files before adopting a queue.
Reviewed bytes are retained under the library's existing owned Studio input
store. Choosing, reordering, removing or restoring items does not play them.
**Play queue** explicitly arms sequential transitions; Stop, Cancel and window
closure revoke delayed starts.

The saved queue records order, original-frame cursor, levels, skip intervals and
the selected output declaration. Reopening loads passive metadata on the existing
desktop worker. **Review saved queue without playing** checks retained media
before applying it. A removed or reordered output cannot silently become the
default; review with current controls must be selected explicitly. Save and Save
All retain compare-before-write behavior. Failed saving is visible, and closing
without saving requires a separate confirmation.

The popup has no additional player, timer or executor. Direct destruction revokes
its callbacks through the captured live window ID. Prepared listening results
are either adopted by the same player or held for deterministic cleanup. An
unconfirmed native close retains the original owner and temporary bytes; it is
not reported as silence.

## Evidence status for this composition

This UI source reuses PR152's reviewed modal and standalone-owner design and the
current host's stronger source, progress, native output and lifecycle contracts.
Portable tests include real retained PCM, the existing DesktopExecutor, delayed
quiet callbacks, copied-root/source changes, cancelled review, compare-before-
write Save All, direct destruction and dropped GUI completion. The Windows
probe exercises actual MCI and owned WinMM output, actual FFmpeg speed/pitch
copies, real native controls and retained queue save/review through DesktopFrame.

All newly composed files remain **NOT_RUN** until the exact source/installed and
native gates execute. Synthetic PCM transport evidence does not qualify voice
models, acoustic quality, human listening or NVDA.
