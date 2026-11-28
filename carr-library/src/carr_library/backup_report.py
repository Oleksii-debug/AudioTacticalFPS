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
    """Describe activated state separately from preserved historical records."""
    from .backup_compat.archive import recovery_notices
    lines = ['Restored library folder: ' + str(Path(root)),
             'External engines and credentials may need to be reconnected. '
             'Review restored jobs before resuming them.']
    labels = {'restored': 'Restored', 'restored-disabled': 'Restored, disabled',
              'inert': 'Preserved, not activated'}
    for index, notice in enumerate(recovery_notices(root), 1):
        lines.extend(('', 'Earlier backup ' + str(index) + ': original archived bytes are preserved.'))
        for component in notice['components']:
            status = labels.get(component['status'], component['status'].replace('-', ' '))
            lines.append(status + ' — ' + component['component'] + ': ' + component['reason'])
        if notice['required_inputs']:
            lines.append('External inputs listed for this earlier backup: ' +
                         str(len(notice['required_inputs'])) + '. They have not been opened or downloaded.')
    return '\n'.join(lines)
