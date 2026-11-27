"""Actual managed A-to-B acceptance in an isolated Windows CI account.

Both setups must be real compiled artifacts from their exact source receipts.
Only local synthetic TUF keys, a temporary Windows vault anchor and original
synthetic library text are created. Inno, registration, shortcuts, installed
Python, backup and rollback are real; this does not claim NVDA acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup, verify_backup
from carr_library.security._files import TrustError, json_bytes, json_value, read_bytes
from carr_library.security.anchors import Anchor, WindowsVaultAnchor
from carr_library.security.managed_installation import ManagedUpdateTransaction, WindowsManagedSetupAdapter
from carr_library.security.updates import LocalRepositoryClient, UpdatePolicy
from carr_library.storage.revisions import SCHEMAS
from carr_library.audio.capture_types import AudioCleanupPending


HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evidence(setup, evidence, expected_source):
    value = json_value(read_bytes(evidence, 128 * 1024), maximum=128 * 1024)
    if (not isinstance(expected_source, str) or not re.fullmatch('[0-9a-f]{40}', expected_source)
            or type(value) is not dict or value.get('status') != 'BUILT_NOT_INSTALLED'
            or value.get('source_commit') != expected_source
            or value.get('setup_sha256') != sha_file(setup)
            or value.get('setup_bytes') != setup.stat().st_size
            or value.get('release_status') != 'unsigned-private-candidate'):
        raise RuntimeError('A real compiled setup and its exact source receipt are required.')
    return value


def sha_file(path):
    with Path(path).open('rb') as stream:
        from hashlib import file_digest
        return file_digest(stream, 'sha256').hexdigest()


def _repository(root, package, build):
    """Synthetic publisher exists only in this probe's fresh isolated directory."""
    from securesystemslib.signer import CryptoSigner
    from tuf.api.metadata import Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp
    (root / 'metadata').mkdir(parents=True, exist_ok=False)
    (root / 'targets').mkdir()
    signers = {role: CryptoSigner.generate_ed25519()
               for role in ('root', 'targets', 'snapshot', 'timestamp')}
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    metadata = {'root': Metadata(Root(expires=expires, consistent_snapshot=True)),
                'targets': Metadata(Targets(expires=expires)),
                'snapshot': Metadata(Snapshot(expires=expires)),
                'timestamp': Metadata(Timestamp(expires=expires))}
    for role, signer in signers.items():
        metadata['root'].signed.add_key(signer.public_key, role)

    def encode(role):
        metadata[role].signatures.clear()
        metadata[role].sign(signers[role])
        return metadata[role].to_bytes()

    bootstrap = encode('root')
    (root / 'metadata/1.root.json').write_bytes(bootstrap)
    target = TargetFile.from_file('managed-update.zip', str(package), ['sha256'])
    target.unrecognized_fields['custom'] = dict(
        schema=1, kind='carr-application', product='carr-managed-native-probe',
        channel='synthetic', version=build['application_version'], build=2,
        source_commit=build['source_commit'], data_schema=max(SCHEMAS),
        notes='Synthetic isolated native acceptance; no production publisher authority.')
    metadata['targets'].signed.targets = {'managed-update.zip': target}
    destination = root / 'targets' / (target.hashes['sha256'] + '.managed-update.zip')
    shutil.copyfile(package, destination)
    if sha_file(destination) != target.hashes['sha256']:
        raise RuntimeError('The native fixture target changed while copying.')
    targets = encode('targets')
    (root / 'metadata/1.targets.json').write_bytes(targets)
    metadata['snapshot'].signed.meta = {'targets.json': MetaFile.from_data(1, targets, ['sha256'])}
    snapshot = encode('snapshot')
    (root / 'metadata/1.snapshot.json').write_bytes(snapshot)
    metadata['timestamp'].signed.snapshot_meta = MetaFile.from_data(1, snapshot, ['sha256'])
    (root / 'metadata/timestamp.json').write_bytes(encode('timestamp'))
    return UpdatePolicy(product='carr-managed-native-probe', channel='synthetic',
        bootstrap=bootstrap, bootstrap_sha256=sha256(bootstrap).hexdigest(), current_build=1)


def _check_source(app, workspace):
    found = app.search(workspace, 'immutable')
    if not found or found[0].quote != 'immutable':
        raise RuntimeError('The original synthetic research source was not preserved.')


def probe(setup_a, evidence_a, prepared_b, source_a, source_b, work, report):
    if os.name != 'nt':
        raise RuntimeError('This acceptance requires real Windows; there is no substitute skip.')
    import carr_library
    package_root = Path(carr_library.__file__).resolve()
    if package_root.is_relative_to(HERE.parent / 'src'):
        raise RuntimeError('Run this gate through the installed host wheel, outside the app being replaced.')
    standard = _load('carr_managed_native_standard_probe', HERE / 'windows_standard_installer_smoke.py')
    target_builder = _load('carr_managed_native_target_builder',
        HERE.parent / 'packaging/windows_installer/create_update_target.py')
    # This source is the trusted host checkout, never code loaded from target B.
    maintenance = target_builder.builder.maintenance
    record_a = _evidence(setup_a, evidence_a, source_a)
    if standard.registry() is not None:
        raise RuntimeError('This account already has a standard installation; it will not be modified.')
    menu = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Resonance'
    if os.path.lexists(menu):
        raise RuntimeError('This account has preexisting Resonance shortcuts; they will not be modified.')
    if work == report or work.is_relative_to(report) or report.is_relative_to(work):
        raise RuntimeError('Choose distinct native work and report directories.')
    work.mkdir(parents=False, exist_ok=False)
    report.mkdir(parents=False, exist_ok=False)
    root = work / 'Managed Resonance Україна Ω'
    if Path(sys.executable).resolve().is_relative_to(root):
        raise RuntimeError('The external acceptance host must not run from the replaced application.')
    target = work / 'prepared-target'
    target_report = target_builder.create_update_target(prepared_b, source_b, target)
    if target_report['bundle_manifest_sha256'] == record_a['bundle_manifest_sha256']:
        raise RuntimeError('Supply distinct real A and B bundles; same-build Repair is not an upgrade proof.')
    common = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-']
    print('MANAGED_STAGE install-A', flush=True)
    standard.owned_process([setup_a, *common, '/DIR=' + str(root),
                            '/LOG=' + str(report / 'install-A.log')],
        cwd=work, log=report / 'install-A-process.log', timeout=1800)
    standard.inspect_shortcuts(root, report)
    app = LibraryApp(work / 'original synthetic research')
    workspace = app.create_workspace('Managed native update')
    imported = app.add_text(workspace, 'Original immutable source',
                            'Original immutable Ukrainian source. Незмінне джерело.')
    if imported.get('state') != 'complete':
        raise RuntimeError('The real synthetic research import did not complete.')
    _check_source(app, workspace)
    native = WindowsManagedSetupAdapter(maintenance)
    native.verify_current(root)
    old_marker = read_bytes(root / maintenance.MARKER, 8192)
    old_owner = read_bytes(root / maintenance.OWNER, maintenance.MAX_MANIFEST)
    old_wrapper = native.registration.snapshot(root)
    old_uninstall = {name: sha_file(root / name) for name in ('unins000.exe', 'unins000.dat')}
    old_inventory = maintenance.inventory(root / 'app')
    repository = work / 'synthetic signed repository'
    policy = _repository(repository, target / target_report['target'], target_report)
    client = LocalRepositoryClient(work / 'verified target cache', policy)
    reviewed = client.check(repository, 'managed-update.zip')
    package = client.download(repository, 'managed-update.zip', reviewed_sha256=reviewed.sha256)
    token = uuid.uuid4().hex
    profile = 'native-managed-' + token
    anchor = WindowsVaultAnchor('carr/provider/' + profile + '/' + token,
        sha256(str(root).encode('utf-8')).hexdigest(), product=policy.product, profile_id=profile)
    provisioned = finished = False
    try:
        # Explicitly provision only this random, temporary native test identity.
        anchor.vault.put(anchor.vault_reference, anchor.vault_binding,
                         anchor.encode(Anchor(1, record_a['setup_sha256'], int(time.time()))))
        provisioned = True
        transaction = ManagedUpdateTransaction(app, root, work / 'update recovery',
            policy, anchor, native, idle_lease=nullcontext)
        python_a = root / 'app/.venv/Scripts/python.exe'
        ready, release = work / 'held-A-ready', work / 'held-A-release'
        try:
            child = standard.JobProcess([str(python_a), '-I', '-B', '-X', 'utf8', '-c',
                standard.HOLD_APPLICATION, str(ready), str(release)], cwd=work,
                stdin_pipe=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except AudioCleanupPending as error:
            standard.wait_cleanup_owner(error.cleanup_owner)
            raise
        try:
            standard.wait_ready(ready, child)
            try:
                transaction.apply(package)
            except RuntimeError as error:
                if 'Close Resonance' not in str(error):
                    raise
            else:
                raise RuntimeError('A live installed A application did not block the update.')
            if child.poll() is not None or read_bytes(root / maintenance.MARKER, 8192) != old_marker:
                raise RuntimeError('Refused update changed or stopped the running A installation.')
        finally:
            release.write_text('release', encoding='utf-8')
            try:
                child.wait(timeout=10)
            finally:
                standard.stop_owned_process(child)
        print('MANAGED_STAGE real-B-then-injected-health-refusal', flush=True)
        actual_health = native.health

        def reject_after_real_health(*args):
            if not actual_health(*args):
                raise RuntimeError('The actual B health gate failed before the intentional rollback test.')
            return False

        native.health = reject_after_real_health
        try:
            try:
                transaction.apply(package)
            except TrustError as error:
                if 'complete health check' not in str(error):
                    raise
            else:
                raise RuntimeError('The intentional post-B health refusal did not trigger recovery.')
        finally:
            native.health = actual_health
        if (read_bytes(root / maintenance.MARKER, 8192) != old_marker
                or read_bytes(root / maintenance.OWNER, maintenance.MAX_MANIFEST) != old_owner
                or native.registration.snapshot(root) != old_wrapper
                or {name: sha_file(root / name) for name in old_uninstall} != old_uninstall
                or maintenance.inventory(root / 'app') != old_inventory
                or anchor.load().sequence != 1):
            raise RuntimeError('Real B refusal did not restore the complete exact A installation.')
        _check_source(app, workspace)
        standard.owned_process([root / 'app/ResearchLibrary.exe', '--check-installation'],
            cwd=work, log=report / 'restored-A-native-check.log', timeout=300)
        print('MANAGED_STAGE commit-real-B', flush=True)
        accepted = transaction.apply(package)
        if accepted['status'] != 'complete' or anchor.load().sequence != 2:
            raise RuntimeError('The actual managed update did not commit its protected state.')
        previous = Path(accepted['previous'])
        if (maintenance.inventory(previous / 'app') != old_inventory
                or read_bytes(previous / maintenance.OWNER, maintenance.MAX_MANIFEST) != old_owner
                or not (previous / 'maintenance/ResonanceSetup.exe').is_file()):
            raise RuntimeError('The successful update did not retain the complete previous application/cache.')
        verify_backup(Path(accepted['backup']), accepted['backup_sha256'])
        restored = LibraryApp(restore_backup(Path(accepted['backup']), work / 'verified backup restore',
                                             accepted['backup_sha256']))
        _check_source(restored, workspace)
        _check_source(app, workspace)
        standard.owned_process([root / 'app/ResearchLibrary.exe', '--check-installation'],
            cwd=work, log=report / 'installed-B-native-check.log', timeout=300)
        # Existing helper uses a fixed output filename, so use its own new folder.
        shortcut_report = report / 'B-shortcuts'
        shortcut_report.mkdir()
        standard.inspect_shortcuts(root, shortcut_report)
        print('MANAGED_STAGE repair-and-uninstall-B', flush=True)
        standard.owned_process([root / 'maintenance/ResonanceSetup.exe', *common,
            '/LOG=' + str(report / 'repair-B.log')], cwd=work,
            log=report / 'repair-B-process.log', timeout=1800)
        _check_source(app, workspace)
        standard.owned_process([root / 'unins000.exe', *common,
            '/LOG=' + str(report / 'uninstall-B.log')], cwd=work,
            log=report / 'uninstall-B-process.log', timeout=900)
        if (root / 'app').exists() or standard.registry() is not None or any((menu / name).exists()
                for name in ('Resonance.lnk', 'Repair Resonance.lnk', 'Uninstall Resonance.lnk')):
            raise RuntimeError('B could not be removed through its actual standard uninstaller.')
        if not previous.is_dir():
            raise RuntimeError('Standard uninstall removed the retained prior recovery version.')
        _check_source(app, workspace)
        finished = True
        result = dict(schema=1, status='PASS', source_a=source_a, source_b=source_b,
            setup_a_sha256=record_a['setup_sha256'], setup_b_sha256=target_report['setup_sha256'],
            target_sha256=target_report['sha256'], host_package=str(package_root),
            actual_inno='PASS', actual_windows_registration='PASS', actual_windows_vault='PASS',
            live_app_exclusion='PASS', full_wrapper_rollback='PASS', actual_backup_restore='PASS',
            update='PASS', repair='PASS', uninstall='PASS', user_source_preserved='PASS',
            production_publisher='NOT_CONFIGURED', authenticode='NOT_CLAIMED', nvda='NOT_RUN')
        (report / 'managed-update.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                                                  encoding='utf-8')
        return result
    finally:
        if provisioned and finished:
            # Never delete any preexisting credential or reset a production anchor.
            anchor.vault.delete(anchor.vault_reference, anchor.vault_binding)
        elif provisioned:
            # Keep this isolated test anchor if recovery may still be needed;
            # the note contains only its opaque identity, never credential bytes.
            (report / 'retained-native-recovery.json').write_text(json.dumps(dict(
                schema=1, status='ATTENTION', program=str(root),
                recovery_work=str(work / 'update recovery'), reference=anchor.reference,
                binding=anchor.binding, product=anchor.product, profile_id=anchor.profile_id,
                protected_anchor='RETAINED_FOR_REVIEW'), indent=2) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup-a', required=True, type=Path)
    parser.add_argument('--build-a', required=True, type=Path)
    parser.add_argument('--prepared-b', required=True, type=Path)
    parser.add_argument('--source-a', required=True)
    parser.add_argument('--source-b', required=True)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    result = probe(args.setup_a.absolute(), args.build_a.absolute(), args.prepared_b.absolute(),
                   args.source_a, args.source_b, args.work.absolute(), args.report.absolute())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()

