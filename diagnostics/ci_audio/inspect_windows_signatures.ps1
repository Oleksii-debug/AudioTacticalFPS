# Read-only metadata helper for inspect_vbcable_pack46.py.
# This script does not execute any package member or install/register a driver.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InputFile,
    [Parameter(Mandatory=$true)][string]$OutputFile
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Short-Text([object]$Value) {
    if ($null -eq $Value) { return $null }
    $text = [string]$Value
    if ($text.Length -gt 4096) { return $text.Substring(0, 4096) + '[truncated]' }
    return $text
}
function Certificate-Record($Certificate) {
    if ($null -eq $Certificate) { return $null }
    return [ordered]@{
        subject = Short-Text $Certificate.Subject
        issuer = Short-Text $Certificate.Issuer
        thumbprint_sha1 = $Certificate.Thumbprint
        serial_number = $Certificate.SerialNumber
        not_before_utc = $Certificate.NotBefore.ToUniversalTime().ToString('o')
        not_after_utc = $Certificate.NotAfter.ToUniversalTime().ToString('o')
        enhanced_key_usage = @($Certificate.Extensions | Where-Object {
            $_.Oid.Value -eq '2.5.29.37'
        } | ForEach-Object { Short-Text ($_.Format($false)) })
    }
}
function Signature-Record([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    return [ordered]@{
        status = [string]$signature.Status
        status_message = Short-Text $signature.StatusMessage
        signature_type = [string]$signature.SignatureType
        is_os_binary = [bool]$signature.IsOSBinary
        signer = Certificate-Record $signature.SignerCertificate
        timestamp_signer = Certificate-Record $signature.TimeStamperCertificate
    }
}

if (Test-Path -LiteralPath $OutputFile) { throw 'Metadata output already exists.' }
$inputRecord = Get-Content -LiteralPath $InputFile -Raw -Encoding utf8 | ConvertFrom-Json
if ($inputRecord.schema -ne 'resonance-vbcable-signature-input/1') {
    throw 'Unsupported metadata request.'
}
$files = @($inputRecord.files)
if ($files.Count -eq 0 -or $files.Count -gt 64) { throw 'Signature member bound exceeded.' }
$os = Get-CimInstance -ClassName Win32_OperatingSystem -OperationTimeoutSec 15
$record = [ordered]@{
    schema = 'resonance-windows-signature-metadata/1'
    powershell_version = [string]$PSVersionTable.PSVersion
    os = [ordered]@{
        caption = $os.Caption
        version = $os.Version
        build_number = $os.BuildNumber
        architecture = $os.OSArchitecture
        product_type = [int]$os.ProductType
    }
    secure_boot = $null
    device_guard = $null
    audio_services = @()
    sound_devices = @()
    authenticode = @()
    signtool = $null
}
try {
    $record.secure_boot = [ordered]@{
        observed = $true; enabled = [bool](Confirm-SecureBootUEFI)
    }
} catch {
    $record.secure_boot = [ordered]@{
        observed = $false; reason = 'Read-only Secure Boot query unavailable.'
        error_type = $_.Exception.GetType().FullName
    }
}
try {
    $guard = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard -OperationTimeoutSec 15
    $record.device_guard = [ordered]@{
        observed = $true
        virtualization_based_security_status = $guard.VirtualizationBasedSecurityStatus
        code_integrity_policy_enforcement_status = $guard.CodeIntegrityPolicyEnforcementStatus
        usermode_code_integrity_policy_enforcement_status = $guard.UsermodeCodeIntegrityPolicyEnforcementStatus
        security_services_configured = @($guard.SecurityServicesConfigured)
        security_services_running = @($guard.SecurityServicesRunning)
    }
} catch {
    $record.device_guard = [ordered]@{
        observed = $false; reason = 'Read-only Device Guard query unavailable.'
        error_type = $_.Exception.GetType().FullName
    }
}
$record.audio_services = @(Get-Service -Name 'Audiosrv', 'AudioEndpointBuilder' |
    ForEach-Object {
        [ordered]@{name=$_.Name; status=[string]$_.Status; start_type=[string]$_.StartType}
    })
$record.sound_devices = @(Get-CimInstance -ClassName Win32_SoundDevice -OperationTimeoutSec 15 |
    Select-Object -First 64 | ForEach-Object {
        [ordered]@{
            name = Short-Text $_.Name
            manufacturer = Short-Text $_.Manufacturer
            status = Short-Text $_.Status
            config_manager_error_code = $_.ConfigManagerErrorCode
            pnp_device_id = Short-Text $_.PNPDeviceID
        }
    })
foreach ($member in $files) {
    $record.authenticode += [ordered]@{
        member = [string]$member.member
        signature = Signature-Record ([string]$member.path)
    }
}

# Inspect only an already installed Microsoft Windows SDK x64 SignTool.
# No PATH lookup, tool download, certificate import, catdb, or signing command.
$sdkRoot = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits\10\bin'
$candidates = @()
if (Test-Path -LiteralPath $sdkRoot -PathType Container) {
    $versions = @(Get-ChildItem -LiteralPath $sdkRoot -Directory)
    if ($versions.Count -gt 128) { throw 'Windows SDK directory bound exceeded.' }
    foreach ($version in $versions) {
        if ($version.Name -notmatch '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$') { continue }
        if (($version.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse SDK version directory is not admitted.'
        }
        $candidate = Join-Path $version.FullName 'x64\signtool.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $candidates += [pscustomobject]@{version=[version]$version.Name; path=$candidate}
        }
    }
}
foreach ($candidate in @($candidates | Sort-Object -Property version -Descending)) {
    $item = Get-Item -LiteralPath $candidate.path
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Reparse SignTool executable is not admitted.'
    }
    $signature = Signature-Record $candidate.path
    if ($signature.status -ne 'Valid' -or $null -eq $signature.signer -or
        $signature.signer.subject -notmatch '(?:^|,\s*)O=Microsoft Corporation(?:,|$)') {
        continue
    }
    $record.signtool = [ordered]@{
        path = $candidate.path
        sdk_root = $sdkRoot
        sdk_version = [string]$candidate.version
        file_version = $item.VersionInfo.FileVersion
        size = $item.Length
        sha256 = (Get-FileHash -LiteralPath $candidate.path -Algorithm SHA256).Hash.ToLowerInvariant()
        signature = $signature
    }
    break
}
$json = $record | ConvertTo-Json -Depth 16
[IO.File]::WriteAllText($OutputFile, $json, [Text.UTF8Encoding]::new($false))
