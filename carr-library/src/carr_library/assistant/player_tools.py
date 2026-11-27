"""Bounded assistant handoff to the current source reader's existing owner.

Only the current LiveReadingControls/LiveReader/SourceSequence can be bound.
The assistant worker uses the existing UI queue; this module creates no audio
device, executor, thread or timer. Pause never resumes. Stop is acknowledged
only after the same reading future and its native resources have settled.
"""
from dataclasses import asdict
from hashlib import sha256
import math
import re
import threading
from time import monotonic
from uuid import uuid4

from .models import Scope, canonical
from ..durable_state import local_path, regular


RULES = {'player.status': (set(), set()),
         'player.control': ({'session', 'source_sha256', 'action'}, set())}


def check_arguments(step):
    if step.tool not in RULES or type(step.arguments) is not dict:
        raise ValueError('Choose an existing typed current-player tool.')
    required, _optional = RULES[step.tool]
    if set(step.arguments) != required:
        raise ValueError('Use only the exact typed current-player arguments.')
    if step.tool == 'player.control':
        args = step.arguments
        if (type(args['session']) is not str or not re.fullmatch('[0-9a-f]{32}', args['session'])
                or type(args['source_sha256']) is not str
                or not re.fullmatch('[0-9a-f]{64}', args['source_sha256'])
                or args['action'] not in ('pause', 'stop')):
            raise ValueError('Choose Pause or Stop with the exact reviewed reader session and source signature.')


class LivePlayerBridge:
    """An explicitly opened assistant bound to one existing native source reader."""
    def __init__(self, app, controls):
        from ..ui.live_reading import LiveReadingControls
        from ..reader.live_reading import LiveReader, SourceSequence
        from ..reader.reading_desk import ReadingDesk
        if (type(controls) is not LiveReadingControls or type(controls.core) is not LiveReader
                or type(controls.source) is not SourceSequence
                or type(controls.source.desk) is not ReadingDesk
                or controls.core.source is not controls.source
                or controls.source.desk.library is not app):
            raise TypeError('The exact current source-reader controls and their owned core are required.')
        if not controls.wx.IsMainThread():
            raise RuntimeError('Bind the selected source reader on the native UI thread.')
        self.app, self.controls, self.wx = app, controls, controls.wx
        self.core, self.source, self.desk = controls.core, controls.source, controls.source.desk
        self.dialog, self.host = controls.dialog, controls._host
        self.closed = threading.Event()
        if controls.closed or not self.dialog or not self.host:
            raise PermissionError('The selected source reader is closed.')
        self.native_id = self.dialog.GetId()
        self.root = local_path(app.root, exists=True)
        info = regular(self.root, directory=True)
        self.root_identity = info.st_dev, info.st_ino
        self.identity, self.metadata = self.desk.identity, self.desk.metadata
        self.workspace, self.topic = self.desk.workspace, self.desk.topic
        self.host_scope = tuple(getattr(self.host, 'active', ()) or ())
        if not self.host_scope or self.host_scope[0] != self.workspace:
            raise PermissionError('Choose the same current library as the source reader.')
        self._operation = object()
        self._session = uuid4().hex
        self._section = None
        self._owner()

    def close(self):
        self.closed.set()  # The existing reader keeps its own playback/resources.

    def _owner(self):
        """UI-only lifetime and current-root checks, before any native access."""
        if not self.wx.IsMainThread():
            raise RuntimeError('Native reader ownership is checked on its UI thread.')
        controls = self.controls
        if (self.closed.is_set() or controls.closed or controls.core is not self.core
                or controls.source is not self.source or self.core.source is not self.source
                or self.source.desk is not self.desk or self.desk.library is not self.app
                or controls.dialog is not self.dialog or controls._host is not self.host
                or not self.dialog or not self.host):
            raise PermissionError('The exact selected reader owner is no longer available.')
        if (self.dialog.GetId() != self.native_id
                or getattr(self.host, 'library', None) is not self.app
                or getattr(self.host, '_closing', False)
                or tuple(getattr(self.host, 'active', ()) or ()) != self.host_scope
                or not controls.ready()):
            raise PermissionError('The selected native reader or library changed.')
        root = local_path(self.app.root, exists=True)
        info = regular(root, directory=True)
        if (root != self.root or (info.st_dev, info.st_ino) != self.root_identity
                or self.source.root != self.root or self.core.root != self.root
                or self.desk.workspace != self.workspace or self.desk.topic != self.topic
                or self.desk.identity != self.identity or self.desk.metadata != self.metadata):
            raise PermissionError('The selected source root or pinned revision changed.')
        self.source.guard()
        # A reentrant owner guard may close/replace the selected peer.
        if (self.closed.is_set() or controls.closed or controls.core is not self.core
                or controls.source is not self.source or not self.dialog or not self.host
                or getattr(self.host, 'library', None) is not self.app
                or tuple(getattr(self.host, 'active', ()) or ()) != self.host_scope):
            raise PermissionError('The selected reader changed during authorization.')
        with self.app.read(self.workspace) as db:
            identity, metadata = self.desk._header(db)
        if identity != self.identity or metadata != self.metadata:
            raise PermissionError('The exact source revision or metadata is no longer current.')
        if self.core._closed:
            raise PermissionError('The owned live reader is closed.')

    def _capture(self, action, expected):
        self._owner()
        with self.core._lock:
            future, view = self.core._future, self.core.status()
            if future is not self._operation:
                if action != 'status':
                    raise PermissionError('The current reading operation changed; review its status again.')
                self._operation, self._session, self._section = future, uuid4().hex, None
            if view.section is not None:
                self._section = view.section
            section = self._section
            location = asdict(section.location) if section is not None else None
            text_hash = sha256(section.text.encode('utf-8')).hexdigest() if section is not None else None
            binding = {'identity': asdict(self.identity), 'metadata': asdict(self.metadata),
                       'topic': self.topic, 'location': location, 'text_sha256': text_hash}
            signature = sha256(canonical(binding).encode('utf-8')).hexdigest()
            context = {'future': future, 'section': section, 'session': self._session,
                       'source_sha256': signature, 'source_text_sha256': text_hash,
                       'initial_section': view.section, 'action': action}
        if expected is not None and (
                expected['session'] != context['session']
                or expected['source_sha256'] != signature or expected['action'] != action):
            raise PermissionError('The reviewed reading operation or exact source changed before dispatch.')
        return context

    def _source_check(self, context, cancelled):
        if cancelled():
            raise InterruptedError('The current-reader request was stopped.')
        section = context['section']
        if section is not None:
            # Existing bounded source attestation runs on the assistant worker.
            self.source.check(section, cancelled, full=True)
        else:
            # An idle reader still names an exact retained edition. This is a
            # verification read through its current desk, never audio playback.
            self.source._desk(cancelled).page(1)
        if cancelled():
            raise InterruptedError('The current-reader request was stopped.')

    def _view(self, context, *, after_action=False):
        # Caller holds the core's existing RLock through the action/observation.
        if (self.core._future is not context['future']
                or self._session != context['session']):
            raise PermissionError('A different reading operation cannot inherit the queued command.')
        view = self.core.status()
        expected = context['initial_section']
        if view.section is not expected and not (
                after_action and context['action'] == 'stop' and view.section is None):
            raise PermissionError('The current source section changed during the queued request.')
        return view

    def _apply(self, context, stopped):
        self._owner()
        with self.core._lock:
            self._view(context)
            if stopped():
                raise InterruptedError('Player authority was withdrawn before native dispatch.')
            if context['action'] == 'pause':
                if self.core.busy and self.core.status().state not in ('discovering', 'stopping'):
                    # The worker acknowledges pause asynchronously. Do not
                    # toggle a pending pause back into audible playback.
                    if not self.core._pause:
                        self.controls.pause()
                elif self.core.busy:
                    raise PermissionError('Wait for the current reading transition before pausing.')
            elif context['action'] == 'stop':
                self.controls.stop()

    def _observed(self, context):
        self._owner()
        with self.core._lock:
            view = self._view(context, after_action=True)
            busy = self.core.busy
            quiet = self.core.quiescent
            retained = self.core._retained_resources is not None
            action = context['action']
            if action == 'stop' or action == 'pause' and not busy:
                if busy:
                    return None
                if retained or not quiet:
                    raise RuntimeError('The existing reader has not confirmed native cleanup; its resources remain owned.')
                state = 'stopped'
            elif action == 'pause':
                if not self.core._pause:
                    raise PermissionError('The requested pause was changed before acknowledgement.')
                if view.state != 'paused' or not quiet:
                    return None
                state = 'paused'
            else:
                state = view.state
            position, duration = view.audio_seconds, view.duration_seconds
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in (position, duration)):
                raise ValueError('The existing reader returned invalid measured status.')
            return {'schema': 'carr-current-player-receipt/1', 'session': context['session'],
                'source_sha256': context['source_sha256'], 'workspace': self.workspace,
                'edition': self.identity.edition, 'revision': self.identity.revision,
                'asset_sha256': self.identity.asset_sha256,
                'source_text_sha256': context['source_text_sha256'], 'action': action,
                'state': state, 'reader_state': view.state, 'busy': busy,
                'quiescent': quiet, 'cleanup_pending': retained,
                'position_seconds': position, 'duration_seconds': duration,
                'position_approximate': bool(view.position_is_approximate),
                'notice': 'Current source-reader acknowledgement. No player was created and no audible command was issued.'}

    def _request(self, scope, action, expected=None, *, cancelled=lambda: False):
        if not isinstance(scope, Scope) or scope.workspace_id != self.workspace:
            raise PermissionError('Choose the same library as the current source reader.')
        if self.wx.IsMainThread():
            raise RuntimeError('Run current-player requests on the existing assistant worker.')
        deadline = monotonic() + min(30.0, scope.max_seconds)
        withdrawn, guard = threading.Event(), threading.Lock()

        def stopped():
            return self.closed.is_set() or withdrawn.is_set() or bool(cancelled()) or monotonic() >= deadline

        def on_ui(operation):
            done, claimed, outcome = threading.Event(), [False], {}
            def dispatch():
                try:
                    with guard:
                        if claimed[0]:
                            return
                        claimed[0] = True
                        if stopped():
                            raise InterruptedError('The queued reader request no longer has permission to run.')
                    value = operation()
                    with guard:
                        if stopped():
                            raise InterruptedError('Reader acknowledgement arrived after cancellation; review its actual state.')
                        outcome['value'] = value
                except BaseException as exc:
                    outcome['error'] = exc
                finally:
                    done.set()
            if stopped():
                raise InterruptedError('The current-reader request was cancelled.')
            if self.wx.IsMainThread():
                dispatch()
            else:
                self.wx.CallAfter(dispatch)
            while not done.wait(.025):
                if stopped():
                    with guard:
                        withdrawn.set()
                    raise InterruptedError('The reader request stopped; queued playback authority was withdrawn.')
            if 'error' in outcome:
                raise outcome['error']
            if stopped():
                raise InterruptedError('The reader request stopped before acknowledgement.')
            return outcome['value']

        def bounded(value):
            if type(value) is not dict or len(canonical(value).encode('utf-8')) > min(16384, scope.max_bytes):
                raise ValueError('The current-reader receipt exceeds the selected byte budget.')
            return value

        try:
            context = on_ui(lambda: self._capture(action, expected))
            # Verify the actual retained source outside the native event queue.
            self._source_check(context, stopped)
            if action != 'status':
                # Fail a clearly undersized receipt budget before issuing a control.
                probe = dict(context, action='status')
                bounded(on_ui(lambda: self._observed(probe)))
                on_ui(lambda: self._apply(context, stopped))
            while True:
                result = on_ui(lambda: self._observed(context))
                if result is not None:
                    break
                if self.wx.IsMainThread():
                    raise RuntimeError('Asynchronous reader acknowledgement requires the existing assistant worker.')
                if withdrawn.wait(.025) or stopped():
                    raise InterruptedError('The existing reader did not settle before this request was stopped.')
            self._source_check(context, stopped)
            # Exact current-owner acknowledgement after source revalidation.
            final = on_ui(lambda: self._observed(context))
            if final is None:
                raise PermissionError('The reader state changed before final acknowledgement.')
            return bounded(final)
        finally:
            withdrawn.set()

    def execute(self, scope, step, intent, persist, cancelled):
        check_arguments(step)
        if step.tool == 'player.status':
            return self._request(scope, 'status', cancelled=cancelled)
        if self.wx.IsMainThread():
            raise RuntimeError('Run current-player controls on the existing assistant worker.')
        if intent.get('player_dispatch') is not None:
            raise PermissionError('A prior player action has an uncertain outcome. Read its current state and review a new action.')
        intent['player_dispatch'] = dict(step.arguments)
        persist()
        result = self._request(scope, step.arguments['action'], step.arguments, cancelled=cancelled)
        intent['player_dispatch_complete'] = True
        persist()
        return result

    def verify_completed(self, scope, step, result, cancelled):
        check_arguments(step)
        current = self._request(scope, 'status', cancelled=cancelled)
        if (type(result) is not dict or current['session'] != result.get('session')
                or current['source_sha256'] != result.get('source_sha256')
                or current['workspace'] != result.get('workspace')
                or current['revision'] != result.get('revision')
                or current['asset_sha256'] != result.get('asset_sha256')):
            raise PermissionError('This completed receipt belongs to an earlier reader/source; it was not replayed.')
        if step.tool == 'player.control' and (
                result.get('action') != step.arguments['action']
                or result['session'] != step.arguments['session']
                or result['source_sha256'] != step.arguments['source_sha256']):
            raise PermissionError('The completed reader receipt differs from the reviewed action.')
