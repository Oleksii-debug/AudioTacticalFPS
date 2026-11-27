param([Parameter(Mandatory=$true)][string]$InputPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$raw = [System.IO.File]::ReadAllText($InputPath, [System.Text.Encoding]::UTF8)
if ($raw.Length -gt 32768) { throw 'Signature request exceeds its bound' }
$items = @($raw | ConvertFrom-Json)
if ($items.Count -ne 3) { throw 'Exactly both SYS files and CAT signature targets are required' }
$allowed = @('vbaudio_cable64_win10.sys', 'vbaudio_cable64arm_win10.sys', 'vbaudio_cable64_win10.cat')
$reports = @()
foreach ($item in $items) {
    if ($allowed -cnotcontains [string]$item.file) { throw 'Unreviewed signature target' }
    if ([System.IO.Path]::GetFileName([string]$item.path) -cne [string]$item.file) {
        throw 'Signature target basename differs'
    }
    if (-not [System.IO.Path]::IsPathRooted([string]$item.path)) {
        throw 'Signature target must be absolute'
    }
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

