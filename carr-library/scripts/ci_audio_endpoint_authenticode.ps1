param([Parameter(Mandatory=$true)][string]$InputPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$raw = [System.IO.File]::ReadAllText($InputPath, [System.Text.Encoding]::UTF8)
if ($raw.Length -gt 32768) { throw 'Signature request exceeds its bound' }
# Windows PowerShell5.1 emits a JSON array as one pipeline object; wrapping
# that pipeline in @() adds an unwanted outer array. Direct assignment also
# works with PowerShell7, whose default pipeline enumeration differs.
$items = ConvertFrom-Json -InputObject $raw
if ($items -isnot [array] -or $items.Count -ne 3) {
    throw 'Exactly both SYS files and CAT signature targets are required'
}
$allowed = @('vbaudio_cable64_win10.sys', 'vbaudio_cable64arm_win10.sys', 'vbaudio_cable64_win10.cat')
$seen = @()
# Validate the complete request before performing even the first trust query.
foreach ($item in $items) {
    if ($null -eq $item -or $item -isnot [System.Management.Automation.PSCustomObject]) {
        throw 'Signature target must be an object'
    }
    $names = @($item.PSObject.Properties.Name)
    if ($names.Count -ne 2 -or $names -cnotcontains 'file' -or $names -cnotcontains 'path' -or
        $item.file -isnot [string] -or $item.path -isnot [string]) {
        throw 'Signature target requires only string file and path fields'
    }
    if ($allowed -cnotcontains [string]$item.file) { throw 'Unreviewed signature target' }
    if ($seen -ccontains [string]$item.file) { throw 'Duplicate signature target' }
    $seen += [string]$item.file
    if ([System.IO.Path]::GetFileName([string]$item.path) -cne [string]$item.file) {
        throw 'Signature target basename differs'
    }
    if (-not [System.IO.Path]::IsPathRooted([string]$item.path)) {
        throw 'Signature target must be absolute'
    }
}
$reports = @()
foreach ($item in $items) {
    $signature = Get-AuthenticodeSignature -LiteralPath ([string]$item.path)
    $fingerprint = ''
    $simpleName = ''
    $subject = ''
    if ($null -ne $signature.SignerCertificate) {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $fingerprint = ([System.BitConverter]::ToString(
                $sha.ComputeHash($signature.SignerCertificate.RawData)
            )).Replace('-', '').ToLowerInvariant()
        } finally {
            $sha.Dispose()
        }
        $simpleName = $signature.SignerCertificate.GetNameInfo(
            [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false
        )
        $subject = $signature.SignerCertificate.Subject
    }
    $reports += [ordered]@{
        file = [string]$item.file
        status = $signature.Status.ToString()
        status_message = [string]$signature.StatusMessage
        certificate_sha256 = $fingerprint
        simple_name = $simpleName
        subject = $subject
        signature_type = $signature.SignatureType.ToString()
    }
}
ConvertTo-Json -InputObject $reports -Depth 4 -Compress

