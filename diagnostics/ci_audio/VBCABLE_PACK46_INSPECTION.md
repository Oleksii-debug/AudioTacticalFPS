# VB-CABLE Pack46: inspection-only Windows diagnostic

## Purpose and current status

This separate diagnostic collects evidence about the exact official **VB-CABLE
Driver Pack46** archive on the same **windows-2022** runner family used by the
application's Windows gate. It is not an application test, driver installer,
approved dependency lock, or workaround for a failed audio test.

**Prepared source, not yet executed.** The archive SHA-256, byte count, package
signatures and actual Server policy results will be measured by the diagnostic.
They are deliberately not invented or borrowed from a mirror.

The previously observed application failure was a runner with zero waveOut
devices despite running Windows Audio services. A package inspection cannot
create an endpoint or turn that failed application run into a pass.

## Exact acquisition and retained evidence

The only package URL admitted is:

https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip

The official [product page](https://vb-audio.com/Cable/index.htm), reviewed on
2026-11-27, links that filename and labels it NOV 2026. Each run records the
actual HTTPS response URL, redirects, headers, size and archive SHA-256. The
initial measured hash is evidence for a subsequent review, not prior approval.
The archive cannot redirect to a mirror or a different path.

The inspector validates the complete ZIP namespace before extraction. It rejects
links, encrypted members, path traversal, Windows special names, case/Unicode
aliases, ancestor collisions and unsupported compression. Fixed limits are
16 MiB archive, 256 members, 32 MiB per member, 64 MiB expanded, 16 path
components and 1,024 implicit/explicit namespace entries.

Every member receives an exact SHA-256/size inventory. Only EXE, CAT, INF and SYS
members are extracted to a newly owned temporary directory. They are treated as
data. Text/INF inspection is bounded to 1 MiB per member. PDF members receive
hash/size evidence only; their text is explicitly marked unexamined.

The report retains relevant readme/license keyword locations and line hashes,
with at most 25 words quoted per document. It separately records INF Version
fields, Manufacturer/model decorations, hardware-ID rows, catalog directives and
SYS basename candidates. This is lexical data inspection, not INF execution or
an assertion that a particular OS decoration is installable.

## Signature and OS evidence

The read-only metadata helper records the actual OS caption, version, build,
architecture and Server product type; audio-service and sound-device metadata;
waveOut device count; available read-only Secure Boot/Device Guard observations;
and Authenticode status, signer and timestamp certificate metadata for every
extracted signature target.

It locates an **already installed x64 Windows SDK SignTool** under the fixed
Windows Kits directory. The tool must have valid Microsoft Corporation
Authenticode identity; its actual version, size and SHA-256 are recorded and
rechecked before use. No signing tool is downloaded from the package or PATH.

Only the SignTool **verify** operation is permitted:

| Inspection | Policy and binding |
|---|---|
| Setup executable embedded signatures | `verify /pa /all /v /o 2:actual-major.actual-minor.actual-build` |
| CAT and SYS embedded signatures | `verify /kp /all /v /o 2:actual-major.actual-minor.actual-build` |
| Each explicit CAT against each INF/SYS | `verify /kp /v /o 2:actual-major.actual-minor.actual-build /c exact.cat exact-member` |

The complete bounded CAT/member matrix avoids selecting a convenient catalog.
Different architecture/catalog pairs may correctly fail membership. Results
remain distinct: exit 0 verified, exit 2 warning, exit 1 not verified. No warning
is converted into success; unexpected exits, missing required tools, timeout or
incomplete coverage fail the inspection. The original verifier output and
actual exit code are preserved with bounded text capture.

The Windows trust APIs may perform their normal certificate/revocation
resolution. The scripts do not import certificates, add catalogs to a trust
database, sign files, change certificate or code-integrity policy, enable test
signing, or modify Secure Boot. They do not start, stop or reconfigure services.

**Authenticode or /kp results alone do not establish Windows Server HLK/product
support, successful PnP installation, driver load, or a working audio endpoint.**
The report keeps these verdicts separate.

## Installation, reboot and licensing boundary

The [official product instructions](https://vb-audio.com/Cable/index.htm) say to
extract the package, run setup as administrator, and restart after installation
or uninstallation. This diagnostic performs none of those installation actions.

No unattended switch is guessed or approved. Keyword matches in package text
are review candidates; the original setup is never invoked even for help.
The previously reviewed OCT2024 manual is older than Pack46 and is not proof
of Pack46 filenames, switches or signatures.

The [licensing page](https://vb-audio.com/Services/licensing.htm) and
[general terms](https://shop.vb-audio.com/en/content/3-Terms-of-use) must be
reviewed for the intended use. Evaluation guidance does not establish indefinite
free professional CI or a per-ephemeral-runner exemption. This work neither
redistributes the driver nor decides recurring licensing.

A subsequent installation experiment requires a separate reviewed package lock,
supported vendor installation contract, actual Server signing/support evidence,
and a legitimate restart plan. If the disposable hosted job cannot satisfy the
required restart, a supported provisioned image is a different later decision.
No security-policy relaxation or synthetic endpoint result is permitted.

## Running and reading the dedicated diagnostic

The workflow is
`.github/workflows/carr-vbcable-pack46-inspection.yml`.
It accepts pushes only on branches matching
`carr/ci-audio-endpoint-inspection-*` and can be manually dispatched on such a
branch. It does not run on the application integration branch or pull requests.
Checkout is restricted to the diagnostic sources and this workflow.

There are no application imports or application assertions. The workflow first
parses only its own Python/PowerShell sources, then runs the inspector with
`python -I -B`. Total inspection time is bounded to 600 seconds and the job to
20 minutes. Each verifier process has its own bounded output/time; failure
cleanup terminates only the retained Popen process handle.

Only these fresh, bounded UTF-8 artifact members are admitted:

- `inspection.json`: archive/member identities, bounded document/INF evidence,
  actual Windows and signature observations, errors and explicit qualification
  limits.
- `SUMMARY.txt`: inspection outcome and a reminder that no installation,
  endpoint or application qualification occurred.

The workflow checks exact filenames, schema/source binding, UTF-8, file types
and size before upload. The package ZIP, extracted binaries, catalogs, SYS/INF,
certificates and temporary process logs are not uploaded. Owned temporary
payloads are removed; a separate always-run cleanup verifies the exact run's
ownership record before deleting its directory.

A green diagnostic means **COMPLETE_FINDINGS_REQUIRE_REVIEW**. It is not a
signature approval, driver qualification or application PASS. Nonmatching
catalog pairs and other verifier failures remain visible findings. Any partial
inspection is a failed job with its text diagnostics retained when valid.

## Primary references

- [VB-Audio product/download and installation instructions](https://vb-audio.com/Cable/index.htm)
- [VB-Audio licensing](https://vb-audio.com/Services/licensing.htm)
- [VB-Audio general terms](https://shop.vb-audio.com/en/content/3-Terms-of-use)
- [VB-CABLE reference manual; release identity must be checked](https://vb-audio.com/Cable/VBCABLE_ReferenceManual.pdf)
- [Microsoft SignTool and explicit OS/catalog verification](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
- [Microsoft driver SignTool /kp and PnP policy](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/signtool)
- [Microsoft driver signing offerings and Windows Server requirements](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/driver-signing-offerings)
- [Microsoft Get-AuthenticodeSignature behavior](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/get-authenticodesignature)
