"""The spelling dialog borrows the existing operation session and native owner."""
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from carr_library.audio.capture_types import AudioCleanupPending
from carr_library.audio.cleanup import CleanupOwner
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.ui.narration_editor import NarrationController
from carr_library.ui import spellcheck_dialog as ui
from ._fixtures import Widget, Sizer, dictionary


def test_modal_close_waits_for_actual_retained_native_cleanup(tmp_path, monkeypatch):
    callbacks, owner_thread = Queue(), get_ident()
    attempted, release = Event(), Event()
    owner = CleanupOwner(lambda: attempted.set() or release.is_set())
    controller = NarrationController(NarrationStore(tmp_path), new_project('wurld'))
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path),
        cancel_event=Event(), working=False, _closing=False)
    executor = ThreadPoolExecutor(max_workers=1)
    calls, captured = [], {}
    def dispatch(function, *args):
        callbacks.put((function, args))
    def run_job(title, operation, done, failed, **kwargs):
        assert get_ident() == owner_thread and not frame.working
        frame.working = True
        future = executor.submit(operation)
        calls.append(future)
        def arrived(future):
            def settle():
                frame.working = False
                try:
                    done(future.result())
                except Exception as exc:
                    failed(exc)
            dispatch(settle)
        future.add_done_callback(arrived)
        return True
    frame.run_job = run_job

    class Native(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.modal = False
        def Clear(self): self.native(); self.choices = []; self.selection = -1
        def SetItems(self, items): self.Set(items)
        def ShowModal(self):
            self.native()
            self.modal = True
            while self.modal:
                function, args = callbacks.get(timeout=10)
                function(*args)
            return 1
        def IsModal(self): self.native(); return self.modal
        def EndModal(self, value): self.native(); self.modal = False

    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL',
        'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CLOSE',
        'EVT_TEXT', 'EVT_CHOICE', 'ID_CANCEL', 'ID_OK', 'EVT_WINDOW_DESTROY', 'FD_OPEN',
        'FD_FILE_MUST_EXIST')
    wx = SimpleNamespace(**{key: index + 1 for index, key in enumerate(constants)},
        Dialog=Native, ScrolledWindow=Native, StaticText=Native, TextCtrl=Native,
        Choice=Native, Button=Native, BoxSizer=Sizer, CallAfter=dispatch)

    def pending_review(*args, **kwargs):
        raise AudioCleanupPending('Synthetic native family is not yet quiescent.', cleanup_owner=owner)
    monkeypatch.setattr(ui, 'review_spelling', pending_review)
    selected = dictionary(tmp_path)

    def ready(view, session):
        captured.update(view=view, session=session)
        view.dictionary.ChangeValue(str(selected))
        view.on_review()
        assert attempted.wait(5), 'The actual shared cleanup waiter did not attempt closure.'
        assert session.busy and view.busy and not owner.quiescent_event.is_set()
        veto = []
        view.on_close(SimpleNamespace(CanVeto=lambda: True, Veto=lambda: veto.append(True)))
        assert veto == [True] and view.alive and view.IsModal()
        assert session.close_requested and session.cancel.is_set()
        assert not session.closed and not owner.quiescent_event.is_set()
        release.set()

    try:
        ui.show_spellcheck_dialog(None, frame=frame, library_root=tmp_path,
            capture=lambda: ui.capture_spelling(controller, (0, 0)),
            apply=lambda *_: pytest.fail('Close applied a spelling change'),
            undo=lambda *_: pytest.fail('Close undid a reading revision'),
            guard=lambda: None, wx_module=wx, on_ready=ready)
        assert owner.quiescent_event.is_set() and captured['session'].closed
        assert captured['view'].destroyed
        assert controller.pending_text == 'wurld'
    finally:
        release.set()
        owner.close()
        executor.shutdown(wait=True)
        while True:
            try:
                function, args = callbacks.get_nowait()
            except Empty:
                break
            function(*args)
    assert all(future.done() for future in calls) and not frame.working
