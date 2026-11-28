# Run only on the disposable native CI VM, after the production install passed.
# This child owns its firewall rules and restores exactly the profiles it observed.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CurrentGeneration,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [Parameter(Mandatory=$true)][string]$ManifestSha256,
    [Parameter(Mandatory=$true)][string]$TestWheels,
    [Parameter(Mandatory=$true)][string]$Report,
    [Parameter(Mandatory=$true)][string]$Project
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -cne 'Windows_NT' -or $env:CI -cne 'true') {
    throw 'Installed library qualification requires the disposable native Windows CI VM.'
}
if ($SourceCommit -cnotmatch '^[0-9a-f]{40}$' -or $ManifestSha256 -cnotmatch '^[0-9a-f]{64}$') {
    throw 'Provide the exact current B source commit and independently pinned bundle manifest digest.'
}
foreach ($value in @($CurrentGeneration, $TestWheels, $Report, $Project)) {
    if (-not [IO.Path]::IsPathRooted($value) -or $value.StartsWith('\\')) {
        throw 'Qualification paths must be explicit absolute local paths.'
    }
}
$generation = (Resolve-Path -LiteralPath $CurrentGeneration -ErrorAction Stop).Path
$projectRoot = (Resolve-Path -LiteralPath $Project -ErrorAction Stop).Path
$wheelRoot = (Resolve-Path -LiteralPath $TestWheels -ErrorAction Stop).Path
$reportPath = [IO.Path]::GetFullPath($Report)
$reportParent = Split-Path -Parent $reportPath
if (-not (Test-Path -LiteralPath $reportParent -PathType Container) -or
    (Test-Path -LiteralPath $reportPath)) {
    throw 'Use a fresh report path in an existing public CI evidence directory.'
}
$python = Join-Path $generation '.venv/Scripts/python.exe'
$helper = Join-Path $projectRoot 'scripts/qualify_standard_library.py'
$programs = @(
    (Join-Path $generation 'ResearchLibrary.exe'),
    (Join-Path $generation 'runtime/python.exe'),
    (Join-Path $generation 'runtime/pythonw.exe'),
    $python,
    (Join-Path $generation '.venv/Scripts/pythonw.exe'),
    (Join-Path $generation 'ocr/tesseract.exe'),
    (Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'),
    (Join-Path $env:SystemRoot 'SysWOW64/WindowsPowerShell/v1.0/powershell.exe')
) | ForEach-Object { [IO.Path]::GetFullPath($_) }
foreach ($path in @($programs) + @($helper)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "A required installed program or fixed qualification helper is absent: $path"
    }
}
$wheels = @(Get-ChildItem -LiteralPath $wheelRoot -Force -ErrorAction Stop)
if ($wheels.Count -lt 2 -or $wheels.Count -gt 64) {
    throw 'The acquired test wheel directory must contain 2 to 64 bounded wheel files.'
}
$wheelEvidence = @()
foreach ($wheel in $wheels) {
    if ($wheel.PSIsContainer -or $wheel.Extension -cne '.whl' -or
        $wheel.Length -le 0 -or $wheel.Length -gt 536870912 -or
        ($wheel.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The existing test dependency inventory contains an unsupported entry.'
    }
    $wheelEvidence += [ordered]@{
        name=$wheel.Name
        sha256=(Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$evidence = Join-Path $reportParent ('standard-library-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $evidence -ErrorAction Stop | Out-Null
$baseline = Join-Path $evidence 'before-test-dependencies.json'
$functional = Join-Path $evidence 'installed-library.json'
$prefix = 'CARR-installed-library-' + [guid]::NewGuid().ToString('N')
$ruleNames = @()
$profilesBefore = @(Get-NetFirewallProfile -ErrorAction Stop | Select-Object Name, Enabled)
if ($profilesBefore.Count -ne 3 -or
    @($profilesBefore.Name | Sort-Object -Unique).Count -ne 3) {
    throw 'Cannot capture all three existing Windows Firewall profiles.'
}
function Invoke-Logged {
    param([string[]]$Arguments, [string]$Log)
    & $python @Arguments 2>&1 | Tee-Object -FilePath $Log
    if ($LASTEXITCODE -ne 0) {
        throw "Installed qualification command failed ($LASTEXITCODE); see $Log"
    }
}
function Assert-OfflineRules {
    $profiles = @(Get-NetFirewallProfile -ErrorAction Stop)
    if ($profiles.Count -ne 3 -or
        @($profiles | Where-Object { [string]($_.Enabled) -ne 'True' }).Count -ne 0) {
        throw 'All Windows Firewall profiles must remain enabled during qualification.'
    }
    $active = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $ruleNames -ErrorAction Stop)
    if ($active.Count -ne $programs.Count -or
        @($active | Where-Object {
            [string]($_.Enabled) -ne 'True' -or
            [string]($_.Direction) -ne 'Outbound' -or
            [string]($_.Action) -ne 'Block'
        }).Count -ne 0) {
        throw 'The exact owned outbound block rules are not active.'
    }
    $filters = @($active | Get-NetFirewallApplicationFilter -ErrorAction Stop |
        Select-Object -ExpandProperty Program |
        ForEach-Object { [IO.Path]::GetFullPath($_) })
    if (@(Compare-Object ($programs | Sort-Object) ($filters | Sort-Object)).Count -ne 0) {
        throw 'Active firewall rules do not cover the exact installed programs.'
    }
}
$operationError = $null
$cleanupErrors = @()
$qualification = $null
try {
    Set-NetFirewallProfile -Profile Domain, Private, Public -Enabled True -ErrorAction Stop
    foreach ($program in $programs) {
        $ruleName = $prefix + '-' + $ruleNames.Count
        # Retain the name before creation so a partial cmdlet failure is cleaned too.
        $ruleNames += $ruleName
        New-NetFirewallRule -Name $ruleName -DisplayName $ruleName -Direction Outbound -Action Block -Profile Any -Program $program -Enabled True -ErrorAction Stop | Out-Null
    }
    Assert-OfflineRules
    foreach ($name in @('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS',
                        'CARR_CI_ALLOW_LOOPBACK', 'CARR_FFMPEG')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
    $env:ORT_DISABLE_TELEMETRY = '1'
    $env:HF_HUB_DISABLE_TELEMETRY = '1'
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $env:DO_NOT_TRACK = '1'
    $common = @('--bundle', $generation, '--project', $projectRoot,
        '--source-commit', $SourceCommit, '--manifest-sha256', $ManifestSha256,
        '--baseline', $baseline)
    Invoke-Logged -Arguments (@('-I', '-B', $helper, 'snapshot') + $common) -Log (Join-Path $evidence 'snapshot.log')
    $baselineHash = (Get-FileHash -LiteralPath $baseline -Algorithm SHA256).Hash.ToLowerInvariant()
    # Production installation already succeeded. This is the only permitted
    # test-only addition, from the existing acquisition directory, offline.
    $pipArguments = @('-I', '-B', '-m', 'pip', '--isolated',
        '--disable-pip-version-check', 'install', '--no-index', '--no-cache-dir',
        '--only-binary=:all:', '--no-compile', '--find-links', $wheelRoot,
        'pytest==9.0.2', 'reportlab==4.4.9')
    Invoke-Logged -Arguments $pipArguments -Log (Join-Path $evidence 'test-dependencies.log')
    Assert-OfflineRules
    $checkArguments = @('-I', '-B', $helper, 'qualify') + $common +
        @('--baseline-sha256', $baselineHash, '--report', $functional)
    Invoke-Logged -Arguments $checkArguments -Log (Join-Path $evidence 'qualification.log')
    $qualification = Get-Content -LiteralPath $functional -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($qualification.schema -cne 'carr-installed-library-qualification/1' -or
        $qualification.status -cne 'PASS' -or
        $qualification.source_commit -cne $SourceCommit -or
        $qualification.manifest_sha256 -cne $ManifestSha256 -or
        $qualification.passed -ne 50 -or $qualification.failed -ne 0 -or $qualification.skipped -ne 0 -or
        $qualification.expected_failures -isnot [bool] -or $qualification.expected_failures -ne $false -or
        $qualification.audio_payload -isnot [bool] -or $qualification.audio_payload -ne $false) {
        throw 'The installed library report does not match its exact success contract.'
    }
    foreach ($flag in @('installed_package', 'sealed_files_unchanged',
                         'production_distributions_unchanged')) {
        if ($qualification.$flag -isnot [bool] -or $qualification.$flag -ne $true) {
            throw "A required installed qualification proof is absent: $flag"
        }
    }
    Assert-OfflineRules
    $afterWheels = @(Get-ChildItem -LiteralPath $wheelRoot -Force -ErrorAction Stop)
    if (@(Compare-Object ($wheels.Name | Sort-Object) ($afterWheels.Name | Sort-Object)).Count -ne 0) {
        throw 'The acquired offline test wheel inventory changed.'
    }
    foreach ($item in $wheelEvidence) {
        $path = Join-Path $wheelRoot $item.name
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $item.sha256) {
            throw 'An acquired offline test wheel changed during qualification.'
        }
    }
} catch {
    $operationError = $_
} finally {
    try {
        # A service/query failure is not evidence of absence. Enumerate once
        # with terminating errors, then touch only the exact UUID-owned names.
        $owned = @(Get-NetFirewallRule -ErrorAction Stop |
            Where-Object { $ruleNames -ccontains $_.Name })
        if ($owned.Count) { $owned | Remove-NetFirewallRule -ErrorAction Stop }
        $remaining = @(Get-NetFirewallRule -ErrorAction Stop |
            Where-Object { $ruleNames -ccontains $_.Name })
        if ($remaining.Count) { throw 'An owned firewall rule remains after cleanup.' }
    } catch { $cleanupErrors += $_.Exception.Message }
    foreach ($profile in $profilesBefore) {
        try {
            Set-NetFirewallProfile -Profile $profile.Name -Enabled $profile.Enabled -ErrorAction Stop
            $restored = @(Get-NetFirewallProfile -Name $profile.Name -ErrorAction Stop)
            if ($restored.Count -ne 1 -or [string]($restored[0].Enabled) -ne [string]($profile.Enabled)) {
                throw 'An original Windows Firewall profile was not restored.'
            }
        } catch { $cleanupErrors += $_.Exception.Message }
    }
}
if ($null -ne $operationError) {
    if ($cleanupErrors.Count) { Write-Warning ($cleanupErrors -join '; ') }
    throw $operationError
}
if ($cleanupErrors.Count) { throw ($cleanupErrors -join '; ') }
$result = [ordered]@{
    schema='carr-standard-library-qualification/1'
    status='PASS'
    source_commit=$SourceCommit
    manifest_sha256=$ManifestSha256
    generation=$generation
    passed=50
    failed=0
    skipped=0
    qualification=$qualification
    test_wheels=$wheelEvidence
    firewall=[ordered]@{
        outbound_blocked_programs=$programs
        profiles_restored=$true
        owned_rules_removed=$true
    }
    manual_nvda='NOT_RUN'
    real_corpus_quality='NOT_RUN'
}
$encoded = ([Text.UTF8Encoding]::new($false)).GetBytes(($result | ConvertTo-Json -Depth 12) + [Environment]::NewLine)
if ($encoded.Length -gt 4194304) { throw 'The public qualification report exceeds its byte budget.' }
$stream = [IO.File]::Open($reportPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $stream.Write($encoded, 0, $encoded.Length); $stream.Flush($true) } finally { $stream.Dispose() }
Write-Output "PASS: exact installed audio-free library workflows: 50 passed, 0 failed, 0 skipped."
