"""One accessible, read-only About flow using the desktop's existing job runner."""
import threading

from .. import component_notices as inventory
from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command

ABOUT_COMMAND = CommandSpec('help.about_components', 'About Resonance and component notices')


class AboutSession:
    """Read-only dialog state; all file work runs through frame.run_job."""

    def __init__(self, frame, *, changed, choose_notice, display_notice):
        self.frame = frame
        self.library = getattr(frame, 'library', None)
        self.root = getattr(self.library, 'root', None)
        self.changed, self.choose_notice, self.display_notice = changed, choose_notice, display_notice
        self.cancelled = threading.Event()
        self.closed, self.busy, self.epoch = False, False, 0
        self.catalogue = None
        self.selected = -1
        self.message = 'Installed component metadata has not been read yet.'

    def current(self):
        return (not self.closed and not getattr(self.frame, '_closing', False)
                and getattr(self.frame, 'library', None) is self.library
                and getattr(self.library, 'root', None) == self.root)

    def _cancelled(self):
        host_cancel = getattr(self.frame, 'cancel_event', None)
        return self.cancelled.is_set() or not self.current() or (
            host_cancel is not None and host_cancel.is_set())

    def _notify(self):
        if self.current():
            self.changed(self)

    def _start(self, title, operation, completed):
        if not self.current():
            return False
        if self.busy or getattr(self.frame, 'working', False):
            self.message = 'Another operation is running. Wait or use Cancel current operation.'
            self._notify()
            return False
        self.epoch += 1
        token = self.epoch
        self.busy = True
        self.cancelled.clear()
        self.message = title + '…'
        self._notify()
        settled = False

        def settle(result=None, error=None):
            nonlocal settled
            if settled:
                return
            settled = True
            if token != self.epoch:
                return
            self.busy = False
            if not self.current():
                return
            if error is not None:
                self.message = ('Notice reading cancelled.' if isinstance(error, InterruptedError)
                    else 'Component notices unavailable: ' + str(error))
                self._notify()
                return
            try:
                completed(result)
            except Exception as exc:
                self.message = 'Component notices unavailable: ' + str(exc)
            self._notify()

        try:
            # run_job returns None on the real desktop. Its own executor,
            # progress binding, admission failure and completion hooks are reused.
            self.frame.run_job(title, operation, done=lambda value: settle(value),
                               failed=lambda error: settle(error=error))
        except Exception as exc:
            settle(error=exc)
        return True

    def refresh(self):
        def complete(value):
            if type(value) is not inventory.Catalogue:
                raise ValueError('The installed component catalogue is invalid.')
            self.catalogue = value
            self.selected = 0 if value.components else -1
            self.message = 'Choose a component, then read its upstream notices.'
        return self._start('Read installed component inventory',
            lambda: inventory.catalogue(cancelled=self._cancelled), complete)

    def select(self, index):
        if self.busy or not self.current():
            return
        if self.catalogue is None or type(index) is not int or not 0 <= index < len(self.catalogue.components):
            self.selected = -1
            self.message = 'Choose an installed or delivered component.'
        else:
            self.selected = index
            component = self.catalogue.components[index]
            self.message = (component.label + '\n\nDeclared upstream licence metadata:\n'
                + (component.declared_license or 'No licence expression was supplied in this metadata entry.')
                + '\n\nThe selected text file, when available, retains its own licence and copyright terms.')
        self._notify()

    def read_selected(self):
        if self.catalogue is None or not 0 <= self.selected < len(self.catalogue.components):
            self.message = 'Choose a component first.'
            self._notify()
            return False
        component = self.catalogue.components[self.selected]

        def complete(notices):
            if not notices:
                self.message = 'No supported plain-text notice is listed for this component in this installation.'
                return
            index = self.choose_notice(component, notices)
            if index is None or not self.current():
                return
            if type(index) is not int or not 0 <= index < len(notices):
                raise ValueError('Choose one notice from the displayed inventory.')
            notice = notices[index]
            self._start('Read selected upstream notice',
                lambda: inventory.read_notice(notice, cancelled=self._cancelled),
                lambda text: self.display_notice(component.name + ' — upstream notice', text))
        return self._start('Read selected component notice inventory',
            lambda: inventory.notices(component, cancelled=self._cancelled), complete)

    def close(self):
        self.closed = True
        self.busy = False
        self.epoch += 1
        self.cancelled.set()


def create_about_dialog(frame):
    import wx

    class AboutDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='About Resonance and component notices', size=(800, 650),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive = True
            self.destroyed = False
            self._labels = None
            layout = wx.BoxSizer(wx.VERTICAL)
            self.about = wx.TextCtrl(self, value='Resonance\nSceneria Innovative Technologies\n\n'
                'Reading the local installed component inventory…',
                style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.about.SetName('About Resonance, version and notice availability')
            layout.Add(self.about, 2, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(self, label='Installed and delivered &component'), 0, wx.ALL, 4)
            self.components = wx.Choice(self, choices=[])
            self.components.SetName('Installed and delivered component')
            self.components.Bind(wx.EVT_CHOICE, lambda event: self.session.select(self.components.GetSelection()))
            layout.Add(self.components, 0, wx.EXPAND | wx.ALL, 4)
            self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.details.SetName('Component licence metadata and reading status')
            layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 8)
            actions = wx.BoxSizer(wx.HORIZONTAL)
            self.read_button = wx.Button(self, label='&Read upstream notices…')
            self.read_button.SetName('Read selected component upstream notices')
            self.read_button.Bind(wx.EVT_BUTTON, lambda event: self.session.read_selected())
            actions.Add(self.read_button, 0, wx.ALL, 4)
            self.refresh_button = wx.Button(self, label='Re&fresh component list')
            self.refresh_button.SetName('Refresh installed component list')
            self.refresh_button.Bind(wx.EVT_BUTTON, lambda event: self.session.refresh())
            actions.Add(self.refresh_button, 0, wx.ALL, 4)
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            self.close_button.SetName('Close About Resonance')
            self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
            actions.Add(self.close_button, 0, wx.ALL, 4)
            layout.Add(actions, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.session = AboutSession(frame, changed=self.update_state,
                choose_notice=self.choose_notice, display_notice=self.display_notice)
            self.update_state(self.session)
            self.about.SetFocus()

        def update_state(self, session):
            if not self.alive:
                return
            if session.catalogue is not None:
                self.about.SetValue(session.catalogue.about)
                labels = [component.label for component in session.catalogue.components]
                if labels != self._labels:
                    self._labels = labels
                    self.components.Set(labels)
                self.components.SetSelection(session.selected)
            self.details.SetValue(session.message)
            self.components.Enable(not session.busy)
            self.read_button.Enable(not session.busy and session.selected >= 0)
            self.refresh_button.Enable(not session.busy)

        def choose_notice(self, component, notices):
            dialog = wx.SingleChoiceDialog(self, 'Choose the actual upstream notice to read.',
                component.name + ' — notice files', [notice.title for notice in notices])
            try:
                return dialog.GetSelection() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()

        def display_notice(self, title, text):
            if self.alive and self.session.current():
                from .desktop_app import show_text
                show_text(self, title, text)

        def on_close(self, event=None):
            if not self.alive:
                return
            self.session.close()
            self.alive = False
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Destroy()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.session.close()
                self.alive = False
                self.destroyed = True
            event.Skip()

    return AboutDialog()


def show_about(frame, event=None):
    import wx
    if getattr(frame, '_closing', False):
        return
    if getattr(frame, 'working', False):
        frame.report('Wait for the current operation or cancel it before opening About.')
        return
    dialog = create_about_dialog(frame)
    try:
        wx.CallAfter(dialog.session.refresh)
        dialog.ShowModal()
    finally:
        dialog.session.close()
        if not dialog.destroyed:
            dialog.Destroy()


def install_about_actions(frame):
    import wx
    if getattr(frame, '_about_components_installed', False):
        return
    bar = frame.GetMenuBar()
    candidates = [bar.GetMenu(index) for index in range(bar.GetMenuCount())
                  if bar.GetMenuLabel(index).split('\t', 1)[0].replace('&', '').strip().casefold() == 'help']
    if len(candidates) != 1:
        raise ValueError('The About command requires the existing Help menu.')
    item = candidates[0].Append(wx.ID_ANY, '&About Resonance and component notices…')
    frame.Bind(wx.EVT_MENU, lambda event: show_about(frame, event), item)
    register_menu_command(frame, ABOUT_COMMAND, item)
    frame._about_components_installed = True
