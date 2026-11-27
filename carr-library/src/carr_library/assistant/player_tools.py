"""Qualified, bounded UI handoff to the existing reader-owned WavePlayer.

There is no playback backend, thread, timer or executor here. The current
assistant worker waits for the existing native event queue; cancellation
withdraws queued authority before a late callback can touch a player.
"""
import re
import threading
from time import monotonic

from .models import canonical

RULES = {'player.status': (set(), set()),
         'player.control': ({'session', 'source_sha256', 'action'}, set())}


def check_arguments(step):
    required, optional = RULES[step.tool]
    if set(step.arguments) != required:
        raise ValueError('Use only the exact typed current-player arguments.')
    if step.tool == 'player.control':
        args = step.arguments
        if (type(args['session']) is not str or not re.fullmatch('[0-9a-f]{32}', args['session'])
                or type(args['source_sha256']) is not str
                or not re.fullmatch('[0-9a-f]{64}', args['source_sha256'])
                or args['action'] not in ('pause', 'stop')):
            raise ValueError('Choose Pause or Stop with an exact reviewed player session and source signature.')


class LivePlayerBridge:
    """One explicitly opened assistant bound to one registered native reader."""
    def __init__(self, app, desktop, reader_window):
        from ..ui.desktop_integration import DesktopIntegration
        if (not isinstance(desktop, DesktopIntegration) or desktop.frame.library is not app
                or reader_window is None):
            raise TypeError('The current native desktop and its exact reader owner are required.')
        if not desktop.wx.IsMainThread():
            raise RuntimeError('Bind the selected reader on the native UI thread.')
        current = desktop.reader
        qualified = getattr(desktop, '_assistant_reader', None)
        if current is None or current[1] is not reader_window or qualified is None or qualified['token'] is not current[0]:
            raise PermissionError('Open the assistant from the current qualified reading window.')
        self.app, self.desktop, self.reader_window = app, desktop, reader_window
        self.token = current[0]
        self.closed = threading.Event()

    def close(self):
        self.closed.set()  # The reader remains the sole player/resource owner.

    def _request(self, scope, action, expected=None, *, cancelled=lambda: False):
        if scope.workspace_id is None:
            raise PermissionError('Choose the same library as the current reader.')
        deadline = monotonic() + min(30.0, scope.max_seconds)
        done, withdrawn, guard = threading.Event(), threading.Event(), threading.Lock()
        outcome = {}
        def stopped():
            return self.closed.is_set() or withdrawn.is_set() or bool(cancelled()) or monotonic() >= deadline
        def dispatch():
            try:
                with guard:
                    if stopped():
                        raise InterruptedError('The queued player request no longer has permission to run.')
                # Exactly the selected live reader and desktop remain necessary.
                if self.desktop.frame.library is not self.app:
                    raise PermissionError('The native library owner changed.')
                value = self.desktop.assistant_player(self.reader_window, self.token,
                    scope.workspace_id, action, expected=expected, cancelled=stopped)
                with guard:
                    if stopped():
                        raise InterruptedError('Player acknowledgement arrived after cancellation; review its actual state.')
                    outcome['value'] = value
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                done.set()
        if stopped():
            raise InterruptedError('The current-player request was cancelled.')
        if self.desktop.wx.IsMainThread():
            dispatch()
        else:
            self.desktop.wx.CallAfter(dispatch)
        while not done.wait(.025):
            if stopped():
                with guard:
                    withdrawn.set()
                raise InterruptedError('Current-player request stopped; queued playback authority was withdrawn.')
        if 'error' in outcome:
            raise outcome['error']
        if stopped():
            withdrawn.set()
            raise InterruptedError('Current-player request stopped before acknowledgement.')
        value = outcome['value']
        # UI status is a small receipt, never a raw object or executable callback.
        if type(value) is not dict or len(canonical(value).encode('utf-8')) > 16384:
            raise ValueError('The current-player status is invalid.')
        return value

    def execute(self, scope, step, intent, persist, cancelled):
        check_arguments(step)
        if step.tool == 'player.status':
            return self._request(scope, 'status', cancelled=cancelled)
        if intent.get('player_dispatch') is not None:
            raise PermissionError('A previous player action has an uncertain outcome. Read its current state and review a new action.')
        intent['player_dispatch'] = dict(step.arguments)
        persist()
        result = self._request(scope, step.arguments['action'], step.arguments, cancelled=cancelled)
        intent['player_dispatch_complete'] = True
        persist()
        return result

    def verify_completed(self, scope, step, result, cancelled):
        current = self._request(scope, 'status', cancelled=cancelled)
        if (type(result) is not dict or current['session'] != result.get('session')
                or current['source_sha256'] != result.get('source_sha256')
                or current['workspace'] != result.get('workspace')):
            raise PermissionError('This completed player receipt belongs to an earlier reader or source; it was not replayed.')
        if step.tool == 'player.control' and (
                result.get('action') != step.arguments['action']
                or result['session'] != step.arguments['session']
                or result['source_sha256'] != step.arguments['source_sha256']):
            raise PermissionError('The completed player receipt differs from the reviewed action.')
