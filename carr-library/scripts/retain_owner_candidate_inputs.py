import hashlib, importlib.metadata, importlib.util, json, os, pathlib, shutil, sys
from packaging.utils import canonicalize_name, parse_wheel_filename
checkout = pathlib.Path.cwd().resolve()
sys.path.insert(0, str(checkout / 'packaging'))
from build_windows_bundle import read_lock, verify_native_launcher, verify_application_source
from install_windows import verify
from create_delivery import _require_transferable_audio_inventory
bundle, compiled, output, evidence = map(pathlib.Path, sys.argv[1:5])
source_commit, source_tree, manifest_before, full_outcome = sys.argv[5:9]
assert full_outcome == 'failure', 'This artifact never changes the failed full-bundle verdict.'
assert source_commit == os.environ['CARR_CANDIDATE_SOURCE_SHA']
assert not output.exists(), 'Use a new candidate-input directory.'
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
raw_manifest = (bundle / 'bundle-manifest.json').read_bytes()
assert hashlib.sha256(raw_manifest).hexdigest() == manifest_before
manifest = json.loads(raw_manifest)
assert manifest['source_commit'] == source_commit and manifest['source_tree'] == source_tree
assert manifest['bundled_trusted_client'] is True and manifest['bundled_launcher'] is True
_require_transferable_audio_inventory(manifest)
assert manifest.get('bundled_citation_runtime', False) is False
assert all(pathlib.PurePosixPath(name).parts[0].casefold() not in
           {'audio', 'audio-runtime', 'citation-runtime'} and
           pathlib.PurePosixPath(name).name.casefold() not in
           {'node', 'node.exe', 'node.dll', 'ffmpeg.exe', 'ffprobe.exe'}
           for name in manifest['files']), 'Audio or Node payload is not an owner-candidate input.'
verify(bundle, manifest_bytes=raw_manifest)
def sealed(relative):
    path = bundle / relative
    value = digest(path)
    assert manifest['files'][relative] == value, relative
    return path, value
binary, receipt = verify_native_launcher(bundle, source_commit,
                                        receipt_name='native-launcher-build.json')
assert binary == (compiled / 'ResearchLibrary.exe').read_bytes()
assert receipt == (compiled / 'build-evidence.json').read_bytes()
launcher, launcher_hash = sealed('ResearchLibrary.exe')
build_receipt, receipt_hash = sealed('native-launcher-build.json')
native_process_path = checkout / 'ci-results/native-launcher-process.json'
native_process = json.loads(native_process_path.read_text(encoding='utf-8-sig'))
assert native_process['status'] == 'passed'
assert native_process['native_launcher_execution_verified'] is True
assert native_process['launcher_sha256'] == launcher_hash
native_installed = json.loads((evidence / 'transferable-native-launcher.json').read_text(encoding='utf-8-sig'))
assert native_installed['status'] == 'passed'
assert native_installed['launcher_sha256'] == launcher_hash
assert native_installed['actual_installed_app_self_test_verified'] is True
assert native_installed['checks'] == ['--check-installation', '--self-test']
transfer = json.loads((evidence / 'transferable-bundle.json').read_text(encoding='utf-8'))
probe_path = checkout / 'scripts/windows_transferable_bundle_smoke.py'
spec = importlib.util.spec_from_file_location('owner_candidate_transfer_contract', probe_path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
assert transfer['status'] == 'PASS'
assert transfer['source_commit'] == source_commit and transfer['source_tree'] == source_tree
assert transfer['bundle_manifest_sha256'] == manifest_before
assert transfer['suites'] == list(probe.SUITES)
assert type(transfer['passed']) is int and transfer['passed'] > 0
assert transfer['failed'] == transfer['skipped'] == 0
assert transfer['expected_failures'] is False
assert transfer['manifest_unchanged'] is True and transfer['sealed_files_verified'] is True
assert transfer['audio_payload'] is False and transfer['transfer_policy'] == 'PASS'
lock = checkout / 'packaging/windows-semantic-trusted-cp311.lock'
base_lock = checkout / 'packaging/windows-semantic-cp311.lock'
pins, previous = read_lock(lock), read_lock(base_lock)
extra = {'cffi', 'cryptography', 'pycparser', 'securesystemslib', 'tuf'}
assert len(pins) == 56 and len(previous) == 51 and set(pins) - set(previous) == extra
assert all(pins[name] == item for name, item in previous.items())
source_path, source_hash = sealed('application-source.json')
source_proof = json.loads(source_path.read_text(encoding='utf-8'))
assert source_proof['source_commit'] == source_commit and source_proof['source_tree'] == source_tree
rows, seen, application, optional = [], set(), [], []
for path in sorted((bundle / 'wheelhouse').iterdir()):
    assert path.is_file() and not path.is_symlink() and path.suffix == '.whl'
    name, version, _, _ = parse_wheel_filename(path.name)
    name = canonicalize_name(name)
    _, value = sealed('wheelhouse/' + path.name)
    if name not in pins:
        assert name == 'universal-research-library' and value == source_proof['wheel_sha256']
        application.append(path)
        continue
    assert name not in seen and str(version) == pins[name][0] and value in pins[name][1]
    assert importlib.metadata.version(name) == pins[name][0]
    seen.add(name)
    rows.append(dict(name=name, version=str(version), filename=path.name,
                     bytes=path.stat().st_size, sha256=value, retained=name in extra))
    if name in extra:
        optional.append(path)
assert seen == set(pins) and len(application) == 1 and len(optional) == 5
wheel = application[0]
assert verify_application_source(wheel, source_commit) == source_proof
assert transfer['wheel'] == wheel.name and transfer['wheel_sha256'] == source_proof['wheel_sha256']
sbom_path, sbom_hash = sealed('runtime-sbom.cdx.json')
assert sbom_path.read_bytes() == (checkout / 'packaging/windows-semantic-trusted-sbom.cdx.json').read_bytes()
review_path, review_hash = sealed('trusted-client-dependencies.json')
assert review_path.read_bytes() == (checkout / 'packaging/trusted-client-dependencies.json').read_bytes()
# Recheck the original complete inventory after the final installed/native phase.
verify(bundle, manifest_bytes=raw_manifest)
assert (bundle / 'bundle-manifest.json').read_bytes() == raw_manifest
# A closed file map prevents runtime/model/driver/audio or private-parent copying.
files = {
    'application/' + wheel.name: wheel,
    'launcher/ResearchLibrary.exe': launcher,
    'launcher/build-evidence.json': build_receipt,
    'metadata/application-source.json': source_path,
    'metadata/no-audio-bundle-manifest.json': bundle / 'bundle-manifest.json',
    'metadata/runtime-sbom.cdx.json': sbom_path,
    'metadata/trusted-client-dependencies.json': review_path,
    'metadata/windows-semantic-trusted-cp311.lock': lock,
    'metadata/windows-semantic-cp311.lock': base_lock,
    'evidence/native-launcher-process.json': native_process_path,
}
for path in optional:
    files['optional-wheels/' + path.name] = path
for name in (
    'transferable-firewall.json', 'transferable-install.txt',
    'transferable-self-test.json', 'transferable-native-launcher.json',
    'transferable-native-launcher.txt', 'transferable-native.txt',
    'transferable-academic-native.txt', 'transferable-advanced-search.txt',
    'transferable-audio-setup-plan.txt', 'transferable-bundle.txt',
    'transferable-bundle.json', 'transferable-bundle.xml',
):
    files['evidence/' + name] = evidence / name
full_failure = checkout / 'ci-results/required-native-failures.json'
if full_failure.is_file():
    files['evidence/failed-full-native-gates.json'] = full_failure
snapshots = {}
for relative, path in files.items():
    assert path.is_file() and not path.is_symlink(), relative
    assert path.stat().st_nlink == 1, relative
    snapshots[relative] = dict(sha256=digest(path), bytes=path.stat().st_size)
output.mkdir(mode=0o700, exist_ok=False)
for relative, path in files.items():
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    assert digest(target) == snapshots[relative]['sha256']
    assert target.stat().st_size == snapshots[relative]['bytes']
assert {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()} == set(files)
assert all(digest(path) == snapshots[relative]['sha256'] for relative, path in files.items())
assert (bundle / 'bundle-manifest.json').read_bytes() == raw_manifest
report = dict(
    schema='resonance-owner-candidate-inputs/1', status='OWNER_CANDIDATE_INPUTS_READY',
    source_commit=source_commit, source_tree=source_tree,
    repository=os.environ['GITHUB_REPOSITORY'], run_id=os.environ['GITHUB_RUN_ID'],
    run_attempt=os.environ['GITHUB_RUN_ATTEMPT'], complete_bundle_outcome=full_outcome,
    full_qualification='NOT_ESTABLISHED', release_eligibility='NOT_APPROVED',
    assembled_owner_bundle=False, private_parent_included=False,
    no_audio_installation_and_native_library_checks='PASS',
    no_audio_bundle_manifest_sha256=manifest_before,
    application_wheel=wheel.name, application_wheel_sha256=source_proof['wheel_sha256'],
    application_source_sha256=source_hash, launcher_sha256=launcher_hash,
    native_build_receipt_sha256=receipt_hash,
    dependency_count=56, retained_optional_count=5, wheels=rows,
    dependency_lock_sha256=digest(lock), semantic_parent_lock_sha256=digest(base_lock),
    sbom_sha256=sbom_hash, dependency_review_sha256=review_hash,
    no_audio_probe_sha256=digest(probe_path),
    failed_full_native_receipt_included=full_failure.is_file(),
    files=snapshots,
    assembly='Existing refresh_private_bundle.py with the independently verified private parent, followed by unchanged create_delivery.py checks.',
    excluded_payloads=['private-parent', 'Python runtime', 'OCR runtime/source', 'models',
                       'drivers', 'audio runtimes', 'Node', 'Setup installer', 'user documents'],
    actual_audio_output='NOT_QUALIFIED', acoustic_listening='NOT_RUN',
    private_model_inference='NOT_RUN', production_issuer='UNCONFIGURED',
    authenticode='NOT_RUN', manual_nvda='NOT_RUN',
    qualification='Only the exact declared no-audio installed/library checks passed. All full, hardware, manual and release gates remain required and unchanged.',
)
(output / 'owner-candidate-inputs.json').write_text(
    json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps({key: report[key] for key in (
    'status', 'source_commit', 'source_tree', 'complete_bundle_outcome',
    'full_qualification', 'assembled_owner_bundle', 'no_audio_bundle_manifest_sha256',
    'application_wheel_sha256', 'launcher_sha256', 'retained_optional_count',
)}, sort_keys=True), flush=True)
