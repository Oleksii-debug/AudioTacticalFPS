"""Explicit reviewed update/permission tools. No issuer or signing commands."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

from ._files import read_bytes
from .controller import TrustController
from .entitlements import PublicKey, TrustPolicy, verify_envelope
from .updates import LocalRepositoryClient, UpdatePolicy


from carr_library.installation_lease import installed_entrypoint


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description='Private status and independently trusted offline verification.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Show the default private perpetual status.')
    entitlement = commands.add_parser('verify-permission', help='Verify a signed file without installing a grant.')
    entitlement.add_argument('file', type=Path)
    entitlement.add_argument('--public-key-hex', required=True)
    entitlement.add_argument('--product', required=True)
    entitlement.add_argument('--subject', required=True)
    entitlement.add_argument('--device')
    entitlement.add_argument('--policy-version', action='append', required=True)
    entitlement.add_argument('--feature', action='append', default=[])
    for name in ('check-update', 'download-update'):
        action = commands.add_parser(name)
        source = action.add_mutually_exclusive_group(required=True)
        source.add_argument('--repository', type=Path)
        source.add_argument('--repository-url', help='Explicit reviewed HTTPS repository directory; no redirects')
        action.add_argument('--ca-file', type=Path)
        action.add_argument('--ca-sha256')
        action.add_argument('--bootstrap', type=Path, required=True)
        action.add_argument('--bootstrap-sha256', required=True, help='Digest obtained independently from the chosen repository.')
        action.add_argument('--product', required=True)
        action.add_argument('--channel', required=True)
        action.add_argument('--current-build', type=int, required=True)
        action.add_argument('--cache', type=Path, required=True)
        action.add_argument('--target', required=True)
        if name == 'download-update':
            action.add_argument('--reviewed-sha256', required=True)
    for name in ('review-configuration', 'check-configured-update', 'download-configured-update',
                 'apply-update', 'recover-update'):
        action = commands.add_parser(name)
        action.add_argument('--configuration', type=Path, required=True)
        action.add_argument('--configuration-sha256', required=True)
        if name != 'review-configuration':
            action.add_argument('--data-dir', type=Path, required=True)
        if name in ('download-configured-update', 'apply-update', 'recover-update'):
            action.add_argument('--reviewed-sha256', required=True)
        if name in ('apply-update', 'recover-update'):
            action.add_argument('--accept', action='store_true',
                                help='Explicitly accept this exact signed release and retained-backup maintenance')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            print(TrustController().status_text())
            return 0
        if args.command == 'verify-permission':
            policy = TrustPolicy(args.product, args.subject, args.device,
                (PublicKey(bytes.fromhex(args.public_key_hex)),),
                frozenset(args.policy_version), frozenset(args.feature))
            result = verify_envelope(read_bytes(args.file), policy, now=int(time.time()))
            print(json.dumps({'verified': True, 'imported': False, 'product': result.product,
                              'profile': result.profile, 'sequence': result.sequence,
                              'features': sorted(result.features), 'expires_at': result.expires_at}, sort_keys=True))
            return 0
        if args.command in ('review-configuration', 'check-configured-update', 'download-configured-update',
                            'apply-update', 'recover-update'):
            from .maintenance import UpdateConfiguration, MaintenanceSession
            from ._files import directory, TrustError
            configuration = UpdateConfiguration.read(args.configuration, args.configuration_sha256)
            if args.command == 'review-configuration':
                print(configuration.description())
                return 0
            from ..application import LibraryApp
            session = MaintenanceSession(LibraryApp(directory(args.data_dir)), configuration)
            if args.command in ('apply-update', 'recover-update') and not args.accept:
                raise TrustError('Explicit --accept and the previously reviewed package SHA256 are required.')
            release = session.check()
            if args.command == 'check-configured-update':
                result = asdict(release)
            else:
                if release.sha256 != args.reviewed_sha256:
                    raise TrustError('The current signed release differs from the exact reviewed package.')
                package = session.download(release)
                if args.command == 'download-configured-update':
                    result = {'release': asdict(package.release), 'package': str(package.path),
                              'activated': False, 'maintenance_command': session.command()}
                elif args.command == 'apply-update':
                    result = session.apply(accepted=True)
                else:
                    result = session.recover(accepted=True)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if isinstance(result, dict) and result.get('warning'):
                print(result['warning'], file=sys.stderr)
            return 0
        policy = UpdatePolicy(args.product, args.channel, read_bytes(args.bootstrap, 512000),
                              args.bootstrap_sha256, args.current_build)
        if args.repository_url:
            from .https import HTTPSRepository, HTTPSRepositoryClient
            ca = None if args.ca_file is None else read_bytes(args.ca_file, 128 * 1024)
            repository = HTTPSRepository(args.repository_url, ca_pem=ca, ca_sha256=args.ca_sha256)
            client = HTTPSRepositoryClient(args.cache, policy, repository)
        else:
            if args.ca_file is not None or args.ca_sha256 is not None:
                raise ValueError('TLS CA options require an explicit HTTPS source.')
            repository = args.repository
            client = LocalRepositoryClient(args.cache, policy)
        if args.command == 'check-update':
            result = asdict(client.check(repository, args.target))
        else:
            package = client.download(repository, args.target, reviewed_sha256=args.reviewed_sha256)
            result = {'release': asdict(package.release), 'package': str(package.path), 'activated': False}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print('Update operation interrupted. Retain its prepared package, owner receipts and recovery workspace.', file=sys.stderr)
        return 130
    except Exception:
        # Repository/provider diagnostics can include private paths or headers.
        print('Verification failed. Check the independently trusted key/root, product, device, channel, validity, current build and input files.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
