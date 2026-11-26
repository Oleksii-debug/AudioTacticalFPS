"""Dialog compatibility on the shared command registry; no second keymap store."""
from dataclasses import dataclass

from .command_registry import CommandRegistry, CommandSpec, MAX_COMMANDS


@dataclass(frozen=True)
class CommandBinding:
    id: str
    label: str
    default: str
    handler: object


# Declare the existing editor identities before loading the shared saved map.
# Opening/closing a dialog only attaches native handlers, never removes its IDs.
CONTEXT_SPECS = tuple(CommandSpec(identity, label, context, shortcut)
    for context, rows in (
        ('reading_copy', (
            ('reading_copy.save', 'Save reading copy', 'Ctrl+S'),
            ('reading_copy.save_as', 'Save reading copy as new project', 'Ctrl+Shift+S'),
            ('reading_copy.find', 'Find in reading copy', 'Ctrl+F'),
            ('reading_copy.replace', 'Replace literal text in reading copy', 'Ctrl+H'),
            ('reading_copy.status', 'Reading-copy status', 'F6'),
            ('reading_copy.read', 'Open voice reader with current reading copy', 'F5'))),
        ('speech_reader', (
            ('speech_reader.save', 'Save voice reading copy and settings', 'Ctrl+S'),
            ('speech_reader.save_as', 'Save voice reading project as', 'Ctrl+Shift+S'),
            ('speech_reader.read', 'Read selected text or all text', 'F6'),
            ('speech_reader.pause', 'Pause or resume speech', 'F7'),
            ('speech_reader.stop', 'Stop synthesis or playback', 'F8'))),
        ('metadata', (('metadata.save', 'Save book metadata', 'Ctrl+S'),)),
        ('reference', (('reference.save', 'Save bibliographic reference', 'Ctrl+S'),)),
        ('engines', (('engines.save', 'Save local engine settings', 'Ctrl+S'),)),
        ('local_assistant', (
            ('assistant.preview', 'Preview local plan', 'Ctrl+Shift+P'),
            ('assistant.run', 'Run or resume reviewed plan', 'Ctrl+R'),
            ('assistant.pause', 'Pause local operation', 'F8'),
            ('assistant.status', 'Read plan and results', 'F6'))),
    ) for identity, label, shortcut in rows)


def bind_context_shortcuts(window, parent, context, bindings):
    """Resolve either host attribute to the same authoritative manager."""
    seen = set()
    while parent is not None and id(parent) not in seen:
        seen.add(id(parent))
        manager = getattr(parent, 'keyboard_commands', None) or getattr(parent, 'keyboard', None)
        if manager is not None:
            return manager.bind_window(window, context, bindings)
        parent = parent.GetParent() if hasattr(parent, 'GetParent') else None
    return lambda: None


class _DialogAdapter:
    def __init__(self, manager, window, bindings, convert):
        import wx
        self.wx, self.manager, self.window = wx, manager, window
        self.bindings, self.convert = bindings, convert
        self.ids = []
        self.closed = False
        self.previous_table = window.GetAcceleratorTable()

    def attach(self):
        for binding in self.bindings:
            identifier = self.wx.NewIdRef()
            self.ids.append(identifier)
            self.window.Bind(self.wx.EVT_MENU,
                lambda event, binding=binding: self.invoke(binding, event), id=int(identifier))
        self.window.Bind(self.wx.EVT_WINDOW_DESTROY, self.destroyed)
        self.refresh()

    def alive(self):
        try:
            return not self.closed and not self.window.IsBeingDeleted()
        except RuntimeError:
            return False

    def invoke(self, binding, event):
        if (not self.alive() or not self.window.IsEnabled()
                or getattr(self.manager.frame, '_closing', False)):
            return False
        # Use the actual editor handler and its existing scope/dirty/busy guards.
        binding.handler(event)
        return True

    def refresh(self):
        if not self.alive():
            return
        entries = []
        for binding, identifier in zip(self.bindings, self.ids):
            shortcut = self.manager.shortcut(binding.id)
            if shortcut:
                entries.append(self.convert(shortcut, int(identifier)))
        self.window.SetAcceleratorTable(self.wx.AcceleratorTable(entries))

    def destroyed(self, event):
        if event.GetEventObject() is self.window:
            self.close()
        event.Skip()

    def close(self):
        if self.closed:
            return
        alive = self.alive()
        self.closed = True
        self.manager._adapters.discard(self)
        if alive:
            for identifier in self.ids:
                self.window.Unbind(self.wx.EVT_MENU, id=int(identifier))
            self.window.Unbind(self.wx.EVT_WINDOW_DESTROY, handler=self.destroyed)
            self.window.SetAcceleratorTable(self.previous_table)


def bind_window(manager, window, context, bindings, convert):
    """Attach a bounded, validated dialog context and return idempotent cleanup."""
    bindings = tuple(bindings)
    if not 1 <= len(bindings) <= MAX_COMMANDS:
        raise ValueError('A dialog needs a bounded nonempty command list.')
    if any(getattr(adapter, 'window', None) is window for adapter in manager._adapters):
        raise ValueError('This dialog already has a keyboard context.')
    proposed = CommandRegistry(manager.registry.commands())
    seen = set()
    for binding in bindings:
        if not isinstance(binding, CommandBinding) or not callable(binding.handler):
            raise ValueError('Dialog commands require a CommandBinding and callable handler.')
        spec = CommandSpec(binding.id, binding.label, context, binding.default)
        if spec.command_id in seen:
            raise ValueError('A dialog repeats a command identity.')
        seen.add(spec.command_id)
        if spec.command_id in proposed.specs:
            if proposed.get(spec.command_id) != spec:
                raise ValueError('A dialog command conflicts with its registered definition.')
        else:
            proposed.register(spec)
    proposed.validate(manager.overrides)
    for binding in bindings:
        shortcut = proposed.shortcut(binding.id, manager.overrides)
        if shortcut:
            convert(shortcut, 1)
    previous_registry = manager.registry
    adapter = _DialogAdapter(manager, window, bindings, convert)
    manager.registry = proposed
    try:
        adapter.attach()
        manager._adapters.add(adapter)
    except Exception:
        adapter.close()
        manager.registry = previous_registry
        raise
    return adapter.close
