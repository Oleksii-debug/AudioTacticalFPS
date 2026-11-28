# Final vault-comparison deadline fixture — 2026-11-28

## Actual observed result

Exact source `e828403becce67eead4f7def3ec826f7893a2970`,
Trust [run 46104046650](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46104046650),
Windows job `140852688628`, completed with 216 PASS / 1 FAIL / 0 SKIP
across the required source suites. The publication-integrity suite accounted
for 17 PASS / 1 FAIL. The Windows vault and native-status steps were not reached.

The deadline variant of
`test_stop_during_final_canonical_vault_compare_prevents_protected_put`
failed at `entered.wait(1)`: it did not reach the second canonical vault read.
The fixture started a real 50 ms deadline before provider response verification
and durable staging, then assumed those operations would finish
before its intended final-comparison barrier. The production guard can
correctly expire before that barrier; this failure does not demonstrate a
protected publication after an expired deadline.

Artifact `15246777382`: 9,057 bytes, SHA-256
`881d7aaa1e8f968de88a08be67a944fb2e94973528bc56e1423c330d15ba786b`.

## Narrow fixture correction

Only the deadline variant replaces the `refresh.time` module binding with a
proxy containing a controlled `monotonic` function. It does not mutate the
global time module or change production code. The real worker, vault events,
1-second arrival bound, 2-second release/join bounds and 50 ms request budget
remain. The separate close variant retains its 20-second budget.

The controlled clock reaches exactly the inclusive deadline only after the
real second vault-read barrier is acknowledged. Before cleanup can call
`worker.close()`, the test requires that the production guard actually read
the expired clock and that the worker terminated with its stop/cancel events
still unset. Existing anchor-sequence, exact current-grant bytes and protected
put-count assertions remain unchanged.

The source change is **NOT_RUN**. Fresh required Trust execution on both
platforms, including the Windows vault and native-status stages, remains
necessary. No earlier source's successful result qualifies this checkpoint.
