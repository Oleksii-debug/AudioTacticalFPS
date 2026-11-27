"""Native document conversion to separately identified reading derivatives."""
from pathlib import Path
import re

from ..conversion import ConversionService, ConversionBudget
from ..conversion.service import IMAGES


def convert_selected_documents(frame):
    import wx
    from ..durable_state import local_path, regular
    selected_library = frame.library
    selected_root = local_path(selected_library.root, exists=True)

    def alive():
        return bool(frame) and not getattr(frame, '_closing', False)

    def guard():
        if (getattr(frame, '_closing', False) or frame.library is not selected_library
                or local_path(frame.library.root, exists=True) != selected_root):
            raise ValueError('The library changed or closed. Reopen document conversion.')

    def report(error):
        if alive():
            frame.report(str(error))

    dialog = wx.FileDialog(frame, 'Choose documents to convert for reading',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
    try:
        result = dialog.ShowModal()
        if result != wx.ID_OK or not dialog or not alive():
            return
        guard()
        sources = tuple(Path(value) for value in dialog.GetPaths())
    except Exception as exc:
        report(exc)
        return
    finally:
        if dialog:
            dialog.Destroy()
    if not sources:
        return
    languages = ()
    if any(source.suffix.casefold() in IMAGES for source in sources):
        from .ocr_options import choose_installed_languages
        try:
            config = choose_installed_languages(frame)
            if not alive():
                return
            guard()
        except Exception as exc:
            report(f'Could not choose installed OCR models: {exc}')
            return
        if config is None:
            return
        languages = tuple(config.languages.split('+'))
    office = None
    from ..conversion.office import OFFICE_SUFFIXES, OfficeSelection
    if any(source.suffix.casefold() in OFFICE_SUFFIXES for source in sources):
        converter_dialog = wx.FileDialog(frame,
            'Select installed LibreOffice program/soffice for legacy reading conversion (4 GiB worker budget)',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            result = converter_dialog.ShowModal()
            if result != wx.ID_OK or not converter_dialog or not alive():
                return
            guard()
            office = OfficeSelection.capture(Path(converter_dialog.GetPath()))
        except Exception as exc:
            report(exc)
            return
        finally:
            if converter_dialog:
                converter_dialog.Destroy()
    destination = wx.DirDialog(frame, 'Choose the folder for new reading text and provenance reports')
    try:
        result = destination.ShowModal()
        if result != wx.ID_OK or not destination or not alive():
            return
        guard()
        root = local_path(Path(destination.GetPath()), exists=True)
        info = regular(root, directory=True)
        root_identity = info.st_dev, info.st_ino
    except Exception as exc:
        report(exc)
        return
    finally:
        if destination:
            destination.Destroy()

    def stopped():
        guard()
        current = regular(local_path(root, exists=True), directory=True)
        if (current.st_dev, current.st_ino) != root_identity:
            raise ValueError('The selected output directory changed. Choose it again.')
        return frame.cancel_event.is_set()

    def run():
        stopped()
        options = {} if office is None else {'office': office, 'budget': ConversionBudget(memory_bytes=4 * 1024**3)}
        converter = ConversionService(root, input_roots=tuple(dict.fromkeys(source.parent for source in sources)), **options)
        completed = []
        for index, source in enumerate(sources, 1):
            if stopped():
                raise InterruptedError('Conversion stopped. Completed derivative files remain in the chosen folder.')
            name = re.sub(r'[^\w .-]', '_', source.stem, flags=re.UNICODE).strip(' .')[:100] or 'document'
            result = converter.extract(source, f'{index:04d}-{name}-reading.txt',
                                       cancelled=stopped, ocr_languages=languages)
            message = f'{source.name}\nReading text: {result.path}\nProvenance: {result.report_path}'
            if result.cleanup_warning:
                message += '\n' + result.cleanup_warning
            completed.append(message)
        return '\n\n'.join(completed)

    def done(summary):
        if not alive():
            return
        try:
            guard()
        except Exception as exc:
            report(exc)
            return
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
    from .command_registry import CommandSpec
    from .document_batch import show_document_batches
    from .keyboard_settings import register_menu_command
    menu = wx.Menu()
    frame.menu_bar.Append(menu, '&Convert')
    for label, handler, command in [
            ('Convert &documents for reading...', convert_selected_documents, 'conversion.run'),
            ('Document &batches — split, name and produce...', show_document_batches, 'conversion.batch'),
            ('Supported document &formats...', show_conversion_formats, 'conversion.formats')]:
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, handler=handler: handler(frame), item)
        register_menu_command(frame, CommandSpec(command, label.replace("&", "")), item)
