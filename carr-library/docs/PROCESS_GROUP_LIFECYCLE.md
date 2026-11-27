# Owned process-group lifecycle

The shared runner in operations/process.py now retains its Linux session leader
until the inherited process group stops executing. It observes exit with waitid
and WNOWAIT, requests termination, confirms the group's terminal state through
/proc, and only then reaps the leader and accepts output. Keeping the leader
waitable prevents its owned PID/group identity being reused during this cleanup.
The runner closes group execution before waiting for diagnostic reader threads:
a child can otherwise keep stdout or stderr open after its parent exits.

This adapts the bounded Linux observation from the reviewed public change
[0484f51](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/0484f51a3a19b583d20cf95e3c788cab5bbc1ac7)
into the existing runner. It does not install another process or resource framework.

## Limits of the guarantee

- On Linux, normal return requires that no observed member of the inherited
  process group is still executing. Terminal zombies do not execute or keep
  diagnostic streams open. Descendants that deliberately escape the group are
  outside this mechanism; this is not a native-code sandbox.
- A shutdown that cannot be confirmed raises ProcessError and cannot return a
  successful ProcessResult. Forceful termination is still attempted and the
  direct child is reaped where possible. Startup/drainer cleanup remains owned
  by this same runner.
- ResourceBroker retains its existing context-manager policy. Its reservation
  remains held during successful native cleanup, but it releases when an error
  unwinds the context. This change does not claim a retained reservation after
  an unconfirmed shutdown or an unconditional guarantee across parent failure.
- Windows keeps the existing direct-process terminate/kill path. This runner
  does not currently use a Windows Job Object, and the Linux descendant result
  is not evidence of a Windows process-tree guarantee.

## Focused verification

The Linux-only test file tests/operations/test_linux_process_group.py starts a
real leader and a synchronized child that ignores SIGTERM and keeps inherited
pipes open. Both successful and rejected leader outcomes must stop that child
before return/error. A separate failure-injection case verifies that inability
to confirm shutdown cannot return a normal result. No engine, model or private
source is used.

The dedicated workflow also requires the existing native startup-cleanup,
resource-admission, transcription cancellation and verified-recovery suites.
Windows runs its existing startup/resource contracts without selecting the
Linux-only suite. Execution verdicts belong to the exact workflow run, not to
this source description.
