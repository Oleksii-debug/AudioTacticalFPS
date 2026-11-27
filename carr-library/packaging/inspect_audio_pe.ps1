# Capture native import-table evidence only; does not execute or distribute FFmpeg.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Runtime,
    [Parameter(Mandatory)][string]$SourceLock,
    [Parameter(Mandatory)][string]$Evidence
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $Runtime).Path
$lockPath = (Resolve-Path -LiteralPath $SourceLock).Path
$lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
$evidencePath = [IO.Path]::GetFullPath($Evidence)
New-Item -ItemType Directory -Force -Path $evidencePath | Out-Null
# Use the runner's existing Visual C++ tool, not a new desktop dependency.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (!(Test-Path -LiteralPath $vswhere -PathType Leaf)) { throw 'vswhere not available for PE evidence.' }
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or @($vs).Count -ne 1 -or !$vs) { throw 'Visual C++ tools not available for PE evidence.' }
$versionFile = Join-Path $vs 'VC/Auxiliary/Build/Microsoft.VCToolsVersion.default.txt'
$vcVersion = (Get-Content -Raw -LiteralPath $versionFile).Trim()
$dumpbin = Join-Path $vs "VC/Tools/MSVC/$vcVersion/bin/Hostx64/x64/dumpbin.exe"
if (!(Test-Path -LiteralPath $dumpbin -PathType Leaf)) { throw 'dumpbin not available for PE evidence.' }
$locked = @($lock.archive_files.PSObject.Properties | Where-Object { $_.Name -match '^bin/[^/]+\.(exe|dll)$' } | Sort-Object Name)
$bin = Join-Path $root 'bin'
$actual = @(Get-ChildItem -LiteralPath $bin -File | Where-Object { $_.Extension -in '.exe', '.dll' } | ForEach-Object { 'bin/' + $_.Name } | Sort-Object)
if (!$locked.Count -or @(Compare-Object @($locked.Name) $actual -CaseSensitive).Count) {
    throw 'Native audio image inventory differs from the source lock.'
}
$images = @()
Push-Location -LiteralPath $bin
try {
    foreach ($entry in $locked) {
        $name = Split-Path $entry.Name -Leaf
        $image = Get-Item -LiteralPath $name
        $hash = (Get-FileHash -LiteralPath $name -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($image.Length -ne $entry.Value.size -or $hash -cne $entry.Value.sha256) {
            throw "Locked audio image differs: $name"
        }
        # IMPORTS preserves import names and delay-import information where present.
        # It cannot reveal statically linked objects or every LoadLibrary target.
        $output = & $dumpbin /NOLOGO /IMPORTS $name
        if ($LASTEXITCODE -ne 0 -or !$output) { throw "PE import inspection failed: $name" }
        $report = "audio-pe-$name.txt"
        $reportPath = Join-Path $evidencePath $report
        $output | Set-Content -LiteralPath $reportPath -Encoding utf8
        $images += @{
            name=$entry.Name; size=$image.Length; sha256=$hash; report=$report
            report_sha256=(Get-FileHash -LiteralPath $reportPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
} finally { Pop-Location }
@{
    status='PASS'; scope='PE_IMPORT_TABLE_ONLY'; source_lock_sha256=(Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    archive_sha256=$lock.archive.sha256; runtime_manifest_sha256=(Get-FileHash -LiteralPath (Join-Path $root 'audio-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    dumpbin_sha256=(Get-FileHash -LiteralPath $dumpbin -Algorithm SHA256).Hash.ToLowerInvariant()
    dumpbin_version=(Get-Item -LiteralPath $dumpbin).VersionInfo.FileVersion
    images=$images; static_linkage='NOT_RUN'; dynamic_runtime_loads='NOT_RUN'
    third_party_complete_source_bundle=$lock.review.third_party_complete_source_bundle
    public_distribution_clearance=$lock.review.public_distribution_clearance
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $evidencePath 'audio-pe-imports.json') -Encoding utf8
