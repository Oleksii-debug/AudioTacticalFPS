param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$HostPython,
    [Parameter(Mandatory=$true)][string]$Source
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'qualify_official_tiny.py'
$runtime = Join-Path $Root 'runtime\Scripts\python.exe'
$rules = @()
$profiles = @()
$proof = $null
$process = $null
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'The native network-denial proof requires this ephemeral runner to be an administrator.'
}
& $HostPython -I -B $script synthesize --root $Root --source $Source
if ($LASTEXITCODE -ne 0) { throw 'Actual SAPI synthesis failed.' }
& $runtime -I -S -B $script probe --root $Root
if ($LASTEXITCODE -ne 0) { throw 'The pre-rule native connection control failed.' }
$base = (& $runtime -I -S -c 'import sys; print(sys._base_executable)').Trim()
if ($LASTEXITCODE -ne 0 -or -not [IO.Path]::IsPathFullyQualified($base) -or
    -not (Test-Path -LiteralPath $base -PathType Leaf)) {
    throw 'The actual virtual-environment base executable was not resolved.'
}
$programs = @($runtime, $base, $HostPython) |
    ForEach-Object { [IO.Path]::GetFullPath($_) } | Select-Object -Unique
$profiles = @(Get-NetFirewallProfile | Select-Object Name, Enabled)
try {
    foreach ($profile in $profiles) {
        if ($profile.Enabled -ne 'True') {
            Set-NetFirewallProfile -Profile $profile.Name -Enabled True
        }
    }
    if (@(Get-NetFirewallProfile | Where-Object {$_.Enabled -ne 'True'}).Count -ne 0) {
        throw 'Windows Filtering Platform profiles are not active.'
    }
    foreach ($program in $programs) {
        $name = 'ResonanceTinyProof-' + [Guid]::NewGuid().ToString('N')
        $parameters = @{
            Name = $name; DisplayName = $name; Direction = 'Outbound'
            Action = 'Block'; Profile = 'Any'; Program = $program
            Protocol = 'Any'; Enabled = 'True'
        }
        New-NetFirewallRule @parameters | Out-Null
        $rules += $name
        $active = Get-NetFirewallRule -Name $name -PolicyStore ActiveStore
        $application = $active | Get-NetFirewallApplicationFilter
        if ($active.Enabled -ne 'True' -or $active.Direction -ne 'Outbound' -or
            $active.Action -ne 'Block' -or $application.Program -ine $program) {
            throw 'The executable-specific outbound rule is not active.'
        }
    }
    & $runtime -I -S -B $script probe --root $Root --blocked
    if ($LASTEXITCODE -ne 0) { throw 'Native Winsock did not demonstrate WSAEACCES after the rule.' }
    $proof = @{
        kind = 'windows-native-network-denial'; version = 1; status = 'enforced'
        mechanism = 'Windows Defender Firewall / Windows Filtering Platform'
        direction = 'outbound'; action = 'block'; protocol = 'any'; profile = 'any'
        native_control = 'connected before rule; WSAEACCES 10013 after rule'
        executable_count = $programs.Count; cleanup_succeeded = $false
        executable_sha256 = @($programs | ForEach-Object {
            (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()
        })
    }
    $arguments = @('-I', '-S', '-B', ('"' + $script + '"'), 'infer',
                   '--root', ('"' + $Root + '"'))
    $start = @{
        FilePath = $runtime; ArgumentList = $arguments; PassThru = $true
        NoNewWindow = $true
        RedirectStandardOutput = (Join-Path $Root 'inference.stdout.log')
        RedirectStandardError = (Join-Path $Root 'inference.stderr.log')
    }
    $process = Start-Process @start
    if (-not $process.WaitForExit(300000)) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
        throw 'The actual tiny model exceeded its 300-second process allowance.'
    }
    $process.Refresh()
    if ($process.ExitCode -ne 0) {
        $errorPath = Join-Path $Root 'inference.stderr.log'
        if (Test-Path -LiteralPath $errorPath) {
            Get-Content -LiteralPath $errorPath -Tail 30 | Write-Host
        }
        throw ('Actual tiny inference failed with exit ' + $process.ExitCode)
    }
} finally {
    if ($null -ne $process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
    }
    foreach ($name in $rules) {
        Remove-NetFirewallRule -Name $name -ErrorAction Stop
    }
    foreach ($profile in $profiles) {
        Set-NetFirewallProfile -Profile $profile.Name -Enabled $profile.Enabled
    }
    if ($null -ne $proof) {
        $remaining = @($rules | ForEach-Object {
            Get-NetFirewallRule -Name $_ -ErrorAction SilentlyContinue
        })
        if ($remaining.Count -ne 0) { throw 'Owned firewall rules were not removed.' }
        foreach ($profile in $profiles) {
            if ((Get-NetFirewallProfile -Name $profile.Name).Enabled -ne $profile.Enabled) {
                throw 'The original firewall profile state was not restored.'
            }
        }
        $proof.cleanup_succeeded = $true
        $proof | ConvertTo-Json -Depth 5 -Compress |
            Set-Content -LiteralPath (Join-Path $Root 'firewall.json') -Encoding utf8
    }
}
