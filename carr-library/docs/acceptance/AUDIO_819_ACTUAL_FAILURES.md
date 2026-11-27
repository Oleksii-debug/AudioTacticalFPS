# Actual Windows819 acceptance and focused repairs

Observed source819d6bd910a92171eacc487806a8968ad25ad485, [run46005184387](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46005184387), job140526455657. The completed log contains 60 strict suites:55 passed,5 failed; no skips. Individual pytest progress lines interleave across the three workers, so suite JUnit verdicts are the evidence boundary.

Four Book suites fail through the shared make_plan fixture's unsupported dict Effect parameters. The fixture now uses the current immutable tuple-of-pairs Effect contract. Production validation is unchanged. The native Book window independently exposed a real label keyword collision in its field helper; caption is now distinct from wx.CheckBox's label argument.

The remaining failed schedule test completed its actual two-step job, then ordinary Resume attempted to save status=running while the old schedule still declared complete. Completed scheduled Resume now rechecks current artifacts under the existing job lock without rerunning or publishing that inconsistent transient record. Additional real-file tamper and cancellation regressions require the original completion record and one-time side effect to remain unchanged.

Actual installed native schedule and structured translation probes both passed. The Book native probe failed during construction. Recording controls35/35, Calibre selection59/59, Calibre process ownership3/3, typed tools26/26, scheduling CLI11/11, scheduling handlers8/8, source translation17/17, translation UI9/9, translation CLI10/10, structured backup3/3 and corpus contracts4/4 passed in this run. These counts overlap broader/source suites and must not be summed as unique release tests.

Repairs are source changes, NOT_RUN until their new exact-head required gates complete. This report does not qualify real Calibre-format conversion, provider translation quality, physical audio or NVDA.
