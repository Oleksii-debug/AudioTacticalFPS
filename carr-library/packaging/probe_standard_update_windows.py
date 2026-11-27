"""Disposable Windows CI proof for the current signed standard-setup route.

Uses an actual freshly built setup/envelope, installed source-bound maintenance
code, real TUF signatures, Windows Credential Manager, complete library backups,
the existing Inno install/repair/uninstall path and actual installed self-tests.
The authority is synthetic. Reusing one payload at advancing synthetic builds
qualifies the transaction and rollback, not cross-version migration or signing.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import runpy
import stat
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent


_SETUP_FIREWALL = r"""param(
  [Parameter(Mandatory=$true)][ValidateSet('admit','remove')][string]$Operation,
  [Parameter(Mandatory=$true)][string]$Name,
  [Parameter(Mandatory=$true)][string]$Program,
  [Parameter(Mandatory=$true)][string]$Proof
)
$ErrorActionPreference = 'Stop'
if ($Name -notmatch '^CARR-standard-update-[0-9a-f]{32}-[0-9]{1,2}$') { throw 'Unexpected disposable firewall owner.' }
if ($Operation -eq 'remove') {
  $owned = @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.Name -ceq $Name })
  if ($owned.Count -gt 1) { throw 'Ambiguous disposable firewall rule.' }
  if ($owned.Count -eq 1) { $owned | Remove-NetFirewallRule -ErrorAction Stop }
  $left = @(Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop | Where-Object { $_.Name -ceq $Name })
  if ($left.Count -ne 0) { throw 'Disposable firewall rule removal was not confirmed.' }
  exit 0
}
if (!(Test-Path -LiteralPath $Program -PathType Leaf) -or (Test-Path -LiteralPath $Proof)) { throw 'Expected one existing selected executable and a new coverage proof.' }
$programPath = [System.IO.Path]::GetFullPath($Program)
New-NetFirewallRule -Name $Name -DisplayName $Name -Direction Outbound -Action Block -Profile Any -Program $programPath -Enabled True -ErrorAction Stop | Out-Null
$profiles = @(Get-NetFirewallProfile -ErrorAction Stop)
$active = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $Name -ErrorAction Stop)
if (@($profiles | Where-Object { $_.Enabled -ne 'True' }).Count -ne 0 -or $active.Count -ne 1 -or $active[0].Enabled -ne 'True' -or $active[0].Direction -ne 'Outbound' -or $active[0].Action -ne 'Block' -or $active[0].Profile -ne 'Any') { throw 'The exact selected executable has no enforced outbound deny.' }
$filters = @($active | Get-NetFirewallApplicationFilter -ErrorAction Stop | Select-Object -ExpandProperty Program)
if ($filters.Count -ne 1 -or ![StringComparer]::OrdinalIgnoreCase.Equals([System.IO.Path]::GetFullPath($filters[0]), $programPath)) { throw 'Active firewall path differs from the selected executable.' }
$result = @{ status='PASS'; rule=$Name; program=$programPath; active_store_verified=$true; enabled_profiles=@($profiles.Name) }
[System.IO.File]::WriteAllText($Proof, ($result | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))
"""


class _SetupFirewall:
    """CI-only coverage of the exact setup copies selected by the real adapter.

    The current adapter still owns and executes every native process. This
    wrapper adds and verifies a path-specific outbound deny immediately before
    that call; it supplies no replacement exit code, setup or health result.
    """
    def __init__(self, work):
        self.work = Path(work).absolute()
        self.prefix = 'CARR-standard-update-' + uuid4().hex
        self.rules, self.executions = [], []
        self.adapter = None

    def __enter__(self):
        from carr_library.security.standard_handoff import StandardSetupAdapter
        self.owner = StandardSetupAdapter
        self.original = self.owner._run_owned
        self.had_override = '_run_owned' in self.owner.__dict__
        def covered(adapter, command, *, root, timeout):
            executable = Path(command[0]).absolute()
            selected = executable.name.casefold()
            proof = None
            if (selected in ('resonancesetup.exe', 'originalsetup.exe', 'maintenance-setup.exe')
                    or re.fullmatch(r'unins[0-9]{3}\.exe', selected)):
                proof = self._admit(adapter, executable)
            result = self.original(adapter, command, root=root, timeout=timeout)
            if proof is not None:
                proof['native_exit_code'] = result
            return result
        self.owner._run_owned = covered
        return self

    def _identity(self, executable):
        from carr_library.durable_state import _open_json_file
        from carr_library.security._files import file_info
        before = file_info(executable)
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                              info.st_ctime_ns, info.st_nlink, stat.S_IFMT(info.st_mode),
                              getattr(info, 'st_file_attributes', 0))
        if not 0 < before.st_size <= 2 * 1024**3:
            raise AssertionError('Selected setup exceeds the existing native package byte bound.')
        digest, count = sha256(), 0
        with _open_json_file(executable) as stream:
            if stamp(os.fstat(stream.fileno())) != stamp(before):
                raise AssertionError('Selected setup changed before firewall admission.')
            while block := stream.read(256 * 1024):
                count += len(block)
                if count > before.st_size:
                    raise AssertionError('Selected setup grew during firewall admission.')
                digest.update(block)
            if count != before.st_size or stamp(os.fstat(stream.fileno())) != stamp(before):
                raise AssertionError('Selected setup changed during firewall admission.')
        if stamp(file_info(executable)) != stamp(before):
            raise AssertionError('Selected setup path changed during firewall admission.')
        return stamp(before), digest.hexdigest()

    def _policy(self, adapter, operation, name, executable, proof):
        from carr_library.speech._io import system_powershell, windows_directory
        from carr_library.security._files import file_info, read_bytes
        script = self.work / 'setup-firewall.ps1'
        expected = b'\xef\xbb\xbf' + _SETUP_FIREWALL.encode('utf-8')
        if not script.exists():
            with script.open('xb') as stream:
                stream.write(expected)
        file_info(script)
        if read_bytes(script, len(expected)) != expected:
            raise AssertionError('The exact local firewall policy script changed.')
        powershell = system_powershell(
            windows_directory() / 'System32/WindowsPowerShell/v1.0/powershell.exe',
            architecture='64')
        code = self.original(adapter, [powershell, '-NoLogo', '-NoProfile', '-NonInteractive',
            '-File', script, '-Operation', operation, '-Name', name,
            '-Program', executable, '-Proof', proof], root=self.work, timeout=60)
        if code != 0:
            raise AssertionError('Exact selected setup firewall policy failed: ' + operation)

    def _admit(self, adapter, executable):
        from carr_library.security._files import directory, file_info, read_bytes
        self.work = directory(self.work).resolve(strict=True)
        file_info(executable)
        executable = executable.resolve(strict=True)
        relative = executable.relative_to(self.work)
        parts = tuple(value.casefold() for value in relative.parts)
        allowed = (parts == ('initial-setup', 'resonancesetup.exe')
            or (len(parts) == 3 and parts[0] == 'maintenance'
                and re.fullmatch(r'(setup|recovery)-[0-9a-f]{32}', parts[1])
                and parts[2] in ('resonancesetup.exe', 'originalsetup.exe'))
            or (len(parts) == 2 and parts[0] == 'program'
                and re.fullmatch(r'unins[0-9]{3}\.exe', parts[1])))
        if not allowed or len(self.rules) >= 16:
            raise AssertionError('The selected setup is outside this finite private native scenario.')
        identity = self._identity(executable)
        name = self.prefix + '-' + str(len(self.rules))
        proof_path = self.work / ('setup-firewall-' + str(len(self.rules)) + '.json')
        self.adapter = adapter
        self.rules.append((name, executable, proof_path))  # retain ownership even on late policy failure
        self._policy(adapter, 'admit', name, executable, proof_path)
        proof = json.loads(read_bytes(proof_path, 16384))
        if (proof.get('status') != 'PASS' or proof.get('rule') != name
                or proof.get('active_store_verified') is not True
                or os.path.normcase(proof.get('program', '')) != os.path.normcase(str(executable))
                or self._identity(executable) != identity):
            raise AssertionError('Firewall proof or selected setup identity changed before execution.')
        result = dict(path=str(executable), sha256=identity[1], rule=name,
                      active_store_verified=True)
        self.executions.append(result)
        return result

    def __exit__(self, kind, error, traceback):
        if self.had_override:
            self.owner._run_owned = self.original
        else:
            del self.owner._run_owned
        failures = []
        for name, executable, proof in reversed(self.rules):
            try:
                self._policy(self.adapter, 'remove', name, executable, proof)
            except (Exception, KeyboardInterrupt) as failure:
                failures.append(failure)
        if failures:
            raise RuntimeError('Native setup coverage cleanup was not confirmed for every owned rule.') from failures[0]
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--envelope', type=Path, required=True)
    parser.add_argument('--envelope-sha256', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--application-version', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--disposable-ci', action='store_true')
    args = parser.parse_args(argv)
    if (os.name != 'nt' or os.environ.get('CI', '').lower() != 'true'
            or not args.disposable_ci or not sys.flags.isolated or not sys.dont_write_bytecode):
        raise RuntimeError('Use a disposable Windows CI account, --disposable-ci and installed Python -I -B.')
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        with _SetupFirewall(args.work) as firewall:
            result = inspect(args)
            if (len(firewall.executions) != 5
                    or any(row.get('native_exit_code') != 0 for row in firewall.executions)):
                raise AssertionError('All four real setup/repair calls and the real uninstaller need exact active coverage.')
            result['setup_firewall'] = dict(status='PASS', executions=firewall.executions,
                scope='Exact selected setup and uninstaller paths; future runtime paths are covered by the parent workflow.')
    finally:
        if runtime is not None:
            runtime.close()
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2); stream.write('\n')
    print(json.dumps(result, indent=2))
    return 0


def inspect(args):
    from tuf.api.metadata import TargetFile
    from carr_library.application import LibraryApp
    from carr_library.assistant.credentials import WindowsCredentialVault
    from carr_library.security import maintenance, standard_handoff, handoff, installation, updates, anchors
    from carr_library.security._files import TrustError, directory, file_info, json_bytes
    from carr_library.security.anchors import Anchor, WindowsVaultAnchor
    from carr_library.security.standard_handoff import StandardUpdateHandoff, StandardSetupAdapter
    from carr_library.security.updates import LocalRepositoryClient
    from carr_library.library_backup import restore_backup
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.reading import ReadingDocument
    from carr_library.speech.session import ReaderResume, ReaderDraftStore
    from carr_library.speech.source import read_source_range

    binding = runpy.run_path(str(HERE.parent / 'scripts/windows_publishing_smoke.py'))['verify_installed_sources']
    modules = (maintenance, standard_handoff, handoff, installation, updates, anchors)
    origins = binding(*modules)
    native = runpy.run_path(str(HERE / 'probe_standard_installer_windows.py'))
    if native['_registration']() is not None or native['_shortcut']().exists():
        raise RuntimeError('An existing standard installation or shortcut is never changed by this probe.')
    for value, size in ((args.envelope_sha256, 64), (args.source_commit, 40), (args.manifest_sha256, 64)):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(size) + '}', value):
            raise ValueError('Independent exact envelope/source/manifest identities are required.')
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,95}', args.application_version):
        raise ValueError('The actual application distribution version is required.')
    envelope = Path(args.envelope).absolute()
    file_info(envelope)
    work = Path(args.work).absolute()
    directory(work.parent)
    if os.path.lexists(work):
        raise FileExistsError('Use a new disposable native update work directory.')
    if envelope.is_relative_to(work) or work.is_relative_to(envelope.parent):
        raise ValueError('Keep the immutable envelope and disposable installation work separate.')
    work.mkdir()
    work = work.resolve(strict=True)
    fixtures = runpy.run_path(str(HERE.parent / 'tests/security/conftest.py'))
    repository = fixtures['Repository'](work / 'synthetic-repository')
    policy = repository.policy(max_package_bytes=2 * 1024**3,
                               max_unpacked_bytes=4 * 1024**3, max_files=50000)
    copied = repository.root / 'targets' / (args.envelope_sha256 + '.' + repository.target_path)
    size = standard_handoff._copy_verified(envelope, copied, args.envelope_sha256,
                                           policy.max_package_bytes)
    target = TargetFile(size, {'sha256': args.envelope_sha256}, repository.target_path)
    def publish(build):
        target.unrecognized_fields['custom'] = dict(schema=1, kind='carr-application',
            product=policy.product, channel=policy.channel, version=args.application_version,
            build=build, source_commit=args.source_commit, data_schema=policy.data_schema,
            notes='Synthetic native acceptance: same exact reviewed payload; no migration or production authority claim.')
        repository.md['targets'].signed.targets = {repository.target_path: target}
        repository.flush()
    publish(2)
    client = LocalRepositoryClient(work / 'initial-cache', policy)
    release = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)
    report, manifest = standard_handoff.prepare_signed_setup(package, policy, work / 'initial-setup')
    if (report['source_commit'] != args.source_commit
            or sha256(manifest).hexdigest() != args.manifest_sha256):
        raise AssertionError('Authenticated envelope differs from the independently selected build.')
    verify = fixtures['installer_verify']()
    adapter = StandardSetupAdapter(verify)
    program_root = work / 'program'
    checks = []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    profile = 'native-update-' + uuid4().hex
    reference = 'carr/provider/' + profile + '/' + uuid4().hex
    vault = WindowsCredentialVault()
    anchor = WindowsVaultAnchor(reference, sha256(str(work).encode()).hexdigest(),
                                product=policy.product, profile_id=profile, vault=vault)
    provisioned = False
    try:
        check('Actual current Inno setup installs the initial generation',
              adapter.run_setup(work / 'initial-setup/ResonanceSetup.exe', program_root, work) == 0)
        generations = standard_handoff._generations(program_root)
        check('Initial setup adds exactly one native generation', len(generations) == 1)
        initial = program_root / 'versions' / next(iter(generations))
        check('Initial installed self-test imports its own actual environment',
              adapter.generation_health(initial, manifest, args.application_version))
        check('Initial Windows registration and real shortcut select that generation',
              adapter.registered(program_root, initial, work))
        initial_manifest = (initial / 'bundle-manifest.json').read_bytes()
        initial_setup = initial / 'maintenance-setup.exe'
        standard_handoff._copy_verified(initial_setup, None, report['installer']['sha256'], policy.max_package_bytes)
        check('Initial bundle bytes match the compiled source identity',
              initial_manifest == manifest and initial.name.startswith(args.source_commit[:12] + '-'))

        library = LibraryApp(work / 'research')
        workspace = library.create_workspace('Native update research')
        book = library.add_text(workspace, 'Retained source', 'Original exact quotation. Незмінний текст.')
        topic = library.create_topic(workspace, 'Retained notes')
        hit, = library.search(workspace, 'Original')
        library.save_hit(workspace, topic, hit.ref, hit.revision, 'Keep this exact independent note.')
        reading = read_source_range(ReadingDesk(library, workspace, book['edition']), 1, 1).reading_document()
        draft = ReadingDocument.from_dict(reading.to_dict())
        draft.replace(0, len(draft.text), 'A separate editable native update draft.')
        draft_store = ReaderDraftStore(library.root, slot='native-update-draft')
        draft_store.save(ReaderResume(draft, cursor=2))
        unknown = initial / 'unowned-user-recording.wav'
        unknown.write_bytes(b'Unknown user file; never an installer input.')
        unknown_bytes = unknown.read_bytes()
        provisioned = True  # The unique reference remains ours even if native acknowledgement fails late.
        vault.put(profile, anchor.encode(Anchor(1, sha256(initial_manifest).hexdigest(), int(time.time()))),
                  reference=reference)
        check('Only this synthetic Windows-vault anchor was explicitly provisioned', anchor.load().sequence == 1)

        bootstrap = work / 'owner-root.json'; bootstrap.write_bytes(repository.bootstrap)
        verifier = HERE / 'install_windows.py'
        configuration = dict(schema='carr-update-owner/1', product=policy.product, channel=policy.channel,
            current_build=1, bootstrap=dict(path=str(bootstrap), sha256=sha256(repository.bootstrap).hexdigest()),
            repository=dict(kind='local', path=str(repository.root)), target=repository.target_path,
            cache=str(work / 'session-cache'),
            installation=dict(kind='standard', program=str(initial), work=str(work / 'maintenance'),
                manifest_sha256=args.manifest_sha256, setup_sha256=report['installer']['sha256'],
                version=args.application_version),
            anchor=dict(profile_id=profile, reference=reference, binding=anchor.binding),
            verifier=dict(path=str(verifier), sha256(verifier.read_bytes()).hexdigest()), helper_python=None)
        config_path = work / 'owner-configuration.json'
        raw = json_bytes(configuration); config_path.write_bytes(raw)
        selected = maintenance.UpdateConfiguration.read(config_path, sha256(raw).hexdigest())
        session = maintenance.MaintenanceSession(library, selected)
        reviewed = session.check(); session.download(reviewed)
        completed = session.apply(accepted=True)
        active = Path(completed['program'])
        check('Actual signed session invokes current setup and advances protected state',
              completed['status'] == 'complete' and anchor.load().sequence == 2 and active != initial)
        check('Successful update retains original sealed generation and unknown user bytes',
              (initial / 'bundle-manifest.json').read_bytes() == initial_manifest
              and unknown.read_bytes() == unknown_bytes and (initial / '.venv/.installation-complete').is_file())
        verify(initial, manifest_bytes=initial_manifest)
        check('Fresh authenticated recovery confirms the actual selected generation',
              session.recover(accepted=True)['program'] == str(active))
        archived_raw = (work / 'maintenance/standard-activation.json').read_bytes()
        archived_value = json.loads(archived_raw)
        restored = LibraryApp(restore_backup(Path(completed['backup']), work / 'restored',
                                            completed['backup_sha256']))
        check('Actual complete backup restores retained evidence and editable draft',
              restored.evidence(workspace, topic)[0].note == 'Keep this exact independent note.'
              and ReaderDraftStore(restored.root, slot='native-update-draft').load().document.text
              == 'A separate editable native update draft.')

        # The next reviewed request uses the returned current generation pins
        # and the SAME workspace. Only the acceptance predicate is deliberately
        # failed after a genuine installed health subprocess has succeeded.
        repository.advance(); publish(3)
        next_policy = replace(policy, current_build=2)
        next_client = LocalRepositoryClient(work / 'next-cache', next_policy)
        next_release = next_client.check(repository.root, repository.target_path)
        next_package = next_client.download(repository.root, repository.target_path,
                                            reviewed_sha256=next_release.sha256)
        class RejectOneRealHealth(StandardSetupAdapter):
            rejected = False
            health_calls = []
            def generation_health(self, generation, raw, version):
                actual = super().generation_health(generation, raw, version)
                self.health_calls.append((str(generation), actual))
                if actual and generation != active and not self.rejected:
                    self.rejected = True
                    return False
                return actual
        failing = RejectOneRealHealth(verify)
        next_helper = StandardUpdateHandoff(library, active, work / 'maintenance', next_policy, anchor, verify,
            installed_manifest_sha256=completed['installed_manifest_sha256'],
            installed_setup_sha256=completed['installed_setup_sha256'],
            installed_version=completed['version'], adapter=failing)
        try:
            next_helper.apply(next_package)
        except TrustError as error:
            check('Actual new health result reaches the deliberate negative control',
                  failing.rejected and 'health' in str(error))
        else:
            raise AssertionError('The explicit post-health negative control was not exercised.')
        record = json.loads(next_helper.journal.read_bytes())
        repaired = program_root / 'versions' / record['restored']
        check('Failed next acceptance uses real cached setup repair without advancing protected state',
              record['phase'] == 'rolled-back' and anchor.load().sequence == 2
              and repaired not in (initial, active) and failing.health_calls
              and all(passed for _name, passed in failing.health_calls))
        check('The prior exact journal is retained during same-workspace update',
              (work / 'maintenance' / ('history-' + archived_value['transaction'] + '.json')).read_bytes() == archived_raw)
        check('Actual rollback selection is confirmed by Windows and installed health',
              adapter.registered(program_root, repaired, work)
              and adapter.generation_health(repaired, manifest, args.application_version))
        check('Original, accepted, rejected and repaired generations are retained',
              len(standard_handoff._generations(program_root)) == 4
              and (program_root / 'versions' / record['candidate']).is_dir()
              and unknown.read_bytes() == unknown_bytes)
        check('Research evidence and derivative bytes survive update and rollback',
              library.evidence(workspace, topic)[0].note == 'Keep this exact independent note.'
              and draft_store.load().document.text == 'A separate editable native update draft.'
              and draft_store.load().document.source_text == reading.source_text)

        registration = native['_registration']()
        uninstaller = Path(registration['UninstallString'].strip('"'))
        if uninstaller.parent.resolve(strict=True) != program_root or not re.fullmatch(r'unins[0-9]{3}\.exe', uninstaller.name, re.I):
            raise AssertionError('The final uninstaller is outside the actual selected setup root.')
        file_info(uninstaller)
        check('Existing append-log uninstaller completes without a replacement stack',
              adapter._run_owned([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                   '/LOG=' + str(work / 'maintenance-uninstall.log')], root=work, timeout=900) == 0)
        check('Uninstall removes actual registration and the one owned shortcut',
              native['_registration']() is None and not native['_shortcut']().exists())
        check('Uninstall retains unknown user file and research library',
              unknown.read_bytes() == unknown_bytes
              and library.evidence(workspace, topic)[0].note == 'Keep this exact independent note.')
        check('Native maintenance imported source bytes remain exact', binding(*modules) == origins)
        return dict(status='PASS', checks=checks, source_commit=args.source_commit,
            envelope_sha256=args.envelope_sha256, manifest_sha256=args.manifest_sha256,
            setup_sha256=report['installer']['sha256'], application_version=args.application_version,
            installed_sources=origins, actual_native_setup=True, actual_windows_vault=True,
            actual_installed_health=True, actual_rollback_repair=True, actual_uninstall=True,
            same_workspace_history=True, authority='synthetic fixture only',
            cross_version_migration='NOT_RUN; identical payload with advancing synthetic build numbers',
            authenticode='NOT_RUN', nvda='NOT_RUN')
    except BaseException:
        print('Retained native standard update workspace: ' + str(work), flush=True)
        raise
    finally:
        if provisioned:
            vault.delete(profile, reference)


if __name__ == '__main__':
    raise SystemExit(main())
