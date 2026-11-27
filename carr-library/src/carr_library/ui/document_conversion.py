"""Accessible reading-file actions on the existing desktop job lifecycle."""
from pathlib import Path

from ..conversion import ConversionBudget, ConversionService, capabilities
from ..conversion.capabilities import IMAGES, OFFICE_SUFFIXES
from ..conversion.legacy import QUALIFIED_SUFFIXES, kind_for
from ..extract.document_formats import file_dialog_wildcard


def formats_text():
    lines = [
        'Reading document conversion', '',
        'Create new UTF-8 text plus a source-hash provenance report. Original documents remain unchanged.',
        'Logical positions and physical PDF pages are retained separately from printed pagination.',
        'OCR and converted text require review before quotation; installed dependencies alone do not prove fidelity.',
        'No model, converter or remote content is downloaded.', '',
    ]
    for row in capabilities():
        status = ('Explicit local tool/model selection required'
                  if row['qualification'] == 'explicit_selection_required'
                  else 'Reader dependencies present' if row['available'] else 'Unavailable')
        lines.extend((row['extension'] + ': ' + status, row['limitation']))
        if row['missing']:
            lines.append('Required: ' + ', '.join(row['missing']))
        lines.append('')
    lines.extend((
        'Budgets: 64 MiB input, 32 MiB reading text, 10,000 logical segments or 1,000 PDF pages/image frames per file; 20 million pixels per frame and 80 million per image file.',
        'Each isolated worker has a 120-second default deadline. Cancel current operation also stops that worker.',
        'PDFs and images use the existing local OCR model chooser. Image OCR requires explicit selection and preserves image-frame boundaries. Unreadable or mixed native/image PDF pages require the reviewed import workflow.',
        'RTF requires explicit selection of an installed Pandoc executable. The worker checks its exact bytes and uses fixed --sandbox RTF-to-plain-text options.',
        'DOC/PPT/XLS, supported WordPerfect and PalmDoc PDB require selection of the actual installed LibreOffice program/soffice.com console executable on Windows, or program/soffice on POSIX. Each worker uses a new profile with macros, active content and untrusted links disabled; converted ODF passes the existing reader checks.',
        'A batch keeps already completed file/report pairs if a later input fails or is cancelled.',
        'A report is the completion marker. A publication error is reported even if a moved file remains for inspection.',
    ))
    return '\n'.join(lines)


def show_formats(frame):
    from .desktop_app import show_text
    show_text(frame, 'Reading document formats', formats_text())


def choose_pandoc(frame):
    """Selecting a document never activates an executable from its contents."""
    import wx
    dialog = wx.FileDialog(frame, 'Select installed Pandoc executable for sandboxed RTF reading',
        wildcard='Local executable|*.exe;*|All files|*', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return Path(dialog.GetPath())
    finally:
        dialog.Destroy()


def choose_legacy(frame, kind):
    """A document never supplies an executable or silently chooses PATH."""
    import wx
    labels = {'calibre': ('calibre ebook-convert', 'ebook-convert.exe;ebook-convert'),
              'djvu': ('DjVuLibre djvutxt', 'djvutxt.exe;djvutxt'),
              'chm': ('7-Zip', '7z.exe;7zz;7z')}
    label, names = labels[kind]
    dialog = wx.FileDialog(frame, 'Select installed ' + label + ' executable',
        wildcard='Selected local executable|' + names + '|All files|*',
        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return Path(dialog.GetPath()).absolute()
    finally:
        dialog.Destroy()


def choose_office(frame):
    import wx
    dialog = wx.FileDialog(frame, 'Select LibreOffice program/soffice.com on Windows or program/soffice on POSIX',
        wildcard='LibreOffice console executable|soffice.com;soffice|All files|*',
        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return Path(dialog.GetPath())
    finally:
        dialog.Destroy()


def convert_documents(frame):
    import wx
    from .desktop_app import show_text
    from .ocr_options import choose_model_config, choose_ocr_config

    if getattr(frame, '_closing', False):
        return
    if getattr(frame, 'working', False):
        frame.report('An operation is already running. Wait or choose Cancel current operation.')
        return
    dialog = wx.FileDialog(frame, 'Choose documents for new reading text',
                           wildcard=(('Qualified selected book formats|' + ';'.join('*' + item for item in sorted(QUALIFIED_SUFFIXES)) + '|') if QUALIFIED_SUFFIXES else '') + ('Selected legacy documents and raster images|*.doc;*.ppt;*.xls;*.wpd;*.pdb;*.rtf;*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.webp|'
                                     + file_dialog_wildcard()),
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
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
        image_ocr = any(path.suffix.casefold() in IMAGES for path in sources)
        config = (choose_model_config(frame, preset_root=getattr(library, 'root', None))
                  if image_ocr else choose_ocr_config(frame, sources))
        if config is None:
            return
        pandoc = None
        if any(path.suffix.casefold() == '.rtf' for path in sources):
            pandoc = choose_pandoc(frame)
            if pandoc is None:
                return
        office = None
        if any(path.suffix.casefold() in OFFICE_SUFFIXES for path in sources):
            office = choose_office(frame)
            if office is None:
                return
        selected_legacy = {}
        for kind in sorted({kind_for(path.suffix.casefold()) for path in sources
                            if path.suffix.casefold() in QUALIFIED_SUFFIXES}):
            selected_legacy[kind] = choose_legacy(frame, kind)
            if selected_legacy[kind] is None:
                return
        converter = ConversionService(destination, input_roots=sources, ocr_config=config,
                                      pandoc=pandoc, image_ocr=image_ocr, office=office, **selected_legacy)
    except Exception as error:
        frame.report('Document conversion could not start. ' + str(error))
        return

    completed = []

    def operation():
        options = {}
        if library is not None:
            from ..operations import ResourceBroker
            options['broker'] = ResourceBroker.for_app(library.root, cancelled=frame.cancel_event.is_set)
        return converter.extract_many(sources, cancelled=frame.cancel_event.is_set,
                                      progress=lambda number, result: completed.append(result), **options)

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
    frame.run_job('Converting reading documents', operation, done=done, failed=failed)


def install_conversion_actions(frame):
    if getattr(frame, '_carr_conversion_actions_installed', False):
        return
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command

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
    for command, label, handler in (
        ('conversion.documents', 'Convert documents to reading text...', convert_documents),
        ('conversion.formats', 'Reading conversion formats and limits...', show_formats),
    ):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, operation=handler: operation(frame), item)
        register_menu_command(frame, CommandSpec(command, label.rstrip('.'), 'main'), item)
    frame._carr_conversion_actions_installed = True
