#Requires -Version 7.0
<#
Run after ci_standard_installer.ps1, using its successful private B handoff.
PreviousCheckout is a separate exact public A checkout with complete ancestry.
Build a real A wheel and standard setup with the already acquired reviewed
inputs. Never relabel B's wheel, fetch runtime assets, or configure a publisher.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PreviousCheckout,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousSource,
    [string]$CandidateHandoff = $env:CARR_STANDARD_B_HANDOFF
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or $env:CI -ne 'true') {
    throw 'The two-generation proof requires a disposable native Windows CI account.'
}
foreach ($value in @($env:RUNNER_TEMP, $env:CARR_PYTHON, $env:CARR_CI_MODEL_DIR,
                      $env:CARR_CI_MODEL_MANIFEST, $env:CARR_CI_OCR_DIR, $CandidateHandoff)) {
    if ([string]::IsNullOrWhiteSpace($value)) { throw 'The existing complete native bundle and successful standard B gate are required.' }
}
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$previousRepository = (Resolve-Path -LiteralPath $PreviousCheckout).Path
$previousProject = Join-Path $previousRepository 'carr-library'
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
$application = Join-Path $private 'previous-application'
$baseBundle = Join-Path $private 'previous-base-bundle'
$previousBundle = Join-Path $private 'previous-sealed-bundle'
$previousBuild = Join-Path $private 'previous-build'
$probeWork = Join-Path $private 'native-proof'

function Digest([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Log) {
    & $Executable @Arguments 2>&1 | Tee-Object -FilePath (Join-Path $evidence $Log)
    if ($LASTEXITCODE -ne 0) { throw "Two-generation command failed with exit $LASTEXITCODE; see standard-update/$Log." }
}
function Require-Free([long]$Bytes, [string]$Phase) {
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($private))
    if ($drive.AvailableFreeSpace -lt $Bytes) {
        throw "$Phase requires $Bytes free bytes. Existing approved payloads and failed native generations were preserved."
    }
}
if (Test-Path -LiteralPath $evidence) { throw 'Use new two-generation evidence, not a previous run.' }
New-Item -ItemType Directory -Path $private, $application, $evidence | Out-Null
Push-Location $project
try {
    $preflight = @'
import hashlib, importlib.util, json, pathlib, re, subprocess, sys
previous, project, handoff_path, source, launcher, evidence = sys.argv[1:]
previous, project, handoff_path, launcher, evidence = map(pathlib.Path,
    (previous, project, handoff_path, launcher, evidence))
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
assert re.fullmatch('[0-9a-f]{40}', source)
record = json.loads(handoff_path.read_text(encoding='utf-8-sig'))
assert record['schema'] == 1 and record['kind'] == 'verified-standard-installer-ci-inputs'
candidate = git(project.parent, 'rev-parse', 'HEAD')
assert candidate == record['source_commit'] != source
assert git(project.parent, 'rev-parse', 'HEAD^{tree}') == record['source_tree']
assert git(previous, 'rev-parse', 'HEAD') == source
subprocess.run(['git', '--no-replace-objects', '-C', str(previous),
    'merge-base', '--is-ancestor', source, candidate], check=True, timeout=30)
for repository in (previous, project.parent):
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
    assert digest(previous / 'carr-library' / relative) == digest(project / relative), (
        'A and B require the same already acquired reviewed dependency closure: ' + relative)
# Reuse the existing native image only if A's own current source validator
# accepts its exact source/build evidence. No new native toolchain is acquired.
native = module(previous / 'carr-library/packaging/native_launcher.py', 'previous_native_validator')
native.capture_native_launcher(launcher / 'ResearchLibrary.exe', launcher / 'build-evidence.json')
value = {'schema': 1, 'status': 'verified-inputs', 'previous_source': source,
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
    Invoke-Checked $installedPython @('-I', '-B', '-c', $preflight, $previousRepository,
        $project, $handoffPath, $PreviousSource, $nativeLauncher, $evidence) 'preflight.log'
    Require-Free (3 * [long]$handoff.payload_bytes + 512MB) 'Building the real previous source'

    # The source checkout is explicit. pip sees only already installed build
    # tools; no dependency resolution or package network access is permitted.
    Invoke-Checked $env:CARR_PYTHON @('-I', '-B', '-m', 'pip', 'wheel', '--no-index',
        '--no-deps', '--no-build-isolation', '--wheel-dir', $application, $previousProject) 'previous-wheel-build.log'
    $wheels = @(Get-ChildItem -LiteralPath $application -File -Filter '*.whl')
    if ($wheels.Count -ne 1) { throw 'Expected one wheel actually built from the exact previous checkout.' }
    $previousWheel = $wheels[0].FullName
    $sourceProof = @'
import hashlib, importlib.util, json, pathlib, shutil, sys
previous, source, wheel, candidate_bundle, evidence = sys.argv[1:]
previous, wheel, candidate_bundle, evidence = map(pathlib.Path, (previous, wheel, candidate_bundle, evidence))
spec = importlib.util.spec_from_file_location('previous_exact_wheel', previous / 'carr-library/packaging/source_wheel.py')
verify = importlib.util.module_from_spec(spec); spec.loader.exec_module(verify)
proof = verify.verify_application_wheel(wheel, previous, source)
candidate = json.loads((candidate_bundle / 'application-source.json').read_text(encoding='utf-8'))
assert proof['source_commit'] == source != candidate['source_commit']
assert proof['wheel_sha256'] != candidate['wheel_sha256'], 'A must be its real source wheel, never relabeled B.'
destination = evidence / 'previous-application-wheel'
destination.mkdir()
target = destination / wheel.name
shutil.copyfile(wheel, target)
with target.open('rb') as stream:
    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
assert actual == proof['wheel_sha256']
manifest = {'schema': 1, 'kind': 'tested-public-source-wheel', **proof,
    'wheel_file': wheel.name, 'wheel_bytes': target.stat().st_size,
    'candidate_source_commit': candidate['source_commit'], 'candidate_wheel_sha256': candidate['wheel_sha256'],
    'native_qualification': 'NOT_RUN', 'production_authority_qualified': False,
    'authenticode_verified': False, 'nvda_verified': False}
with (destination / 'manifest.json').open('x', encoding='utf-8') as stream:
    json.dump(manifest, stream, indent=2); stream.write('\n')
print(json.dumps(manifest, sort_keys=True))
'@
    Invoke-Checked $installedPython @('-I', '-B', '-c', $sourceProof, $previousRepository,
        $PreviousSource, $previousWheel, $handoff.bundle, $evidence) 'previous-wheel-proof.log'

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
    Invoke-Checked $env:CARR_PYTHON $arguments 'previous-base-bundle.log'
    Invoke-Checked $env:CARR_PYTHON @('-I', '-B', (Join-Path $previousProject 'packaging/add_native_runtime.py'),
        '--bundle', $baseBundle, '--native-runtime', (Join-Path $bundleWork 'native-runtime'),
        '--output', $previousBundle) 'previous-native-runtime.log'
    Remove-Item -LiteralPath $baseBundle -Recurse -Force
    $previousManifest = Join-Path $previousBundle 'bundle-manifest.json'
    $previousManifestSha = Digest $previousManifest
    Invoke-Checked $env:CARR_PYTHON @('-I', '-B', (Join-Path $previousProject 'packaging/build_standard_installer.py'),
        '--bundle', $previousBundle, '--source-commit', $PreviousSource,
        '--manifest-sha256', $previousManifestSha, '--compiler', $compiler,
        '--compiler-sha256', $compilerSha, '--output', $previousBuild) 'previous-standard-build.log'
    $previousSetup = Join-Path $previousBuild ('dist/Resonance-Setup-' + $PreviousSource.Substring(0, 12) + '.exe')
    $previousSetupSha = Digest $previousSetup
    $checkPrevious = @'
import hashlib, importlib.util, json, pathlib, sys
project, previous, bundle, build, source, setup_hash, manifest_hash, compiler_hash, evidence = sys.argv[1:]
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
wheel = json.loads((bundle / 'application-source.json').read_text(encoding='utf-8'))
artifact = json.loads((evidence / 'previous-application-wheel/manifest.json').read_text(encoding='utf-8'))
assert wheel['source_commit'] == artifact['source_commit'] == source
assert wheel['wheel_sha256'] == artifact['wheel_sha256']
(evidence / 'previous-build.json').write_bytes(raw_report)
(evidence / 'previous-manifest.json').write_bytes(raw_manifest)
print(json.dumps({'status': 'verified-build', 'source_commit': source, 'setup_sha256': setup_hash}))
'@
    Invoke-Checked $installedPython @('-I', '-B', '-c', $checkPrevious, $project, $previousRepository,
        $previousBundle, $previousBuild, $PreviousSource, $previousSetupSha, $previousManifestSha,
        $compilerSha, $evidence) 'previous-build-verification.log'
    # Only this successful builder's redundant payload copy is removed.
    # The separate exact sealed input, reports and compiled setup are retained.
    Remove-Item -LiteralPath (Join-Path $previousBuild 'payload') -Recurse -Force
    [long]$setupBytes = (Get-Item -LiteralPath $previousSetup).Length
    $candidateSetup = Join-Path $handoff.build ('dist/Resonance-Setup-' + $handoff.source_commit.Substring(0, 12) + '.exe')
    $setupBytes = [Math]::Max($setupBytes, (Get-Item -LiteralPath $candidateSetup).Length)
    Require-Free (4 * ([long]$handoff.payload_bytes + [long]$handoff.installed_environment_bytes) +
        14 * $setupBytes + 512MB) 'Actual A, two rejected B generations, verified B and fresh-process TUF/target snapshots'
    $candidateProof = Get-Content -LiteralPath (Join-Path $handoff.bundle 'application-source.json') -Raw | ConvertFrom-Json
    Invoke-Checked $installedPython @('-I', '-B', 'packaging/probe_standard_update_windows.py',
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
import hashlib, importlib.util, json, pathlib, re, sys
project, evidence, handoff_path, previous_build, previous_bundle, previous_setup, previous_setup_hash, previous_manifest_hash = sys.argv[1:]
project, evidence, handoff_path, previous_build, previous_bundle, previous_setup = map(pathlib.Path,
    (project, evidence, handoff_path, previous_build, previous_bundle, previous_setup))
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
    'candidate_manifest_sha256', 'verified_target_sha256', 'backup_sha256', 'restart_recovery_proof_sha256']
assert set(report) == set(fields + true_flags + false_flags)
assert type(report['schema']) is int and report['schema'] == 1 and report['status'] == 'passed'
assert type(report['checks']) is list and all(type(item) is str for item in report['checks'])
assert Counter(report['checks']) == Counter(checks) and len(report['checks']) == 15
assert all(report[name] is True for name in true_flags) and all(report[name] is False for name in false_flags)
assert report['previous_source'] == inputs['previous_source'] != inputs['candidate_source']
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
probe.build_inputs(previous_build, inputs['previous_source'], previous_setup_hash,
    previous_manifest_hash, delivery, bundle=previous_bundle)
probe.build_inputs(record['build'], record['source_commit'], record['setup_sha256'],
    record['manifest_sha256'], delivery, bundle=record['bundle'])
full = pathlib.Path(record['full_manifest'])
assert digest(full) == inputs['existing_full_manifest_sha256'] == record['full_manifest_sha256']
delivery._trusted_verify(full.parent, manifest_bytes=full.read_bytes())
assert digest(full) == record['full_manifest_sha256']
wheel_directory = evidence / 'previous-application-wheel'
artifact = json.loads((wheel_directory / 'manifest.json').read_text(encoding='utf-8'))
assert artifact['source_commit'] == inputs['previous_source']
assert artifact['wheel_sha256'] == digest(wheel_directory / artifact['wheel_file'])
assert artifact['wheel_bytes'] == (wheel_directory / artifact['wheel_file']).stat().st_size
assert artifact['wheel_sha256'] != artifact['candidate_wheel_sha256']
report.update(previous_tree=inputs['previous_tree'], candidate_tree=inputs['candidate_tree'],
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
        $previousManifestSha) 'verdict.log'
}
finally {
    # Public evidence contains the exact public source A wheel and fixed proof
    # documents/logs only. Native binaries, model/runtime payloads and failed
    # generations remain private on the runner, with no forced cleanup.
    Pop-Location
}
