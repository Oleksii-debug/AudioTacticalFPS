"""Small native-widget/serial-worker fixtures reused from PR152; no engine imitation."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event, get_ident


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


class ReaderWorker:
    """The reader's admission/cancel/queued-completion contract with real threads."""
    def __init__(self, cancel):
        self.cancel, self.owner = cancel, get_ident()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='spelling-handler')
        self.callbacks, self.gate = Queue(), Event()
        self.gate.set()
        self.busy, self.future, self.worker_threads = False, None, []
        self.reject = False

    def __call__(self, operation, done, *, on_error):
        assert get_ident() == self.owner
        if self.busy or self.reject:
            return False
        self.busy = True
        self.cancel.clear()
        def work():
            self.worker_threads.append(get_ident())
            if not self.gate.wait(5):
                raise TimeoutError('Synthetic worker admission barrier timed out.')
            return operation()
        self.future = self.executor.submit(work)
        self.future.add_done_callback(lambda future: self.callbacks.put((future, done, on_error)))
        return True

    def ready(self):
        assert self.future is not None
        try:
            self.future.result(timeout=5)
        except Exception:
            pass

    def drain(self):
        assert get_ident() == self.owner
        future, done, failed = self.callbacks.get(timeout=5)
        self.busy = False
        try:
            done(future.result(timeout=5))
        except Exception as exc:
            failed(exc)

    def close(self):
        self.cancel.set(); self.gate.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
        while not self.callbacks.empty():
            self.drain()


