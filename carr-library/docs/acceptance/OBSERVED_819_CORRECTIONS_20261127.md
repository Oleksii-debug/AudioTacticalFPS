# Observed source819 integration failures and exact correction boundaries

New source is NOT_RUN until its own required Actions jobs complete.

## Actual Windows and Linux observations

- Windows integrated job140526456685 at819d6bd910a92171eacc487806a8968ad25ad485
  completed six of eight strict ASR suites successfully. Historical recovery had
 20 passing and4 failing cases; transcript export had11 passing and1 failing case.
- Linux integrated job140526456707 at the same source completed153 of161 required
  suites successfully with zero skipped cases. Its eight failed suites were the
  same four Book fixture suites, scheduler resume, two ASR suites and desktop
  menu inventory. Per-test terminal lines were interleaved between workers;
  the per-suite JUnit summaries are the source of these counts.
- Windows audio job140526455657 at819 completed55 of60 strict suites; its five
  failed suites were those same four Book fixtures and scheduler resume.
- Windows sync job140534499641 at16207681b7bfda3021cc509dc85a30ad15e0c69d
  reported393 passing and9 failing cases from stale hook/child-origin/menu fixtures.
- Signed-client819 Linux140526455217 and Windows140526455633 each passed all ten
  security suites and failed only the composed desktop menu fixture suite.

These are historical failures and corrections, not inherited PASS for this tree.

## Product fixes

At8911b2, completed scheduler Resume verifies its actual durable checkpoints
without first writing an invalid running/complete schedule state. Native book
dialog construction uses distinct caption and control-label arguments.

At8697ce93e7cbd5fa0e9573e48fb95fa5ef6fe1cd the existing typed inert historical
ASR namespace is dispatched as historical source bytes instead of managed
project PCM. Archive/job schemas, exact sizes and SHA256 remain mandatory.
Negative regressions retain rejection of forged inner history and malformed
current managed WAV. Arbitrary WAV paths receive no new exception.

## Fixture/protocol fixes

The shared Book fixture uses the current typed Effect tuple. Native menu protocol
fixtures include the actual book action and semantic keyboard binding.
The Vault fault hook forwards the current committed callback. Crash children
start isolated and select the parent's exact imported package; exact origin
assertions remain. The transcript export test accepts each actual refusal
branch while requiring no partial output or leftover publication stage.

No runtime security, format or zero-skip gate has been removed.

## Queue publication follow-up

910a80ff43f6f3a7342af3789449b1385b687498 binds temporary queue/PCM files to their
original directory and inode, hashes the staged bytes in bounded blocks, and
rechecks native change metadata at the publisher's final callbacks.
Cleanup removes only the owned stage. Exact commit receipts remain available
after a move followed by uncertain directory flush. A verified retained asset
from a previous failed attempt must successfully sync its directory before
a queue may acknowledge its reference.
