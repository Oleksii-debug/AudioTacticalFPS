"""Native document conversion to separately identified reading derivatives."""
from pathlib import Path
import re
import stat

from ..conversion import ConversionService
from ..conversion.service import IMAGES
from ..conversion.safety import local_path


class _LibraryBinding:
    """One captured library identity, including the real directory entry.

    Invalidity is sticky: switching away and back cannot revive a stale modal
    selection or queued completion. Worker checks read Python state and local
    filesystem metadata; they do not call native window methods off-thread.
    """
    def __init__(self, frame):
        self.frame = frame
        self.library = getattr(frame, 'library', None)
        self.private_root = Path(self.library.root)
        self.paths = self._paths()
        self.cancel_event = frame.cancel_event
        self.invalid = False
        try:
            self.root_identity = self._directory_identity()
        except (OSError, ValueError):
            self.root_identity = None
            self.invalid = True

    def _paths(self):
        return (self.library.root, self.library.database, self.library.assets_dir,
                self.library.assets.root)

    def _directory_identity(self):
        root = local_path(self.private_root, exists=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('The captured library folder is unavailable.')
        return info.st_dev, info.st_ino

    def current(self):
        if self.invalid:
            return False
        try:
            valid = (bool(self.frame) and not getattr(self.frame, '_closing', False)
                     and self.frame.library is self.library and self._paths() == self.paths
                     and self._directory_identity() == self.root_identity)
        except (OSError, ValueError, RuntimeError, AttributeError):
            valid = False
        self.invalid = not valid
        return valid

    def cancelled(self):
        return not self.current() or self.cancel_event.is_set()

    def require_running(self):
        if self.cancelled():
            raise InterruptedError('Conversion stopped because the active library changed, closed or '
                                   'the operation was cancelled. Previously completed files remain '
                                   'in the selected export folder.')


def _capture_binding(frame):
    try:
        if not frame or getattr(frame, '_closing', False):
            return None
        binding = _LibraryBinding(frame)
        return binding if binding.current() else None
    except (OSError, ValueError, RuntimeError, AttributeError, TypeError):
        return None


def _output_path(root, index, source):
    name = re.sub(r'[^\w .-]', '_', source.stem, flags=re.UNICODE).strip(' .')[:100] or 'document'
    return root / f'{index:04d}-{name}-reading.txt'


def _select_sources(frame, binding):
    """The native chooser; probes may explicitly inject this selector only."""
    import wx
    dialog = wx.FileDialog(frame, 'Choose documents to convert for reading',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
    try:
        accepted = dialog.ShowModal() == wx.ID_OK
        if not binding.current() or not accepted:
            return ()
        return tuple(Path(value) for value in dialog.GetPaths())
    finally:
        dialog.Destroy()


def _select_destination(frame, binding):
    import wx
    dialog = wx.DirDialog(frame, 'Choose the folder for new reading text and provenance reports')
    try:
        accepted = dialog.ShowModal() == wx.ID_OK
        if not binding.current() or not accepted:
            return None
        return Path(dialog.GetPath())
    finally:
        dialog.Destroy()


def convert_selected_documents(frame):
    binding = _capture_binding(frame)
    if binding is None:
        return
    sources = _select_sources(frame, binding)
    if not binding.current() or not sources:
        return
    languages = ()
    profile = 'installed'
    if any(source.suffix.casefold() in IMAGES for source in sources):
        from .ocr_selection import choose_model_config
        try:
            config = choose_model_config(frame)
        except Exception as exc:
            if binding.current():
                frame.report(f'Could not choose installed OCR models: {exc}')
            return
        if not binding.current() or config is None:
            return
        if config.orientation or config.model_policy != 'default':
            frame.report('Image-to-text conversion uses the selected installed or managed profile. '
                         'Choose orientation off and the default model policy for this text-only operation.')
            return
        languages = tuple(config.languages.split('+'))
        profile = config.model_profile
    root = _select_destination(frame, binding)
    if not binding.current() or root is None:
        return

    def run():
        from ..research.export_file import capture_export_destination
        binding.require_running()
        capture_export_destination(_output_path(root, 1, sources[0]), private_root=binding.private_root)
        binding.require_running()
        converter = ConversionService(root, input_roots=sources)
        completed = []
        for index, source in enumerate(sources, 1):
            binding.require_running()
            output = _output_path(root, index, source)
            capture_export_destination(output, private_root=binding.private_root)
            binding.require_running()
            result = converter.extract(source, output,
                                       cancelled=binding.cancelled, ocr_languages=languages,
                                       ocr_profile=profile)
            warning = ('\n' + '\n'.join(result.warnings)) if result.warnings else ''
            completed.append(f'{source.name}\nReading text: {result.path}\nProvenance: {result.report_path}' + warning)
            binding.require_running()
        binding.require_running()
        return '\n\n'.join(completed)

    def done(summary):
        if binding.cancelled():
            return
        from .desktop_app import show_text
        show_text(frame, 'Converted reading documents',
                  'Original files were preserved. These separate derivatives are for reading and narration; '
                  'quotation approval still requires source review. Existing output files are never replaced.\n\n' + summary)
    if binding.current():
        frame.run_job('Convert documents for reading', run, done)


def show_conversion_formats(frame):
    binding = _capture_binding(frame)
    if binding is None:
        return
    from .desktop_app import show_text
    def run():
        binding.require_running()
        converter = ConversionService(binding.private_root)
        rows = [f'{item["extension"]}: {"available" if item["available"] else "unavailable"}; '
                f'{item["adapter"]}. {item["limitation"]}'
                for item in converter.capabilities(cancelled=binding.cancelled)]
        binding.require_running()
        return '\n\n'.join(rows)
    def done(text):
        if binding.current() and not binding.cancelled():
            show_text(frame, 'Document conversion formats', text)
    if binding.current():
        frame.run_job('Inspect document converters', run, done)


def install_conversion_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, '_conversion_menu_items', None)
    if existing is not None:
        return existing
    menu = wx.Menu()
    commands = (
        ('conversion.documents', 'Convert documents for reading', 'Convert &documents for reading...', convert_selected_documents),
        ('conversion.formats', 'Supported document formats', 'Supported document &formats...', show_conversion_formats),
    )
    items = []
    for identity, title, label, handler in commands:
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, handler=handler: handler(frame), item)
        items.append(item)
    frame.menu_bar.Append(menu, '&Convert')
    for (identity, title, _, _), item in zip(commands, items):
        register_menu_command(frame, CommandSpec(identity, title, 'main', ''), item)
    frame._conversion_menu_items = tuple(items)
    return frame._conversion_menu_items
