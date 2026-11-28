#Requires -Version 7.0
<#
Run after ci_standard_installer.ps1, using its successful private B handoff.
OriginalCheckout is the exact original O checkout; PreviousCheckout is its
explicit maintenance successor A. Build O and A from their own public trees,
then prove ordinary O-to-A maintenance separately from managed A-to-B.
Reuse the already acquired reviewed inputs. Never relabel a wheel, fetch
runtime assets, patch an installed generation, or configure a publisher.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$OriginalCheckout,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$OriginalSource,
    [Parameter(Mandatory=$true)][string]$PreviousCheckout,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousSource,
    [string]$CandidateHandoff = $env:CARR_STANDARD_B_HANDOFF
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or $env:CI -ne 'true') {
    throw 'The original-maintenance-update proof requires a disposable native Windows CI account.'
}
foreach ($value in @($env:RUNNER_TEMP, $env:CARR_PYTHON, $env:CARR_CI_MODEL_DIR,
                      $env:CARR_CI_MODEL_MANIFEST, $env:CARR_CI_OCR_DIR, $CandidateHandoff)) {
    if ([string]::IsNullOrWhiteSpace($value)) { throw 'The existing complete native bundle and successful standard B gate are required.' }
}
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$originalRepository = (Resolve-Path -LiteralPath $OriginalCheckout).Path
$previousRepository = (Resolve-Path -LiteralPath $PreviousCheckout).Path
$handoffPath = (Resolve-Path -LiteralPath $CandidateHandoff).Path
if ((Get-Item -LiteralPath $handoffPath).Length -gt 16384) { throw 'Oversized private standard-installer handoff.' }
$handoff = Get-Content -LiteralPath $handoffPath -Raw | ConvertFrom-Json
$installedPython = $handoff.installed_python
$compiler = $handoff.compiler
$compilerSha = $handoff.compiler_sha256
$bundleWork = Join-Path $env:RUNNER_TEMP 'carr-bundle-ci'
$nativeLauncher = Join-Path $env:RUNNER_TEMP 'carr-native-launcher-private'
$private = Join-Path $env:RUNNER_TEMP ('carr-standard-update-' + [guid]::NewGuid().ToString('N'))
$evidence = Join-Path $project 'ci-results/standard-update'
$probeWork = Join-Path $private 'native-proof'

function Digest([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Log) {
    & $Executable @Arguments 2>&1 | Tee-Object -FilePath (Join-Path $evidence $Log) | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Standard-generation command failed with exit $LASTEXITCODE; see standard-update/$Log." }
}
function Require-Free([long]$Bytes, [string]$Phase) {
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($private))
    if ($drive.AvailableFreeSpace -lt $Bytes) {
        throw "$Phase requires $Bytes free bytes. Existing approved payloads and failed native generations were preserved."
    }
}
if ((Test-Path -LiteralPath $evidence) -or (Test-Path -LiteralPath $private)) {
    throw 'Use fresh original-maintenance-update outputs and evidence, not a previous run.'
}
New-Item -ItemType Directory -Path $private, $evidence | Out-Null
Push-Location $project
try {
    $preflight = @'
import hashlib, importlib.util, json, pathlib, re, subprocess, sys
original, original_source, previous, project, handoff_path, source, launcher, evidence = sys.argv[1:]
original, previous, project, handoff_path, launcher, evidence = map(pathlib.Path,
    (original, previous, project, handoff_path, launcher, evidence))
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value
def git(root, *args):
    return subprocess.check_output(['git', '--no-replace-objects', '-C', str(root), *args],
        timeout=30).decode('utf-8').strip()
assert all(re.fullmatch('[0-9a-f]{40}', value) for value in (original_source, source))
record = json.loads(handoff_path.read_text(encoding='utf-8-sig'))
assert record['schema'] == 1 and record['kind'] == 'verified-standard-installer-ci-inputs'
candidate = git(project.parent, 'rev-parse', 'HEAD')
assert candidate == record['source_commit'] and len({original_source, source, candidate}) == 3
assert git(project.parent, 'rev-parse', 'HEAD^{tree}') == record['source_tree']
assert git(original, 'rev-parse', 'HEAD') == original_source
assert git(previous, 'rev-parse', 'HEAD') == source
for repository, ancestor, descendant in ((previous, original_source, source),
                                        (project.parent, source, candidate)):
    subprocess.run(['git', '--no-replace-objects', '-C', str(repository),
        'merge-base', '--is-ancestor', ancestor, descendant], check=True, timeout=30)
for repository in (original, previous, project.parent):
    subprocess.run(['git', '--no-replace-objects', '-C', str(repository),
        'diff', '--quiet', 'HEAD', '--', 'carr-library'], check=True, timeout=30)
assert pathlib.Path(sys.executable).resolve() == pathlib.Path(record['installed_python']).resolve()
import carr_library, carr_library.security.standard_installation
for value in (carr_library, carr_library.security.standard_installation):
    assert pathlib.Path(value.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
proof_path = pathlib.Path(record['native_proof'])
assert digest(proof_path) == record['native_proof_sha256']
proof = json.loads(proof_path.read_text(encoding='utf-8'))
assert proof['status'] == 'passed' and proof['source_commit'] == candidate
assert proof['native_install_repair_uninstall'] is True
assert proof['actual_installed_app_self_test'] is True
assert proof['manifest_sha256'] == record['manifest_sha256']
assert proof['installer_sha256'] == record['setup_sha256']
assert digest(pathlib.Path(record['compiler'])) == record['compiler_sha256']
assert digest(pathlib.Path(record['compiler']).with_name('ISCmplr.dll')) == record['compiler_engine_sha256']
full = pathlib.Path(record['full_manifest'])
assert digest(full) == record['full_manifest_sha256']
delivery = module(project / 'packaging/create_delivery.py', 'update_ci_delivery')
delivery._trusted_verify(full.parent, manifest_bytes=full.read_bytes())
probe = module(project / 'packaging/probe_standard_update_windows.py', 'update_ci_probe')
probe.build_inputs(record['build'], candidate, record['setup_sha256'], record['manifest_sha256'],
    delivery, bundle=record['bundle'])
for relative in ('packaging/windows-integrated-cp311.lock', 'packaging/windows-integrated-sbom.cdx.json'):
    expected = digest(project / relative)
    for repository in (original, previous):
        assert digest(repository / 'carr-library' / relative) == expected, (
            'O, maintenance A and B require the same already reviewed dependency closure: ' + relative)
# Each predecessor's own validator must accept the exact existing native image.
# No new toolchain, native runtime, model or dependency is acquired.
for label, repository in (('original', original), ('previous', previous)):
    native = module(repository / 'carr-library/packaging/native_launcher.py', label + '_native_validator')
    native.capture_native_launcher(launcher / 'ResearchLibrary.exe', launcher / 'build-evidence.json')
value = {'schema': 1, 'status': 'verified-inputs',
    'original_source': original_source, 'original_tree': git(original, 'rev-parse', 'HEAD^{tree}'),
    'previous_source': source,
    'previous_tree': git(previous, 'rev-parse', 'HEAD^{tree}'),
    'candidate_source': candidate, 'candidate_tree': record['source_tree'],
    'compiler_sha256': record['compiler_sha256'],
    'compiler_engine_sha256': record['compiler_engine_sha256'],
    'candidate_setup_sha256': record['setup_sha256'],
    'candidate_manifest_sha256': record['manifest_sha256'],
    'existing_full_manifest_sha256': record['full_manifest_sha256'],
    'dependency_lock_sha256': digest(project / 'packaging/windows-integrated-cp311.lock'),
    'dependency_sbom_sha256': digest(project / 'packaging/windows-integrated-sbom.cdx.json'),
    'synthetic_publisher_and_anchor': True, 'production_authority_qualified': False,
    'authenticode_verified': False, 'nvda_verified': False}
with (evidence / 'inputs.json').open('x', encoding='utf-8') as stream:
    json.dump(value, stream, indent=2); stream.write('\n')
print(json.dumps(value, sort_keys=True))
'@
    Invoke-Checked $installedPython @('-I', '-B', '-c', $preflight, $originalRepository, $OriginalSource,
        $previousRepository, $project, $handoffPath, $PreviousSource, $nativeLauncher, $evidence) 'preflight.log'
    function Build-ReviewedGeneration(
        [string]$Checkout,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$Source,
        [ValidateSet('original', 'previous')][string]$Label
    ) {
        $previousRepository = $Checkout
        $PreviousSource = $Source
        $previousProject = Join-Path $previousRepository 'carr-library'
        $application = Join-Path $private ($Label + '-application')
        $baseBundle = Join-Path $private ($Label + '-base-bundle')
        $previousBundle = Join-Path $private ($Label + '-sealed-bundle')
        $previousBuild = Join-Path $private ($Label + '-build')
        foreach ($path in @($application, $baseBundle, $previousBundle, $previousBuild)) {
            if (Test-Path -LiteralPath $path) { throw 'Generation output already exists; no previous bytes were changed.' }
        }
        New-Item -ItemType Directory -Path $application | Out-Null
        Require-Free (3 * [long]$handoff.payload_bytes + 512MB) ('Building exact ' + $Label + ' source')

        # The source checkout is explicit. pip sees only already installed build
        # tools; no dependency resolution or package network access is permitted.
        Invoke-Checked $env:CARR_PYTHON @('-I', '-B', '-m', 'pip', 'wheel', '--no-index',
            '--no-deps', '--no-build-isolation', '--wheel-dir', $application, $previousProject) ($Label + '-wheel-build.log')
        $wheels = @(Get-ChildItem -LiteralPath $application -File -Filter '*.whl')
        if ($wheels.Count -ne 1) { throw 'Expected one wheel actually built from the exact previous checkout.' }
        $previousWheel = $wheels[0].FullName
        $sourceProof = @'
import hashlib, importlib.util, json, pathlib, shutil, sys, zipfile
previous, source, wheel, candidate_bundle, candidate_project, candidate_source, candidate_manifest_hash, evidence, label = sys.argv[1:]
assert label in {'original', 'previous'}
previous, wheel, candidate_bundle, candidate_project, evidence = map(pathlib.Path,
    (previous, wheel, candidate_bundle, candidate_project, evidence))
spec = importlib.util.spec_from_file_location('previous_exact_wheel', previous / 'carr-library/packaging/source_wheel.py')
verify = importlib.util.module_from_spec(spec); spec.loader.exec_module(verify)
proof = verify.verify_application_wheel(wheel, previous, source)
# The sealed bundle contains its actual wheelhouse, not an application-source.json sidecar.
# Re-prove B against its current exact checkout rather than inferring it from a filename.
candidate_manifest_bytes = (candidate_bundle / 'bundle-manifest.json').read_bytes()
assert hashlib.sha256(candidate_manifest_bytes).hexdigest() == candidate_manifest_hash
candidate_manifest = json.loads(candidate_manifest_bytes)
assert candidate_manifest['source_commit'] == candidate_source
candidate_wheels = list((candidate_bundle / 'wheelhouse').glob('universal_research_library-*.whl'))
assert len(candidate_wheels) == 1
candidate_wheel = candidate_wheels[0]
relative = candidate_wheel.relative_to(candidate_bundle).as_posix()
with candidate_wheel.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == candidate_manifest['files'][relative]
candidate_spec = importlib.util.spec_from_file_location('candidate_exact_wheel',
    candidate_project / 'packaging/source_wheel.py')
candidate_verify = importlib.util.module_from_spec(candidate_spec); candidate_spec.loader.exec_module(candidate_verify)
candidate = candidate_verify.verify_application_wheel(candidate_wheel, candidate_project.parent, candidate_source)
candidate_name = ('original-' if label == 'original' else '') + 'candidate-application-wheel.json'
with (evidence / candidate_name).open('x', encoding='utf-8') as stream:
    json.dump(candidate, stream, indent=2); stream.write('\n')
assert proof['source_commit'] == source != candidate['source_commit']
assert proof['wheel_sha256'] != candidate['wheel_sha256'], 'A must be its real source wheel, never relabeled B.'
destination = evidence / (label + '-application-wheel')
destination.mkdir()
target = destination / wheel.name
shutil.copyfile(wheel, target)
with target.open('rb') as stream:
    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
assert actual == proof['wheel_sha256']
with zipfile.ZipFile(wheel) as archive:
    expanded = sum(item.file_size for item in archive.infolist() if not item.is_dir())
assert 0 < expanded < 2 * 1024**3
manifest = {'schema': 1, 'kind': 'tested-public-source-wheel', **proof,
    'wheel_file': wheel.name, 'wheel_bytes': target.stat().st_size, 'wheel_expanded_bytes': expanded,
    'candidate_source_commit': candidate['source_commit'], 'candidate_wheel_sha256': candidate['wheel_sha256'],
    'native_qualification': 'NOT_RUN', 'production_authority_qualified': False,
    'authenticode_verified': False, 'nvda_verified': False}
with (destination / 'manifest.json').open('x', encoding='utf-8') as stream:
    json.dump(manifest, stream, indent=2); stream.write('\n')
print(json.dumps(manifest, sort_keys=True))
'@
        Invoke-Checked $installedPython @('-I', '-B', '-c', $sourceProof, $previousRepository,
            $PreviousSource, $previousWheel, $handoff.bundle, $project, $handoff.source_commit,
            $handoff.manifest_sha256, $evidence, $Label) ($Label + '-wheel-proof.log')

        Require-Free (3 * ([long]$handoff.payload_bytes + (Get-Item -LiteralPath $previousWheel).Length) + 512MB) `
            ('Preparing exact ' + $Label + ' sealed payload and setup')
        $arguments = @('-I', '-B', (Join-Path $previousProject 'packaging/build_windows_bundle.py'),
            '--wheel', $previousWheel, '--wheelhouse', (Join-Path $bundleWork 'wheelhouse'),
            '--lock', (Join-Path $previousProject 'packaging/windows-integrated-cp311.lock'),
            '--sbom', (Join-Path $previousProject 'packaging/windows-integrated-sbom.cdx.json'),
            '--python-archive', (Join-Path $bundleWork 'windows-python.tar.gz'),
            '--model-dir', $env:CARR_CI_MODEL_DIR, '--model-manifest', $env:CARR_CI_MODEL_MANIFEST,
            '--ocr-dir', $env:CARR_CI_OCR_DIR,
            '--ocr-source-archive', (Join-Path $bundleWork 'ocr-corresponding-source.tar'),
            '--native-launcher', (Join-Path $nativeLauncher 'ResearchLibrary.exe'),
            '--native-launcher-evidence', (Join-Path $nativeLauncher 'build-evidence.json'),
            '--source-commit', $PreviousSource, '--output', $baseBundle)
        Invoke-Checked $env:CARR_PYTHON $arguments ($Label + '-base-bundle.log')
        Invoke-Checked $env:CARR_PYTHON @('-I', '-B', (Join-Path $previousProject 'packaging/add_native_runtime.py'),
            '--bundle', $baseBundle, '--native-runtime', (Join-Path $bundleWork 'native-runtime'),
            '--output', $previousBundle) ($Label + '-native-runtime.log')
        Remove-Item -LiteralPath $baseBundle -Recurse -Force
        $previousManifest = Join-Path $previousBundle 'bundle-manifest.json'
        $previousManifestSha = Digest $previousManifest
        Invoke-Checked $env:CARR_PYTHON @('-I', '-B', (Join-Path $previousProject 'packaging/build_standard_installer.py'),
            '--bundle', $previousBundle, '--source-commit', $PreviousSource,
            '--manifest-sha256', $previousManifestSha, '--compiler', $compiler,
            '--compiler-sha256', $compilerSha, '--output', $previousBuild) ($Label + '-standard-build.log')
        $previousSetup = Join-Path $previousBuild ('dist/Resonance-Setup-' + $PreviousSource.Substring(0, 12) + '.exe')
        $previousSetupSha = Digest $previousSetup
        $checkPrevious = @'
import hashlib, importlib.util, json, pathlib, sys
project, previous, bundle, build, source, setup_hash, manifest_hash, compiler_hash, evidence, label = sys.argv[1:]
assert label in {'original', 'previous'}
project, previous, bundle, build, evidence = map(pathlib.Path, (project, previous, bundle, build, evidence))
def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value
delivery = module(project / 'packaging/create_delivery.py', 'previous_delivery')
probe = module(project / 'packaging/probe_standard_update_windows.py', 'previous_probe')
setup, raw_report, raw_manifest = probe.build_inputs(build, source, setup_hash, manifest_hash, delivery, bundle=bundle)
report, manifest = json.loads(raw_report), json.loads(raw_manifest)
assert report['compiler_sha256'] == compiler_hash
assert report['recipe_sha256'] == hashlib.sha256((build / 'Resonance.iss').read_bytes()).hexdigest()
assert all(manifest[key] is True for key in ('bundled_python', 'bundled_model', 'bundled_ocr',
    'bundled_native_runtime', 'bundled_native_launcher'))
assert manifest['bundled_audio_runtime'] is False
assert 0 < sum((bundle / name).stat().st_size for name in manifest['files']) < 2 * 1024**3
artifact = json.loads((evidence / (label + '-application-wheel') / 'manifest.json').read_text(encoding='utf-8'))
wheel_path = bundle / 'wheelhouse' / artifact['wheel_file']
assert wheel_path.parent == bundle / 'wheelhouse'
assert hashlib.sha256(wheel_path.read_bytes()).hexdigest() == manifest['files'][wheel_path.relative_to(bundle).as_posix()]
source_verifier = module(previous / 'carr-library/packaging/source_wheel.py', 'previous_sealed_source')
wheel = source_verifier.verify_application_wheel(wheel_path, previous, source)
assert wheel['source_commit'] == artifact['source_commit'] == source
assert wheel['wheel_sha256'] == artifact['wheel_sha256']
with (evidence / (label + '-build.json')).open('xb') as stream:
    stream.write(raw_report)
with (evidence / (label + '-manifest.json')).open('xb') as stream:
    stream.write(raw_manifest)
print(json.dumps({'status': 'verified-build', 'source_commit': source, 'setup_sha256': setup_hash}))
'@
        Invoke-Checked $installedPython @('-I', '-B', '-c', $checkPrevious, $project, $previousRepository,
            $previousBundle, $previousBuild, $PreviousSource, $previousSetupSha, $previousManifestSha,
            $compilerSha, $evidence, $Label) ($Label + '-build-verification.log')
        # Only this successful builder's redundant payload copy is removed.
        # The separate exact sealed input, reports and compiled setup are retained.
        Remove-Item -LiteralPath (Join-Path $previousBuild 'payload') -Recurse -Force
        [long]$payloadBytes = 0
        $sealed = Get-Content -LiteralPath $previousManifest -Raw | ConvertFrom-Json
        foreach ($entry in $sealed.files.PSObject.Properties) {
            $payloadBytes += (Get-Item -LiteralPath (Join-Path $previousBundle $entry.Name)).Length
        }
        $wheelReceipt = Get-Content -LiteralPath (Join-Path $evidence ($Label + '-application-wheel/manifest.json')) -Raw | ConvertFrom-Json
        return [pscustomobject]@{
            Build = $previousBuild; Bundle = $previousBundle; Source = $PreviousSource
            Setup = $previousSetup; SetupSha = $previousSetupSha; ManifestSha = $previousManifestSha
            PayloadBytes = $payloadBytes; SetupBytes = (Get-Item -LiteralPath $previousSetup).Length
            WheelExpandedBytes = [long]$wheelReceipt.wheel_expanded_bytes
        }
    }
    $original = Build-ReviewedGeneration -Checkout $originalRepository -Source $OriginalSource -Label 'original'
    $previous = Build-ReviewedGeneration -Checkout $previousRepository -Source $PreviousSource -Label 'previous'
    $previousBuild = $previous.Build
    $previousBundle = $previous.Bundle
    $previousSetup = $previous.Setup
    $previousSetupSha = $previous.SetupSha
    $previousManifestSha = $previous.ManifestSha
    [long]$setupBytes = (Get-Item -LiteralPath $previousSetup).Length
    $candidateSetup = Join-Path $handoff.build ('dist/Resonance-Setup-' + $handoff.source_commit.Substring(0, 12) + '.exe')
    $setupBytes = [Math]::Max($setupBytes, (Get-Item -LiteralPath $candidateSetup).Length)
    $setupBytes = [Math]::Max($setupBytes, [long]$original.SetupBytes)
    # Sealed O/A payload sizes and expanded application wheels are measured.
    # The same exact dependency closure uses B's measured environment as a
    # conservative baseline; O/A application bytes are reserved additionally.
    # Retain all old generations, rejected B copies and restart snapshots.
    [long]$nativePayloadBytes = $original.PayloadBytes + $previous.PayloadBytes + 3 * [long]$handoff.payload_bytes
    [long]$nativeEnvironmentBytes = 5 * [long]$handoff.installed_environment_bytes + $original.WheelExpandedBytes + $previous.WheelExpandedBytes
    [long]$nativeSetupBytes = 14 * $setupBytes + $original.SetupBytes
    [long]$requiredNativeBytes = $nativePayloadBytes + $nativeEnvironmentBytes + $nativeSetupBytes + 512MB
    [ordered]@{
        schema = 1; original_payload_bytes = $original.PayloadBytes; maintenance_payload_bytes = $previous.PayloadBytes
        candidate_payload_bytes = [long]$handoff.payload_bytes
        measured_candidate_environment_bytes = [long]$handoff.installed_environment_bytes
        original_application_expanded_bytes = $original.WheelExpandedBytes
        maintenance_application_expanded_bytes = $previous.WheelExpandedBytes
        native_payload_reservation_bytes = $nativePayloadBytes
        native_environment_reservation_bytes = $nativeEnvironmentBytes
        native_setup_reservation_bytes = $nativeSetupBytes
        headroom_bytes = 512MB; required_free_bytes = $requiredNativeBytes
        actual_original_environment_measurement = 'NOT_YET_INSTALLED'
        actual_maintenance_environment_measurement = 'NOT_YET_INSTALLED'
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $evidence 'disk-budget.json') -Encoding utf8
    Require-Free $requiredNativeBytes 'Original O, maintenance A, rejected B generations, verified B and restart snapshots'
    $candidateProof = Get-Content -LiteralPath (Join-Path $evidence 'candidate-application-wheel.json') -Raw | ConvertFrom-Json
    Invoke-Checked $installedPython @('-I', '-B', 'packaging/probe_standard_update_windows.py',
        '--original-build', $original.Build, '--original-bundle', $original.Bundle,
        '--original-source', $OriginalSource, '--original-setup-sha256', $original.SetupSha,
        '--original-manifest-sha256', $original.ManifestSha,
        '--previous-build', $previousBuild, '--previous-bundle', $previousBundle,
        '--previous-source', $PreviousSource, '--previous-setup-sha256', $previousSetupSha,
        '--previous-manifest-sha256', $previousManifestSha,
        '--candidate-build', $handoff.build, '--candidate-bundle', $handoff.bundle,
        '--candidate-source', $handoff.source_commit, '--candidate-setup-sha256', $handoff.setup_sha256,
        '--candidate-manifest-sha256', $handoff.manifest_sha256,
        '--candidate-version', $candidateProof.project_version, '--work', $probeWork,
        '--library-test-wheels', (Join-Path $env:RUNNER_TEMP 'carr-test-wheels'),
        '--library-report', (Join-Path $evidence 'library.json'),
        '--report', (Join-Path $evidence 'native.json'), '--disposable-ci') 'native-probe.log'

    $verify = @'
from collections import Counter
import hashlib, importlib.util, json, pathlib, re, stat, sys
(project, evidence, handoff_path, previous_build, previous_bundle, previous_setup,
 previous_setup_hash, previous_manifest_hash, original_build, original_bundle,
 original_setup, original_setup_hash, original_manifest_hash) = sys.argv[1:]
(project, evidence, handoff_path, previous_build, previous_bundle, previous_setup,
 original_build, original_bundle, original_setup) = map(pathlib.Path,
    (project, evidence, handoff_path, previous_build, previous_bundle, previous_setup,
     original_build, original_bundle, original_setup))
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value
record = json.loads(handoff_path.read_text(encoding='utf-8-sig'))
inputs = json.loads((evidence / 'inputs.json').read_text(encoding='utf-8'))
report = json.loads((evidence / 'native.json').read_text(encoding='utf-8'))
checks = ['installed-module-origin', 'two-distinct-source-bound-setups', 'native-installed-a',
    'running-a-retains-shared-lease', 'actual-tuf-verification',
    'native-b-health-rejection-rolls-back-a', 'exact-activation-metadata-rollback',
    'rejected-b-retained', 'verified-library-backup', 'actual-standard-a-to-b',
    'old-process-and-sealed-a-retained', 'protected-anchor-b', 'actual-b-self-test',
    'actual-active-uninstall-refusal', 'quiescent-uninstall-preserves-user-data']
true_flags = ['actual_two_compiled_generations', 'actual_native_activation', 'actual_guarded_rollback',
    'actual_old_process_preserved', 'synthetic_publisher_and_anchor', 'actual_windows_vault', 'actual_restart_recovery']
false_flags = ['production_authority_qualified', 'authenticode_verified', 'nvda_verified']
fields = ['schema', 'status', 'checks', 'previous_source', 'candidate_source',
    'previous_setup_sha256', 'candidate_setup_sha256', 'previous_manifest_sha256',
    'candidate_manifest_sha256', 'verified_target_sha256', 'backup_sha256', 'restart_recovery_proof_sha256',
    'system_helper', 'maintenance']
assert set(report) == set(fields + true_flags + false_flags)
assert type(report['schema']) is int and report['schema'] == 1 and report['status'] == 'passed'
helper = report['system_helper']
assert type(helper) is dict and set(helper) == {'selection', 'path', 'device', 'inode',
    'bytes', 'mtime_ns', 'mode', 'attributes', 'links'}
assert helper['selection'] == 'GetSystemDirectoryW/fixed-WindowsPowerShell-v1.0'
assert type(helper['path']) is str and pathlib.Path(helper['path']).is_absolute()
assert all(type(helper[name]) is int and helper[name] >= 0 for name in (
    'device', 'inode', 'bytes', 'mtime_ns', 'mode', 'attributes', 'links'))
assert helper['inode'] > 0 and helper['bytes'] > 0 and helper['mtime_ns'] > 0 and helper['links'] >= 1
assert stat.S_ISREG(helper['mode']) and not stat.S_ISLNK(helper['mode'])
assert helper['attributes'] & 0x400 == 0
from carr_library.security.standard_windows import WindowsStandardHost
actual_helper = WindowsStandardHost._powershell()
actual_info = actual_helper.lstat()
assert helper == {'selection': 'GetSystemDirectoryW/fixed-WindowsPowerShell-v1.0',
    'path': str(actual_helper), 'device': actual_info.st_dev, 'inode': actual_info.st_ino,
    'bytes': actual_info.st_size, 'mtime_ns': actual_info.st_mtime_ns,
    'mode': actual_info.st_mode, 'attributes': getattr(actual_info, 'st_file_attributes', 0),
    'links': actual_info.st_nlink}
assert type(report['checks']) is list and all(type(item) is str for item in report['checks'])
assert Counter(report['checks']) == Counter(checks) and len(report['checks']) == 15
assert all(report[name] is True for name in true_flags) and all(report[name] is False for name in false_flags)
assert report['previous_source'] == inputs['previous_source'] != inputs['candidate_source']
assert len({inputs['original_source'], inputs['previous_source'], inputs['candidate_source']}) == 3
maintenance = report['maintenance']
maintenance_checks = [
    'exact-original-source-installed',
    'ordinary-maintenance-installer-without-update-authority',
    'original-process-and-sealed-generation-preserved',
    'maintenance-native-self-test-and-user-data-preserved']
maintenance_true = ['actual_original_process_preserved',
    'actual_original_sealed_generation_preserved', 'user_data_preserved']
assert type(maintenance) is dict
assert set(maintenance) == set(['status', 'original_source', 'original_setup_sha256',
    'original_manifest_sha256', 'previous_source', 'checks'] + maintenance_true)
assert maintenance['status'] == 'passed' and maintenance['checks'] == maintenance_checks
assert all(maintenance[name] is True for name in maintenance_true)
assert maintenance['original_source'] == inputs['original_source']
assert maintenance['previous_source'] == inputs['previous_source']
assert maintenance['original_setup_sha256'] == original_setup_hash == digest(original_setup)
assert maintenance['original_manifest_sha256'] == original_manifest_hash == digest(evidence / 'original-manifest.json')
maintenance_receipt = evidence / 'original-maintenance.json'
assert json.loads(maintenance_receipt.read_text(encoding='utf-8')) == maintenance
assert report['candidate_source'] == inputs['candidate_source'] == record['source_commit']
assert report['previous_setup_sha256'] == previous_setup_hash == digest(previous_setup)
assert report['previous_manifest_sha256'] == previous_manifest_hash == digest(evidence / 'previous-manifest.json')
assert report['candidate_setup_sha256'] == record['setup_sha256']
assert report['candidate_manifest_sha256'] == record['manifest_sha256']
library = json.loads((evidence / 'library.json').read_text(encoding='utf-8'))
assert library['schema'] == 'carr-standard-library-qualification/1' and library['status'] == 'PASS'
assert library['source_commit'] == record['source_commit']
assert library['manifest_sha256'] == record['manifest_sha256']
assert (library['passed'], library['failed'], library['skipped']) == (50, 0, 0)
assert library['firewall']['profiles_restored'] is library['firewall']['owned_rules_removed'] is True
functional = library['qualification']
qualifier = module(project / 'scripts/qualify_standard_library.py', 'final_library_qualifier')
assert functional['schema'] == 'carr-installed-library-qualification/1' and functional['status'] == 'PASS'
assert functional['case_counts'] == qualifier.EXPECTED and functional['suites'] == qualifier.SUITES
assert functional['source_commit'] == record['source_commit'] and functional['source_tree'] == record['source_tree']
assert functional['manifest_sha256'] == record['manifest_sha256']
assert all(functional[name] is True for name in ('installed_package', 'sealed_files_unchanged', 'production_distributions_unchanged'))
assert functional['audio_payload'] is functional['expected_failures'] is False
for name in ('verified_target_sha256', 'backup_sha256', 'restart_recovery_proof_sha256'):
    assert type(report[name]) is str and re.fullmatch('[0-9a-f]{64}', report[name])
assert report['restart_recovery_proof_sha256'] == digest(evidence / 'native-restart-recover.json')
restart = json.loads((evidence / 'native-restart-recover.json').read_text(encoding='utf-8'))
assert restart['status'] == 'rolled-back' and restart['candidate_source'] == record['source_commit']
assert all(restart[name] is True for name in ('actual_fresh_process', 'actual_windows_vault', 'fresh_tuf_verification'))
assert digest(pathlib.Path(record['compiler'])) == inputs['compiler_sha256'] == record['compiler_sha256']
assert digest(pathlib.Path(record['compiler']).with_name('ISCmplr.dll')) == inputs['compiler_engine_sha256'] == record['compiler_engine_sha256']
delivery = module(project / 'packaging/create_delivery.py', 'final_update_delivery')
probe = module(project / 'packaging/probe_standard_update_windows.py', 'final_update_probe')
probe.build_inputs(original_build, inputs['original_source'], original_setup_hash,
    original_manifest_hash, delivery, bundle=original_bundle)
probe.build_inputs(previous_build, inputs['previous_source'], previous_setup_hash,
    previous_manifest_hash, delivery, bundle=previous_bundle)
probe.build_inputs(record['build'], record['source_commit'], record['setup_sha256'],
    record['manifest_sha256'], delivery, bundle=record['bundle'])
full = pathlib.Path(record['full_manifest'])
assert digest(full) == inputs['existing_full_manifest_sha256'] == record['full_manifest_sha256']
delivery._trusted_verify(full.parent, manifest_bytes=full.read_bytes())
assert digest(full) == record['full_manifest_sha256']
source_artifacts = {}
for label in ('original', 'previous'):
    wheel_directory = evidence / (label + '-application-wheel')
    artifact = json.loads((wheel_directory / 'manifest.json').read_text(encoding='utf-8'))
    assert artifact['source_commit'] == inputs[label + '_source']
    assert artifact['wheel_sha256'] == digest(wheel_directory / artifact['wheel_file'])
    assert artifact['wheel_bytes'] == (wheel_directory / artifact['wheel_file']).stat().st_size
    assert type(artifact['wheel_expanded_bytes']) is int and 0 < artifact['wheel_expanded_bytes'] < 2 * 1024**3
    assert artifact['wheel_sha256'] != artifact['candidate_wheel_sha256']
    source_artifacts[label] = artifact
artifact = source_artifacts['previous']
original_artifact = source_artifacts['original']
assert len({artifact['wheel_sha256'], original_artifact['wheel_sha256'],
            artifact['candidate_wheel_sha256']}) == 3
assert original_artifact['candidate_wheel_sha256'] == artifact['candidate_wheel_sha256']
assert json.loads((evidence / 'original-candidate-application-wheel.json').read_text(encoding='utf-8')) == json.loads(
    (evidence / 'candidate-application-wheel.json').read_text(encoding='utf-8'))
report.update(original_source=inputs['original_source'], original_tree=inputs['original_tree'],
    original_setup_sha256=original_setup_hash, original_manifest_sha256=original_manifest_hash,
    original_wheel_sha256=original_artifact['wheel_sha256'],
    ordinary_maintenance_receipt_sha256=digest(maintenance_receipt),
    disk_budget_sha256=digest(evidence / 'disk-budget.json'),
    maintenance_check_count=4, actual_three_compiled_generations=True,
    historical_original_direct_managed_update='NOT_QUALIFIED',
    previous_tree=inputs['previous_tree'], candidate_tree=inputs['candidate_tree'],
    previous_wheel_sha256=artifact['wheel_sha256'], candidate_wheel_sha256=artifact['candidate_wheel_sha256'],
    compiler_sha256=record['compiler_sha256'], check_count=15,
    existing_full_sealed_payload_unchanged=True, audio_transfer_guard='ENFORCED',
    audio_free_installed_library_cases=50, audio_free_library_proof_sha256=digest(evidence / 'library.json'),
    source_files_sha256={name: digest(project / name) for name in (
        'scripts/ci_standard_update.ps1', 'packaging/probe_standard_update_windows.py',
        'packaging/probe_standard_recovery_child.py', 'src/carr_library/security/standard_recovery.py',
        'scripts/qualify_standard_library.py', 'scripts/qualify_standard_library.ps1',
        'src/carr_library/security/standard_installation.py', 'src/carr_library/security/standard_windows.py')})
with (evidence / 'proof.json').open('x', encoding='utf-8') as stream:
    json.dump(report, stream, indent=2); stream.write('\n')
print(json.dumps(report, sort_keys=True))
'@
    Invoke-Checked $installedPython @('-I', '-B', '-c', $verify, $project, $evidence, $handoffPath,
        $previousBuild, $previousBundle, $previousSetup, $previousSetupSha,
        $previousManifestSha, $original.Build, $original.Bundle, $original.Setup,
        $original.SetupSha, $original.ManifestSha) 'verdict.log'
}
finally {
    # Public evidence contains the exact public O/A source wheels and fixed proof
    # documents/logs only. Native binaries, model/runtime payloads and failed
    # generations remain private on the runner, with no forced cleanup.
    Pop-Location
}
