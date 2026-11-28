# One-off Windows CI audio endpoint evaluation

This is an isolated test-first experiment for branch
carr/ci-endpoint-evaluation-20261127. It is not an application feature,
redistribution package, recurring CI dependency or general driver installer.
The production reader probe remains unchanged.

## Original source and terms

Only the package linked by the publisher's [VB-CABLE page](https://vb-audio.com/Cable/) is allowed:

- URL: https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip
- Exact size: 1,324,680 bytes.
- SHA-256: bc44e8974191a5a2427d6c979e2dab08dbb09925f3f435dfa2f0ba3e45b6eb90.
- The adjacent pins document records all 31 archive members, the four staged
  file hashes and exact accepted signing-certificate hashes.
- Original readme.txt SHA-256:
  f865f3e78e37006d48e56c93f51eff4ca79acda9969854400d79bfa3db38a8d5.

VB-CABLE is copyright Vincent Burel / VB-Audio and is donationware. Consult the
[publisher's licensing terms](https://vb-audio.com/Services/licensing.htm) and
the original notice retained unchanged in the downloaded archive.
The publisher permits testing before deciding to obtain a license;
professional/organizational use has separate paid-license requirements.
This one-off test-first authorization does not authorize recurring unlicensed
installation or product redistribution. No payment, purchase, account
registration or contact with the publisher is performed.

The package is not copied into the repository, application, installer or
uploaded CI artifacts. The original notice is verified in memory. No modified
vendor package is distributed.

The vendor requires reboot after installation. Any Windows restart request
fails this job. A no-reboot result plus actual probes qualifies only the
observed endpoint in this disposable VM, not the full vendor-supported
installation lifecycle or a general exception to the reboot instruction.

## Preconditions and ordinary Windows policy

The helper requires the exact branch/ref/repository, GITHUB_ACTIONS=true,
RUNNER_OS=Windows, RUNNER_ENVIRONMENT=github-hosted, RUNNER_ARCH=X64, a permitted
branch-push event and a native 64-bit Windows process. Environment variables
identify this narrow experiment; they are not an OS security sandbox.

The caller must already be elevated. No elevation, service startup-policy
change, Secure Boot/code-integrity change, test signing, certificate import,
trust-store change or vendor EXE execution occurs.

The exact official HTTPS origin is used without proxy fallback or redirects.
Size/hash are checked before ZIP parsing. The bounded complete inventory
rejects aliases, traversal, NUL truncation, symlinks, encryption and unsupported
compression. Only the pinned INF, x64 SYS, passive ARM64 SYS and CAT are staged.
The ARM64 file completes the INF's declared SourceDisksFiles inventory; the
process, selected install section, devnode and installed-driver hash remain
strictly x64. No icon, vendor executable or coinstaller is referenced by the
active install sections.

Windows read handles deny write/delete sharing on staged inputs throughout
signature verification and installation. A fixed PowerShell verifier calls
Get-AuthenticodeSignature with no execution-policy override. Both SYS files and CAT must
return Valid and an exact reviewed certificate SHA-256 plus publisher name.
Each SYS permits its reviewed vendor or nested Microsoft signer; CAT requires its
reviewed Microsoft Windows Hardware Compatibility Publisher signer.
INF is pinned data, not a separately embedded-signed executable; normal
Windows catalog/package verification still applies during installation.
No exception is made if trust or installation fails.

Static OpenSSL inspection does not establish Windows Authenticode, revocation
or kernel acceptance. Those remain unqualified until the actual Windows test.

## SetupAPI contract

The original INF declares hardware ID VBAudioVACWDM, MEDIA class GUID
{4d36e96c-e325-11ce-bfc1-08002be10318}, version3.5.2.0 dated2026-10-31, and service
VBAudioVACMME. It is a type1 kernel driver with demand-start3 and normal error
control1, located at %13%\vbaudio_cable64_win10.sys. Standard system ks.inf /
wdmaudio.inf registrations provide audio interfaces.

Existing devices, including nonpresent instances, are enumerated before
download and again before registration. An existing matching hardware ID
prevents installation. An existing audio output also refuses this experiment:
it is specifically for the observed zero-device runner.

The sequence follows Microsoft's
[DevCon source](https://github.com/microsoft/Windows-driver-samples/blob/main/setup/devcon/cmds.cpp),
reviewed Git blob0002b393a58cbd9f456c41c28292bbe22baa579e:

1. SetupDiGetINFClassW and exact class validation.
2. SetupDiCreateDeviceInfoList and generated root device information.
3. Double-NUL UTF-16 hardware ID property, without prepending ROOT.
4. SetupDiCallClassInstaller with DIF_REGISTERDEVICE.
5. Both DI_NEEDRESTART and DI_NEEDREBOOT checked.
6. UpdateDriverForPlugAndPlayDevicesW with INSTALLFLAG_NONINTERACTIVE only,
   no FORCE, and a non-null reboot-output BOOL.
7. Reboot output and both install flags checked again.

The helper restores only its own prior noninteractive SetupAPI mode.
Required UI, access denial, signature failure or reboot is a failure.
NONINTERACTIVE can leave partial state when UI is required; the receipt
retains that fact. There is no retry with weaker settings.

Microsoft references:

- https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupdicreatedeviceinfow
- https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupdisetdeviceregistrypropertyw
- https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupdicallclassinstaller
- https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupsetnoninteractivemode
- https://learn.microsoft.com/en-us/windows/win32/api/newdev/nf-newdev-updatedriverforplugandplaydevicesw

## Actual acceptance and retained state

The installed devnode must start with no problem code. Installed INF/version/
provider/service, DriverStore path and installed SYS bytes must match the
reviewed package; the receipt includes both installed INF and SYS hashes.

Both actual Pack46 endpoints and WAVE_MAPPER must accept the unchanged reader
fixture: PCM16 mono16000Hz, block alignment2, average bytes per second32000.
This is a real WinMM query. The subsequent unchanged installed reader probe
provides actual MCI playback/pause/loop/sleep/cleanup evidence. The helper alone
does not claim playback or audible physical-device output.

Run45959125829 verified both vendor SYS signatures and the Microsoft hardware
catalog, installed the exact INF/SYS, and observed a started/problem-free devnode
with no restart flags. Its final endpoint-selection assertion failed because
Pack46 exposes WAVEOUTCAPSW names `Speakers (VB-Audio Virtual Cabl` and
`CABLE In 16 Ch (VB-Audio Virtua`, rather than the older `CABLE Input` prefix.
The bounded evaluator now requires that exact two-name set on the initially
empty VM and rejects extra or duplicate names/IDs. Format queries and the real
reader playback gate remain unchanged and were NOT_RUN in that failed run.

Every runtime phase writes a durable JSON receipt. A180-second whole-helper
watchdog writes FAILED before terminating; the outer workflow is also bounded.
A successful or partial device/package remains installed until GitHub disposes
of the VM. Closing handles is not uninstalling it. No reboot, retry or automatic
device/package removal is attempted. Temporary download/staging files are
removed during normal Python unwinding. Only receipts are uploaded.

## Portable contract tests

tests/ci_endpoint/test_endpoint_evaluation.py exercises refusal before side
effects, durable errors, ZIP/hash/name/size boundaries, exact signer/status
rejection, restart flags, NONINTERACTIVE/no-FORCE dispatch and the exact WinMM
wire format. Controlled native-call fixtures never install a Windows driver
and do not substitute for the one-off Windows qualification.

The signature subprocess removes only its inherited PSModulePath variable.
This is Microsoft's documented correction for a PowerShell7 -> Python ->
Windows PowerShell5.1 process chain: Windows PowerShell reconstructs its own
normal module search path instead of loading incompatible PowerShell7 modules.
No execution policy, trust store, machine environment or signing rule changes.
The parent environment is retained unchanged. Reference:
https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_psmodulepath?view=powershell-7.6#starting-windows-powershell-from-powershell-7

