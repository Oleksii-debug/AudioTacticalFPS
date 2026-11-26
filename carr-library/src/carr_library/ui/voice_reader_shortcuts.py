"""Live-reader commands use the existing context-aware keyboard manager."""


def voice_reader_commands():
    from .command_registry import CommandSpec
    return tuple(CommandSpec(identity, label, 'voice_reader', shortcut) for identity, label, shortcut in (
        ('voice_reader.read', 'Read selection or all', 'F6'),
        ('voice_reader.read_from_cursor', 'Read from cursor', 'F9'),
        ('voice_reader.pause', 'Pause or resume playback', 'F7'),
        ('voice_reader.stop', 'Stop synthesis or playback', 'F8'),
        ('voice_reader.save', 'Save reading copy', 'Ctrl+S'),
        ('voice_reader.save_as', 'Save reading copy as', 'Ctrl+Shift+S'),
        ('voice_reader.seek', 'Seek playback time', ''),
    ))


class VoiceReaderKeyboardAdapter:
    def __init__(self, dialog, manager, buttons):
        import wx
        self.dialog, self.manager, self.buttons = dialog, manager, buttons
        self.closed = False
        self.identifiers = {}
        for spec in manager.registry.commands('voice_reader'):
            if spec.command_id not in buttons:
                continue
            button, _ = buttons[spec.command_id]
            button.SetName(spec.label)
            identifier = wx.NewIdRef()
            self.identifiers[spec.command_id] = identifier
            dialog.Bind(wx.EVT_MENU, lambda event, identity=spec.command_id: self.invoke(identity), id=int(identifier))
        manager._adapters.add(self)
        self.refresh()

    def available(self, identity):
        return not self.closed and identity in self.buttons and self.buttons[identity][0].IsEnabled()

    def invoke(self, identity):
        if not self.available(identity):
            return False
        self.buttons[identity][1](None)
        return True

    def refresh(self):
        if self.closed:
            return
        import wx
        from .keyboard_settings import accelerator_tuple
        entries = []
        for identity, identifier in self.identifiers.items():
            button, _ = self.buttons[identity]
            shortcut = self.manager.shortcut(identity)
            button.SetLabel(button.GetName() + (f' ({shortcut})' if shortcut else ''))
            if shortcut:
                entries.append(accelerator_tuple(shortcut, identifier))
        self.dialog.SetAcceleratorTable(wx.AcceleratorTable(entries))

    def close(self):
        if not self.closed:
            import wx
            self.closed = True
            self.manager._adapters.discard(self)
            for identifier in self.identifiers.values():
                self.dialog.Unbind(wx.EVT_MENU, id=int(identifier))
