"""Native document conversion to separately identified reading derivatives."""
from pathlib import Path
import re

from ..conversion import ConversionService
from ..conversion.service import IMAGES


def convert_selected_documents(frame):
    import wx
    dialog = wx.FileDialog(frame, 'Choose documents to convert for reading',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
        sources = tuple(Path(value) for value in dialog.GetPaths())
    finally:
        dialog.Destroy()
    if not sources:
        return
    languages = ()
    if any(source.suffix.casefold() in IMAGES for source in sources):
        from .ocr_options import choose_installed_languages
        try:
            config = choose_installed_languages(frame)
        except Exception as exc:
            frame.report(f'Could not choose installed OCR models: {exc}')
            return
        if config is None:
            return
        languages = tuple(config.languages.split('+'))
    destination = wx.DirDialog(frame, 'Choose the folder for new reading text and provenance reports')
    try:
        if destination.ShowModal() != wx.ID_OK:
            return
        root = Path(destination.GetPath())
    finally:
        destination.Destroy()

    def run():
        converter = ConversionService(root, input_roots=tuple(dict.fromkeys(source.parent for source in sources)))
        completed = []
        for index, source in enumerate(sources, 1):
            if frame.cancel_event.is_set():
                raise InterruptedError('Conversion stopped. Completed derivative files remain in the chosen folder.')
            name = re.sub(r'[^\w .-]', '_', source.stem, flags=re.UNICODE).strip(' .')[:100] or 'document'
            result = converter.extract(source, f'{index:04d}-{name}-reading.txt',
                                       cancelled=frame.cancel_event.is_set, ocr_languages=languages)
            completed.append(f'{source.name}\nReading text: {result.path}\nProvenance: {result.report_path}')
        return '\n\n'.join(completed)

    def done(summary):
        from .desktop_app import show_text
        show_text(frame, 'Converted reading documents',
                  'Original files were preserved. These separate derivatives are for reading and narration; '
                  'quotation approval still requires source review. Existing output files are never replaced.\n\n' + summary)
    frame.run_job('Convert documents for reading', run, done)


def show_conversion_formats(frame):
    from .desktop_app import show_text
    try:
        converter = ConversionService(frame.library.root / 'state' / 'conversion')
        rows = [f'{item["extension"]}: {"available" if item["available"] else "unavailable"}; '
                f'{item["adapter"]}. {item["limitation"]}' for item in converter.capabilities()]
        show_text(frame, 'Document conversion formats', '\n\n'.join(rows))
    except Exception as exc:
        frame.report(f'Could not inspect document converters: {exc}')


def install_conversion_actions(frame):
    import wx
    from .document_batch import show_document_batches
    menu = wx.Menu()
    for label, handler in [('Convert &documents for reading...', convert_selected_documents),
                           ('Document &batches — split, name and produce...', show_document_batches),
                           ('Supported document &formats...', show_conversion_formats)]:
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, handler=handler: handler(frame), item)
    frame.menu_bar.Append(menu, '&Convert')
