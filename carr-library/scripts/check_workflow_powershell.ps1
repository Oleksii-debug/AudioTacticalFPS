[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$Output
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$ruby = (Get-Command ruby -ErrorAction Stop).Source

# Use the hosted image's standard YAML library. Workflow bodies remain data.
$extractYaml = @'
require 'yaml'
require 'json'
require 'digest'

root = File.realpath(ARGV.fetch(0))
paths = Dir.glob(File.join(root, '.github', 'workflows', '*.{yml,yaml}')).sort
raise 'No committed workflow YAML files were found.' if paths.empty?
files = []
steps = []
skipped = []
def default_shell(value)
  return nil unless value.is_a?(Hash)
  defaults = value['defaults']
  return nil unless defaults.is_a?(Hash)
  run = defaults['run']
  run.is_a?(Hash) ? run['shell'] : nil
end
paths.each do |path|
  raw = File.read(path, encoding: 'UTF-8')
  value = YAML.safe_load(raw, permitted_classes: [], permitted_symbols: [], aliases: true, filename: path)
  raise "Workflow root is not a mapping: #{path}" unless value.is_a?(Hash)
  jobs = value['jobs']
  raise "Workflow jobs are not a mapping: #{path}" unless jobs.is_a?(Hash)
  relative = path.delete_prefix(root + File::SEPARATOR)
  count = 0
  jobs.each do |job_id, job|
    raise "Job is not a mapping: #{relative}/#{job_id}" unless job.is_a?(Hash)
    next unless job.key?('steps')
    raise "Job steps are not an array: #{relative}/#{job_id}" unless job['steps'].is_a?(Array)
    job['steps'].each_with_index do |step, index|
      raise "Step is not a mapping: #{relative}/#{job_id}/#{index}" unless step.is_a?(Hash)
      next unless step.key?('run')
      body = step['run']
      raise "Run body is not text: #{relative}/#{job_id}/#{index}" unless body.is_a?(String)
      shell = step['shell'] || default_shell(job) || default_shell(value)
      if shell.nil?
        runner = job['runs-on']
        if runner.is_a?(String) && runner.include?('$' + '{{')
          raise "Declare an explicit shell for a dynamic runner: #{relative}/#{job_id}/#{index}"
        end
        labels = runner.is_a?(Array) ? runner : [runner]
        windows = labels.any? { |label| label.is_a?(String) && label.match?(/\Awindows(?:-|\z)/i) }
        shell = windows ? 'pwsh' : 'bash'
      end
      raise "Shell is not explicit text: #{relative}/#{job_id}/#{index}" unless shell.is_a?(String)
      raise "Dynamic shell cannot be syntax-qualified: #{relative}/#{job_id}/#{index}" if shell.include?('$' + '{{')
      record = {
        'workflow' => relative, 'job' => job_id.to_s, 'index' => index,
        'name' => (step['name'] || step['id'] || "step-#{index}").to_s,
        'shell' => shell, 'body_sha256' => Digest::SHA256.hexdigest(body)
      }
      if shell.strip.match?(/\A(?:pwsh|powershell)(?:\s|\z)/i)
        steps << record.merge('body' => body)
        count += 1
      else
        skipped << record
      end
    end
  end
  files << {'path' => relative, 'sha256' => Digest::SHA256.hexdigest(raw), 'powershell_steps' => count}
end
%w[carr-windows.yml carr-v3.yml carr-transcription-bridge.yml].each do |name|
  match = files.find { |item| item['path'] == ".github/workflows/#{name}" }
  raise "Required workflow has no extracted PowerShell bodies: #{name}" unless match && match['powershell_steps'] > 0
end
puts JSON.generate({
  'schema' => 'resonance-workflow-powershell-input/1',
  'ruby' => RUBY_VERSION, 'psych' => Psych::VERSION,
  'libyaml' => Psych.libyaml_version, 'files' => files, 'steps' => steps,
  'other_shell_steps' => skipped
})
'@
$yamlOutput = @(& $ruby -e $extractYaml $root)
if ($LASTEXITCODE -ne 0) { throw 'The real YAML parser could not extract the committed workflow bodies.' }
$inventory = ($yamlOutput -join [Environment]::NewLine) | ConvertFrom-Json -AsHashtable -Depth 100
if ($inventory['schema'] -cne 'resonance-workflow-powershell-input/1' -or @($inventory['steps']).Count -eq 0) {
    throw 'The extracted PowerShell inventory is empty or invalid.'
}

function Get-NativeParseResult([string]$Text, [string]$Identity) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput(
        $Text, $Identity, [ref]$tokens, [ref]$parseErrors)
    $errors = @($parseErrors | Where-Object { $null -ne $_ } | ForEach-Object {
        [ordered]@{
            id = $_.ErrorId
            message = $_.Message
            start_line = $_.Extent.StartLineNumber
            start_column = $_.Extent.StartColumnNumber
            end_line = $_.Extent.EndLineNumber
            end_column = $_.Extent.EndColumnNumber
        }
    })
    return [pscustomobject]@{ errors = $errors; token_count = @($tokens).Count }
}

# The actual regression: the only difference within each pair is its final comma.
$badFast = @(
    '$completionSuites = @('
    "  'tests/storage/test_local_schedules.py',"
    "  'tests/storage/test_transcription_backup.py',"
    ')'
) -join [Environment]::NewLine
$goodFast = $badFast.Replace("'tests/storage/test_transcription_backup.py',", "'tests/storage/test_transcription_backup.py'")
$badFull = @(
    '$suites = @('
    "  'tests/ui/test_local_schedules.py',"
    "  'tests/packaging/test_local_schedules_cli.py',"
    ')'
) -join [Environment]::NewLine
$goodFull = $badFull.Replace("'tests/packaging/test_local_schedules_cli.py',", "'tests/packaging/test_local_schedules_cli.py'")
$literalHereString = @(
    '$python = @' + "'"
    'values = [1, 2,]'
    "'" + '@'
) -join [Environment]::NewLine
$ownedTemporary = Join-Path ([IO.Path]::GetTempPath()) ('carr-workflow-parser-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $ownedTemporary | Out-Null
try {
    $marker = Join-Path $ownedTemporary 'must-not-exist.txt'
    $neverExecute = "[IO.File]::WriteAllText('" + $marker.Replace("'", "''") + "', 'unexpected execution')"
    $controls = @(
        @{ name = 'invalid-fast-final-comma'; body = $badFast; expects_error = $true }
        @{ name = 'corrected-fast-array'; body = $goodFast; expects_error = $false }
        @{ name = 'invalid-full-final-comma'; body = $badFull; expects_error = $true }
        @{ name = 'corrected-full-array'; body = $goodFull; expects_error = $false }
        @{ name = 'python-trailing-comma-is-literal-here-string'; body = $literalHereString; expects_error = $false }
        @{ name = 'parse-never-executes-commands'; body = $neverExecute; expects_error = $false }
    )
    $controlReports = [System.Collections.Generic.List[object]]::new()
    $controlFailures = [System.Collections.Generic.List[string]]::new()
    foreach ($control in $controls) {
        $parsed = Get-NativeParseResult $control.body ('control/' + $control.name)
        $hasError = $parsed.errors.Count -ne 0
        if ($hasError -ne $control.expects_error) { $controlFailures.Add($control.name) }
        $controlReports.Add([ordered]@{
            name = $control.name
            expected_error = $control.expects_error
            actual_error = $hasError
            errors = $parsed.errors
        })
    }
    if (Test-Path -LiteralPath $marker) { $controlFailures.Add('parse-never-executes-commands') }

    $results = [System.Collections.Generic.List[object]]::new()
    $failedBodies = 0
    foreach ($step in $inventory['steps']) {
        $identity = $step['workflow'] + '/' + $step['job'] + '/' + $step['index']
        $parsed = Get-NativeParseResult $step['body'] $identity
        if ($parsed.errors.Count -ne 0) { $failedBodies += 1 }
        $results.Add([ordered]@{
            workflow = $step['workflow']; job = $step['job']; index = $step['index']
            name = $step['name']; shell = $step['shell']; body_sha256 = $step['body_sha256']
            token_count = $parsed.token_count; errors = $parsed.errors
        })
    }
    $head = (& git -C $root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'The workflow source commit is unavailable.' }
    $report = [ordered]@{
        schema = 'resonance-workflow-powershell-syntax/1'
        status = $(if ($failedBodies -eq 0 -and $controlFailures.Count -eq 0) { 'PASS' } else { 'FAIL' })
        source_commit = $head
        powershell = $PSVersionTable.PSVersion.ToString()
        ruby = $inventory['ruby']; psych = $inventory['psych']; libyaml = $inventory['libyaml']
        workflows = $inventory['files']
        controls = $controlReports.ToArray(); control_failures = $controlFailures.ToArray()
        parsed_bodies = $results.Count; failed_bodies = $failedBodies; bodies = $results.ToArray()
        other_shell_steps = $inventory['other_shell_steps']
        workflow_expressions = 'Parsed as committed; no expression evaluation or substitution'
        runtime_qualification = 'NOT_RUN; syntax only, workflow commands are never executed'
    }
    $destination = [IO.Path]::GetFullPath($Output)
    New-Item -ItemType Directory -Force (Split-Path -Parent $destination) | Out-Null
    if (Test-Path -LiteralPath $destination) { throw 'Syntax evidence must use a fresh report path.' }
    $report | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $destination -Encoding utf8
    [ordered]@{
        status = $report.status; source_commit = $head; parsed_bodies = $results.Count
        failed_bodies = $failedBodies; controls = $controls.Count; control_failures = $controlFailures.Count
        report = $destination; runtime_qualification = 'NOT_RUN'
    } | ConvertTo-Json -Compress
    if ($failedBodies -ne 0 -or $controlFailures.Count -ne 0) {
        foreach ($item in $results) {
            foreach ($failure in $item.errors) {
                Write-Host ($item.workflow + '/' + $item.job + '/' + $item.name +
                    ': line ' + $failure.start_line + ', column ' + $failure.start_column + ': ' + $failure.message)
            }
        }
        throw 'The native PowerShell parser rejected one or more workflow bodies or regression controls.'
    }
} finally {
    Remove-Item -LiteralPath $ownedTemporary -Recurse -Force -ErrorAction Stop
}
