# Google token-renewal native validation

This is a validation contract for the explicit, already-authorized Google Desktop refresh-grant feature. The current corrected script is source-reviewed only; Python parsing, Windows execution and real Google access remain **NOT_RUN** until exact-head CI records them.

## Required installed Windows command

Run from the reviewed checkout with its installed application interpreter:

```text
<installed-python> -I -B scripts/windows_google_oauth_smoke.py \
  --source-root <checkout>/carr-library/src \
  --require-installed --synthetic-transport \
  --output <new-report.json>
```

The interpreter must have the current wxPython and existing application dependencies. The script retains the existing native DLL activation handle until all host cleanup has completed. It needs an interactive Windows desktop, but no audio device, microphone, Office installation, Google account or external native runtime.

The selected source directory must be the exact checkout under qualification. All 18 runtime module files are compared byte-for-byte by SHA256 with that source before and after the run. With --require-installed, every file must be under the selected installed program directory or interpreter's site-packages and outside the source directory. Source mode may omit --require-installed; it does not qualify installed imports.

## Actual and synthetic boundaries

The actual current DesktopFrame opens the actual provider-settings dialog. Real keyboard input drives the selected account, new renewal form, Cancel, default-No confirmation, explicit Yes and the local configuration check. ProfileStore writes actual temporary files. A changed account during the real confirmation must prevent credential replacement.

The credential vault is an explicit in-memory fixture, so this run never opens or changes Windows Credential Manager. Both HTTPSConnection endpoints are controlled responses. Direct socket connections and name resolution are denied. No token, account grant, secret or document is sent to a real service.

After native configuration, seven bounded operations use the existing frame.run_job and its one worker: LOCAL_ONLY denial, missing-approval denial, two separately approved synthetic translations, removed-credential denial, cancellation immediately after vault read, and a redacted HTTP 400 refresh refusal. The actual Gateway, token cache and usage ledger execute. The required transcript is two synthetic token requests and two synthetic translation requests; a refresh failure remains not_sent and never triggers automatic translation retry.

An additional actual native failure-cleanup fixture opens a nested modal and its native MessageBox. Cleanup must answer only that process's exact owned question with No, leave the suspended parents intact, then close only the captured active modal on a later event turn. It does not call EndModal on every open window. A 180-second watchdog covers all work, window closure, host finalizers and native-runtime release; a cleanup failure cannot become PASS.

## Strict acceptance

Require process exit zero, an exclusively created output JSON report, and equality between that report and the final JSON object on stdout. Stdout is ASCII-safe; the report file is UTF-8.

Require all 32 unique named checks from the companion source manifest, all 18 exact module-origin records, four completed actual renewal forms, two synthetic token requests and two synthetic translation requests. These fields must be PASS:

- actual_windows_controls
- actual_profile_store
- actual_gateway_and_ledger
- synthetic_vault_and_https
- shared_host_worker
- native_cleanup

These fields must remain NOT_RUN:

- real_google
- real_credential_manager
- manual_nvda

The 49 core and 15 UI cases remain mandatory as whole source and installed suites. Their fixtures are controlled protocol/widget evidence. The native script complements them with actual Windows controls and the existing host worker; it does not qualify a real Google account, provider availability, charges, language quality or screen-reader behavior. No test or dependency is added to permit real network access.

## Reports and secrets

Only source and bounded JSON/text validation evidence belong in CI artifacts. The script scans its actual profile/usage JSON for the synthetic secret markers and checks that errors/status never include a refresh grant, desktop secret or access token. No real secret is required or accepted by the script's fixed fixture setup.

Google grant issuance, user consent, account eligibility and live translation remain separate operator-controlled qualification. This feature uses only an explicitly supplied existing grant; it does not open a browser, discover ADC, enable an account automatically or transfer vault references through profile export.
