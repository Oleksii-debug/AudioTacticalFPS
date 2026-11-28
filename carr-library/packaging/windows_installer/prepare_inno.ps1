param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
$cursor = $output
while ($cursor) {
    if (Test-Path (Join-Path $cursor '.git')) { throw 'Installer toolchains must remain outside Git worktrees.' }
    $parent = Split-Path $cursor -Parent
    if ($parent -eq $cursor) { break }
    $cursor = $parent
}
if (Test-Path $output) { throw 'Choose a new toolchain directory.' }
New-Item -ItemType Directory $output | Out-Null
$lock = Get-Content (Join-Path $PSScriptRoot 'toolchain.lock.json') -Raw | ConvertFrom-Json
$archive = Join-Path $output 'innosetup.exe'
Invoke-WebRequest -Uri $lock.url -OutFile $archive
if ((Get-Item $archive).Length -ne $lock.bytes -or (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $lock.sha256) { throw 'Inno Setup does not match the reviewed official release.' }
$signature = Get-AuthenticodeSignature $archive
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Pyrsys B\.V\.') { throw 'The pinned Inno Setup toolchain has no valid expected publisher signature.' }
$tools = Join-Path $output 'tools'
$process = Start-Process -FilePath $archive -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',('/DIR="' + $tools + '"')) -PassThru -Wait
if ($process.ExitCode -ne 0) { throw 'Pinned Inno Setup tool installation failed.' }
$iscc = Join-Path $tools 'ISCC.exe'
if (-not (Test-Path $iscc -PathType Leaf)) { throw 'Pinned Inno compiler is unavailable.' }
$receipt = @{ schema=1; version=$lock.version; archive_sha256=$lock.sha256; iscc_sha256=(Get-FileHash $iscc -Algorithm SHA256).Hash.ToLowerInvariant(); upstream_signature='Valid'; publisher='Pyrsys B.V.' }
$receipt | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $output 'toolchain-receipt.json')
$receipt | ConvertTo-Json -Depth 5
