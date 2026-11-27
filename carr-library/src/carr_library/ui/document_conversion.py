"""Explicit native conversion and guarded reading-copy handoff on the current host."""
from pathlib import Path
import os
import re

from ..conversion import ConversionService
from ..conversion.service import IMAGES
from ..durable_state import local_path, regular


MAX_SELECTED = 1000


def _capture_owner(frame):
    library, workspace = frame.library, frame._workspace()
    root = local_path(library.root, exists=True)
    info = regular(root, directory=True)
    identity = info.st_dev, info.st_ino
    def guard():
        if (getattr(frame, '_closing', False) or frame.library is not library
                or frame._workspace() != workspace
                or local_path(library.root, exists=True) != root):
            raise ValueError('The selected library or workspace changed.')
        current = regular(local_path(root, exists=True), directory=True)
        if (current.st_dev, current.st_ino) != identity:
            raise ValueError('The selected library directory changed.')
    guard()
    return library, workspace, guard


def _choose_executable(frame, title, guard):
    import wx
    dialog = wx.FileDialog(frame, title, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        guard()  # Check before reading the selected path from native controls.
        return Path(dialog.GetPath())
    finally:
        dialog.Destroy()


def convert_selected_documents(frame):
    import wx
    try:
        library, workspace, guard = _capture_owner(frame)
        dialog = wx.FileDialog(frame, 'Choose documents to convert for reading',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            guard()
            sources = tuple(Path(value) for value in dialog.GetPaths())
        finally:
            dialog.Destroy()
        if not sources:
            return
        if len(sources) > MAX_SELECTED:
            raise ValueError('Choose at most 1000 documents for one conversion operation.')
        from ..conversion.calibre import CALIBRE_SUFFIXES
        from ..conversion.office import OFFICE_SUFFIXES
        suffixes = {path.suffix.casefold() for path in sources}
        tools = {}
        if suffixes & (CALIBRE_SUFFIXES | OFFICE_SUFFIXES) and os.name != 'nt':
            raise ValueError('Selected Calibre and LibreOffice reading conversion currently requires Windows.')
        for key, needed, title in (
            ('calibre', bool(suffixes & CALIBRE_SUFFIXES), 'Choose the installed Calibre ebook-convert executable'),
            ('office', bool(suffixes & OFFICE_SUFFIXES), 'Choose the installed LibreOffice program/soffice executable'),
            ('pandoc', '.rtf' in suffixes, 'Choose the installed Pandoc executable for RTF')):
            if needed:
                chosen = _choose_executable(frame, title, guard)
                if chosen is None:
                    return
                tools[key] = chosen
        languages = ()
        if suffixes & set(IMAGES):
            from .ocr_options import choose_installed_languages
            config = choose_installed_languages(frame)
            guard()
            if config is None:
                return
            languages = tuple(config.languages.split('+'))
        destination = wx.DirDialog(frame, 'Choose the folder for new reading copies')
        try:
            if destination.ShowModal() != wx.ID_OK:
                return
            guard()
            root = Path(destination.GetPath())
        finally:
            destination.Destroy()
        # Capture selected metadata before queueing. Expensive hashes and all
        # native work occur on the existing host worker after live revalidation.
        converter = ConversionService(root, input_roots=sources,
            state_root=library.root / 'state', guard=guard, **tools)
        guard()
    except Exception as error:
        if not getattr(frame, '_closing', False):
            frame.report('Conversion not started: ' + str(error))
        return

    def cancelled():
        guard()
        return frame.cancel_event.is_set()

    def run():
        guard()
        completed = []
        try:
            for index, source in enumerate(sources, 1):
                if cancelled():
                    raise InterruptedError('Conversion stopped. Completed reading copies remain in the selected folder.')
                name = re.sub(r'[^\w .-]', '_', source.stem, flags=re.UNICODE).strip(' .')[:100] or 'document'
                result = converter.extract(source, f'{index:04d}-{name}-reading.txt',
                    cancelled=cancelled, ocr_languages=languages)
                completed.append(result)
        except BaseException as error:
            actual = getattr(error, 'completed_conversion', None)
            if actual is not None and all(item is not actual for item in completed):
                completed.append(actual)
            error.completed_conversions = tuple(completed)
            raise
        return tuple(completed)

    def current():
        try:
            guard()
            return True
        except Exception:
            return False

    def done(results):
        if current():
            show_reading_copies(frame, library, workspace, results, guard=guard)

    def failed(error):
        results = getattr(error, 'completed_conversions', ())
        if results and current():
            show_reading_copies(frame, library, workspace, results, guard=guard,
                notice='These copies were actually saved before the operation stopped. ' + str(error))
    frame.run_job('Convert documents for reading', run, done, failed)


def show_reading_copies(frame, library, workspace, results, *, guard, notice=''):
    """Small modeless result window; all verification/imports use frame.run_job."""
    import wx
    from ..conversion.selected import import_result, read_result
    if not results:
        return
    guard()
    previous = getattr(frame, 'conversion_results_dialog', None)
    if previous is not None:
        previous.Close()
    dialog = wx.Dialog(frame, title='Converted reading copies', size=(780, 580),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    frame.conversion_results_dialog = dialog
    state = {'alive': True, 'working': False, 'generation': 0}
    panel = wx.Panel(dialog)
    layout = wx.BoxSizer(wx.VERTICAL)
    explanation = wx.StaticText(panel, label=(
        'Reading copies have no native page or quotation approval. For selected legacy converters, '
        'each folder contains the exact original, UTF-8 text and a fidelity report. '
        'Import adds only the reading text to this library. Keep the original and report in that folder; '
        'they are not included in the imported text source or its library backup.'))
    explanation.Wrap(730)
    layout.Add(explanation, 0, wx.ALL | wx.EXPAND, 10)
    choices = wx.ListBox(panel, choices=[str(item.path) for item in results])
    choices.SetName('Saved reading copies')
    choices.SetSelection(0)
    layout.Add(choices, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
    details = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
    details.SetName('Reading copy original and fidelity report')
    layout.Add(details, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
    status = wx.TextCtrl(panel, value=notice, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Reading copy action status')
    layout.Add(status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    read = wx.Button(panel, label='&Read verified copy')
    read.SetName('Read verified reading copy')
    ingest = wx.Button(panel, label='&Import reading copy')
    ingest.SetName('Import verified reading copy into selected library')
    close = wx.Button(panel, wx.ID_CLOSE, '&Close')
    for button in (read, ingest, close):
        buttons.Add(button, 0, wx.ALL, 5)
    layout.Add(buttons, 0, wx.ALL, 5)
    panel.SetSizer(layout)
    outer = wx.BoxSizer(wx.VERTICAL)
    outer.Add(panel, 1, wx.EXPAND)
    dialog.SetSizer(outer)

    def current():
        if not state['alive']:
            raise ValueError('The reading-copy window is closed.')
        guard()

    def usable():
        try:
            current()
            return True
        except Exception:
            return False

    def selected():
        index = choices.GetSelection()
        if not 0 <= index < len(results):
            raise ValueError('Choose one completed reading copy.')
        return index, results[index]

    def refresh(event=None):
        if not usable():
            return
        _, result = selected()
        text = 'Reading text: ' + str(result.path) + '\nFidelity report: ' + str(result.report_path)
        if result.original_path is not None:
            text += '\nRetained exact original: ' + str(result.original_path)
        text += ''.join('\n' + item for item in result.notices)
        details.SetValue(text)
        admitted = result.original_path is not None and not state['working']
        read.Enable(admitted)
        ingest.Enable(admitted)

    def finish(rows, error=None):
        if not usable():
            return
        state['working'] = False
        refresh()
        if rows is not None:
            status.SetValue('Reading-copy import returned: ' + ' '.join(
                str(row.get('file', '')) + ': ' + str(row.get('state', 'unknown')) + '.'
                for row in rows) + '\nThe retained native original and report remain in the external folder.'
                + (('\nAttention: ' + str(error)) if error is not None else ''))
            if frame.book is not None:
                frame.book.GetPage(1).refresh()
        elif error is not None:
            status.SetValue('Reading-copy action stopped: ' + str(error))

    def begin(action):
        current()
        if state['working'] or frame.working:
            raise ValueError('Wait for the current operation or cancel it before using this reading copy.')
        index, result = selected()
        if result.original_path is None:
            raise ValueError('This action requires a complete retained reading-copy folder.')
        if action == 'import':
            answer = wx.MessageBox(
                'Import only this verified UTF-8 reading copy? The original and fidelity report stay '
                'in the external folder and do not become native catalog source or backup authority.',
                'Import verified reading copy', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, dialog)
            current()
            current_index, current_result = selected()
            if current_index != index or current_result is not result:
                raise ValueError('The selected reading copy changed during confirmation.')
            if answer != wx.YES:
                return
        state['working'] = True
        state['generation'] += 1
        generation = state['generation']
        refresh()
        def worker_check():
            current()
            if state['generation'] != generation:
                raise ValueError('The reading-copy action changed.')
        def stop():
            worker_check()
            return frame.cancel_event.is_set()
        def operation():
            worker_check()
            if action == 'read':
                return read_result(result, cancelled=stop, guard=worker_check)
            return import_result(result, library, workspace, cancelled=stop, guard=worker_check)
        def done(value):
            if not usable() or generation != state['generation']:
                return
            state['working'] = False
            refresh()
            if action == 'read':
                from .speech_dialog import show_speech_reader
                current()
                show_speech_reader(frame, text=value, title='Reading copy: ' + result.path.name,
                                   source_guard=guard, recovery_slot='converted-reading-copy')
            else:
                finish(value)
        def failed(error):
            if usable() and generation == state['generation']:
                finish(getattr(error, 'completed_import', None), error)
        try:
            future = frame.run_job('Read verified copy' if action == 'read' else 'Import verified reading copy',
                                   operation, done, failed)
        except Exception:
            state['working'] = False
            refresh()
            raise
        if future is None and usable():
            state['working'] = False
            refresh()

    def clicked(action):
        try:
            begin(action)
        except Exception as error:
            if usable():
                status.SetValue(str(error))

    def closed(event):
        if state['alive']:
            state['alive'] = False
            state['generation'] += 1
            if state['working']:
                frame.cancel_event.set()
            if getattr(frame, 'conversion_results_dialog', None) is dialog:
                frame.conversion_results_dialog = None
        if event.GetEventType() == wx.EVT_CLOSE.typeId:
            dialog.Destroy()
        else:
            event.Skip()

    def destroyed(event):
        if event.GetEventObject() is dialog:
            closed(event)
        else:
            event.Skip()
    read.Bind(wx.EVT_BUTTON, lambda event: clicked('read'))
    ingest.Bind(wx.EVT_BUTTON, lambda event: clicked('import'))
    close.Bind(wx.EVT_BUTTON, lambda event: dialog.Close())
    choices.Bind(wx.EVT_LISTBOX, refresh)
    dialog.Bind(wx.EVT_CLOSE, closed)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    refresh()
    dialog.Show()
    choices.SetFocus()
    return dialog


def show_conversion_formats(frame):
    from .desktop_app import show_text
    try:
        from ..conversion.service import capabilities
        rows = [f'{item["extension"]}: {"available" if item["available"] else "unavailable"}; '
                f'{item["adapter"]}. {item["limitation"]}' for item in capabilities()]
        show_text(frame, 'Document conversion formats', '\n\n'.join(rows))
    except Exception as error:
        frame.report(f'Could not inspect document converters: {error}')


def install_conversion_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu = wx.Menu()
    for label, handler in [('Convert &documents for reading...', convert_selected_documents),
                           ('Supported document &formats...', show_conversion_formats)]:
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, handler=handler: handler(frame), item)
        command = "conversion.run" if handler is convert_selected_documents else "conversion.formats"
        register_menu_command(frame, CommandSpec(command, label.replace("&", "")), item)
    frame.menu_bar.Append(menu, '&Convert')
