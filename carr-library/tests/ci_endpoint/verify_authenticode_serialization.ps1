param([Parameter(Mandatory=$true)][string]$Report)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$record = [ordered]@{
    schema = 'carr-ci-signature-serialization-probe/1'
    status = 'RUNNING'
    scope = 'JSON request/response serialization only; trust is deliberately stubbed'
    powershell_version = $PSVersionTable.PSVersion.ToString()
    tested = 0
    passed = 0
    cases = @()
}
$reportPath = [System.IO.Path]::GetFullPath($Report)
[System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($reportPath)) | Out-Null
function Write-Receipt {
    $json = ConvertTo-Json -InputObject $record -Depth 6
    [System.IO.File]::WriteAllText($reportPath, $json, [System.Text.UTF8Encoding]::new($false))
}
Write-Receipt
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ('carr-signature-json-' + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($temporary) | Out-Null
$production = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot '../../scripts/ci_audio_endpoint_authenticode.ps1'))
$request = Join-Path $temporary 'request.json'

# This stub exists only in this probe process. The subsequent real evaluator
# launches a fresh Windows PowerShell process with -NoProfile and the ordinary
# Microsoft cmdlet. No certificate, driver or installation API is used here.
$global:CarrEndpointSignatureCalls = [System.Collections.Generic.List[string]]::new()
function Global:Get-AuthenticodeSignature {
    param([string]$LiteralPath)
    $global:CarrEndpointSignatureCalls.Add($LiteralPath)
    [pscustomobject]@{
        Status = 'NotSigned'
        StatusMessage = 'Serialization-only probe; no trust verification performed'
        SignerCertificate = $null
        SignatureType = 'None'
    }
}
try {
    $files = @('vbaudio_cable64_win10.sys', 'vbaudio_cable64arm_win10.sys',
               'vbaudio_cable64_win10.cat')
    $rows = @(
        [ordered]@{ file=$files[0]; path=(Join-Path $temporary $files[0]) },
        [ordered]@{ file=$files[1]; path=(Join-Path $temporary $files[1]) },
        [ordered]@{ file=$files[2]; path=(Join-Path $temporary $files[2]) }
    )
    $cases = @(
        @{ name='three-unique-targets'; valid=$true; data=$rows },
        @{ name='missing-target'; valid=$false; data=@($rows[0], $rows[1]) },
        @{ name='duplicate-target'; valid=$false; data=@($rows[0], $rows[0], $rows[2]) },
        @{ name='extra-target'; valid=$false; data=@($rows[0], $rows[1], $rows[2], $rows[2]) },
        @{ name='object-with-count'; valid=$false; data=[ordered]@{Count=3; file=$files[0]; path=(Join-Path $temporary $files[0])} },
        @{ name='unreviewed-target'; valid=$false; data=@($rows[0], $rows[1], [ordered]@{file='other.cat'; path=(Join-Path $temporary 'other.cat')}) },
        @{ name='wrong-basename'; valid=$false; data=@($rows[0], $rows[1], [ordered]@{file=$files[2]; path=(Join-Path $temporary 'other.cat')}) },
        @{ name='relative-path'; valid=$false; data=@($rows[0], $rows[1], [ordered]@{file=$files[2]; path=$files[2]}) },
        @{ name='null-entry'; valid=$false; data=@($rows[0], $rows[1], $null) },
        @{ name='extra-field'; valid=$false; data=@($rows[0], $rows[1], [ordered]@{file=$files[2]; path=(Join-Path $temporary $files[2]); extra='not allowed'}) }
    )
    foreach ($case in $cases) {
        $global:CarrEndpointSignatureCalls.Clear()
        $raw = ConvertTo-Json -InputObject $case.data -Depth 6 -Compress
        [System.IO.File]::WriteAllText($request, $raw, [System.Text.UTF8Encoding]::new($false))
        $rejected = $false
        $output = $null
        try {
            $output = & $production -InputPath $request
        } catch {
            $rejected = $true
            $detail = $_.Exception.Message
        }
        $record.tested += 1
        if ($case.valid) {
            if ($rejected) { throw ('Valid three-target request rejected: ' + $detail) }
            $response = ConvertFrom-Json -InputObject ([string]$output)
            if ($response -isnot [array] -or $response.Count -ne 3) {
                throw 'Response did not preserve the three-target array'
            }
            if ($global:CarrEndpointSignatureCalls.Count -ne 3) {
                throw 'Valid request did not query each target exactly once'
            }
            for ($i=0; $i -lt 3; $i++) {
                if ($response[$i].file -cne $files[$i] -or
                    $response[$i].status -cne 'NotSigned' -or
                    $response[$i].certificate_sha256 -cne '') {
                    throw 'Response target/status identity changed'
                }
                if ($global:CarrEndpointSignatureCalls[$i] -cne $rows[$i].path) {
                    throw 'Wrong target reached the serialization-only stub'
                }
            }
        } else {
            if (-not $rejected) { throw ('Invalid request accepted: ' + $case.name) }
            if ($global:CarrEndpointSignatureCalls.Count -ne 0) {
                throw ('Invalid request reached a trust query: ' + $case.name)
            }
        }
        $record.passed += 1
        $record.cases += $case.name
        Write-Receipt
    }
    $record.status = 'PASS'
    Write-Receipt
    ConvertTo-Json -InputObject $record -Depth 6 -Compress
} catch {
    $record.status = 'FAILED'
    $record.error = $_.Exception.Message
    Write-Receipt
    throw
} finally {
    Remove-Item -LiteralPath 'Function:\Get-AuthenticodeSignature' -ErrorAction SilentlyContinue
    Remove-Variable -Name CarrEndpointSignatureCalls -Scope Global -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}

