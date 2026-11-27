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
    profile = 'installed'
    if any(source.suffix.casefold() in IMAGES for source in sources):
        from .ocr_selection import choose_model_config
        try:
            config = choose_model_config(frame)
        except Exception as exc:
            frame.report(f'Could not choose installed OCR models: {exc}')
            return
        if config is None:
            return
        if config.orientation or config.model_policy != 'default':
            frame.report('Image-to-text conversion uses the selected installed or managed profile. '
                         'Choose orientation off and the default model policy for this text-only operation.')
            return
        languages = tuple(config.languages.split('+'))
        profile = config.model_profile
    destination = wx.DirDialog(frame, 'Choose the folder for new reading text and provenance reports')
    try:
        if destination.ShowModal() != wx.ID_OK:
            return
        root = Path(destination.GetPath())
    finally:
        destination.Destroy()

    def run():
        from ..research.export_file import capture_export_destination
        converter = ConversionService(root, input_roots=sources)
        completed = []
        for index, source in enumerate(sources, 1):
            if frame.cancel_event.is_set():
                raise InterruptedError('Conversion stopped. Completed derivative files remain in the chosen folder.')
            name = re.sub(r'[^\w .-]', '_', source.stem, flags=re.UNICODE).strip(' .')[:100] or 'document'
            output = root / f'{index:04d}-{name}-reading.txt'
            capture_export_destination(output, private_root=frame.library.root)
            result = converter.extract(source, output,
                                       cancelled=frame.cancel_event.is_set, ocr_languages=languages,
                                       ocr_profile=profile)
            warning = ('\n' + '\n'.join(result.warnings)) if result.warnings else ''
            completed.append(f'{source.name}\nReading text: {result.path}\nProvenance: {result.report_path}' + warning)
        return '\n\n'.join(completed)

    def done(summary):
        from .desktop_app import show_text
        show_text(frame, 'Converted reading documents',
                  'Original files were preserved. These separate derivatives are for reading and narration; '
                  'quotation approval still requires source review. Existing output files are never replaced.\n\n' + summary)
    frame.run_job('Convert documents for reading', run, done)


def show_conversion_formats(frame):
    from .desktop_app import show_text
    def run():
        converter = ConversionService(frame.library.root)
        rows = [f'{item["extension"]}: {"available" if item["available"] else "unavailable"}; '
                f'{item["adapter"]}. {item["limitation"]}'
                for item in converter.capabilities(cancelled=frame.cancel_event.is_set)]
        return '\n\n'.join(rows)
    frame.run_job('Inspect document converters', run,
                  lambda text: show_text(frame, 'Document conversion formats', text))


def install_conversion_actions(frame):
    import wx
    menu = wx.Menu()
    for label, handler in [('Convert &documents for reading...', convert_selected_documents),
                           ('Supported document &formats...', show_conversion_formats)]:
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, handler=handler: handler(frame), item)
    frame.menu_bar.Append(menu, '&Convert')
