"""Reuse the desktop dispatcher for cancellable destination capture and consent.

Only the background job inspects existing file bytes. Native confirmation
receives that immutable snapshot and performs no filesystem reads.
"""
from ..research.export_file import ExportDestination, capture_export_destination, export_completion


def confirm_export_destination(parent, approval, *, wx_module=None):
    """Ask on the UI thread about one already-captured destination."""
    if type(approval) is not ExportDestination:
        raise ValueError('Capture the export destination in the existing worker before confirmation.')
    if not approval.require_absent:
        if wx_module is None:
            import wx as wx_module
        if wx_module.MessageBox(
                f'Replace the existing export {approval.target.name}?', 'Replace export',
                wx_module.YES_NO | wx_module.NO_DEFAULT | wx_module.ICON_QUESTION,
                parent) != wx_module.YES:
            return None
    return approval


def completion_message(message, receipt):
    """A cleanup warning follows a successful publication, never a retry cue."""
    return export_completion(message, receipt)


def start_export(frame, path, *, private_root, check_scope, operation, done,
                 title, failed=None, declined=None, wx_module=None):
    """Two jobs on the existing dispatcher; no second executor or event loop.

    operation receives the approved snapshot, a live cancellation callback,
    and a scope check to repeat after rendering immediately before publication.
    """
    cancel_event = frame.cancel_event

    def cancelled():
        return (getattr(frame, '_closing', False)
                or frame.cancel_event is not cancel_event or cancel_event.is_set())

    def check_live():
        # Check the scope first so a changed selection remains a clear scope
        # error. No native control is inspected by a worker callback.
        check_scope()
        if cancelled():
            raise InterruptedError('Export cancelled; the destination was not changed.')

    def capture():
        check_live()
        try:
            approval = capture_export_destination(path, private_root=private_root, cancelled=cancelled)
        except TimeoutError as exc:
            if cancelled():
                raise InterruptedError(str(exc)) from exc
            raise
        check_live()
        return approval

    def captured(approval):
        if getattr(frame, '_closing', False):
            return
        check_live()
        approved = confirm_export_destination(frame, approval, wx_module=wx_module)
        if approved is None:
            if declined is not None and not getattr(frame, '_closing', False):
                declined()
            return
        # run_job clears the shared cancel event when it starts. Consume any
        # cancellation/Close/scope change from the modal before starting it.
        # Forced Close can destroy controls while the native modal unwinds;
        # return quietly so the dispatcher does not report through a dead UI.
        if getattr(frame, '_closing', False):
            return
        check_live()

        def publish():
            check_live()
            try:
                return operation(approved, cancelled, check_live)
            except TimeoutError as exc:
                if cancelled():
                    raise InterruptedError(str(exc)) from exc
                raise

        frame.run_job(title, publish, done, failed)

    if getattr(frame, '_closing', False):
        return
    check_scope()
    frame.run_job('Checking export destination', capture, captured, failed)
