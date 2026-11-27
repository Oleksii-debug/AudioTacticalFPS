#Requires -Version 7.0
<#
Run after the existing sealed/offline full-bundle step, on the same disposable
Windows runner, with a caller step timeout-minutes: 60. No acquisition occurs
here. Keep the caller's existing full bundle and offline replay unchanged.
#>
[CmdletBinding()]
param(
    [string]$CompilerPath = $env:CARR_INNO_COMPILER
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or $env:CI -ne 'true') {
    throw 'Standard-installer acceptance requires a disposable native Windows CI account.'
}
if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP) -or
    [string]::IsNullOrWhiteSpace($env:CARR_PYTHON) -or
    [string]::IsNullOrWhiteSpace($env:CARR_CI_MODEL_DIR) -or
    [string]::IsNullOrWhiteSpace($env:CARR_CI_MODEL_MANIFEST) -or
    [string]::IsNullOrWhiteSpace($env:CARR_CI_OCR_DIR)) {
    throw 'Run the existing model/OCR acquisition and sealed full-bundle acceptance first.'
}

$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$bundleWork = Join-Path $env:RUNNER_TEMP 'carr-bundle-ci'
$fullBundle = Join-Path $bundleWork 'complete bundle'
$installedPython = Join-Path $fullBundle '.venv/Scripts/python.exe'
$wheelhouse = Join-Path $bundleWork 'wheelhouse'
$applicationWheels = Join-Path $bundleWork 'application'
$pythonArchive = Join-Path $bundleWork 'windows-python.tar.gz'
$nativeRuntime = Join-Path $bundleWork 'native-runtime'
$nativeLauncher = Join-Path $env:RUNNER_TEMP 'carr-native-launcher-private'
$ocrSource = Join-Path $bundleWork 'ocr-corresponding-source.tar'
$fullManifest = Join-Path $fullBundle 'bundle-manifest.json'
$evidence = Join-Path $project 'ci-results/standard-installer'
$private = Join-Path $env:RUNNER_TEMP ('carr-standard-installer-' + [guid]::NewGuid().ToString('N'))
$baseBundle = Join-Path $private 'base-bundle'
$bundle = Join-Path $private 'sealed-bundle'
$goodBuild = Join-Path $private 'good-build'
$failedBuild = Join-Path $private 'negative-build'
$probeWork = Join-Path $private 'native-probe'
$nativeReport = Join-Path $evidence 'native.json'

function Assert-Path([string]$Path, [string]$Kind) {
    if (-not (Test-Path -LiteralPath $Path -PathType $Kind)) {
        throw "Missing already-acquired $Kind input: $Path"
    }
}
function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Write-PublicJson([object]$Value, [string]$Name) {
    $Value | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $evidence $Name) -Encoding utf8
}
function Invoke-Logged([string]$Executable, [string[]]$Arguments, [string]$LogName) {
    & $Executable @Arguments 2>&1 | Tee-Object -FilePath (Join-Path $evidence $LogName)
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Standard-installer command failed with exit $code; see standard-installer/$LogName."
    }
}
function Assert-FreeSpace([long]$Bytes, [string]$Phase) {
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($private))
    $free = $drive.AvailableFreeSpace
    if ($free -lt $Bytes) {
        throw "$Phase needs at least $Bytes free bytes under RUNNER_TEMP; observed $free. Preserve the approved inputs and use a runner with sufficient disk."
    }
    return $free
}

foreach ($directory in @($bundleWork, $fullBundle, $wheelhouse, $applicationWheels,
                          $nativeRuntime, $env:CARR_CI_MODEL_DIR, $env:CARR_CI_OCR_DIR)) {
    Assert-Path $directory 'Container'
}
foreach ($file in @($env:CARR_PYTHON, $installedPython, $pythonArchive, $fullManifest,
                   $env:CARR_CI_MODEL_MANIFEST, $ocrSource,
                   (Join-Path $nativeLauncher 'ResearchLibrary.exe'),
                   (Join-Path $nativeLauncher 'build-evidence.json'))) {
    Assert-Path $file 'Leaf'
}
if ((Resolve-Path -LiteralPath $env:CARR_CI_OCR_DIR).Path -ne
    (Resolve-Path -LiteralPath (Join-Path $bundleWork 'ocr')).Path) {
    throw 'The selected OCR directory is not the already-approved main-job OCR input.'
}

# Never search PATH, download a compiler, or infer a pin from a setup result.
if ([string]::IsNullOrWhiteSpace($CompilerPath)) {
    $candidates = @()
    foreach ($programs in @([Environment]::GetFolderPath('ProgramFilesX86'),
                            [Environment]::GetFolderPath('ProgramFiles'))) {
        if (-not [string]::IsNullOrWhiteSpace($programs)) {
            $candidates += Join-Path $programs 'Inno Setup 6/ISCC.exe'
        }
    }
    $CompilerPath = $candidates | Select-Object -Unique |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($CompilerPath)) {
    throw 'Inno Setup 6.4+ is not already installed at a reviewed Program Files path. Provision the runner beforehand or pass -CompilerPath with an explicit installed ISCC.exe; this driver does not download tools.'
}
if (-not [IO.Path]::IsPathFullyQualified($CompilerPath) -or
    [IO.Path]::GetFileName($CompilerPath) -ine 'ISCC.exe') {
    throw 'CompilerPath must be the absolute path of an already-installed ISCC.exe.'
}
Assert-Path $CompilerPath 'Leaf'
$CompilerPath = (Resolve-Path -LiteralPath $CompilerPath).Path
$compilerSha = Get-Sha256 $CompilerPath
$versionInfo = (Get-Item -LiteralPath $CompilerPath).VersionInfo
$compilerVersion = [version]::new($versionInfo.FileMajorPart, $versionInfo.FileMinorPart,
                                 $versionInfo.FileBuildPart, $versionInfo.FilePrivatePart)
if ($compilerVersion -lt [version]'6.4.0.0' -or (Get-Sha256 $CompilerPath) -ne $compilerSha) {
    throw 'The explicit compiler must be unchanged Inno Setup 6.4+.'
}

$wheels = @(Get-ChildItem -LiteralPath $applicationWheels -Filter '*.whl' -File)
if ($wheels.Count -ne 1) {
    throw 'Expected exactly the one application wheel already used by full-bundle acceptance.'
}
$applicationWheel = $wheels[0].FullName
$wheelKey = 'wheelhouse/' + $wheels[0].Name
$fullBefore = Get-Sha256 $fullManifest
$full = Get-Content -LiteralPath $fullManifest -Raw | ConvertFrom-Json
$wheelEntry = $full.files.PSObject.Properties[$wheelKey]
if ($null -eq $wheelEntry -or $wheelEntry.Value -ne (Get-Sha256 $applicationWheel)) {
    throw 'The acquired application wheel differs from the existing accepted full-bundle seal.'
}

# Estimate before creating another copy. The actual new seal is checked again.
[long]$payloadEstimate = 0
foreach ($entry in $full.files.PSObject.Properties) {
    if ($entry.Name -notmatch '^(audio|audio-runtime)/') {
        $payloadEstimate += (Get-Item -LiteralPath (Join-Path $fullBundle $entry.Name)).Length
    }
}
$deliveryLimit = 2GB - 1
if ($payloadEstimate -gt $deliveryLimit) {
    throw "The requested model/Python/OCR/native payload exceeds the unchanged delivery budget ($deliveryLimit bytes). No model was silently omitted and no guard was widened."
}
$initialFree = Assert-FreeSpace (2 * $payloadEstimate + 512MB) 'Fresh sealed-bundle assembly'
if (Test-Path -LiteralPath $evidence) {
    throw 'Use fresh CI evidence; standard-installer output already exists.'
}
New-Item -ItemType Directory -Path $evidence | Out-Null
New-Item -ItemType Directory -Path $private | Out-Null

Push-Location $project
try {
    $sourceCommit = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceCommit -cnotmatch '^[0-9a-f]{40}$') {
        throw 'Cannot bind the installer to the exact checked-out source commit.'
    }
    $sourceTree = (git rev-parse 'HEAD^{tree}').Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceTree -cnotmatch '^[0-9a-f]{40}$' -or
        $full.source_commit -cne $sourceCommit) {
        throw 'The accepted full bundle does not belong to the exact current source.'
    }
    Write-PublicJson @{
        schema = 1; status = 'recorded'; compiler_path = $CompilerPath
        compiler_version = $compilerVersion.ToString(); compiler_sha256 = $compilerSha
        source_commit = $sourceCommit; source_tree = $sourceTree
        application_wheel_sha256 = Get-Sha256 $applicationWheel
        python_archive_sha256 = Get-Sha256 $pythonArchive
        model_manifest_sha256 = Get-Sha256 $env:CARR_CI_MODEL_MANIFEST
        ocr_source_archive_sha256 = Get-Sha256 $ocrSource
        native_launcher_sha256 = Get-Sha256 (Join-Path $nativeLauncher 'ResearchLibrary.exe')
        existing_full_manifest_sha256 = $fullBefore
        estimated_payload_bytes = $payloadEstimate; initial_free_bytes = $initialFree
        authenticode_verified = $false; nvda_verified = $false
    } 'inputs.json'

    # A fresh builder invocation omits --audio-dir. It does not strip or alter
    # the existing full bundle, its manifest, or its current local audio proof.
    $arguments = @('-I', '-B', 'packaging/build_windows_bundle.py',
        '--wheel', $applicationWheel, '--wheelhouse', $wheelhouse,
        '--lock', 'packaging/windows-integrated-cp311.lock',
        '--sbom', 'packaging/windows-integrated-sbom.cdx.json',
        '--python-archive', $pythonArchive,
        '--model-dir', $env:CARR_CI_MODEL_DIR, '--model-manifest', $env:CARR_CI_MODEL_MANIFEST,
        '--ocr-dir', $env:CARR_CI_OCR_DIR, '--ocr-source-archive', $ocrSource,
        '--native-launcher', (Join-Path $nativeLauncher 'ResearchLibrary.exe'),
        '--native-launcher-evidence', (Join-Path $nativeLauncher 'build-evidence.json'),
        '--source-commit', $sourceCommit, '--output', $baseBundle)
    Invoke-Logged $env:CARR_PYTHON $arguments 'base-build.log'
    Invoke-Logged $env:CARR_PYTHON @('-I', '-B', 'packaging/add_native_runtime.py',
        '--bundle', $baseBundle, '--native-runtime', $nativeRuntime, '--output', $bundle) 'native-runtime.log'
    # Only this invocation's successfully consumed intermediate is removed.
    Remove-Item -LiteralPath $baseBundle -Recurse -Force

    $manifestPath = Join-Path $bundle 'bundle-manifest.json'
    $manifestSha = Get-Sha256 $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    foreach ($flag in @('bundled_python', 'bundled_model', 'bundled_ocr',
                         'bundled_native_runtime', 'bundled_native_launcher')) {
        if ($manifest.$flag -isnot [bool] -or -not $manifest.$flag) {
            throw "The fresh standard-installer bundle is missing $flag."
        }
    }
    if ($manifest.bundled_audio_runtime -isnot [bool] -or $manifest.bundled_audio_runtime -ne $false) {
        throw 'The fresh bundle must explicitly contain no audio runtime.'
    }
    [long]$payloadBytes = 0
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $payloadBytes += (Get-Item -LiteralPath (Join-Path $bundle $entry.Name)).Length
    }
    if ($payloadBytes -gt $deliveryLimit) {
        throw 'The fresh payload exceeds the existing delivery byte limit.'
    }
    $compileFree = Assert-FreeSpace (4 * $payloadBytes + 512MB) 'Both independently verified Inno builds'
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $evidence 'payload-manifest.json')
    $setupName = 'Resonance-Setup-' + $sourceCommit.Substring(0, 12) + '.exe'
    $setup = Join-Path $goodBuild ('dist/' + $setupName)
    $failedSetup = Join-Path $failedBuild ('dist/' + $setupName)
    $common = @('--bundle', $bundle, '--source-commit', $sourceCommit,
        '--manifest-sha256', $manifestSha, '--compiler', $CompilerPath,
        '--compiler-sha256', $compilerSha)
    Invoke-Logged $env:CARR_PYTHON (@('-I', '-B', 'packaging/build_standard_installer.py') +
        $common + @('--output', $goodBuild)) 'good-build.log'
    Invoke-Logged $env:CARR_PYTHON (@('-I', '-B', 'tests/packaging/build_standard_failure_fixture.py') +
        $common + @('--output', $failedBuild)) 'negative-build.log'
    $setupSha = Get-Sha256 $setup
    $failedSetupSha = Get-Sha256 $failedSetup
    if ($setupSha -ceq $failedSetupSha) {
        throw 'The negative bootstrap setup must be a distinct verified fixture.'
    }
    foreach ($pair in @(@($goodBuild, 'good'), @($failedBuild, 'negative'))) {
        $buildDirectory, $label = $pair
        Copy-Item -LiteralPath (Join-Path $buildDirectory 'installer-build.json') -Destination (Join-Path $evidence ($label + '-build.json'))
        Copy-Item -LiteralPath (Join-Path $buildDirectory 'payload/bundle-manifest.json') -Destination (Join-Path $evidence ($label + '-manifest.json'))
    }

    $verifyBuilds = @'
import hashlib, importlib.util, json, pathlib, sys
project, bundle, source, manifest_hash, compiler_hash, good, negative, evidence, installed = sys.argv[1:]
project, bundle, good, negative, evidence, installed = map(pathlib.Path, (project, bundle, good, negative, evidence, installed))
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
spec = importlib.util.spec_from_file_location('standard_driver_delivery', project / 'packaging/create_delivery.py')
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)
raw = delivery._read_manifest(bundle, lambda: False)
assert hashlib.sha256(raw).hexdigest() == manifest_hash
manifest = delivery._manifest(raw, source)
delivery._require_transferable_audio_inventory(manifest)
delivery._trusted_verify(bundle, manifest_bytes=raw)
assert manifest['bundled_model'] is True and manifest['bundled_native_runtime'] is True
import carr_library
origin = pathlib.Path(carr_library.__file__).resolve()
assert origin.is_relative_to(installed.resolve().parents[1])
assert pathlib.Path(sys.executable).resolve() == installed.resolve()
for folder, label in ((good, 'good'), (negative, 'negative')):
    report = json.loads((folder / 'installer-build.json').read_text(encoding='utf-8'))
    raw = (folder / 'payload/bundle-manifest.json').read_bytes()
    item = delivery._manifest(raw, source)
    delivery._require_transferable_audio_inventory(item)
    delivery._trusted_verify(folder / 'payload', manifest_bytes=raw)
    expected_path = 'dist/Resonance-Setup-' + source[:12] + '.exe'
    executable = folder / expected_path
    assert report['schema'] == 1 and report['kind'] == 'standard-windows-installer'
    assert report['status'] == 'compiled' and report['source_commit'] == source
    assert report['compiler_sha256'] == compiler_hash
    assert report['manifest_sha256'] == hashlib.sha256(raw).hexdigest()
    assert report['recipe_sha256'] == digest(folder / 'Resonance.iss')
    assert report['installer']['path'] == expected_path
    assert report['installer']['sha256'] == digest(executable)
    assert report['installer']['bytes'] == executable.stat().st_size > 0
    assert report['payload_files'] == len(item['files'])
    assert report['native_install_repair_uninstall'] == 'NOT_RUN'
    assert all(report[name] is False for name in ('authenticode_verified', 'nvda_verified', 'update_or_entitlement_authority', 'automatic_network'))
    if label == 'good':
        assert report['manifest_sha256'] == manifest_hash
    else:
        assert item['fixture_purpose'] == 'NEGATIVE TEST ONLY: bootstrap exits 23'
        assert report['manifest_sha256'] != manifest_hash
    assert (evidence / (label + '-manifest.json')).read_bytes() == raw
print(json.dumps({'status': 'PASS', 'exact_builds_verified': True, 'installed_probe_origin': True,
                  'payload_bytes': sum((bundle / name).stat().st_size for name in manifest['files'])}))
'@
    Invoke-Logged $installedPython @('-I', '-B', '-c', $verifyBuilds, $project, $bundle,
        $sourceCommit, $manifestSha, $compilerSha, $goodBuild, $failedBuild, $evidence, $installedPython) 'build-verification.log'
    # Keep setups, recipes, exact public manifests and reports. These two fresh
    # verified source copies are no longer needed by the native setup probe.
    Remove-Item -LiteralPath (Join-Path $goodBuild 'payload') -Recurse -Force
    Remove-Item -LiteralPath (Join-Path $failedBuild 'payload') -Recurse -Force
    [long]$environmentBytes = (Get-ChildItem -LiteralPath (Join-Path $fullBundle '.venv') -File -Recurse |
        Measure-Object -Property Length -Sum).Sum
    $probeFree = Assert-FreeSpace (4 * $payloadBytes + 2 * $environmentBytes + 512MB) 'Actual install/failure/repair/uninstall'
    Write-PublicJson @{
        status = 'recorded'; payload_bytes = $payloadBytes; installed_environment_bytes = $environmentBytes
        free_before_compile = $compileFree; free_before_probe = $probeFree
        authenticode_verified = $false; nvda_verified = $false
    } 'disk-budget.json'

    Invoke-Logged $installedPython @('-I', '-B', 'packaging/probe_standard_installer_windows.py',
        '--setup', $setup, '--setup-sha256', $setupSha,
        '--failed-setup', $failedSetup, '--failed-setup-sha256', $failedSetupSha,
        '--source-commit', $sourceCommit, '--manifest-sha256', $manifestSha,
        '--work', $probeWork, '--report', $nativeReport, '--disposable-ci') 'native-probe.log'

    $verifyNative = @'
from collections import Counter
import hashlib, importlib.util, json, pathlib, sys
evidence, source, tree, manifest_hash, setup, setup_hash, failed, failed_hash, compiler, compiler_hash, full_manifest, full_hash, project = sys.argv[1:]
evidence, setup, failed, compiler, full_manifest, project = map(pathlib.Path, (evidence, setup, failed, compiler, full_manifest, project))
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
report = json.loads((evidence / 'native.json').read_text(encoding='utf-8'))
checks = ['active-application-blocks-uninstall', 'failed-bootstrap-preserves-previous', 'install', 'repair', 'uninstall', 'failed-environment-preserved']
true_flags = ['old_process_survived_repair', 'active_application_blocks_uninstall', 'failed_bootstrap_preserves_previous_registration',
              'failed_environment_preserved', 'research_and_unknown_files_preserved',
              'one_start_menu_entry', 'native_install_repair_uninstall', 'actual_installed_app_self_test']
false_flags = ['authenticode_verified', 'nvda_verified']
assert set(report) == set(['schema', 'status', 'source_commit', 'installer_sha256',
                          'manifest_sha256', 'failed_installer_sha256', 'checks'] + true_flags + false_flags)
assert type(report['schema']) is int and report['schema'] == 1 and report['status'] == 'passed'
assert type(report['checks']) is list and len(report['checks']) == 6
assert all(type(item) is str for item in report['checks']) and Counter(report['checks']) == Counter(checks)
assert all(report[name] is True for name in true_flags)
assert all(report[name] is False for name in false_flags)
assert report['source_commit'] == source
assert report['manifest_sha256'] == manifest_hash == digest(evidence / 'payload-manifest.json')
assert report['installer_sha256'] == setup_hash == digest(setup)
assert report['failed_installer_sha256'] == failed_hash == digest(failed)
assert setup_hash != failed_hash and digest(compiler) == compiler_hash
assert digest(full_manifest) == full_hash
spec = importlib.util.spec_from_file_location('standard_final_delivery', project / 'packaging/create_delivery.py')
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)
# This checks the original local full-bundle seal; it grants no transfer
# authority for that bundle's audio and does not change any of its bytes.
delivery._trusted_verify(full_manifest.parent, manifest_bytes=full_manifest.read_bytes())
assert digest(full_manifest) == full_hash
inputs = json.loads((evidence / 'inputs.json').read_text(encoding='utf-8-sig'))
assert inputs['source_commit'] == source and inputs['source_tree'] == tree
assert inputs['compiler_sha256'] == compiler_hash and inputs['existing_full_manifest_sha256'] == full_hash
report.update(source_tree=tree, check_count=6, compiler_sha256=compiler_hash,
              compiler_version=inputs['compiler_version'], existing_full_sealed_payload_unchanged=True,
              probe_interpreter='existing-installed-application', audio_transfer_guard='ENFORCED',
              source_files_sha256={name: digest(project / name) for name in (
                  'scripts/ci_standard_installer.ps1', 'packaging/build_standard_installer.py',
                  'packaging/probe_standard_installer_windows.py', 'packaging/standard_windows.iss.in',
                  'tests/packaging/build_standard_failure_fixture.py')})
with (evidence / 'proof.json').open('x', encoding='utf-8') as stream:
    json.dump(report, stream, indent=2)
    stream.write('\n')
print(json.dumps(report, sort_keys=True))
'@
    Invoke-Logged $installedPython @('-I', '-B', '-c', $verifyNative, $evidence,
        $sourceCommit, $sourceTree, $manifestSha, $setup, $setupSha, $failedSetup, $failedSetupSha,
        $CompilerPath, $compilerSha, $fullManifest, $fullBefore, $project) 'verdict.log'

    # A later, separate A-to-B gate may reuse this exact compiled B. This is a
    # private path handoff, written only after the original native gate passes.
    # Its consumer rechecks these hashes and the complete sealed payload.
    $handoff = Join-Path $private 'standard-update-inputs.json'
    @{
        schema = 1; kind = 'verified-standard-installer-ci-inputs'
        source_commit = $sourceCommit; source_tree = $sourceTree
        build = $goodBuild; bundle = $bundle; setup_sha256 = $setupSha
        manifest_sha256 = $manifestSha; compiler = $CompilerPath
        compiler_sha256 = $compilerSha; installed_python = $installedPython
        full_manifest = $fullManifest; full_manifest_sha256 = $fullBefore
        native_proof = (Join-Path $evidence 'proof.json')
        native_proof_sha256 = (Get-Sha256 (Join-Path $evidence 'proof.json'))
        payload_bytes = $payloadBytes; installed_environment_bytes = $environmentBytes
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $handoff -Encoding utf8
    if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) {
        "CARR_STANDARD_B_HANDOFF=$handoff" | Add-Content -LiteralPath $env:GITHUB_ENV -Encoding utf8
    }
}
finally {
    # Preserve only fixed, synthetic native text logs in public CI evidence.
    # Never upload setups, wheels, model/runtime binaries, generations or user
    # sentinels; never force-delete a failed installation to manufacture PASS.
    foreach ($name in @('install-setup.log', 'install-process.log', 'install-shortcut.txt',
                         'failed-bootstrap-setup.log', 'failed-bootstrap-process.log',
                         'repair-setup.log', 'repair-process.log', 'repair-shortcut.txt',
                         'uninstall-setup.log', 'uninstall-process.log',
                         'blocked-uninstall-setup.log', 'blocked-uninstall-process.log')) {
        $log = Join-Path $probeWork $name
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            try {
                Copy-Item -LiteralPath $log -Destination (Join-Path $evidence $name)
            }
            catch {
                Write-Warning "Could not retain the optional fixed native log $name."
            }
        }
    }
    Pop-Location
}
