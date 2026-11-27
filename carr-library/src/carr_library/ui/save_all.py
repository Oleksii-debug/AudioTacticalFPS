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
        if not self.remaining:
            return prefix + 'No unsaved drafts remain.'
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


@dataclass(eq=False)
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

    def save_all(self, completed, *, cancelled=lambda: False):
        if not callable(completed) or not callable(cancelled):
            raise ValueError('Save completion and cancellation handlers must be callable.')
        if self.busy:
            return False
        self.busy = True
        pending = deque(self.participants.items())
        # Keep failures by registration identity, not just the reusable key.
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
                    failures[(key, participant)] = (participant.label, str(exc))
                    continue
                if dirty and (key, participant) not in failures:
                    failures[(key, participant)] = (participant.label, 'newer or unsaved edits are retained')
            self.busy = False
            # A late stop cannot undo already confirmed durable writes.
            # Keep cancellation when work remains or no write was confirmed.
            summary = SaveSummary(tuple(saved), tuple(failures.values()),
                                  stopped and (bool(failures) or not saved))
            completed(summary)
            # A late reply belongs to its original registration, not a new
            # document that happens to reuse the same key. Focus only a live
            # failed registration, rechecking after the completion callback.
            for key, participant in failures:
                if participant is None or self.participants.get(key) is not participant:
                    continue
                if participant.focus is not None:
                    try:
                        participant.focus()
                    except Exception:
                        pass
                break

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
                    failures[(key, participant)] = (participant.label, 'save completion was not confirmed')
                elif not result.saved:
                    failures[(key, participant)] = (participant.label, result.message or 'save failed; draft retained')
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
                        failures[(None, None)] = ('Save All', f'Cancellation status could not be read: {exc}')
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
                        failures[(key, participant)] = (participant.label, 'the open document changed during saving')
                        continue
                    try:
                        dirty = participant.dirty()
                    except Exception as exc:
                        failures[(key, participant)] = (participant.label, str(exc))
                        continue
                    if dirty:
                        dispatch(key, participant)
            finally:
                draining = False

        advance()
        return True
