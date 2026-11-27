"""Completion-aware saving for registered desktop drafts; no GUI dependency."""
from dataclasses import dataclass
from collections import deque


@dataclass(frozen=True)
class SaveResult:
    saved: bool
    message: str = ''


@dataclass(frozen=True)
class SaveSummary:
    saved: tuple[str, ...]
    remaining: tuple[tuple[str, str], ...]
    cancelled: bool = False

    @property
    def complete(self):
        return not self.remaining and not self.cancelled

    def message(self):
        if self.complete:
            return (f'All {len(self.saved)} open drafts saved.' if self.saved else
                    'No unsaved drafts. Completed changes are already stored.')
        prefix = f'{len(self.saved)} drafts saved. '
        if self.cancelled:
            prefix += 'Save All cancelled. '
        return prefix + 'Unsaved changes remain: ' + '; '.join(
            f'{label}: {reason}' for label, reason in self.remaining)


def coordinator_for(window):
    """Find the owning main window's coordinator through native parent links."""
    seen = set()
    while window is not None and id(window) not in seen:
        seen.add(id(window))
        coordinator = getattr(window, '_save_coordinator', None)
        if coordinator is not None:
            return coordinator
        parent = getattr(window, 'GetParent', None)
        window = parent() if callable(parent) else None
    return None


@dataclass
class _Participant:
    label: str
    dirty: object
    save: object
    focus: object


class SaveCoordinator:
    """UI-thread coordinator; participant completion must run on that thread.

    A participant calls completion(SaveResult(...)) only after its durable write
    and visible saved baseline are updated. Its dirty predicate remains true
    when newer edits exist. No truthy return value is treated as a completed save.
    """
    def __init__(self):
        self.participants = {}
        self.busy = False
        self._completion_observers = []

    def register(self, key, label, *, dirty, save, focus=None):
        if not isinstance(key, str) or not key or not isinstance(label, str) or not label:
            raise ValueError('A named save participant is required.')
        if not callable(dirty) or not callable(save) or (focus is not None and not callable(focus)):
            raise ValueError('Save participants require callable draft and save handlers.')
        participant = _Participant(label, dirty, save, focus)
        self.participants[key] = participant

        def unregister():
            if self.participants.get(key) is participant:
                self.participants.pop(key, None)
        return unregister

    def has_unsaved(self):
        """Check current drafts without starting a write or approving a job.

        Validation failures propagate so callers keep the window open.
        """
        return any(participant.dirty() for participant in tuple(self.participants.values()))

    def after_current(self, completed):
        """Observe the active save once; do not start or repeat any write."""
        if not callable(completed):
            raise ValueError('A save completion handler is required.')
        if not self.busy:
            return False
        self._completion_observers.append(completed)
        return True

    def save_all(self, completed, *, cancelled=lambda: False):
        if not callable(completed) or not callable(cancelled):
            raise ValueError('Save completion and cancellation handlers must be callable.')
        if self.busy:
            return False
        self.busy = True
        pending = deque(self.participants.items())
        saved, failures = [], {}
        stopped = False
        waiting = False
        draining = False
        finished = False

        def finish():
            nonlocal finished
            if finished:
                return
            finished = True
            # Recheck every live draft, including newly registered documents.
            for key, participant in tuple(self.participants.items()):
                try:
                    dirty = participant.dirty()
                except Exception as exc:
                    failures[key] = (participant.label, str(exc))
                    continue
                if dirty and key not in failures:
                    failures[key] = (participant.label, 'newer or unsaved edits are retained')
            observers, self._completion_observers = self._completion_observers, []
            self.busy = False
            summary = SaveSummary(tuple(saved), tuple(failures.values()), stopped)
            try:
                completed(summary)
            finally:
                for observer in observers:
                    observer(summary)
            if failures:
                key = next(iter(failures))
                participant = self.participants.get(key)
                if participant is not None and participant.focus is not None:
                    try:
                        participant.focus()
                    except Exception:
                        pass

        def dispatch(key, participant):
            nonlocal waiting
            waiting = True
            replied = False

            def receive(result):
                nonlocal replied, waiting
                if replied or finished:
                    return
                replied = True
                waiting = False
                if not isinstance(result, SaveResult):
                    failures[key] = (participant.label, 'save completion was not confirmed')
                elif not result.saved:
                    failures[key] = (participant.label, result.message or 'save failed; draft retained')
                else:
                    saved.append(participant.label)
                advance()

            try:
                participant.save(receive)
            except Exception as exc:
                receive(SaveResult(False, str(exc)))

        def advance():
            # A save handler may complete synchronously or asynchronously. The
            # trampoline keeps both paths on one queue without nesting Python
            # calls once per document, including already-clean documents.
            nonlocal stopped, draining
            if draining or finished:
                return
            draining = True
            try:
                while not waiting and not finished:
                    try:
                        stop = cancelled()
                    except Exception as exc:
                        stopped = True
                        failures[object()] = ('Save All', f'Cancellation status could not be read: {exc}')
                        finish()
                        break
                    if stop:
                        stopped = True
                        finish()
                        break
                    if not pending:
                        finish()
                        break
                    key, participant = pending.popleft()
                    if self.participants.get(key) is not participant:
                        failures[key] = (participant.label, 'the open document changed during saving')
                        continue
                    try:
                        dirty = participant.dirty()
                    except Exception as exc:
                        failures[key] = (participant.label, str(exc))
                        continue
                    if dirty:
                        dispatch(key, participant)
            finally:
                draining = False

        advance()
        return True



def defer_close(owner, coordinator, *, close, report, alive, post, event=None):
    """Keep a user close request pending until an active save really finishes.

    UI adapters supply their existing close action and event dispatcher. A
    failed save, newly typed draft or destroyed window never triggers shutdown.
    Forced operating-system shutdown cannot be vetoed by this helper.
    """
    if coordinator is None or not coordinator.busy:
        return False
    if event is not None:
        can_veto = getattr(event, 'CanVeto', None)
        if callable(can_veto) and not can_veto():
            return False
        veto = getattr(event, 'Veto', None)
        if callable(veto):
            veto()
    if getattr(owner, '_save_close_pending', False):
        return True
    owner._save_close_pending = True

    def finish(summary):
        owner._save_close_pending = False
        if not alive():
            return
        if not summary.complete:
            report('The window remains open. ' + summary.message())
            return

        def retry():
            if not alive():
                return
            try:
                if coordinator.busy or coordinator.has_unsaved():
                    report('The window remains open because newer unsaved changes are retained.')
                    return
            except Exception as exc:
                report('The window remains open because saving could not be verified: ' + str(exc))
                return
            close()

        post(retry)

    if not coordinator.after_current(finish):
        owner._save_close_pending = False
        return False
    report('Waiting for the current saves before closing. A failure or newer edit keeps this window open.')
    return True
