"""Native-control stand-ins shared by appearance and pacing qualification.

Ported from the selected owner test seams; no spelling engine or runtime assets.
"""
from threading import get_ident


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.owner = get_ident()
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name = -1, True, ''
        self.destroyed, self.children, self.handlers = False, [], {}
        self.parent = parent if isinstance(parent, Widget) else None
        if isinstance(parent, Widget):
            parent.children.append(self)

    def native(self):
        assert get_ident() == self.owner, 'Native control accessed from the worker.'
        assert not self.destroyed, 'Native control accessed after destruction.'

    def GetId(self): self.native(); return id(self)
    def __bool__(self): return not self.destroyed
    def SetName(self, name): self.native(); self.name = name
    def GetName(self): self.native(); return self.name
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def SetValue(self, value): self.native(); self.value = value
    def Set(self, values): self.native(); self.choices = list(values); self.selection = -1
    def SetSelection(self, index): self.native(); self.selection = index
    def GetSelection(self): self.native(); return self.selection
    def Enable(self, enabled=True): self.native(); self.enabled = enabled
    def IsEnabled(self): self.native(); return self.enabled
    def Bind(self, event, handler, *args): self.native(); self.handlers[event] = handler
    def SetSizer(self, *args): self.native()
    def SetScrollRate(self, *args): self.native()
    def FitInside(self): self.native()
    def Wrap(self, *args): self.native()
    def SetFocus(self): self.native(); self.focused = True
    def IsModal(self): self.native(); return False
    def Hide(self): self.native(); self.hidden = True
    def Destroy(self):
        self.native()
        for child in tuple(self.children):
            child.Destroy()
        if self.parent is not None:
            self.parent.children.remove(self)
        self.destroyed = True


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class NativeText:
    def __init__(self, source, normalize):
        self.normalize, self.selection = normalize, (0, 0)
        self.ChangeValue(source)

    def ChangeValue(self, text):
        self.text = text.replace('\r\n', '\n') if self.normalize else text
        self.selection = (0, 0)

    def GetValue(self): return self.text
    def GetLastPosition(self): return len(self.text.encode('utf-16-le')) // 2
    def GetRange(self, start, end):
        return self.text.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
    def GetSelection(self): return self.selection
    def SetSelection(self, start, end): self.selection = (start, end)
    def SetInsertionPoint(self, value): self.selection = (value, value)
