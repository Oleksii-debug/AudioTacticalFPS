# Current managed queue, accepted publication and complete corpus restore

This integration extends source 8911b2ae232b4786677890c40560f4393a0202a9.
New changes are source reviewed and integrated; execution for this composition
is NOT_RUN until the exact commit's jobs complete. Earlier results below do not
transfer to this source.

## Managed queue

Reuse the existing Audio Studio project, WavePlayer, native driver, PCM
preparation, executor, Save All coordinator, resource broker and typed backups.
Queue source 4154c478b16f4b3a700ce6a3ae5b26a7d066ca84 includes retained PCM,
explicit stopped restore/resume, actual position evidence, current output choice,
independent-control preferences and exact queue ownership. Historical coordinates
have a distinct evidence label. The canonical old-queue archive/import bridge is
a separate integration still in progress.

Save All settles only after the accepted worker. A destroyed or revoked window
cannot leave it waiting or receive a focus callback. Save As flushes the original
project queue before changing its binding. A move followed by uncertain final
flush retains its exact committed receipt and requires explicit review.
Previously moved PCM must receive a successful assets-directory flush before
its verified reuse can authorize a new queue.

New core/native tests distinguish synthetic saved frames from observed device
position. Physical output and manual NVDA qualification remain NOT_RUN.

## Accepted publication

Publication source 846e150dcafef8a949a9d652192a537bd8881acb preserves the existing
receipt schema. An ordinary late guard cleanup failure returns the exact
durably accepted receipt plus an ephemeral visible warning. An unconfirmed native
family still raises AudioCleanupPending with its original owner. Interpreter
control signals propagate with the committed receipt attached. No retry or
overwrite is implicitly authorized.

The 14 new fault cases use actual filesystem locks, resource leases and rendered
synthetic PCM. They require the selected FFmpeg in source/installed/native jobs;
this source review is not an execution or acoustic-quality claim.

## Actual corpus complete-library cycle

The harness source ae725a3a6f80cbd27580a9fadc7ea2003005fd6c adds explicit
--complete-backup to each 1k/10k/50k matrix case: complete typed snapshot,
full manifest verification, removal only of that harness's private source
library, independent restore, exact ordered identity digest for every catalog
edition, retained-original and source-tail checks, and fresh-process startup.
No model, credential, operating-system or complete-machine migration is implied.
The existing fixed-size fixtures still are not public import throughput.

Previously completed run 46004721023 at source 431ac08712b4c31c70a6253711481c02d092de94
passed the 1,000-book/20,000-segment and 10,000-book/200,000-segment workloads.
Those earlier runs did not contain this added complete-library backup cycle.
Its 50,000-book job was still running at 14:10 UTC on 2026-11-27.

## Observed CI corrections

Windows encrypted-snapshot run 46007562069, job 140534499641, at source
16207681b7bfda3021cc509dc85a30ad15e0c69d reported 393 passed and 9 failed.
The same nine failures occurred at source819d6bd9.
The failures were one stale no-replace fault-hook signature, six crash-child
processes importing the installed package while their parents tested source,
and two menu inventories omitting the newly integrated book action.

The fault hook now forwards the current committed callback. Crash children use
isolated Python startup and an explicit package root derived from the parent's
actual imported module; exact child-origin assertions remain mandatory.
Both mock and real menu compositions include the book action, and the real
keyboard inventory requires audio.book-batch. Source and exact isolated
installed recovery suites remain required in the sync workflow.

The two desktop-menu inventory failures also caused the Linux and Windows
signed-client source819d6bd9 jobs to fail; their ten security suites passed.
Those failures do not establish a trusted-client product defect or a passing
composed UI. Corrected execution remains pending.
