"""Keep persistent research and backups outside a removable portable program.

The launcher binds its folder explicitly. Direct invocation of a bundled Python
also discovers the bundle beside its interpreter prefix. Resolve existing path
aliases before comparison, including Windows junctions, without moving files.
"""
import os
from pathlib import Path
import sys


def program_directories():
    roots = set()
    configured = os.environ.get('CARR_PROGRAM_DIR')
    if configured is not None:
        path = Path(configured)
        if not configured or not path.is_absolute():
            raise ValueError('The portable program directory must be an absolute path.')
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError('The portable program directory is unavailable.')
        roots.update((Path(os.path.abspath(path)), resolved))
    # No CWD search. A venv or bundled runtime lives immediately under its
    # program root; the second ancestor also covers a nested interpreter prefix.
    for prefix in (sys.prefix, sys.base_prefix):
        base = Path(prefix)
        if not base.is_absolute():
            continue
        for candidate in (base, base.parent, base.parent.parent):
            if os.path.lexists(candidate / 'bundle-manifest.json'):
                roots.update((Path(os.path.abspath(candidate)), candidate.resolve(strict=True)))
    return tuple(sorted(roots, key=str))


def guard_data_directory(value, *, purpose='library data'):
    """Return the original absolute path after checking resolved containment.

    Returning the lexical path preserves callers' existing symlink/reparse
    checks. A new destination may not exist; its existing ancestors are resolved.
    """
    path = Path(value).absolute()
    try:
        lexical = Path(os.path.abspath(path))
        resolved = path.resolve(strict=False)
        roots = program_directories()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f'The {purpose} directory could not be checked safely.') from exc
    for root in roots:
        if lexical.is_relative_to(root) or resolved.is_relative_to(root):
            raise ValueError(
                f'The {purpose} directory is inside the program folder. '
                f'Choose a directory outside "{root}". '
                'Keep existing research files and backups before uninstalling.')
    return path
