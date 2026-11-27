"""Explicit license/update commands over the same optional desktop service."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .delivery_service import DeliveryService, UpdateBackup
from .research.export_file import write_binary_export


def create_update_backup(library, destination, *, cancelled=lambda: False):
    """Reuse the complete library producer, including retained generated assets."""
    path = Path(destination).absolute()
    from .library_backup import create_backup
    checksum = create_backup(library, path, include_generated=True, cancelled=cancelled)
    return UpdateBackup(path, checksum)


def status_text(value):
    preserved = 'Your books, saved work, exports, backups and settings remain accessible.'
    if value is None:
        return 'The private license service is not configured. ' + preserved
    names = {
        'valid': 'Verified license', 'perpetual': 'Owner-configured perpetual license',
        'grace': 'Verified offline grace', 'expired': 'License expired',
        'review_required': 'License review required', 'absent': 'No verified license',
        'unavailable': 'License status unavailable',
    }
    return names[value.state.value] + '. ' + preserved


def main(argv=None, *, service=None):
    service = DeliveryService() if service is None else service
    parser = argparse.ArgumentParser(description='Explicit private license and update operations')
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status')
    commands.add_parser('recover')
    renew = commands.add_parser('renew')
    renew.add_argument('--approve-network', action='store_true')
    check = commands.add_parser('check')
    check.add_argument('--approve-network', action='store_true')
    for name in ('import-license', 'import-update'):
        action = commands.add_parser(name)
        action.add_argument('source', type=Path)
    export = commands.add_parser('export-license')
    export.add_argument('destination', type=Path)
    install = commands.add_parser('install')
    install.add_argument('--stage-id', required=True)
    install.add_argument('--target-build', required=True)
    install.add_argument('--sha256', required=True)
    install.add_argument('--backup-dir', type=Path, required=True)
    install.add_argument('--approve', action='store_true')
    args = parser.parse_args(argv)
    if args.action in ('export-license', 'install') and args.data_dir is None:
        parser.error('This operation requires --data-dir to preserve the library boundary.')
    if args.action == 'install' and not args.approve:
        parser.error('Review the exact staged build and backup destination, then supply --approve.')
    try:
        if args.action == 'status':
            value = service.snapshot()
            result = {'message': status_text(value), 'status': None if value is None else asdict(value)}
        elif args.action == 'renew':
            result = asdict(service.refresh(network_approved=args.approve_network))
        elif args.action == 'check':
            offer = service.check_update(network_approved=args.approve_network)
            result = None if offer is None else asdict(offer)
        elif args.action == 'import-license':
            result = asdict(service.import_license(args.source))
        elif args.action == 'export-license':
            raw = service.export_license()
            write_binary_export(args.destination, raw, private_root=args.data_dir, require_absent=True)
            result = {'status': 'exported', 'bytes': len(raw)}
        elif args.action == 'import-update':
            result = asdict(service.import_update(args.source))
        elif args.action == 'recover':
            prepared = service.recover_update()
            result = None if prepared is None else asdict(prepared)
        else:
            prepared = service.recover_update()
            if (prepared is None or prepared.stage_id != args.stage_id
                    or prepared.offer.target_build_id != args.target_build
                    or prepared.offer.package_sha256 != args.sha256):
                raise ValueError('The staged update changed. Review it again before installing.')
            from .application import LibraryApp
            library = LibraryApp(args.data_dir)
            approval = service.approve_install(prepared)
            result = asdict(service.install_update(approval, idle=lambda: True,
                backup=lambda: create_update_backup(library, args.backup_dir)))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({'status': 'not_completed', 'message': str(exc)}, ensure_ascii=False))
        return 1
