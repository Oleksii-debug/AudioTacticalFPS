"""Owned disposable Windows child for real process-loss/restart update proof.

The parent explicitly supplies a hash-bound synthetic CI configuration. No
production issuer, credential, policy or installation is discovered or adopted.
Product recovery uses the installed current controller and existing Windows
Credential Manager, with a fresh TUF verification in every child.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def _module(name):
    spec = importlib.util.spec_from_file_location('_carr_recovery_ci_' + name, HERE / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def _report(path, value):
    raw = (json.dumps(value, sort_keys=True) + '\n').encode('utf-8')
    if len(raw) > 65536:
        raise ValueError('The synthetic recovery report exceeded its fixed budget.')
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--configuration-sha256', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != 'nt' or os.environ.get('CI') != 'true' or not sys.flags.isolated:
        raise RuntimeError('Use an isolated installed interpreter on the disposable native Windows CI VM.')
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.security._files import json_bytes, json_value, read_bytes
    from carr_library.security.anchors import WindowsVaultAnchor
    from carr_library.security.standard_installation import StandardUpdateTransaction
    from carr_library.security.standard_windows import WindowsStandardHost
    from carr_library.security.updates import LocalRepositoryClient, UpdatePolicy
    if not Path(carr_library.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise AssertionError('Recovery must use the actual installed product, never the checkout.')
    raw = read_bytes(args.configuration, 1024 * 1024)
    if sha256(raw).hexdigest() != args.configuration_sha256:
        raise AssertionError('The explicit synthetic recovery configuration changed.')
    configuration = json_value(raw, maximum=1024 * 1024)
    expected = {'schema', 'action', 'data', 'generation', 'work', 'repository', 'target',
                'reviewed_sha256', 'policy', 'anchor', 'prior_sha256', 'candidate_source'}
    if (type(configuration) is not dict or set(configuration) != expected
            or configuration['schema'] != 'carr-disposable-standard-recovery/1'
            or configuration['action'] not in ('crash-after-activation', 'recover')):
        raise ValueError('Unsupported disposable recovery configuration.')
    policy_values = dict(configuration['policy'])
    policy_values['bootstrap'] = bytes.fromhex(policy_values['bootstrap'])
    policy = UpdatePolicy(**policy_values)
    anchor = WindowsVaultAnchor(**configuration['anchor'])
    work = Path(configuration['work'])
    client = LocalRepositoryClient(work / ('fresh-ci-tuf-' + configuration['action']), policy)
    release = client.check(configuration['repository'], configuration['target'])
    if (release.sha256 != configuration['reviewed_sha256']
            or release.source_commit != configuration['candidate_source']):
        raise AssertionError('Fresh TUF metadata differs from the explicitly reviewed B target.')
    package = client.download(configuration['repository'], configuration['target'],
                              reviewed_sha256=configuration['reviewed_sha256'])
    delivery, installer = _module('create_delivery'), _module('install_windows')

    def validate(raw, source):
        manifest = delivery._manifest(raw, source)
        delivery._require_transferable_audio_inventory(manifest)
        return manifest

    host = WindowsStandardHost(installer.verify, validate, timeout=1800)
    app = LibraryApp(configuration['data'])
    transaction = StandardUpdateTransaction(app, configuration['generation'], work, policy, anchor, host)
    if transaction._live_prior is not None or transaction._recovery_authority is None:
        raise AssertionError('The new process requires the configured durable Windows authority, with no live prior copy.')
    root = transaction.root
    before = anchor.load()
    if configuration['action'] == 'crash-after-activation':
        original_write = transaction._write

        def interrupt_after_durable_activation(record, phase):
            original_write(record, phase)
            if phase == 'activated':
                actual = host.snapshot(root)
                if actual != record['observed'] or actual['source_commit'] != configuration['candidate_source']:
                    raise AssertionError('The actual B activation was not durably observed before interruption.')
                if anchor.load() != before:
                    raise AssertionError('The controlled interruption must precede the protected build commit.')
                _report(args.report, dict(schema=1, status='controlled-process-exit-after-activation',
                    exit_code=73, candidate_source=actual['source_commit'],
                    generation=actual['generation'], transaction=record['transaction'],
                    current_anchor_sequence=before.sequence, actual_process=os.getpid(),
                    actual_installed_module=str(Path(carr_library.__file__).resolve())))
                # The owned child really exits without Python finally blocks.
                # Windows releases its locks; no parent or unrelated process is stopped.
                os._exit(73)

        transaction._write = interrupt_after_durable_activation
        transaction.apply(package)
        raise AssertionError('The controlled process interruption did not occur.')
    status = transaction.recover(package)
    actual = host.snapshot(root)
    if status != 'rolled-back' or sha256(json_bytes(actual)).hexdigest() != configuration['prior_sha256']:
        raise AssertionError('A fresh process did not restore the exact previously protected A activation.')
    if anchor.load() != before:
        raise AssertionError('Precommit recovery changed the protected accepted build.')
    _report(args.report, dict(schema=1, status=status, actual_fresh_process=True,
        actual_windows_vault=True, fresh_tuf_verification=True, prior_sha256=configuration['prior_sha256'],
        candidate_source=configuration['candidate_source'], actual_process=os.getpid(),
        actual_installed_module=str(Path(carr_library.__file__).resolve())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
