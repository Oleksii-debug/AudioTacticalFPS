"""Earlier ASR recovery inside the current transcription session and editor."""
from pathlib import Path

from .. import legacy_transcription as recovery
from ..research.export_file import export_completion
from .export_destination import approve_export_destination


def _choice(view, title, explanation, labels, *, multiple=False):
    view.session.guard()
    wx = view.wx
    cls = wx.MultiChoiceDialog if multiple else wx.SingleChoiceDialog
    dialog = cls(view.dialog, explanation, title, labels)
    try:
        result = dialog.ShowModal()
        view.session.guard()
        if result != wx.ID_OK:
            return None
        if multiple:
            indices = tuple(dialog.GetSelections())
            if (not indices or any(type(i) is not int or not 0 <= i < len(labels) for i in indices)
                    or len(set(indices)) != len(indices)):
                raise ValueError('Choose one or more historical sources.')
            return indices
        index = dialog.GetSelection()
        if type(index) is not int or not 0 <= index < len(labels):
            raise ValueError('Choose one of the listed historical entries.')
        return index
    finally:
        dialog.Destroy()


def open_recovery(view):
    if view.session.busy:
        return
    try:
        choice = _choice(view, 'Earlier transcription projects',
            'Read stored history, or first retain the earlier installed namespace as inert recovery data. '
            'No old runtime or worker will start.',
            ['Open retained historical projects', 'Retain earlier installed projects, then review'])
        if choice is None:
            return
        def load():
            if choice == 1:
                recovery.retain_live(view.root, cancelled=view.session.stopped)
            return recovery.jobs(view.root, cancelled=view.session.stopped)
        view.session.submit('Reading earlier transcription projects', load,
            lambda rows: _select_job(view, rows), kind='historical-list')
    except Exception as exc:
        view.report(str(exc))


def _select_job(view, rows):
    view.session.guard()
    if not rows:
        view.report('No retained earlier transcription projects were found. '
                    'Restore the complete earlier backup or explicitly retain its installed namespace first.')
        return
    labels = [' '.join(row['title'].split())[:200] + ' — recorded ' + row['recorded_status']
              + ' — ' + str(row['source_count']) + ' retained sources' for row in rows]
    index = _choice(view, 'Select earlier transcription', 'Recorded status is historical; it is not a current execution receipt.', labels)
    if index is None:
        return
    selected = rows[index]
    reference = dict(selected['reference'])
    action = _choice(view, 'Earlier transcription action', 'Original sources and accepted historical outputs remain unchanged.',
        ['Open original transcript as a reading copy', 'Open saved edit as a reading copy',
         'Export an accepted historical output', 'Prepare selected sources as a new current job'])
    if action is None:
        return
    if action == 3:
        _prepare(view, selected, reference)
        return
    sources = [row for row in selected['sources'] if row['has_edit']] if action == 1 else [
        row for row in selected['sources'] if row['has_result']]
    if not sources:
        view.report('The earlier project has no accepted result for this action.')
        return
    chosen = _choice(view, 'Historical transcript source', 'Choose the retained source.',
                     [' '.join(row['name'].split())[:200] for row in sources])
    if chosen is None:
        return
    source = sources[chosen]['id']
    if action == 2:
        _export(view, reference, source)
        return
    def opened(project):
        from ..reader.narration import NarrationStore
        from .narration_editor import open_narration_editor
        view.session.guard()
        open_narration_editor(view.dialog, NarrationStore(view.root), project, guard=view.session.guard)
    view.session.submit('Verifying the saved historical transcript',
        lambda: recovery.reading_copy(view.root, reference, source, edited=action == 1,
                                      cancelled=view.session.stopped),
        opened, kind='historical-reading')


def _prepare(view, selected, reference):
    if view.current_job is not None:
        raise ValueError('Choose New job before preparing a historical source selection.')
    if not view.manifest_path:
        raise ValueError('Choose a current reviewed runtime manifest and current settings before preparing historical sources.')
    from .transcription_dialog import settings_from_controls
    settings = settings_from_controls(view.fields)
    manifest, profile = view.manifest_path, view.runtime_profile
    title = view.title.GetValue()
    if not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise ValueError('Enter a nonempty current job title of at most 200 characters.')
    sources = tuple(selected['sources'])
    chosen = _choice(view, 'Retained sources for a new job',
        'Choose an explicit subset within the current 32 GiB job limit. '
        'Only preparation occurs; Start remains a separate action.',
        [' '.join(row['name'].split())[:200] + ' — ' + str(row['bytes']) + ' bytes' for row in sources],
        multiple=True)
    if chosen is None:
        return
    identities = tuple(sources[index]['id'] for index in chosen)
    def work():
        current_manifest = view._runtime_for(manifest, profile)
        return recovery.import_as_new(view.service, reference, identities, current_manifest, settings,
                                      title=title, cancelled=view.session.stopped)
    def prepared(result):
        view.session.guard()
        if result['definition'] is None:
            # A committed ID is still a success. Review via the ordinary saved
            # job list before granting the native Start action a definition.
            view.current_job = view.definition = view.summary = None
            view.outputs.SetItems([])
            view.controls()
            view.report('Prepared job ' + result['id'] + '. ' + (result['warning'] or '') +
                        ' Refresh saved jobs and review this ID before Start.')
            return
        view._selected_definition(result['id'], result['definition'])
        if result['warning']:
            view.report('Prepared job ' + result['id'] + '. ' + result['warning'])
    view.session.submit('Preparing selected historical originals as a new job', work, prepared,
                        kind='historical-prepare', total=len(identities))


def _export(view, reference, source):
    choice = _choice(view, 'Historical output format', 'The selected format must exist in the accepted saved receipt.',
                     ['TXT', 'SRT', 'VTT', 'JSON'])
    if choice is None:
        return
    extension = ('txt', 'srt', 'vtt', 'json')[choice]
    wx = view.wx
    dialog = wx.FileDialog(view.dialog, 'Export accepted historical output',
        wildcard=extension.upper() + ' (*.' + extension + ')|*.' + extension, style=wx.FD_SAVE)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
        destination = Path(dialog.GetPath())
    finally:
        dialog.Destroy()
    view.session.guard()
    approval = approve_export_destination(view.dialog, destination, private_root=view.root)
    view.session.guard()
    if approval is None:
        return
    view.session.submit('Exporting the accepted historical output',
        lambda: recovery.export_output(view.root, reference, source, extension, destination,
            approval=approval, cancelled=view.session.stopped),
        lambda receipt: view.report(export_completion('Historical output saved: ' + str(destination), receipt)),
        kind='historical-export')
