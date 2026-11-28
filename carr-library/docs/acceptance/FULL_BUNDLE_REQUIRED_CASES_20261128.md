# FULL bundle required cases — 2026-11-28

## Observed run and scope

MAIN source `9bf41c9245ed5ca3503680b4ddd24bf44b9b4ca6`,
[run 46095245116](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46095245116),
ended its installed bulk suite with 4,875 PASS, five fixture encoding failures
and zero skips. Because pytest failed, the following final JUnit requirement
assertion did **not** execute. Standard setup also did not run.

The separate UTF-8 fixture checkpoint retains those observations. This note
records four additional source-proven issues in the final gate, not additional
failures observed in that execution.

## Independently reviewed exact corrections

The complete audit of the original 152 required JUnit names found:

- 137 names observed passing in the actual source log.
- 11 runtime-only semantic/OCR cases with verified source definitions and
  selected modules; their presence is not a new FULL execution claim.
- Three bare names that could not match their parameterized JUnit names.
- One required case whose module/node was missing from the FULL selection.

The reviewed workflow replaces the three bare requirements with all nine exact
parameterized names:

| Existing base name | Required parameter suffixes |
| --- | --- |
| `test_large_v6_real_restore_resume_keeps_exact_owned_source` | `[prepared]`, `[partial]`, `[complete]` |
| `test_late_queued_save_cannot_cross_changed_or_closed_owner` | `[scope]`, `[same_root_app]`, `[root]`, `[close]` |
| `test_actual_hook_preserves_original_and_outside_text_through_apply_and_undo` | `[crlf-native]`, `[lf-native]` |

It also adds this exact selected node immediately after
`tests/audio/test_recording_controls.py`:

`tests/ui/test_recording_dialog.py::test_pause_request_is_not_acknowledged_until_actual_worker_notice`

The resulting inventory is **158 unique required names and 205 selectors**:
147 names observed passing in the source log plus the 11 verified runtime-only
definitions, with zero unresolved mappings. Matching remains exact; no prefix
matching or relaxed acceptance replaces the original assertions.

## Companion workflow and execution boundary

Reviewed workflow chain:

- `f71555240603b6db402c8082c7cdade74b6e1e2f`: executes the five actual encoding
  regressions early in the same installed embedded Python using `-I -B`,
  without a UTF-8 mode override. It retains installed-origin verification,
  the active firewall, separate JUnit, exact collection and refusal of skips,
  failures, XFAIL and XPASS.
- `eee2584a12c58f744f9e361c7d3265c244638473`: applies the exact requirement
  expansions and missing selected node described above.

Root and independent peer review verified the decorators, actual source PASS
entries, selector coverage and the complete requirement inventory. Other bulk
selectors, firewall rules, budgets, runtime profiles and dependency pins remain
unchanged.

The new focused gate, corrected final JUnit assertion, fresh complete FULL run
and standard setup install/repair/uninstall are **NOT_RUN** at this checkpoint.
No prior partial result is relabelled as their successful execution.
