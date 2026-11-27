# Explicit ephemeral-CI prerequisite only. This does not install a device,
# replace a driver, change the player, or establish playback acceptance.
[CmdletBinding()]
param(
    [switch]$Prepare,
    [Parameter(Mandatory = $true)][string]$ReportPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$records = @()
foreach ($name in @('AudioEndpointBuilder', 'Audiosrv')) {
    $record = [ordered]@{ name = $name; requested_prepare = [bool]$Prepare; actions = @() }
    try {
        $service = Get-Service -Name $name -ErrorAction Stop
        $record.before_state = [string]$service.Status
        $record.before_start_type = [string]$service.StartType
        if ($Prepare -and $service.Status -ne 'Running') {
            if ($service.StartType -eq 'Disabled') {
                Set-Service -Name $name -StartupType Manual -ErrorAction Stop
                $record.actions += 'Existing service enabled for explicit CI preparation'
            }
            Start-Service -Name $name -ErrorAction Stop
            $service.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Running,
                                  [TimeSpan]::FromSeconds(15))
            $record.actions += 'Existing service started'
        }
        $service.Refresh()
        $record.after_state = [string]$service.Status
        $record.after_start_type = [string]$service.StartType
    } catch {
        # Fixed service identity and numeric/type diagnostics; no unrelated
        # service inventory, source files, credentials or driver installation.
        $record.error_type = $_.Exception.GetType().FullName
        $record.error_hresult = $_.Exception.HResult
    }
    $records += [pscustomobject]$record
}
$report = [ordered]@{
    schema = 'carr-windows-audio-service-preparation/1'
    purpose = 'Prerequisite preparation; actual native MCI acceptance remains required'
    prepared = [bool]$Prepare
    services = $records
    device_installation = 'NOT_PERFORMED'
    playback_evidence = 'NOT_RUN'
}
$json = $report | ConvertTo-Json -Depth 6
$target = [System.IO.Path]::GetFullPath($ReportPath)
[System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
[System.IO.File]::WriteAllText($target, $json, [System.Text.UTF8Encoding]::new($false))
Write-Output $json
