"""Explicit local verification tools. No network, issuer or signing commands."""
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
        action.add_argument('--repository', type=Path, required=True)
        action.add_argument('--bootstrap', type=Path, required=True)
        action.add_argument('--bootstrap-sha256', required=True, help='Digest obtained independently from the chosen repository.')
        action.add_argument('--product', required=True)
        action.add_argument('--channel', required=True)
        action.add_argument('--current-build', type=int, required=True)
        action.add_argument('--cache', type=Path, required=True)
        action.add_argument('--target', required=True)
        if name == 'download-update':
            action.add_argument('--reviewed-sha256', required=True)
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
        policy = UpdatePolicy(args.product, args.channel, read_bytes(args.bootstrap, 512000),
                              args.bootstrap_sha256, args.current_build)
        client = LocalRepositoryClient(args.cache, policy)
        if args.command == 'check-update':
            result = asdict(client.check(args.repository, args.target))
        else:
            package = client.download(args.repository, args.target, reviewed_sha256=args.reviewed_sha256)
            result = {'release': asdict(package.release), 'package': str(package.path), 'activated': False}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception:
        # Repository/provider diagnostics can include private paths or headers.
        print('Verification failed. Check the independently trusted key/root, product, device, channel, validity, current build and input files.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
