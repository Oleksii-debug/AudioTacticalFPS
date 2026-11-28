"""Explicit caller resource choices shared by the existing backup UI and CLI."""
from dataclasses import asdict
import re

from .backup_budget import BackupBudget, MIB

# Counts are raw integers; byte fields are displayed as integral MiB.
FIELDS = (
    ('database_bytes', 'database-mib', 'Database snapshot limit (MiB)', MIB),
    ('original_bytes', 'original-mib', 'One admitted TXT original limit (MiB)', MIB),
    ('assets', 'assets', 'Retained original count', 1),
    ('asset_bytes', 'originals-mib', 'All retained originals limit (MiB)', MIB),
    ('journal_bytes', 'journal-mib', 'Import journal limit (MiB)', MIB),
    ('pending_files', 'pending-files', 'Pending file count', 1),
    ('pending_bytes', 'pending-mib', 'All pending files limit (MiB)', MIB),
    ('jobs', 'jobs', 'Saved import job count', 1),
    ('checkpoints', 'checkpoints', 'Saved checkpoint count', 1),
    ('manifest_bytes', 'manifest-mib', 'Backup manifest limit (MiB)', MIB),
    ('seconds', 'seconds', 'Whole operation time allowance (seconds)', None),
    ('memory_bytes', 'memory-mib', 'Available memory required (MiB)', MIB),
    ('staging_bytes', 'staging-mib', 'Available staging disk required (MiB)', MIB),
)

NOTICE = (
    'These limits belong to this explicit operation. A backup file cannot raise them. '
    'Larger originals require an existing sealed TXT stream profile; other format safety bounds remain. '
    'Choose memory and disk allowances available on this computer; the operation checks them '
    'before copying. Restoring a large backup requires explicit resource choices again. '
    'This is cooperative accounting, not an operating-system quota.'
)


def resource_values(selected=None):
    if selected is not None and type(selected) is not BackupBudget:
        raise TypeError('Choose an exact backup resource policy.')
    values = asdict(selected or BackupBudget())
    if any(unit is not None and values[name] % unit for name, _flag, _label, unit in FIELDS):
        raise ValueError('These controls display whole MiB; retain other exact-byte policies through the API.')
    return {name: (format(values[name], 'g') if unit is None else str(values[name] // unit))
            for name, _flag, _label, unit in FIELDS}


def parse_resource_values(values):
    if type(values) is not dict or set(values) != {row[0] for row in FIELDS}:
        raise ValueError('Review every backup resource field.')
    decoded = {}
    for name, _flag, label, unit in FIELDS:
        value = values[name]
        if type(value) is not str or not value.strip() or len(value) > 32:
            raise ValueError(label + ': enter a bounded positive number.')
        value = value.strip()
        if unit is None:
            try:
                decoded[name] = float(value)
            except ValueError as error:
                raise ValueError(label + ': enter seconds as a finite number.') from error
        else:
            if re.fullmatch(r'[0-9]+', value) is None:
                raise ValueError(label + ': enter a positive whole number.')
            decoded[name] = int(value) * unit
    return BackupBudget(**decoded)


def add_resource_arguments(parser):
    group = parser.add_argument_group('explicit backup resources')
    group.add_argument('--backup-reviewed-resources', action='store_true',
        help='Use the displayed typed defaults with time, memory and disk preflight.')
    for name, flag, label, unit in FIELDS:
        group.add_argument('--backup-' + flag, dest='backup_resource_' + name,
            metavar='NUMBER', help=label + '; explicitly selects a reviewed resource policy.')
    return parser


def resources_from_arguments(arguments):
    values = resource_values()
    changed = bool(getattr(arguments, 'backup_reviewed_resources', False))
    for name, _flag, _label, _unit in FIELDS:
        value = getattr(arguments, 'backup_resource_' + name, None)
        if value is not None:
            values[name] = value
            changed = True
    return parse_resource_values(values) if changed else None
