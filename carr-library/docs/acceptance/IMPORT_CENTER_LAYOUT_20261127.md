# Native Import Center layout correction — 2026-11-27

## Actual failure

Selected MAIN run [46061873291](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46061873291), Windows job 140717845503, executed source `4b189ea458d8511ac4034cef46a0d966f01f0da3` / tree `186082cca612e57058b162b15b97664641633ca4`. Its 392 required source suites recorded **9,630 PASS / 0 FAIL / 0 SKIP**, including two loopback suites / 45 PASS. The large integrated block itself completed 239 suites / 6,106 PASS. These are overlapping stage observations, not an additional unique-test total.

The next native Import Center workflow failed at `tests/ui/native_import_center_smoke.py:99`: `dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height`. The failure occurred before planning/import; it was not the source-step deadline or the native fixture watchdog. The later full bundle and standard setup steps were **NOT_RUN** in this failed job.

The full terminal log was read independently. Artifact 15230568448 contains 271,053 bytes; SHA-256 `3217290a21bb694f20967068c286013055775a88206108460594e3e62b78fa39`. The archive label contains the PR merge-ref identity; the workflow checkout and evidence remain source 4b as stated above. Runtime: CPython 3.11.9 x64, wxPython 4.2.4 msw / wxWidgets 3.2.8, Windows 10.0.20348.

## Cause and source correction

The constructor stacked the later folder-scan, streamed-text and explicit large-original controls above the saved queues, file list, details and actions inside a fixed 920×760 dialog. Its 800×650 minimum did not admit the resulting unscrolled content. This was a product layout defect; the existing native assertion correctly exposed it.

Product/native-regression candidate: `856af734ee4e1f91adb5a13fb60ecad40d9d616d`, based on `bf0de464e24e9cab05cd55d2b1d128792bf726b3`. The body uses the existing wxPython ScrolledPanel API; status, Cancel and Close remain outside the scrolling body. Named controls, domain handlers, source limits, job ownership, cancellation, durable queue semantics and original-data handling are retained. Focused children scroll into view. Deferred setup must not reset the initial focus on an existing saved batch. No dependency, model or resource-budget change is involved.

The exact installed upstream API was checked at [wxPython 4.2.4](https://github.com/wxWidgets/Phoenix/blob/wxPython-4.2.4/wx/lib/scrolledpanel.py); this reuses an existing dependency. The native regression retains both original sizer-versus-client assertions and adds measured geometry, minimum-window resize, Tab access to a lower action, and saved-batch reopening with visible focus. It continues the real plan, exclude, import, exact search/quote and durable reopen sequence.

## Early native coverage and execution boundary

Workflow candidate: `1d35297ecf1d7487d8b0acdfbfe294da4a0640b2`, based on the same bf0 source. The existing keyboard-native job already installs the 17-core desktop profile, wxPython 4.2.4, pytest/reportlab and the current application, and constructs the same DesktopFrame. The added early source-native Import Center command uses only two synthetic TXT originals; it requests no OCR/model/audio/network capability. It preserves the original full MAIN step and existing job limits, checks, selections and artifact upload. The smoke explicitly prepends checkout source, so it is not an installed-origin proof.

At this source-preparation checkpoint, corrected native and full MAIN execution are **NOT_RUN**. A candidate or static review is not PASS. Later actual runs, exact sources, full bundle/setup outcomes and artifact hashes are recorded in [PR99](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/99). Do not erase the historical failure or substitute the early UI probe for the complete MAIN gate.

Manual NVDA, physical devices and broader display/locale qualification are separate from these automated native controls. Private model qualification, production authority/signing and distribution rights are unchanged.
