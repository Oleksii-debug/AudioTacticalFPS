"""Accessible reading-file actions on the existing desktop job lifecycle."""
from pathlib import Path

from ..conversion import ConversionBudget, ConversionService, capabilities
from ..extract.document_formats import file_dialog_wildcard
from ..progress import ProgressTracker


def formats_text():
    lines = [
        'Reading document conversion', '',
        'Create new UTF-8 text plus a source-hash provenance report. Original documents remain unchanged.',
        'Logical positions and physical PDF pages are retained separately from printed pagination.',
        'OCR and converted text require review before quotation; installed dependencies alone do not prove fidelity.',
        'No model, converter or remote content is downloaded.', '',
    ]
    for row in capabilities():
        status = 'Reader dependencies present' if row['available'] else 'Unavailable'
        lines.extend((row['extension'] + ': ' + status, row['limitation']))
        if row['missing']:
            lines.append('Required: ' + ', '.join(row['missing']))
        lines.append('')
    lines.extend((
        'Budgets: 64 MiB input, 32 MiB reading text, 10,000 logical segments or 1,000 PDF pages per file.',
        'Each isolated worker has a 120-second default deadline. Cancel current operation also stops that worker.',
        'The desktop uses the existing local OCR model choice for PDFs. Unreadable or mixed native/image pages fail explicitly and require the reviewed import workflow.',
        'A batch keeps already completed file/report pairs if a later input fails or is cancelled.',
        'A report is the completion marker. A publication error is reported even if a moved file remains for inspection.',
    ))
    return '\n'.join(lines)


def show_formats(frame):
    from .desktop_app import show_text
    show_text(frame, 'Reading document formats', formats_text())


def convert_documents(frame):
    import wx
    from .desktop_app import show_text
    from .ocr_options import choose_ocr_config

    if getattr(frame, '_closing', False):
        return
    if getattr(frame, 'working', False):
        frame.report('An operation is already running. Wait or choose Cancel current operation.')
        return
    dialog = wx.FileDialog(frame, 'Choose documents for new reading text',
                           wildcard=file_dialog_wildcard(), style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
        sources = tuple(Path(value) for value in dialog.GetPaths())
    finally:
        dialog.Destroy()
    if not sources:
        return
    if len(sources) > ConversionBudget().max_files:
        frame.report('Choose at most 1,000 documents in one conversion batch.')
        return
    output_dialog = wx.DirDialog(frame, 'Choose the folder for new reading files and provenance reports')
    try:
        if output_dialog.ShowModal() != wx.ID_OK:
            return
        destination = Path(output_dialog.GetPath())
    finally:
        output_dialog.Destroy()
    try:
        library = getattr(frame, 'library', None)
        if library is not None and destination.resolve().is_relative_to(Path(library.root).resolve()):
            raise ValueError('Choose an output folder outside the research library. Import a finished reading copy explicitly if needed.')
        config = choose_ocr_config(frame, sources)
        if config is None:
            return
        converter = ConversionService(destination, input_roots=sources, ocr_config=config)
    except Exception as error:
        frame.report('Document conversion could not start. ' + str(error))
        return

    completed = []
    tracker = ProgressTracker('Converting reading documents', total=len(sources), unit='documents')

    def accepted(number, result):
        completed.append(result)
        tracker.update(completed=number, total=len(sources), phase='Verified ' + result.path.name)

    def operation():
        return converter.extract_many(sources, cancelled=frame.cancel_event.is_set,
                                      progress=accepted)

    def done(results):
        lines = ['Created ' + str(len(results)) + ' reading file(s).', '',
                 'Text is a reading derivative and remains unverified for quotation.',
                 'Original documents were preserved. Review each source-hash report for limitations.', '']
        for result in results:
            lines.extend((str(result.path), str(result.report_path), ''))
        show_text(frame, 'Document conversion complete', '\n'.join(lines))

    def failed(error):
        frame.report('Document conversion stopped: ' + str(error) +
                     ('\nAlready completed: ' + str(len(completed)) + ' file/report pair(s) in ' + str(destination)
                      if completed else '\nNo file/report pairs were confirmed complete in this batch.'))

    # DesktopFrame owns the worker, Cancel event and close-safe callback guards.
    frame.run_job('Converting reading documents', operation, done=done, failed=failed,
                  progress_tracker=tracker, progress_outcome=lambda results: 'completed')


def install_conversion_actions(frame):
    if getattr(frame, '_carr_conversion_actions_installed', False):
        return
    import wx

    bar = frame.GetMenuBar()
    if bar is None:
        raise ValueError('Install the native menu bar before document actions.')
    menu = None
    for index in range(bar.GetMenuCount()):
        if bar.GetMenuLabel(index).replace('&', '') in ('Documents', 'Tools'):
            menu = bar.GetMenu(index)
            break
    if menu is None:
        menu = wx.Menu()
        bar.Append(menu, 'Documents')
    elif menu.GetMenuItems():
        menu.AppendSeparator()
    # The existing productivity collector registers these menu actions after _menus.
    for label, handler in (
        ('Convert documents to reading text...', convert_documents),
        ('Reading conversion formats and limits...', show_formats),
    ):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, operation=handler: operation(frame), item)
    frame._carr_conversion_actions_installed = True
