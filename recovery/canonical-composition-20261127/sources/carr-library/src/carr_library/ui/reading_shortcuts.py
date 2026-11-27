"""Contextual Reader commands sharing the application's validated keymap."""
from .keyboard_settings import (
    KeyboardManager, accelerator_tuple, show_command_launcher, show_keyboard_settings,
)


class ReaderKeyboardAdapter:
    def __init__(self, reader, manager=None):
        import wx
        self.reader = reader
        self.manager = manager or KeyboardManager()
        if self.manager.store is None:
            self.manager.private_root = reader.controller.desk.library.root
        self._ids = {}
        self._disposed = False
        for command in self.manager.registry.commands('reader'):
            identifier = wx.NewIdRef()
            self._ids[command.command_id] = identifier
            reader.dialog.Bind(wx.EVT_MENU,
                               lambda event, identity=command.command_id: self.invoke(identity),
                               id=int(identifier))
        self.manager._adapters.add(self)
        self.refresh()

    def available(self, command_id):
        if self._disposed or self.reader.closed:
            return False
        if self.reader.busy and command_id not in ('reader.speech_stop', 'reader.speech_status'):
            return False
        pair = self.reader.command_buttons.get(command_id)
        if pair is None:
            return False
        try:
            return pair[0].IsEnabled()
        except RuntimeError:
            return False

    def invoke(self, command_id):
        if not self.available(command_id):
            return False
        self.reader.command_buttons[command_id][1](None)
        return True

    def refresh(self):
        if self._disposed or self.reader.closed:
            return
        import wx
        entries = []
        for command in self.manager.registry.commands('reader'):
            shortcut = self.manager.shortcut(command.command_id)
            if shortcut:
                entries.append(accelerator_tuple(shortcut, self._ids[command.command_id]))
            pair = self.reader.command_buttons.get(command.command_id)
            if pair is not None:
                # Keep native accessible names stable; the current shortcut is
                # also available through the guide and the visible button label.
                pair[0].SetLabel(pair[0].GetName() + (f' ({shortcut})' if shortcut else ''))
        self.reader.accelerators = entries
        self.reader.dialog.SetAcceleratorTable(wx.AcceleratorTable(entries))

    def apply_overrides(self, overrides):
        return self.manager.apply_overrides(overrides)

    def shortcut(self, command_id):
        return self.manager.shortcut(command_id)

    def guide(self):
        return '\n'.join(
            f'{self.shortcut(command.command_id) or "Not assigned"}: {command.label}.'
            for command in self.manager.registry.commands('reader'))

    def commands(self, event=None):
        if self.reader.closed or self.reader.busy:
            return
        return show_command_launcher(self.reader.dialog, self.manager, context='reader',
                                     invoke=self.invoke, available=self.available)

    def settings(self, event=None):
        if self.reader.closed or self.reader.busy:
            return
        return show_keyboard_settings(self.reader.dialog, self.manager, context='reader')

    def close(self):
        if self._disposed:
            return
        self._disposed = True
        self.manager._adapters.discard(self)
        import wx
        if not self.reader.dialog:
            return
        for identifier in self._ids.values():
            try:
                self.reader.dialog.Unbind(wx.EVT_MENU, id=int(identifier))
            except RuntimeError:
                # Native parent deletion already removed these bindings.
                break
