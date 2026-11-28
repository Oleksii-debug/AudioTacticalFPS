# Frozen e9 full-main failure — 28 November 2026

**Result: FAIL.** The completed Windows integrated job ran all 451 required source selectors: **450 passed and one failed**. Their strict suite receipts record **11,587 passing case events, two failures and no skips**. Both failures are the already identified Windows helper-cleanup fixture precondition; this frozen run did not execute the later fixture repair.

## Exact execution identity

| Field | Value |
| --- | --- |
| Source | `e9bba260fd66cfe5217f8081778fcdfc9dfd202c` |
| Tree | `c6a3df5fe5e6ed328ee6d1552d7360f1b94e0d9e` |
| Run / attempt | [46107707855 / 1](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46107707855) |
| Integrated job | [140864071580](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46107707855/job/140864071580) |
| Source step | 21; 04:37:58–05:24:57 UTC; 46 minutes 59 seconds |
| Job completion | 05:25:06 UTC; failure |
| Main workflow blob | `6a826f25a0a4724b5665b3cfbcf34ab97336971d` |
| Source runner blob | `2d04b9b269ae85c24349ae8d4ad9b295737866fb` |
| Protected oracle blob | `9db65750aa51b1d7011d535ec00274e1b2d828c5` |

The complete decoded job log is 4,777,251 characters / 4,777,257 UTF-8 bytes, SHA256 `402a6f654b469c801466e94a84c22278ca66a769990c449d14ffd589ee05375a`. This is a hash of the decoded text returned by the log tool, not the compressed transport.

## The two actual failures

Both parameter variants of `tests/audio/test_local_voice_helper.py::test_helper_private_directory_cleanup_waits_for_owned_process_and_reader_settlement` failed at line 571:

```python
assert first_refusal.is_set() and released.is_set() and len(attempts) >= 2
```

For both `deadline` and `cancel`, `first_refusal.is_set()` was false. The suite reported **75 passed / two failed / zero skipped**, exit 1. The expected engine exception, elapsed-time check and cleanup observer's settled-process/closed-stream checks preceded this assertion. Assertions after the failure were not completed.

The failed fixture used a metadata-only Windows directory handle with requested access `0x80`; it did not impose the intended deletion-sharing denial. Reviewed fixture-only successor `fe1d4744fb5b758fb006771d1e78280c278f9f7d` requests `0x81` and preserves the application bytes, assertions and case counts. That later repair and its separate qualification are not attributed to this e9 run.

The full failed module traceback excerpt and both case identities are retained in the [machine-readable record](e9-main-observed-failure-20261128.json). There was no additional failed source suite.

## Population reconciliation and one joined output line

The retained workflow's exact 451-selector Counter matches all 451 complete suite receipts. The run contains **11,589 source CASE_START events**, including both failed cases. These are execution events, with directory/file selector overlap retained.

A generic token parser produces **5,271 keys** because this exact parallel-output line concatenates a test function and another suite's header:

```text
2026-11-28T04:48:46.3384035Z CASE_START tests/catalog/test_bibliography.py::test_ris_rejects_multiline_or_control_metadata_but_csl_preserves_ittests\catalog\test_catalog_reports.py 
```

The immutable bibliography source blob `eb75578f8fb6b575c559c4b7c7ecc7ed59bf9478` declares `test_ris_rejects_multiline_or_control_metadata_but_csl_preserves_it` with exactly five literal unsafe-string parameters. Four clean starts exist; the joined line supplies the fifth. There is no `…preserves_ittests` function. The appended header is the retained `tests/catalog/test_catalog_reports.py` selector, source blob `405a6fb0c93b44400a8b9b8560603378694de8ec`.

The JSON preserves the raw 5,271-key Counter and this single explicit correction separately. Moving its one joined token to the actual five-case function gives **5,270 source-correlated keys**, with **11,589 events unchanged**. The resulting complete Counter equals the prior c31 population plus the four reviewed module additions.

| Counter interpretation | SHA256 of compact ASCII-key-sorted JSON |
| --- | --- |
| Raw generic 5,271-key Counter | `4573496dd7479ed305cd5fe89ff98ed007d890446e00834810db3d5c9b97f13a` |
| Explicit source-correlated 5,270-key Counter | `f7a5f4cceff7fde13ddb4276b3747e2fa40528452c024d57cef9541fd4f7a574` |
| Protected Counter, unchanged | `798b61c50977fb8bc6fdd6595c2f62012b4eafb7f231a781fc39ce7d6b4ba8b4` |

All **252 protected modules / 5,947 declared cases** were observed: 5,945 passed and two failed in their direct suite receipts. Their **6,603 weighted starts** match the protected function Counter exactly, without the above correction. The unchanged main artifact reader compares CASE_START only for protected modules; the joined bibliography function is outside that set. No reader or gate exception was added.

**Official protected step 22 was skipped**, as were every main step 22 through 53. Independent source-identity reconciliation does not turn a skipped gate or either failed test into a pass.

## Retained passing scope and unexecuted scope

The early synthetic local-planning loopback gate passed its exact **14 cases**. Within this source run, assistant 656, resources 40, measured-duration 51, production-integrity 62, audio CLI 30 and Audio Studio 105 cases passed with zero failures or skips.

The later installed population, native steps 23–43, complete bundle, standard installation and O→A→B/library qualification were **SKIPPED**. Separate focused, keyboard and auxiliary runs are not transferred to this result.

## Artifact identity and evidence limits

The API and actual upload log independently agree on:

- Artifact `15250485147`: `carr-windows-e9bba260fd66cfe5217f8081778fcdfc9dfd202c-1`.
- Size **300,504 bytes**; upload reported **24 files**.
- SHA256 `f92823a45ae3cac947b2db87aba25f58b951e5b34f2322e7a1f780bacf1cec98`.

No artifact ZIP was downloaded, opened, extracted or executed for this record. The JSON retains all 59 job-step statuses, all 451 source receipts, the original raw function Counter, precise source bindings and both failure tracebacks. This evidence-only component preserves every existing e9 file and changes no application, test, workflow, timeout, oracle or frozen ref.
