"""Readable backup scope and recovery status shared by the desktop and CLI."""
from pathlib import Path


def backup_scope_text(include_generated=False):
    audio = ('Generated audio stored inside this library is included.' if include_generated else
             'Generated audio caches are excluded; saved projects and their settings are included.')
    return ('Libraries, exact original files, saved evidence, notes, searches, reading state, '
            'project settings and retained pending inputs are included.\n' + audio + '\n'
            'External exports, engine/model packs and operating-system credentials are separate. '
            'The backup manifest records included files and required external inputs.')


def recovery_summary(root):
    """Describe only guarantees made by the current canonical restore service."""
    return ('Restored library folder: ' + str(Path(root)) + '\n'
            'The selected backup and its required inputs were verified before publication.\n'
            'Review restored import jobs, translation approvals and output destinations before resuming them. '
            'External engines, model packs and credentials must be reconnected separately.')
