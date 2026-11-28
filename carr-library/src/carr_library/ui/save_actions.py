"""App-wide save and recoverable research-note drafts, without changing source text."""
from pathlib import Path

from ..durable_state import atomic_json, read_json


def install_save_coordinator(frame):
    """Register the existing scoped note editor once on its owning desktop."""
    from .save_all import SaveCoordinator, SaveResult
    existing = getattr(frame, '_save_coordinator', None)
    if existing is not None:
        return existing
    coordinator = frame._save_coordinator = SaveCoordinator()

    def save_note(completed):
        panel = getattr(frame, 'evidence', None)
        if panel is None or not frame._dirty():
            completed(SaveResult(True))
        else:
            panel.save_note(completed=completed)

    coordinator.register('research.note', 'Research note',
        dirty=lambda: not getattr(frame, '_closing', False) and frame._dirty(),
        save=save_note, focus=getattr(frame, '_restore_note_focus', None))
    return coordinator



def _save_owner(window):
    owner, seen = window, set()
    while owner is not None and id(owner) not in seen:
        seen.add(id(owner))
        if getattr(owner, '_save_coordinator', None) is not None:
            return owner
        parent = getattr(owner, 'GetParent', None)
        owner = parent() if callable(parent) else None
    return None


def cancel_save_all(window, event=None, *, report=None):
    """Request cancellation without touching a different active operation."""
    owner = _save_owner(window)
    if owner is None or not owner._save_coordinator.busy:
        if report is not None:
            report('No Save All request is running.')
        return False
    cancel = getattr(owner, 'cancel_event', None)
    if cancel is None:
        if report is not None:
            report('This host cannot cancel Save All; wait for the current save result.')
        return False
    cancel.set()
    message = 'Save All cancellation requested. Completed saves remain saved; later drafts are retained.'
    if not getattr(owner, '_closing', False):
        owner.report(message)
    if report is not None:
        report(message)
    return True


def save_all_open(window, event=None, *, report=None):
    """Save registered drafts through their existing durable save operations.

    A modal editor can invoke the same coordinator while its parent menu is
    disabled. The library/scope snapshot belongs to this explicit request.
    """
    owner = _save_owner(window)
    if owner is None:
        if report is not None:
            report('Save All is unavailable here. Use this editor\'s Save command.')
        return False

    def announce(message):
        if not getattr(owner, '_closing', False):
            owner.report(message)
        if report is not None:
            report(message)

    coordinator = owner._save_coordinator
    if coordinator.busy:
        announce('Save All is already waiting for a durable save result.')
        return False
    if (getattr(owner, '_closing', False) or getattr(owner, 'working', False)
            or getattr(owner, 'busy', False)):
        announce('An operation is running. Wait for completion or cancel it before Save All.')
        return False
    library = getattr(owner, 'library', None)
    scope = getattr(owner, 'active', None)
    evidence = getattr(owner, 'evidence', None)
    cancel = getattr(owner, 'cancel_event', None)
    if cancel is not None:
        # A new explicit Save All starts a new request, like run_job. A later
        # cancellation is observed before the next participant can clear it.
        cancel.clear()

    def cancelled():
        return (getattr(owner, '_closing', False)
                or getattr(owner, 'library', None) is not library
                or getattr(owner, 'active', None) != scope
                or getattr(owner, 'evidence', None) is not evidence
                or (cancel is not None and cancel.is_set()))

    return coordinator.save_all(lambda result: announce(result.message()), cancelled=cancelled)


def install_save_actions(frame):
    import wx
    menu = wx.Menu()

    def save(event=None):
        if frame.working:
            frame.report('A library operation is running. Your editable draft remains available; save when it completes.')
            return
        if frame._dirty():
            frame.evidence.save_note()
        else:
            frame.report('All committed library changes are saved. Reading and studio windows save their own editable projects with Ctrl+S.')

    for title, handler in [('&Save\tCtrl+S', save),
                           ('Save &all', lambda event: save_all_open(frame, event))]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
    menu.AppendSeparator()
    item = menu.Append(wx.ID_EXIT, 'E&xit\tAlt+F4')
    frame.Bind(wx.EVT_MENU, lambda event: frame.Close(), item)
    frame.menu_bar.Insert(0, menu, '&File')
    install_research_recovery(frame, menu)


def install_research_recovery(frame, menu):
    """Share draft recovery and standard edit commands with one Save menu."""
    import wx
    install_save_coordinator(frame)
    if getattr(frame, '_draft_save_timer', None) is not None:
        return

    def recover_legacy(event=None):
        path = frame.library.root / 'state' / 'recovery' / 'research-note.json'
        try:
            if not path.exists():
                frame.report('There is no recovered research-note draft.')
                return
            data = read_json(path)
            panel = getattr(frame, 'evidence', None)
            authorize = getattr(panel, '_authorized_recovery_row', None)
            if not callable(authorize):
                raise ValueError('Open the matching saved evidence before reviewing its legacy draft.')
            row = authorize()
            if (data.get('workspace'), data.get('topic'), data.get('evidence')) != (
                    panel.workspace, panel.topic, row.evidence_id):
                raise ValueError('This legacy draft belongs to different evidence. Its file was retained.')
            from .desktop_app import show_text
            show_text(frame, 'Recovered research-note draft',
                'A saved draft is not an approved quotation. Open the matching library and evidence before applying it.\n'
                f'Library: {data["workspace"]}\nTopic: {data["topic"]}\nEvidence: {data["evidence"]}\n\n{data["note"]}')
        except Exception as exc:
            frame.report(f'Could not recover the draft: {exc}')

    def recover(event=None):
        panel = getattr(frame, 'evidence', None)
        if panel is None or not callable(getattr(panel, 'recover_draft', None)):
            frame.report('Open the saved evidence whose research note you want to recover.')
            return
        panel.recover_draft()

    item = menu.Append(wx.ID_ANY, 'Recover research-note draft...')
    frame.Bind(wx.EVT_MENU, recover, item)
    legacy = menu.Append(wx.ID_ANY, 'Review legacy research-note draft...')
    frame.Bind(wx.EVT_MENU, recover_legacy, legacy)
    timer = wx.Timer(frame)
    frame._draft_save_timer = timer
    owner_id = frame.GetId()
    previous = [None]
    active = True

    def autosave(event):
        if not active or frame._closing or not frame._dirty() or frame.working:
            return
        panel = frame.evidence
        if panel is None or panel.displayed is None or frame.active is None:
            return
        if getattr(panel, '_recovery_store', None) is not None:
            # The per-evidence store owns new checkpoints. Keep the legacy
            # one-slot file unchanged for an explicit read-only review.
            return
        value = {'schema': 1, 'workspace': frame.active[0], 'topic': frame.active[1],
                 'evidence': panel.displayed.evidence_id, 'note': panel.note.GetValue()}
        identity = (str(frame.library.root), value)
        if previous[0] == identity:
            return
        try:
            path = frame.library.root / 'state' / 'recovery' / 'research-note.json'
            atomic_json(path, value, replace=path.exists())
            previous[0] = identity
        except Exception as exc:
            # An autosave failure must be announced, while retaining the live editor.
            frame.status.SetValue(f'Research draft could not be saved automatically: {exc}. Keep this window open and use Save.')

    def stop_autosave(event):
        nonlocal active
        if active and event.GetId() == owner_id:
            # SIP can invalidate the original Python frame before wxFrame
            # emits this event. Its captured native ID still identifies it;
            # do not compare replacement proxies or call the dying frame.
            # Native destruction removes its bindings. This separate timer
            # must stop before another notification dereferences that owner.
            active = False
            timer.Stop()
        event.Skip()

    frame.Bind(wx.EVT_WINDOW_DESTROY, stop_autosave)
    frame.Bind(wx.EVT_TIMER, autosave, timer)
    timer.Start(3000)

    edit = wx.Menu()
    def focused(action):
        target = wx.Window.FindFocus()
        method = getattr(target, action, None)
        if callable(method):
            method()
        else:
            frame.report('This command is available in an editable text control.')
    for label, action in [('Undo', 'Undo'), ('Redo', 'Redo'), ('Cut', 'Cut'), ('Copy', 'Copy'),
                           ('Paste', 'Paste'), ('Select all', 'SelectAll')]:
        item = edit.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, lambda event, action=action: focused(action), item)
    frame.menu_bar.Insert(1, edit, '&Edit')
