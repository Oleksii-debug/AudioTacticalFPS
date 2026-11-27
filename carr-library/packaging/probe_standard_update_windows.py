"""Disposable Windows proof of two real source-bound standard generations.

This probe uses ephemeral synthetic TUF test keys and a synthetic protected
anchor. It proves the existing client/installer transaction, not a production
publisher, signing service, Authenticode approval, NVDA or physical hardware.
Original O, maintenance A and candidate B must already be built from their own
exact source checkouts. O-to-A is an explicit ordinary repair, recorded separately
from the unchanged managed A-to-B acceptance.
No compiler, package, model or runtime is downloaded here.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4
import zipfile

HERE = Path(__file__).resolve().parent
CHECKS = (
    'installed-module-origin',
    'two-distinct-source-bound-setups',
    'native-installed-a',
    'running-a-retains-shared-lease',
    'actual-tuf-verification',
    'native-b-health-rejection-rolls-back-a',
    'exact-activation-metadata-rollback',
    'rejected-b-retained',
    'verified-library-backup',
    'actual-standard-a-to-b',
    'old-process-and-sealed-a-retained',
    'protected-anchor-b',
    'actual-b-self-test',
    'actual-active-uninstall-refusal',
    'quiescent-uninstall-preserves-user-data',
)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def build_inputs(path, source, setup_sha, manifest_sha, delivery, *, bundle=None):
    from carr_library.security._files import directory, json_value, read_bytes
    from carr_library.security.standard_windows import _file_digest
    path = directory(path)
    report_raw = read_bytes(path / 'installer-build.json', 65536)
    report = json_value(report_raw)
    payload = directory(bundle) if bundle is not None else directory(path / 'payload')
    raw = read_bytes(payload / 'bundle-manifest.json', 16 * 1024**2)
    manifest = delivery._manifest(raw, source)
    delivery._require_transferable_audio_inventory(manifest)
    delivery._trusted_verify(payload, manifest_bytes=raw)
    relative = 'dist/Resonance-Setup-' + source[:12] + '.exe'
    if (report.get('source_commit') != source or report.get('status') != 'compiled'
            or report.get('managed_update_protocol') != 'resonance-standard-update/1'
            or sha256(raw).hexdigest() != manifest_sha or report.get('manifest_sha256') != manifest_sha
            or report.get('installer', {}).get('path') != relative
            or report.get('installer', {}).get('sha256') != setup_sha
            or _file_digest(path / relative) != setup_sha):
        raise AssertionError('The actual compiled setup/build report differs from its explicit source-bound inputs.')
    return path / relative, report_raw, raw


def write_target(path, executable, report_raw, manifest_raw):
    from carr_library.audio.jobs import verified_reader
    from carr_library.security._files import json_value
    report = json_value(report_raw)
    digest, size = sha256(), 0
    with path.open('xb') as destination:
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('installer-build.json', report_raw)
            archive.writestr('bundle-manifest.json', manifest_raw)
            with archive.open(report['installer']['path'], 'w', force_zip64=True) as target:
                with verified_reader(executable) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        size += len(block)
                        if size > 2 * 1024**3:
                            raise AssertionError('The compiled setup exceeds the supported target budget.')
                        digest.update(block)
                        target.write(block)
        destination.flush()
        os.fsync(destination.fileno())
    if size != report['installer']['bytes'] or digest.hexdigest() != report['installer']['sha256']:
        raise AssertionError('The actual setup changed during target preparation.')


def inspect(args):
    if os.name != 'nt' or not args.disposable_ci or os.environ.get('CI', '').lower() != 'true':
        raise RuntimeError('Use an otherwise empty disposable native Windows CI account and --disposable-ci.')
    for value, count in ((args.original_source, 40), (args.previous_source, 40), (args.candidate_source, 40),
                         (args.original_setup_sha256, 64), (args.original_manifest_sha256, 64),
                         (args.previous_setup_sha256, 64), (args.candidate_setup_sha256, 64),
                         (args.previous_manifest_sha256, 64), (args.candidate_manifest_sha256, 64)):
        if len(value) != count or any(char not in '0123456789abcdef' for char in value):
            raise ValueError('Exact independent lowercase source/manifest/setup identities are required.')
    if len({args.original_source, args.previous_source, args.candidate_source}) != 3:
        raise ValueError('Original, maintenance and candidate must have three distinct source identities.')
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.audio.engines import _start_process, _stop
    from carr_library.audio.jobs import verified_reader
    from carr_library.installation_lease import installation_lease
    from carr_library.library_backup import restore_backup
    from carr_library.security._files import TrustError, directory, json_bytes, json_value, read_bytes
    from carr_library.security.anchors import Anchor, WindowsVaultAnchor
    from carr_library.security.standard_installation import StandardUpdateTransaction
    from carr_library.security.standard_state import PENDING, PRIOR, REJECTED
    from carr_library.security.standard_windows import WindowsStandardHost, run_owned
    from carr_library.security.updates import LocalRepositoryClient
    if not Path(carr_library.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise AssertionError('Run the native update proof using the actual installed application interpreter.')
    checks = [CHECKS[0]]
    delivery = module(HERE / 'create_delivery.py', '_carr_upgrade_probe_delivery')
    installer = module(HERE / 'install_windows.py', '_carr_upgrade_probe_installer')
    # This is the existing explicitly synthetic test fixture, never a runtime
    # issuer or a root copied from the proposed application's repository.
    fixtures = module(HERE.parent / 'tests' / 'security' / 'conftest.py', '_carr_upgrade_probe_test_keys')
    def validate(raw, source):
        value = delivery._manifest(raw, source)
        delivery._require_transferable_audio_inventory(value)
        return value
    report_root = directory(Path(args.library_report).absolute().parent)
    class RejectOneVerifiedHealth(WindowsStandardHost):
        reject = True
        verified_health_calls = 0
        def health(self, root, snapshot, release, *, cancelled=lambda: False):
            good = super().health(root, snapshot, release, cancelled=cancelled)
            if not good:
                return False
            self.verified_health_calls += 1
            return not self.reject
        def launch(self, root, staged, installer, transaction, *, cancelled):
            # Keep the actual installed host launch path. The only diagnostic
            # instrumentation appends Inno's log switch for this exact setup;
            # all original arguments, environment, ownership and timeout stay.
            from unittest.mock import patch
            from carr_library.security import standard_windows as native_host
            if (not isinstance(transaction, str) or len(transaction) != 32
                    or any(char not in '0123456789abcdef' for char in transaction)):
                raise AssertionError('A fixed diagnostic log requires the exact transaction identity.')
            log = report_root / ('native-managed-' + transaction + '.log')
            if os.path.lexists(log):
                raise FileExistsError('The owned native setup log already exists.')
            actual_run, calls = native_host.run_owned, []
            def logged_run(command, **options):
                if Path(command[0]) == staged / installer:
                    calls.append(tuple(command))
                    command = [*command, '/LOG=' + str(log)]
                return actual_run(command, **options)
            with patch.object(native_host, 'run_owned', logged_run):
                code = super().launch(root, staged, installer, transaction, cancelled=cancelled)
            if len(calls) != 1:
                raise AssertionError('The actual standard host did not launch exactly its reviewed setup.')
            return code
    host = RejectOneVerifiedHealth(installer.verify, validate, timeout=1800)
    system_helper = host._powershell()
    helper_info = system_helper.lstat()
    helper_identity = {'selection': 'GetSystemDirectoryW/fixed-WindowsPowerShell-v1.0',
        'path': str(system_helper), 'device': helper_info.st_dev, 'inode': helper_info.st_ino,
        'bytes': helper_info.st_size, 'mtime_ns': helper_info.st_mtime_ns,
        'mode': helper_info.st_mode, 'attributes': getattr(helper_info, 'st_file_attributes', 0),
        'links': helper_info.st_nlink}
    print(json.dumps({'system_helper_preflight': helper_identity}, sort_keys=True), flush=True)
    try:
        existing = host._registration()
    except FileNotFoundError:
        existing = None
    if existing is not None or os.path.lexists(host.shortcut()):
        raise RuntimeError('An existing registered installation or shortcut must not be changed by this disposable proof.')
    original, o_report, o_manifest = build_inputs(args.original_build, args.original_source,
        args.original_setup_sha256, args.original_manifest_sha256, delivery, bundle=args.original_bundle)
    previous, a_report, a_manifest = build_inputs(args.previous_build, args.previous_source,
        args.previous_setup_sha256, args.previous_manifest_sha256, delivery, bundle=args.previous_bundle)
    candidate, b_report, b_manifest = build_inputs(args.candidate_build, args.candidate_source,
        args.candidate_setup_sha256, args.candidate_manifest_sha256, delivery, bundle=args.candidate_bundle)
    checks.append(CHECKS[1])
    work = Path(args.work).absolute()
    directory(work.parent)
    if os.path.lexists(work):
        raise FileExistsError('Use a new disposable native update proof directory.')
    work.mkdir()
    root, data = work / 'program', work / 'research'
    app = LibraryApp(data)
    workspace = app.create_workspace('Synthetic upgrade proof')
    app.add_text(workspace, 'Preserved source', 'Retain exact user-owned research through application upgrade.')
    sentinel = data / 'user-owned-proof.txt'
    sentinel.write_bytes(b'Unknown user-owned bytes remain unchanged.\n')
    old, original_process = None, None
    ready = work / 'old-installed-process-ready.txt'
    def hold_generation(generation, marker):
        code = ("from pathlib import Path; import sys,time\n"
            "from carr_library.installation_lease import application_lease\n"
            "with application_lease():\n"
            "    Path(sys.argv[1]).write_bytes(b'ready')\n"
            "    time.sleep(7200)\n")
        process = _start_process([str(generation / '.venv' / 'Scripts' / 'python.exe'),
            '-I', '-B', '-c', code, str(marker)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=host.environment())
        try:
            deadline = time.monotonic() + 30
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            if not marker.exists() or process.poll() is not None:
                raise AssertionError('The actual retained installation did not hold its lifetime lease.')
            return process
        except BaseException:
            _stop(process)
            raise
    try:
        # Historical O is kept exactly as built. Do not edit its installed
        # Python, clear pending authority, or relabel it as the fixed A.
        with verified_reader(original):
            code, _ = run_owned([original, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                '/SP-', '/DIR=' + str(root), '/LOG=' + str(report_root / 'original-install.log')],
                cwd=work, environment=host.environment(), timeout=1800)
        if code:
            raise AssertionError('The actual original setup failed.')
        historical = host.snapshot(root)
        if (historical['source_commit'] != args.original_source
                or historical['manifest_sha256'] != args.original_manifest_sha256
                or historical['setup_sha256'] != args.original_setup_sha256):
            raise AssertionError('Installed O differs from its independently reviewed source and setup.')
        original_generation = root.joinpath(*historical['generation'].split('/'))
        original_process = hold_generation(original_generation, work / 'original-process-ready.txt')
        maintenance_checks = ['exact-original-source-installed']
        # Ordinary maintenance is user-selected installation, not a signed
        # update bypass. A pending/recoverable transaction must never be erased
        # to make this path available.
        if os.path.lexists(root / PENDING) or os.path.lexists(root / PRIOR):
            raise AssertionError('Recover pending standard update state before ordinary maintenance.')
        with verified_reader(previous):
            code, _ = run_owned([previous, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                '/SP-', '/DIR=' + str(root), '/LOG=' + str(report_root / 'maintenance-install.log')],
                cwd=work, environment=host.environment(), timeout=1800)
        if code:
            raise AssertionError('The explicit ordinary maintenance setup failed.')
        prior = host.snapshot(root)
        if (prior['source_commit'] != args.previous_source
                or prior['manifest_sha256'] != args.previous_manifest_sha256
                or prior['setup_sha256'] != args.previous_setup_sha256
                or prior['generation'] == historical['generation']
                or os.path.lexists(root / PENDING) or os.path.lexists(root / PRIOR)):
            raise AssertionError('Maintenance A is not a distinct ready ordinary installation.')
        maintenance_checks.append('ordinary-maintenance-installer-without-update-authority')
        a_generation = root.joinpath(*prior['generation'].split('/'))
        checks.append(CHECKS[2])
        if (original_process.poll() is not None
                or read_bytes(original_generation / 'bundle-manifest.json', 16 * 1024**2) != o_manifest):
            raise AssertionError('Ordinary maintenance changed the running original or its sealed payload.')
        installer.verify(original_generation, manifest_bytes=o_manifest)
        maintenance_checks.append('original-process-and-sealed-generation-preserved')
        code, _ = run_owned([a_generation / '.venv' / 'Scripts' / 'python.exe',
            '-I', '-B', '-m', 'carr_library', '--self-test'],
            cwd=a_generation, environment=host.environment(), timeout=180)
        if (code or sentinel.read_bytes() != b'Unknown user-owned bytes remain unchanged.\n'
                or not app.search(workspace, 'exact user-owned research')):
            raise AssertionError('Maintenance failed its actual self-test or changed research data.')
        maintenance_checks.append('maintenance-native-self-test-and-user-data-preserved')
        maintenance = {'status': 'passed', 'checks': maintenance_checks,
            'original_source': args.original_source,
            'original_setup_sha256': args.original_setup_sha256,
            'original_manifest_sha256': args.original_manifest_sha256,
            'previous_source': args.previous_source,
            'actual_original_process_preserved': True,
            'actual_original_sealed_generation_preserved': True,
            'user_data_preserved': True}
        with (report_root / 'original-maintenance.json').open('x', encoding='utf-8') as stream:
            json.dump(maintenance, stream, indent=2); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        old = hold_generation(a_generation, ready)
        try:
            with installation_lease(root, exclusive=True):
                raise AssertionError('The running previous application did not block destructive replacement.')
        except RuntimeError:
            pass
        checks.append(CHECKS[3])
        target = work / 'compiled-standard-target.zip'
        write_target(target, candidate, b_report, b_manifest)
        repository = fixtures.Repository(work / 'synthetic-tuf-repository')
        # Repository.publish accepts bytes; use its same TUF metadata builder
        # with streamed TargetFile.from_data construction for the real multi-MiB setup.
        from tuf.api.metadata import TargetFile
        metadata = dict(schema=1, kind='carr-application', product='carr-test', channel='stable',
            version=args.candidate_version, build=2, source_commit=args.candidate_source,
            data_schema=repository.policy().data_schema, notes='Disposable native proof; synthetic test publisher only.')
        with verified_reader(target) as stream:
            info = TargetFile.from_data(repository.target_path, stream)
        info.unrecognized_fields['custom'] = metadata
        repository.md['targets'].signed.targets = {repository.target_path: info}
        target_copy = repository.root / 'targets' / (info.hashes['sha256'] + '.' + repository.target_path)
        from carr_library.audio.jobs import publish_new
        publish_new(target, target_copy)
        repository.flush()
        policy = repository.policy(max_package_bytes=2 * 1024**3,
            max_unpacked_bytes=4 * 1024**3, max_files=100)
        client = LocalRepositoryClient(work / 'tuf-cache', policy)
        release = client.check(repository.root, repository.target_path)
        package = client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)
        checks.append(CHECKS[4])
        # Explicit synthetic provisioning in this disposable account, using the
        # actual existing Windows vault. A real installation never provisions a
        # missing protected anchor through this test path.
        anchor = WindowsVaultAnchor('url-api-' + uuid4().hex,
            sha256(str(work / 'update-work').encode('utf-8')).hexdigest(),
            product=policy.product, profile_id='standard-update-ci')
        if anchor.vault.get_optional(anchor.reference, anchor.vault_binding) is not None:
            raise AssertionError('The fresh synthetic Windows anchor identity already exists.')
        anchor.vault.put(anchor.reference, anchor.vault_binding, anchor.encode(Anchor()))
        if anchor.load() != Anchor():
            raise AssertionError('The actual Windows vault did not retain the synthetic starting anchor.')
        transaction = StandardUpdateTransaction(app, a_generation, work / 'update-work', policy, anchor, host)
        try:
            transaction.apply(package)
            raise AssertionError('Controlled post-health rejection did not fail the actual upgrade.')
        except TrustError as error:
            if 'health' not in str(error):
                raise
        if host.verified_health_calls != 1:
            raise AssertionError('The rejected B did not first pass the real installed health checks.')
        checks.append(CHECKS[5])
        if host.snapshot(root) != prior or transaction.recover() != 'rolled-back':
            raise AssertionError('Rollback did not restore the exact registry, shortcut and uninstaller bytes.')
        checks.append(CHECKS[6])
        rejected = [path for path in (root / 'versions').iterdir() if (path / REJECTED).exists()]
        if len(rejected) != 1 or not (rejected[0] / '.venv' / '.installation-complete').is_file():
            raise AssertionError('The rejected actual B generation was lost.')
        checks.append(CHECKS[7])
        # A second actual B activation loses its controller process after the
        # durable native checkpoint. A fresh installed process must freshly
        # verify TUF and restore the exact A activation through the protected
        # restart authority; it cannot borrow the parent's live prior snapshot.
        policy_record = asdict(policy)
        policy_record['bootstrap'] = policy.bootstrap.hex()
        common = dict(schema='carr-disposable-standard-recovery/1',
            data=str(data), generation=str(a_generation), work=str(transaction.work),
            repository=str(repository.root), target=repository.target_path,
            reviewed_sha256=package.release.sha256, policy=policy_record,
            anchor=dict(reference=anchor.reference, binding=anchor.binding,
                        product=anchor.product, profile_id=anchor.profile_id),
            prior_sha256=sha256(json_bytes(prior)).hexdigest(), candidate_source=args.candidate_source)
        recovery_reports = {}
        report_root = directory(Path(args.library_report).absolute().parent)
        for action, expected_code, seconds in (('crash-after-activation', 73, 2400), ('recover', 0, 900)):
            configuration = work / ('native-restart-' + action + '.json')
            raw_configuration = json_bytes(dict(common, action=action))
            with configuration.open('xb') as stream:
                stream.write(raw_configuration)
                stream.flush()
                os.fsync(stream.fileno())
            child_report = report_root / ('native-restart-' + action + '.json')
            code, _ = run_owned([sys.executable, '-I', '-B', '-X', 'utf8',
                HERE / 'probe_standard_recovery_child.py', '--configuration', configuration,
                '--configuration-sha256', sha256(raw_configuration).hexdigest(), '--report', child_report],
                cwd=work, environment=dict(host.environment(), CI='true'), timeout=seconds)
            if code != expected_code:
                raise AssertionError('The owned native interruption/recovery child returned an unexpected exit code.')
            recovery_reports[action] = json_value(read_bytes(child_report, 65536))
        crash, recovered = recovery_reports['crash-after-activation'], recovery_reports['recover']
        if (crash.get('status') != 'controlled-process-exit-after-activation'
                or crash.get('exit_code') != 73 or crash.get('candidate_source') != args.candidate_source
                or recovered.get('status') != 'rolled-back'
                or recovered.get('candidate_source') != args.candidate_source
                or recovered.get('prior_sha256') != common['prior_sha256']
                or any(recovered.get(name) is not True for name in
                       ('actual_fresh_process', 'actual_windows_vault', 'fresh_tuf_verification'))
                or recovered.get('actual_process') == crash.get('actual_process')
                or host.snapshot(root) != prior or anchor.load() != Anchor()):
            raise AssertionError('Actual process-loss recovery did not preserve the exact protected A state.')
        rejected = [path for path in (root / 'versions').iterdir() if (path / REJECTED).exists()]
        if len(rejected) != 2 or any(not (path / '.venv' / '.installation-complete').is_file() for path in rejected):
            raise AssertionError('Both rejected real B generations must remain for diagnostics.')
        host.reject = False
        result = transaction.apply(package)
        restored = work / 'restored-library'
        restore_backup(Path(result['backup']), restored, expected_manifest_sha256=result['backup_sha256'])
        if not LibraryApp(restored).search(workspace, 'exact user-owned research'):
            raise AssertionError('The real complete library backup lost its original source.')
        checks.append(CHECKS[8])
        current = host.snapshot(root)
        if (result['status'] != 'complete' or current['source_commit'] != args.candidate_source
                or current['generation'] == prior['generation'] or (root / PENDING).exists()):
            raise AssertionError('A distinct verified B generation was not committed.')
        checks.append(CHECKS[9])
        if old.poll() is not None or read_bytes(a_generation / 'bundle-manifest.json', 16 * 1024**2) != a_manifest:
            raise AssertionError('The running previous process or its sealed generation was changed.')
        installer.verify(a_generation, manifest_bytes=a_manifest)
        if (original_process.poll() is not None
                or read_bytes(original_generation / 'bundle-manifest.json', 16 * 1024**2) != o_manifest
                or sentinel.read_bytes() != b'Unknown user-owned bytes remain unchanged.\n'):
            raise AssertionError('Managed B changed the retained original generation, process or user bytes.')
        installer.verify(original_generation, manifest_bytes=o_manifest)
        checks.append(CHECKS[10])
        if anchor.load().sequence != 2 or anchor.load().digest != package.release.sha256:
            raise AssertionError('The exact verified target did not reach the protected anchor.')
        checks.append(CHECKS[11])
        b_generation = root.joinpath(*current['generation'].split('/'))
        code, _ = run_owned([b_generation / '.venv' / 'Scripts' / 'python.exe', '-I', '-B',
            '-m', 'carr_library', '--self-test'], cwd=b_generation, environment=host.environment(), timeout=180)
        if code:
            raise AssertionError('The real committed B entrypoint failed.')
        checks.append(CHECKS[12])
        # Qualify this exact audio-free installed B while its native generation
        # still exists. The child owns/restores its separate firewall rules;
        # source tests and full-bundle evidence cannot stand in for this proof.
        test_wheels = directory(args.library_test_wheels)
        library_report = Path(args.library_report).absolute()
        directory(library_report.parent)
        if os.path.lexists(library_report):
            raise FileExistsError('Use a new exact installed-library qualification report.')
        git_executable = shutil.which('git')
        if not git_executable:
            raise RuntimeError('The current B Git object database requires the installed CI Git executable.')
        shell = host._powershell()
        system = shell.parents[2]
        qualification_environment = dict(host.environment(), CI='true', OS='Windows_NT',
            PATH=os.pathsep.join((str(Path(git_executable).parent), str(system))))
        code, _ = run_owned([shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', HERE.parent / 'scripts' / 'qualify_standard_library.ps1',
            '-CurrentGeneration', b_generation, '-SourceCommit', args.candidate_source,
            '-ManifestSha256', args.candidate_manifest_sha256, '-TestWheels', test_wheels,
            '-Report', library_report, '-Project', HERE.parent], cwd=work,
            environment=qualification_environment, timeout=1800)
        if code:
            raise AssertionError('The exact installed audio-free library qualification failed.')
        qualification = json_value(read_bytes(library_report, 4 * 1024**2), maximum=4 * 1024**2)
        if (qualification.get('schema') != 'carr-standard-library-qualification/1'
                or qualification.get('status') != 'PASS'
                or qualification.get('source_commit') != args.candidate_source
                or qualification.get('manifest_sha256') != args.candidate_manifest_sha256
                or qualification.get('generation') != str(b_generation)
                or (qualification.get('passed'), qualification.get('failed'), qualification.get('skipped')) != (50, 0, 0)
                or qualification.get('firewall', {}).get('profiles_restored') is not True
                or qualification.get('firewall', {}).get('owned_rules_removed') is not True):
            raise AssertionError('The installed-library proof differs from this exact native B generation.')
        uninstaller = Path(current['registration']['UninstallString']['value'].strip('"'))
        code, _ = run_owned([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'],
            cwd=root, environment=host.environment(), timeout=180)
        if not code or old.poll() is not None or host.snapshot(root) != current:
            raise AssertionError('Active-process uninstall refusal changed the registered B installation.')
        checks.append(CHECKS[13])
        _stop(old)
        old = None
        _stop(original_process)
        original_process = None
        code, _ = run_owned([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'],
            cwd=work, environment=host.environment(), timeout=900)
        if code or os.path.lexists(host.shortcut()) or sentinel.read_bytes() != b'Unknown user-owned bytes remain unchanged.\n':
            raise AssertionError('Quiescent uninstall failed or changed user-owned research.')
        try:
            host._registration()
        except FileNotFoundError:
            pass
        else:
            raise AssertionError('Quiescent uninstall left its registered current installation.')
        if not app.search(workspace, 'exact user-owned research') or any(not (path / REJECTED).is_file() for path in rejected):
            raise AssertionError('Uninstall erased research or retained failed-generation diagnostics.')
        checks.append(CHECKS[14])
        anchor.vault.delete(anchor.reference, anchor.vault_binding)
        if anchor.vault.get_optional(anchor.reference, anchor.vault_binding) is not None:
            raise AssertionError('The owned synthetic Windows vault anchor was not removed after success.')
        return {'schema': 1, 'status': 'passed', 'checks': checks,
            'maintenance': maintenance,
            'previous_source': args.previous_source, 'candidate_source': args.candidate_source,
            'system_helper': helper_identity,
            'previous_setup_sha256': args.previous_setup_sha256,
            'candidate_setup_sha256': args.candidate_setup_sha256,
            'previous_manifest_sha256': args.previous_manifest_sha256,
            'candidate_manifest_sha256': args.candidate_manifest_sha256,
            'verified_target_sha256': package.release.sha256,
            'backup_sha256': result['backup_sha256'],
            'actual_two_compiled_generations': True, 'actual_native_activation': True,
            'actual_guarded_rollback': True, 'actual_old_process_preserved': True,
            'actual_windows_vault': True, 'actual_restart_recovery': True,
            'restart_recovery_proof_sha256': sha256(read_bytes(report_root / 'native-restart-recover.json', 65536)).hexdigest(),
            'synthetic_publisher_and_anchor': True, 'production_authority_qualified': False,
            'authenticode_verified': False, 'nvda_verified': False}
    finally:
        if old is not None:
            _stop(old)
        if original_process is not None:
            _stop(original_process)
        # Failure leaves exact setup state/evidence for inspection. No cleanup
        # may uninstall a registered or unobserved newer generation implicitly.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for prefix in ('original', 'previous', 'candidate'):
        parser.add_argument('--' + prefix + '-build', type=Path, required=True)
        parser.add_argument('--' + prefix + '-bundle', type=Path,
            help='Exact separately retained sealed payload, after removing a verified builder copy.')
        parser.add_argument('--' + prefix + '-source', required=True)
        parser.add_argument('--' + prefix + '-setup-sha256', required=True)
        parser.add_argument('--' + prefix + '-manifest-sha256', required=True)
    parser.add_argument('--candidate-version', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--library-test-wheels', type=Path, required=True,
        help='Existing offline pytest/reportlab wheels acquired before runtime egress was blocked.')
    parser.add_argument('--library-report', type=Path, required=True,
        help='Fresh exact installed audio-free research qualification report.')
    parser.add_argument('--report', type=Path,
        help='Write the exact native JSON result to a new file after every check passes.')
    parser.add_argument('--disposable-ci', action='store_true')
    args = parser.parse_args(argv)
    value = inspect(args)
    if args.report is not None:
        with args.report.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
