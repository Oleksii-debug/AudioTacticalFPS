param(
    [Parameter(Mandatory = $true)][string] $Manifest,
    [Parameter(Mandatory = $true)][string] $Output
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Parser.ParseFile builds an AST; none of the archived script bodies is invoked.
# https://learn.microsoft.com/dotnet/api/system.management.automation.language.parser.parsefile
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$inputRoot = Split-Path -Parent $manifestPath
$inputData = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($inputData.status -ne 'PREPARED_NOT_PARSED' -or @($inputData.scripts).Count -eq 0) {
    throw 'No verified PowerShell inputs were prepared.'
}
$controlTokens = $null
$controlErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseInput(
    'function DeliberatelyIncomplete {', [ref] $controlTokens, [ref] $controlErrors)
if (@($controlErrors).Count -eq 0) { throw 'The actual parser did not reject the negative control.' }
$null = [System.Management.Automation.Language.Parser]::ParseInput(
    'Write-Output "Syntax control only"', [ref] $controlTokens, [ref] $controlErrors)
if (@($controlErrors).Count -ne 0) { throw 'The actual parser rejected the positive control.' }

$results = @()
$failureCount = 0
foreach ($entry in $inputData.scripts) {
    $path = [IO.Path]::GetFullPath((Join-Path $inputRoot $entry.file))
    $prefix = [IO.Path]::GetFullPath($inputRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A prepared parser input leaves its evidence directory.'
    }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.parse_sha256) {
        throw 'A prepared PowerShell input changed before parsing.'
    }
    $tokens = $null
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        $path, [ref] $tokens, [ref] $parseErrors)
    $errors = @($parseErrors | ForEach-Object {
        [ordered]@{ error_id = $_.ErrorId; message = $_.Message
            line = $_.Extent.StartLineNumber; column = $_.Extent.StartColumnNumber }
    })
    $failureCount += $errors.Count
    $results += [ordered]@{
        workflow = $entry.workflow; workflow_blob = $entry.workflow_blob
        workflow_sha256 = $entry.workflow_sha256; job = $entry.job; step = $entry.step
        name = $entry.name; shell = $entry.shell; run_sha256 = $entry.run_sha256
        parse_sha256 = $entry.parse_sha256; expression_replacements = $entry.expression_replacements
        status = $(if ($errors.Count -eq 0) { 'PASS' } else { 'FAIL' }); errors = $errors
    }
}
$report = [ordered]@{
    status = $(if ($failureCount -eq 0) { 'PASS' } else { 'FAIL' })
    scope = 'Actual PowerShell parser only; archived commands were not executed.'
    validation_commit = $inputData.validation_commit
    validation_tree = $inputData.validation_tree
    recovery_manifest_sha256 = $inputData.recovery_manifest_sha256
    powershell = $PSVersionTable.PSVersion.ToString()
    parser_assembly = [System.Management.Automation.Language.Parser].Assembly.FullName
    parser_controls = 'PASS'; scripts = $results; parse_error_count = $failureCount
    other_shell_blocks = $inputData.other_shell_blocks
    installed = 'NOT_RUN'; application_native = 'NOT_RUN'; nvda = 'NOT_RUN'
}
$report | ConvertTo-Json -Depth 14 | Set-Content -LiteralPath $Output -Encoding utf8
Write-Output ('PowerShell syntax: scripts={0}; errors={1}' -f $results.Count, $failureCount)
if ($failureCount -ne 0) { throw 'Archived PowerShell syntax validation failed; see the exact source report.' }
