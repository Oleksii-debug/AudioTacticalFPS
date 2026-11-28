# Standard installer: empty maintenance directory ownership

Date: 2026-11-28. This source follow-up is based on
8415622a8f68c8c01f4f0e5f8ad426f8847d56f1, tree
b8835418b56cf6730087d90f3c47363f7d679575. It changes the standard
setup template, its existing native acceptance diagnostics and focused tests.
It does not change application modules, runtime pins, integrity checks,
process ownership, per-operation deadlines or workflow selectors.

## Actual failure and retained successful evidence

The historical 325ccde85c7a8111465df4231ea97f573e4ec52d MAIN run
[46112989710](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46112989710),
Windows job 140880448807, completed FULL step 45 successfully. Its actual
embedded execution passed the five UTF-8 fixture regressions under cp1252
with utf8_mode=0, 31 loopback cases, 171 installed Sync cases, and 4,881
bulk cases with zero failures or skips. The 158 required-name guard,
private DLL checks, pre-bulk sealed inventory and final sealed inventory
passed. The bulk phase took 3,058.81 seconds; FULL took 56 minutes 54 seconds.

The final tested application wheel receipt binds source 325ccde8, tree
45270d0d8494c13d1f95a8e052851881c944a36a, version 0.2.0.dev3,
447 payload files and 378 declared Python files to wheel SHA-256
7bc318c19e4322856fc092f6547a4bbe4054eb411c0bae4dc008373d5810d32a.
The complete bundle manifest SHA-256 is
ad2579580fbc499f542878bebb7012f0b483bd7a284351f8b51ba35bd9439225.
The exact generated standard setup was 893,291,920 bytes with SHA-256
e975310472dcf1e54fbc806fff4a983bac90bd441f91c7fe0a3710a906fca434.

Step 46 then failed at the unchanged final native assertion:
"Normal uninstall left program files or registration."
A completed standard install/repair/uninstall qualification was therefore
not obtained. Passing CORE Managed update evidence is separate from this
FULL bundle's standard setup.

The preserved artifact is 15255880074, 2,604,784 bytes, SHA-256
5e6455966af31a4140ff1a576da2a1474faabaeb2e8455e98715541928aa0fd0.
An isolated read-only diagnostic run
[46126166164](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46126166164),
job 140921128269, at source aa538a770471b50a3faf628a65e9f0b75a4d4300,
verified that exact artifact's run, head, size and SHA before reading it.
It did not extract the ZIP or execute another installation.

The current 8415622a MAIN run
[46118361007](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46118361007),
Windows job 140897139248, subsequently passed FULL as well: 4,881 bulk cases
in 3,026.89 seconds, early five, 31 loopback, 171 Sync, required-name and
private-DLL guards, and both sealed-inventory checks. Its distinct final
tested wheel SHA-256 is
780ba268cb76896b4a6ab2cf96968453a9f1da4aadede4e40462b087eb5d2ec1;
the complete manifest is
42cc5669ca0ab8ec2392c711c2761354bcbfb45b75fa79145170e2248991a6d6.
Its 893,292,656-byte setup
263e2696a8ca6a70e2522bb43fa13c5a76ea5fe1f6150e735cb7c0187ce9fde4
reached the same final normal-uninstall assertion. Artifact 15256022926
is 2,604,888 bytes with SHA-256
0866c39d616d2d2aeffc330d3a5770df5acbcf98c76d392ff3a48751ee553f85.
This current terminal result is separate from the historical artifact whose
normal uninstall logs were read directly by the read-only diagnostic.

## Evidence for the ownership correction

The complete 7,058-byte normal uninstall log has SHA-256
1f976db2404265f942495a6b4c98beba902f190fa1a315e2ee65e7f711eaec29.
It reports successful helper check and uninstall, deletion of the uninstall
registration, all seven maintenance Python files and the cached setup, and
successful Inno completion without a restart. The helper's PASS is reached
after removal of the app tree. The log explicitly records Start-menu
directory removal; it records no attempt to remove the maintenance directory.
The normal uninstall process log is empty. No final residual inventory was
captured, so the directory's final emptiness was not directly measured.

The source contract explains the missing directory ownership:
maintenance.install creates the fixed maintenance directory before Inno
processes its file entries. The helper removes the exact owned app tree and
leaves wrapper cleanup to Inno. The template did not register the
precreated maintenance directory for empty-directory cleanup.
The [official Inno Dirs contract](https://jrsoftware.org/inno-setup/documentation/topic_dirssection.htm)
states that uninsalwaysuninstall attempts removal only when a directory is
empty, including a directory that existed before the file installation.
The [official FAQ](https://jrsoftware.org/inno-setup/faq) also explains
that ordinary uninstall preserves directories not created by Inno.

The correction declares exactly the fixed {app}\maintenance directory with
uninsalwaysuninstall. It adds no recursive deletion, wildcard, app-root or
user-data deletion. Nonempty directories retain their contents. Existing
helper hash/inventory checks, maintenance lease, second-phase process wait,
original failure conditions and all later native acceptance checks remain.

## Focused proof and remaining execution

The generated-template test runs the actual builder, requires exactly that
one empty-only directory entry, and retains the existing prohibition on
UninstallDelete and filesandordirs. All existing 96 standard-installer cases
and their assertions are preserved. Two additional metadata-diagnostic
cases use real temporary files, enforce the 32-entry scan result and bounded
JSON output, forbid content reads, and exercise a reparse-root refusal.

After the original native failure condition, diagnostics now record the
observed registration presence, app/maintenance path kinds and at most 32
top-level names of at most 128 characters. They read no file contents,
perform no recursion, do not traverse a detected link/reparse root, and
write a bounded JSON report plus console text. Diagnostic errors cannot
turn the original acceptance failure into a successful result.

This follow-up is source-reviewed only at creation: new tests and the
corrected native Inno lifecycle are NOT_RUN. It does not relabel the
historical 325 evidence as the current wheel, claim the full lifecycle
passed, or add Authenticode, public redistribution, unprivileged-account,
physical-audio or NVDA qualification.
